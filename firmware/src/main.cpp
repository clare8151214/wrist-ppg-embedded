#include <Arduino.h>
#include <Wire.h>

#include "board_config.h"
#include "ppg_stream.h"

namespace {
bool sensorBusReady = false;

bool checkIdleLines() {
  const int sda = digitalRead(board_config::kSensorSda);
  const int scl = digitalRead(board_config::kSensorScl);
  Serial.printf("bus_idle sda=%d scl=%d\n", sda, scl);
  if (sda == LOW || scl == LOW) {
    Serial.println("BUS_NOT_IDLE: a signal line is held LOW; transaction skipped.");
    return false;
  }
  return true;
}

void scanSensorBus() {
  if (!sensorBusReady) {
    Serial.println("Sensor I2C bus is not initialized.");
    return;
  }

  if (!checkIdleLines()) {
    return;
  }
  constexpr uint8_t addresses[] = {board_config::kMax30102Address,
                                  board_config::kMpu6050Address, 0x69};
  unsigned int found = 0;
  Serial.println("Probing sensor addresses...");
  for (const uint8_t address : addresses) {
    Serial.printf("  Probe 0x%02X begin\n", address);
    Serial.flush();
    Wire.beginTransmission(address);
    const uint8_t status = Wire.endTransmission();
    if (status != 0) {
      Serial.printf("  I2C status=%u at 0x%02X\n", status, address);
      continue;
    }
    ++found;
    Serial.printf("  I2C ACK at 0x%02X", address);
    if (address == board_config::kMax30102Address) {
      Serial.print(" (expected MAX30102 address)");
    } else if (address == board_config::kMpu6050Address || address == 0x69) {
      Serial.print(" (possible MPU6050 address)");
    }
    Serial.println();
  }
  Serial.printf("Scan complete: %u address(es) acknowledged.\n", found);
  Serial.println("An ACK identifies an address, not a verified sensor model.");
}

bool checkRegister(const char *label, uint8_t address, uint8_t reg,
                   uint8_t expected) {
  constexpr unsigned int attempts = 20;
  unsigned int matched = 0;
  unsigned int readErrors = 0;
  int firstValue = -1;
  for (unsigned int i = 0; i < attempts; ++i) {
    Wire.beginTransmission(address);
    Wire.write(reg);
    if (Wire.endTransmission(false) != 0 ||
        Wire.requestFrom(address, static_cast<size_t>(1), true) != 1) {
      ++readErrors;
    } else {
      const int value = Wire.read();
      if (firstValue < 0) {
        firstValue = value;
      }
      if (value == expected) {
        ++matched;
      }
    }
    delay(5);
  }
  Serial.printf("%s address=0x%02X register=0x%02X expected=0x%02X ",
                label, address, reg, expected);
  if (firstValue >= 0) {
    Serial.printf("first=0x%02X ", firstValue);
  } else {
    Serial.print("first=unavailable ");
  }
  Serial.printf("matched=%u/%u read_errors=%u result=%s\n", matched, attempts,
                readErrors, matched == attempts ? "PASS" : "FAIL");
  return matched == attempts;
}

void verifySensorLinks() {
  if (!sensorBusReady) {
    Serial.println("Sensor I2C bus is not initialized.");
    return;
  }
  if (!checkIdleLines()) {
    return;
  }
  Serial.println("=== SENSOR LINK CHECK ===");
  Serial.printf("sda=%u scl=%u clock_hz=%lu\n", board_config::kSensorSda,
                board_config::kSensorScl,
                static_cast<unsigned long>(board_config::kSensorI2cHz));
  const bool ppg = checkRegister("MAX3010x_PART_ID", board_config::kMax30102Address,
                                 0xFF, 0x15);
  const bool imu = checkRegister("MPU6050_WHO_AM_I", board_config::kMpu6050Address,
                                 0x75, 0x68);
  bool alternateImu = false;
  if (!imu) {
    alternateImu = checkRegister("MPU6050_ALTERNATE_ADDRESS", 0x69, 0x75, 0x68);
  }
  Serial.printf("link_result=%s\n", ppg && imu ? "PASS" :
                ppg && alternateImu ? "PASS_WITH_IMU_ADDRESS_NOTE" : "INCOMPLETE");
  Serial.println("Register matches confirm communication with compatible devices.");
  Serial.println("Supply voltage, PPG data and motion data are not measured here.");
  Serial.println("=== END SENSOR LINK CHECK ===");
}
}  // namespace

void setup() {
  Serial.begin(115200);
  delay(1500);

  Serial.println();
  Serial.println("Wrist PPG project - raw Red/IR capture");
  Serial.println("Board: ESP32-S3-DevKitC-1");
  Serial.printf("Sensor bus: SDA=GPIO%u, SCL=GPIO%u, clock=%lu Hz\n",
                board_config::kSensorSda, board_config::kSensorScl,
                static_cast<unsigned long>(board_config::kSensorI2cHz));

  sensorBusReady = Wire.begin(board_config::kSensorSda,
                              board_config::kSensorScl,
                              board_config::kSensorI2cHz);
  if (!sensorBusReady) {
    Serial.println("Sensor I2C initialization failed.");
  }
  Wire.setTimeOut(50);
  Serial.println("Send s to scan the bus after wiring and supply are checked.");
  Serial.println("Send v to read sensor identification registers (no sensor configuration writes).");
  Serial.println("Send p to start raw Red/IR capture; x to stop and shut down LEDs.");
  Serial.println("Commands s/v stop capture before diagnostics. PC viewer starts capture with p.");
}

void loop() {
  if (Serial.available() > 0) {
    const int command = Serial.read();
    if (command == 's' || command == 'S') {
      ppg_stream::stop();
      scanSensorBus();
    } else if (command == 'v' || command == 'V') {
      ppg_stream::stop();
      verifySensorLinks();
    } else if ((command == 'p' || command == 'P') && sensorBusReady) {
      if (checkIdleLines()) ppg_stream::start();
    } else if (command == 'x' || command == 'X') {
      ppg_stream::stop();
    }
  }
  ppg_stream::service();
  delay(1);
}
