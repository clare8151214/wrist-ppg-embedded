#pragma once

#include <stdint.h>

namespace board_config {
constexpr uint8_t kSensorSda = 8;
constexpr uint8_t kSensorScl = 9;
constexpr uint32_t kSensorI2cHz = 400000;

// Proposed pins for later integration. The starter does not drive these pins.
constexpr uint8_t kPpgInterrupt = 6;
constexpr uint8_t kAlertLed = 7;

constexpr uint8_t kMax30102Address = 0x57;
constexpr uint8_t kMpu6050Address = 0x68;
// MAX30102: 100 paired Red/IR samples per second, no FIFO averaging.
constexpr uint8_t kPpgSpo2Config = 0x47;  // 8192 nA range, 100 sps, 411 us/18 bit.
constexpr uint8_t kPpgLedAmplitude = 0x40;  // Nominal 12.8 mA pulse amplitude/channel.
}  // namespace board_config
