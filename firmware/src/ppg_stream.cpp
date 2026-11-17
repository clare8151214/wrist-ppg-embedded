#include "ppg_stream.h"

#include <Arduino.h>
#include <Wire.h>
#include <esp_timer.h>

#include "board_config.h"

// Register definitions and FIFO byte order: ADI MAX30102 datasheet, rev. 1,
// https://www.analog.com/media/en/technical-documentation/data-sheets/MAX30102.pdf
namespace {
bool running = false;
uint32_t session = 0;
uint32_t samples = 0;
uint32_t i2cErrors = 0;
uint32_t overflowEvents = 0;
uint32_t lostLowerBound = 0;
uint32_t maxBacklog = 0;
uint32_t ambientOverflowEvents = 0;
int64_t startedUs = 0;
int64_t lastPollUs = 0;
int64_t lastReportUs = 0;

bool writeRegister(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(board_config::kMax30102Address);
  Wire.write(reg);
  Wire.write(value);
  if (Wire.endTransmission() != 0) {
    ++i2cErrors;
    return false;
  }
  return true;
}

bool readRegisters(uint8_t reg, uint8_t *data, size_t count) {
  Wire.beginTransmission(board_config::kMax30102Address);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0 ||
      Wire.requestFrom(board_config::kMax30102Address, count, true) != count) {
    ++i2cErrors;
    while (Wire.available()) Wire.read();
    return false;
  }
  for (size_t i = 0; i < count; ++i) data[i] = Wire.read();
  return true;
}

void report() {
  // M,session,paired_samples,elapsed_us,i2c_errors,overflow_events,
  // lost_samples_lower_bound,max_fifo_backlog,ambient_overflow_events
  Serial.printf("M,%lu,%lu,%llu,%lu,%lu,%lu,%lu,%lu\n",
                static_cast<unsigned long>(session),
                static_cast<unsigned long>(samples),
                static_cast<unsigned long long>(esp_timer_get_time() - startedUs),
                static_cast<unsigned long>(i2cErrors),
                static_cast<unsigned long>(overflowEvents),
                static_cast<unsigned long>(lostLowerBound),
                static_cast<unsigned long>(maxBacklog),
                static_cast<unsigned long>(ambientOverflowEvents));
}

void fail(const char *message) {
  Serial.printf("# ERROR %s\n", message);
  running = false;
  writeRegister(0x09, 0x83);  // Shutdown, preserving Red+IR mode selection.
  report();
}
}  // namespace

