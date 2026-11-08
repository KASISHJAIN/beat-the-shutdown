/*
  Beat the Shutdown - facility twin bench test (ESP32-S3-DevKitC-1, Arduino IDE)
  Board: "ESP32S3 Dev Module", cable in the port labelled UART, USB CDC On Boot: Disabled.
  Serial Monitor at 115200, line ending "Newline".

  WIRING
  Water level sensor (the tank)
    +  -> GPIO 5   (powered only while reading, so it doesn't corrode)
    -  -> GND
    S  -> GPIO 4   (analog)
  MPU-9250 / 6500 (vibration, hot-glued to the motor body)
    VCC -> 3V3     GND -> GND     SDA -> GPIO 8     SCL -> GPIO 9
  L298N (pump driver)
    12V terminal -> 4xAA holder red  GND terminal -> 4xAA holder black AND an ESP32 GND pin
    ENA -> GPIO 6  (pull the ENA jumper off first)
    IN1 -> GPIO 15     IN2 -> GPIO 16
    OUT1/OUT2 -> TT motor
    Do NOT connect the L298N 5V pin to the ESP32.

  NO TYPING NEEDED (sending text from the Arduino Serial Monitor can reset this board):
    - At power-up keep the water sensor DRY: it records "empty" automatically.
    - It then measures vibration with the pump off, starts the pump, measures it running,
      and sets the stall threshold automatically. The pump stays on.
    - Press the BOOT button (GPIO 0) any time to toggle the pump on/off.
    - Hold the wheel still to trigger status=2 PUMP FAILURE.

  SERIAL COMMANDS (optional, only if your Serial Monitor doesn't reset the board)
    e  = record "empty" (sensor dry)      f = record "full" (sensor in water to max line)
    1  = pump ON     0 = pump OFF          + / - = pump speed up/down
    q  = record "quiet" vibration (pump off)   r = record "running" vibration (pump on)
    p  = toggle printing
  Each line printed: level_pct, raw, vib, pump_cmd, status (0 off, 1 running, 2 FAULT)
*/

#include <Wire.h>

const int PIN_LVL_PWR = 5, PIN_LVL_SIG = 4;
const int PIN_ENA = 6, PIN_IN1 = 15, PIN_IN2 = 16;
const int PIN_SDA = 8, PIN_SCL = 9;
const uint8_t MPU = 0x68;
const int PIN_BTN = 0;               // BOOT button, reads LOW when pressed
bool lastBtn = HIGH;
unsigned long pumpStartMs = 0;       // ignore stall checks right after the pump starts
int lowCount = 0;                    // consecutive low-vibration readings

int rawEmpty = 0, rawFull = 3000;   // set with 'e' and 'f'
float vibQuiet = 0, vibRun = 0;      // set with 'q' and 'r'
float vibThreshold = 0.05;           // g, auto-set between quiet and running
bool pumpOn = false, printing = true, mpuOk = false;
int speedDuty = 220;                 // 0-255; 4xAA through the L298N leaves ~4V, so start high

int readLevelRaw() {
  digitalWrite(PIN_LVL_PWR, HIGH);
  delay(10);
  long sum = 0;
  for (int i = 0; i < 16; i++) sum += analogRead(PIN_LVL_SIG);
  digitalWrite(PIN_LVL_PWR, LOW);
  return sum / 16;
}

float levelPct(int raw) {
  if (rawFull == rawEmpty) return 0;
  float p = 100.0 * (raw - rawEmpty) / (float)(rawFull - rawEmpty);
  return constrain(p, 0, 100);
}

bool mpuInit() {
  Wire.beginTransmission(MPU);
  Wire.write(0x75);                             // WHO_AM_I
  if (Wire.endTransmission(false) != 0) return false;
  Wire.requestFrom(MPU, (uint8_t)1);
  uint8_t who = Wire.available() ? Wire.read() : 0;
  Serial.printf("IMU WHO_AM_I = 0x%02X (0x68 MPU6050, 0x70 MPU6500, 0x71 MPU9250, 0x73 MPU9255)\n", who);
  Wire.beginTransmission(MPU);
  Wire.write(0x6B); Wire.write(0x00);          // wake up
  if (Wire.endTransmission() != 0) return false;
  Wire.beginTransmission(MPU);
  Wire.write(0x1C); Wire.write(0x00);          // accel range +/-2g
  Wire.endTransmission();
  return true;
}

bool readAccel(float &ax, float &ay, float &az) {
  Wire.beginTransmission(MPU);
  Wire.write(0x3B);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(MPU, (uint8_t)6) != 6) return false;
  int16_t x = (Wire.read() << 8) | Wire.read();
  int16_t y = (Wire.read() << 8) | Wire.read();
  int16_t z = (Wire.read() << 8) | Wire.read();
  ax = x / 16384.0; ay = y / 16384.0; az = z / 16384.0;
  return true;
}

// Vibration = standard deviation of acceleration magnitude over ~200 ms
float vibration() {
  const int N = 100;
  float m[N]; float sum = 0; int n = 0;
  for (int i = 0; i < N; i++) {
    float ax, ay, az;
    if (readAccel(ax, ay, az)) { m[n] = sqrt(ax*ax + ay*ay + az*az); sum += m[n]; n++; }
    delayMicroseconds(2000);
  }
  if (n < 10) return -1;
  float mean = sum / n, var = 0;
  for (int i = 0; i < n; i++) var += (m[i] - mean) * (m[i] - mean);
  return sqrt(var / n);
}

