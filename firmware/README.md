# ESP32-S3 hardware twin

`twin_wifi/twin_wifi.ino` turns an ESP32-S3 into a live extra facility. A water-level sensor is
the tank, a TT motor on an L298N is the pump, and a GY-521 (MPU-6050) on the motor mount senses
vibration. A stalled motor (hold the shaft) stops vibrating, and the board reports
`pump_status` 2.

## Wiring

| Part | ESP32-S3 |
|---|---|
| Water sensor + (power) | GPIO 5 (switched on only while reading) |
| Water sensor S (signal) | GPIO 4 |
| Water sensor − | GND |
| IMU (MPU-9250/6500 or MPU-6050) SDA / SCL | GPIO 8 / GPIO 9 (address 0x68, VCC 3V3), glued to the motor |
| L298N ENA / IN1 / IN2 | GPIO 6 / GPIO 15 / GPIO 16 (remove the ENA jumper) |
| L298N GND | GND (shared with the ESP32) |

## Flash

1. Copy `twin_wifi/secrets.example.h` to `twin_wifi/secrets.h` and fill in the Wi-Fi name,
   password and the laptop's IP. `secrets.h` is gitignored.
2. Arduino IDE with ESP32 core 3.x, board **ESP32S3 Dev Module**, USB CDC On Boot: Disabled, cable in the UART port. Open `twin_wifi.ino` and upload.
   Or from the command line:
   `arduino-cli compile --upload -p COM5 --fqbn esp32:esp32:esp32s3 firmware/twin_wifi`
3. On the laptop: `uvicorn backend.api:app --host 0.0.0.0 --port 8000`. Allow port 8000 through
   the firewall if Windows asks.

No libraries are needed beyond the ESP32 core.

## Calibrate (no typing needed)

Sending text from the Arduino Serial Monitor can reset this board, so calibration is automatic:

1. Set `RAW_FULL` at the top of `twin_wifi.ino` to the raw reading with the sensor in water up to
   your "full" mark (read `raw=` in the serial log, or press `f` in `twin_bench`).
2. Power up with the water sensor **dry**. The board records "empty", measures vibration with
   the pump off (median of 5), runs the pump for 3 s, measures it running (median of 7), and sets
   the stall threshold to quiet + 0.3 x (running - quiet).
3. The pump then runs automatically between the setpoints in `twin_config.json` (the server sends
   them back on every post). The control acts only when the level crosses a setpoint, so a BOOT
   button press sticks until the next crossing.

- **BOOT button (GPIO 0):** toggles the pump on and off.
- **Stall:** hold the wheel. `pump_status` becomes 2 after 2 low-vibration readings in a row,
  never within 2 s of the pump starting.
- Optional serial keys, if your monitor doesn't reset the board: `e`/`f` (empty/full),
  `q`/`r` (quiet/running vibration), `s` (status), `h` (help).

Every 2 s the board posts `{level_pct, pump_status, vibration, ts}` to `/twin/ingest`. `ts` is UTC
from NTP when available; otherwise the server stamps the sample on arrival.

## Bench sketch

`twin_bench/twin_bench.ino` is the same hardware without Wi-Fi: it prints `level_pct`, raw,
vibration, threshold and status on the serial port. Use it to check the wiring and read your
`RAW_FULL` value. Its stall and calibration logic is what `twin_wifi` uses.