namespace ppg_stream {
bool active() { return running; }

void stop() {
  if (!running) return;
  running = false;
  writeRegister(0x09, 0x83);
  report();
  Serial.println("# PPG_STOP");
}

bool start() {
  stop();
  ++session;
  samples = i2cErrors = overflowEvents = lostLowerBound = maxBacklog = 0;
  ambientOverflowEvents = 0;
  startedUs = esp_timer_get_time();
  uint8_t value = 0;
  if (!readRegisters(0xFF, &value, 1) || value != 0x15) {
    fail("MAX3010x_PART_ID_missing_or_incompatible");
    return false;
  }
  if (!writeRegister(0x09, 0x40)) {
    fail("reset_write_failed");
    return false;
  }
  const uint32_t resetStart = millis();
  do {
    if (!readRegisters(0x09, &value, 1)) {
      fail("reset_read_failed");
      return false;
    }
    if (!(value & 0x40)) break;
    delay(1);
  } while (millis() - resetStart < 100);
  if (value & 0x40) {
    fail("reset_timeout");
    return false;
  }

  // No averaging or rollover. Overflow invalidates the run and stops capture.
  const uint8_t registers[][2] = {
      {0x09, 0x83}, {0x02, 0x00}, {0x03, 0x00}, {0x08, 0x00},
      {0x0A, board_config::kPpgSpo2Config},
      {0x0C, board_config::kPpgLedAmplitude},
      {0x0D, board_config::kPpgLedAmplitude},
      {0x04, 0x00}, {0x05, 0x00}, {0x06, 0x00}};
  for (const auto &entry : registers) {
    if (!writeRegister(entry[0], entry[1]) ||
        !readRegisters(entry[0], &value, 1) || value != entry[1]) {
      fail("configuration_write_or_readback_failed");
      return false;
    }
  }
  uint8_t status[2];
  if (!readRegisters(0x00, status, sizeof(status))) {
    fail("status_clear_failed");
    return false;
  }
  Serial.printf("# PPG_START session=%lu rate_setting_hz=100 averaging=1 "
                "adc_bits=18 adc_range_na=8192 led_pa=0x%02X\n",
                static_cast<unsigned long>(session), board_config::kPpgLedAmplitude);
  Serial.println("# D,session,seq,read_elapsed_us,red_raw,ir_raw");
  Serial.println("# M,session,samples,elapsed_us,i2c_errors,overflow_events,lost_lower_bound,max_backlog,alc_events");
  startedUs = lastPollUs = lastReportUs = esp_timer_get_time();
  if (!writeRegister(0x09, 0x03) || !readRegisters(0x09, &value, 1) || value != 0x03) {
    fail("start_mode_readback_failed");
    return false;
  }
  running = true;
  return true;
}

void service() {
  if (!running) return;
  const int64_t now = esp_timer_get_time();
  if (now - lastPollUs < 2000) return;
  lastPollUs = now;
  uint8_t status = 0;
  uint8_t pointers[3];  // WR_PTR, OVF_COUNTER, RD_PTR (read before popping data).
  if (!readRegisters(0x00, &status, 1) || !readRegisters(0x04, pointers, 3)) {
    fail("fifo_status_read_failed");
    return;
  }
  if (status & 0x01) {
    uint8_t mode = 0xFF, config = 0xFF, led = 0xFF;
    readRegisters(0x09, &mode, 1);
    readRegisters(0x0A, &config, 1);
    readRegisters(0x0C, &led, 1);
    Serial.printf("# POWER_READY status=0x%02X mode=0x%02X config=0x%02X led=0x%02X\n",
                  status, mode, config, led);
    fail("sensor_power_reset_detected");
    return;
  }
  if (status & 0x20) ++ambientOverflowEvents;
  const uint8_t overflow = pointers[1] & 0x1F;
  if (overflow != 0) {
    ++overflowEvents;
    lostLowerBound += overflow;  // Hardware counter saturates at 31.
    fail("fifo_overflow_samples_lost");
    return;
  }
  uint8_t count = (pointers[0] - pointers[2]) & 0x1F;
  // A_FULL threshold is 32; distinguish full from empty at equal pointers.
  if (count == 0 && (status & 0x80)) count = 32;
  if (count > maxBacklog) maxBacklog = count;
  for (uint8_t i = 0; i < count; ++i) {
    uint8_t bytes[6];
    if (!readRegisters(0x07, bytes, sizeof(bytes))) {
      // A partial FIFO transaction can advance its read pointer. Stop rather
      // than concealing a gap or replaying a partially consumed sample.
      fail("fifo_data_read_failed");
      return;
    }
    const uint32_t red = ((uint32_t(bytes[0]) << 16) |
                          (uint32_t(bytes[1]) << 8) | bytes[2]) & 0x3FFFF;
    const uint32_t ir = ((uint32_t(bytes[3]) << 16) |
                         (uint32_t(bytes[4]) << 8) | bytes[5]) & 0x3FFFF;
    // Read-completion timestamp, not an invented exact ADC conversion time.
    const uint64_t readUs = esp_timer_get_time() - startedUs;
    Serial.printf("D,%lu,%lu,%llu,%lu,%lu\n",
                  static_cast<unsigned long>(session),
                  static_cast<unsigned long>(samples++),
                  static_cast<unsigned long long>(readUs),
                  static_cast<unsigned long>(red), static_cast<unsigned long>(ir));
  }
  if (samples == 0 && now - startedUs > 3000000) {
    fail("no_fifo_samples_for_3_seconds");
    return;
  }
  if (now - lastReportUs >= 1000000) {
    report();
    lastReportUs = now;
  }
}
}  // namespace ppg_stream