float medianOf(float *v, int n) {
  for (int i = 1; i < n; i++) { float k = v[i]; int j = i - 1; while (j >= 0 && v[j] > k) { v[j+1] = v[j]; j--; } v[j+1] = k; }
  return v[n / 2];
}

void setPump(bool on) {
  if (on && !pumpOn) pumpStartMs = millis();
  pumpOn = on;
  digitalWrite(PIN_IN1, on ? HIGH : LOW);
  digitalWrite(PIN_IN2, LOW);
  analogWrite(PIN_ENA, on ? speedDuty : 0);
}

void setup() {
  Serial.begin(115200);
  delay(1500);
  pinMode(PIN_LVL_PWR, OUTPUT); digitalWrite(PIN_LVL_PWR, LOW);
  pinMode(PIN_IN1, OUTPUT); pinMode(PIN_IN2, OUTPUT);
  analogReadResolution(12);
  Wire.begin(PIN_SDA, PIN_SCL);
  mpuOk = mpuInit();
  setPump(false);
  Serial.println(mpuOk ? "IMU found at 0x68" : "IMU NOT found - check SDA=8, SCL=9, VCC=3V3, GND");
  pinMode(PIN_BTN, INPUT_PULLUP);

  // Automatic calibration
  rawEmpty = readLevelRaw();
  Serial.printf("auto: empty raw = %d (keep sensor dry at power-up)\n", rawEmpty);
  if (mpuOk) {
    float q[5]; for (int i = 0; i < 5; i++) q[i] = vibration();
    vibQuiet = medianOf(q, 5);
    setPump(true);
    delay(3000);                                   // let the motor settle, skip the spin-up jolt
    float r[7]; for (int i = 0; i < 7; i++) r[i] = vibration();
    vibRun = medianOf(r, 7);                       // typical steady running vibration
    // Threshold 30% of the way from quiet to running: a held wheel drops near quiet
    if (vibRun > vibQuiet) vibThreshold = vibQuiet + 0.3 * (vibRun - vibQuiet);
    Serial.printf("auto: quiet vib = %.4f, running vib = %.4f, threshold = %.4f\n",
                  vibQuiet, vibRun, vibThreshold);
  } else {
    setPump(true);
    Serial.println("auto: IMU missing, pump on without stall detection");
  }
  Serial.println("Ready. BOOT button toggles the pump. Hold the wheel to simulate a failure.");
}

void handleSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == 'e') { rawEmpty = readLevelRaw(); Serial.printf("empty raw = %d\n", rawEmpty); }
    if (c == 'f') { rawFull  = readLevelRaw(); Serial.printf("full raw = %d\n", rawFull); }
    if (c == '1') { setPump(true);  Serial.println("pump ON"); }
    if (c == '0') { setPump(false); Serial.println("pump OFF"); }
    if (c == '+') { speedDuty = min(speedDuty + 15, 255); if (pumpOn) setPump(true); Serial.printf("duty %d\n", speedDuty); }
    if (c == '-') { speedDuty = max(speedDuty - 15, 60);  if (pumpOn) setPump(true); Serial.printf("duty %d\n", speedDuty); }
    if (c == 'q') { vibQuiet = vibration(); Serial.printf("quiet vib = %.4f g\n", vibQuiet); }
    if (c == 'r') { vibRun = vibration(); Serial.printf("running vib = %.4f g\n", vibRun); }
    if (c == 't') { vibThreshold = max(0.005f, vibThreshold - 0.01f); Serial.printf("threshold %.4f\n", vibThreshold); }
    if (c == 'T') { vibThreshold += 0.01f; Serial.printf("threshold %.4f\n", vibThreshold); }
    if (c == 'p') printing = !printing;
    if ((c == 'q' || c == 'r') && vibRun > vibQuiet && vibQuiet > 0) {
      vibThreshold = vibQuiet + 0.3 * (vibRun - vibQuiet);
    }
  }
}

void loop() {
  handleSerial();
  bool btn = digitalRead(PIN_BTN);
  if (lastBtn == HIGH && btn == LOW) {
    setPump(!pumpOn);
    Serial.println(pumpOn ? "BOOT button: pump ON" : "BOOT button: pump OFF");
  }
  lastBtn = btn;
  int raw = readLevelRaw();
  float vib = mpuOk ? vibration() : -1;
  // EOG pump status: 0 stopped, 1 running, 2 fault (commanded on but not vibrating)
  // Fault needs 2 low readings in a row, and never in the first 2 s after a start.
  bool settled = millis() - pumpStartMs > 2000;
  if (pumpOn && settled && vib >= 0 && vib < vibThreshold) lowCount++; else lowCount = 0;
  int status = !pumpOn ? 0 : (lowCount >= 2 ? 2 : 1);
  if (printing) {
    Serial.printf("level_pct=%.1f raw=%d vib=%.4f thr=%.4f pump_cmd=%d status=%d%s\n",
                  levelPct(raw), raw, vib, vibThreshold, pumpOn, status,
                  status == 2 ? "  <-- PUMP FAILURE" : "");
  }
  delay(300);
}
