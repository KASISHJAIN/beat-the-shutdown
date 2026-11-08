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
| MPU-6050 SDA / SCL | GPIO 8 / GPIO 9 (address 0x68, VCC 3V3) |
| L298N ENA / IN1 / IN2 | GPIO 6 / GPIO 15 / GPIO 16 (remove the ENA jumper) |
| L298N GND | GND (shared with the ESP32) |

## Flash

1. Copy `twin_wifi/secrets.example.h` to `twin_wifi/secrets.h` and fill in the Wi-Fi name,
   password and the laptop's IP. `secrets.h` is gitignored.
2. Arduino IDE with ESP32 core 3.x, board **ESP32S3 Dev Module**, open `twin_wifi.ino`, Upload.
   Or from the command line:
   `arduino-cli compile --upload -p COM5 --fqbn esp32:esp32:esp32s3 firmware/twin_wifi`
3. On the laptop: `uvicorn backend.api:app --host 0.0.0.0 --port 8000`. Allow port 8000 through
   the firewall if Windows asks.

No libraries are needed beyond the ESP32 core.

## Calibrate (Serial Monitor, 115200 baud)

| Key | Do this first | Effect |
|---|---|---|
| `e` | sensor dry / tank empty | records the raw reading for 0 % |
| `f` | sensor submerged to your "full" mark | records the raw reading for 100 % |
| `q` | motor off, bench still | learns the quiet vibration level |
| `r` | motor free to spin (it runs ~2 s) | learns the running vibration level |
| `s` | | prints level, raw ADC, vibration, threshold, setpoints, server |
| `p` | | holds the pump off / returns it to automatic |

The stall threshold sits halfway between the quiet and running levels. Calibration is saved in
flash. The pump runs automatically between the setpoints in `twin_config.json` (the server sends
them back on every post). Every 2 s the board posts
`{level_pct, pump_status, vibration, ts}` to `/twin/ingest`. `ts` is UTC from NTP when
available; otherwise the server stamps the sample on arrival.
