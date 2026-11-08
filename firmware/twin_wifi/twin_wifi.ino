/*
  Beat the Shutdown: ESP32-S3 hardware twin over Wi-Fi (Arduino core 3.x)
  Board: "ESP32S3 Dev Module" (ESP32-S3-DevKitC-1), cable in the port labelled UART,
  USB CDC On Boot: Disabled. Serial Monitor 115200 (optional: no typing needed).

  The tank is a water-level sensor, the pump is a TT motor on an L298N, and an MPU-9250/6500
  (or MPU-6050) hot-glued to the motor senses vibration. Every 2 s the board POSTs
  {level_pct, pump_status, vibration, ts} to http://<SERVER_HOST>:<SERVER_PORT>/twin/ingest.
  The reply carries the twin's start/stop setpoints from twin_config.json.

  Stall detection and calibration match firmware/twin_bench (the tested bench sketch):
    - vibration = standard deviation of acceleration magnitude over ~200 ms, in g
    - pump_status 2 (fault) = pump commanded on, at least 2 s after it started, and vibration
      below the threshold on 2 readings in a row
    - threshold = quiet + 0.3 x (running - quiet), from medians of settled readings

  NO TYPING NEEDED (sending text from the Serial Monitor can reset this board):
    - Power up with the water sensor DRY: it records "empty" automatically. "Full" is RAW_FULL.
    - It measures vibration with the pump off, runs the pump, measures it running and sets the
      stall threshold. Then the pump runs automatically between the twin's setpoints.
    - BOOT button (GPIO 0): toggle the pump on/off. The automatic control only acts when the
      level crosses a setpoint, so a manual toggle stays until the next crossing.
    - Hold the wheel still to trigger pump_status 2 (pump failure).

  Optional serial keys (if your monitor doesn't reset the board):
    e = empty here   f = full here   q = quiet vibration (pump off)   r = running vibration
    s = status       h = help

  WIRING
    Water sensor   + -> GPIO 5 (powered only while reading), S -> GPIO 4, - -> GND
    IMU            VCC 3V3, GND, SDA -> GPIO 8, SCL -> GPIO 9 (address 0x68)
    L298N          ENA -> GPIO 6 (ENA jumper removed), IN1 -> GPIO 15, IN2 -> GPIO 16,
                   12V/GND from the 4xAA pack, GND also to the ESP32. Not the L298N 5V pin.

  Wi-Fi name, password and laptop IP go in secrets.h (copy secrets.example.h).
*/
#include <Arduino.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <Wire.h>
#include <math.h>
#include <time.h>

#include "secrets.h"  // WIFI_SSID, WIFI_PASSWORD, SERVER_HOST, SERVER_PORT

// ---------------- calibration constant ----------------
// Raw ADC reading with the sensor in water up to your "full" mark (bench sketch: 'f', or read
// raw= in the serial log). Replace with your measured number.
constexpr int RAW_FULL = 3000;

// ---------------- pins ----------------
constexpr int PIN_LVL_PWR = 5, PIN_LVL_SIG = 4;
constexpr int PIN_ENA = 6, PIN_IN1 = 15, PIN_IN2 = 16;
constexpr int PIN_SDA = 8, PIN_SCL = 9;
constexpr uint8_t MPU = 0x68;
constexpr int PIN_BTN = 0;  // BOOT button, LOW when pressed

// ---------------- tuning (same as the bench sketch) ----------------
constexpr uint32_t POST_INTERVAL_MS = 2000;
constexpr uint32_t STALL_IGNORE_MS = 2000;  // no stall checks for 2 s after the pump starts
constexpr int STALL_CONFIRM = 2;            // low readings in a row before status 2
constexpr float THRESH_FRACTION = 0.3f;     // threshold 30 % of the way from quiet to running
constexpr int PWM_FREQ = 20000, PWM_BITS = 8;
constexpr int PUMP_DUTY = 220;              // 4xAA through the L298N leaves ~4 V, so start high
constexpr uint32_t LOOP_DELAY_MS = 300;

// ---------------- state ----------------
int rawEmpty = 0, rawFull = RAW_FULL;
float vibQuiet = 0, vibRun = 0, vibThreshold = 0.05f;
bool mpuOk = false;
bool pumpOn = false;
unsigned long pumpStartMs = 0;
int lowCount = 0;
int pumpStatus = 0;           // 0 stopped, 1 running, 2 fault
float startPct = 70, stopPct = 30;
int levelZone = 1;            // 0 at/below stop, 1 between, 2 at/above start
bool lastBtn = HIGH;
unsigned long lastBtnChange = 0;
float lastLevel = 0, lastVib = 0;
int lastRaw = 0;
unsigned long lastPost = 0;
bool timeSynced = false;
String serverUrl;

// ---------------- water level ----------------
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
  float p = 100.0f * (raw - rawEmpty) / (float)(rawFull - rawEmpty);
  return constrain(p, 0.0f, 100.0f);
}

// ---------------- IMU (raw registers; MPU-6050 / 6500 / 9250) ----------------
bool mpuInit() {
  Wire.beginTransmission(MPU);
  Wire.write(0x75);  // WHO_AM_I
  if (Wire.endTransmission(false) != 0) return false;
  Wire.requestFrom(MPU, (uint8_t)1);
  uint8_t who = Wire.available() ? Wire.read() : 0;
  Serial.printf("IMU WHO_AM_I = 0x%02X (0x68 MPU6050, 0x70 MPU6500, 0x71 MPU9250, 0x73 MPU9255)\n", who);
  Wire.beginTransmission(MPU);
  Wire.write(0x6B); Wire.write(0x00);  // wake up
  if (Wire.endTransmission() != 0) return false;
  Wire.beginTransmission(MPU);
  Wire.write(0x1C); Wire.write(0x00);  // accel +-2 g
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
  ax = x / 16384.0f; ay = y / 16384.0f; az = z / 16384.0f;
  return true;
}

// Vibration = standard deviation of acceleration magnitude over ~200 ms; -1 if the IMU fails
float vibration() {
  const int N = 100;
  float m[N]; float sum = 0; int n = 0;
  for (int i = 0; i < N; i++) {
    float ax, ay, az;
    if (readAccel(ax, ay, az)) { m[n] = sqrtf(ax * ax + ay * ay + az * az); sum += m[n]; n++; }
    delayMicroseconds(2000);
  }
  if (n < 10) return -1;
  float mean = sum / n, var = 0;
  for (int i = 0; i < n; i++) var += (m[i] - mean) * (m[i] - mean);
  return sqrtf(var / n);
}

float medianOf(float *v, int n) {
  for (int i = 1; i < n; i++) { float k = v[i]; int j = i - 1; while (j >= 0 && v[j] > k) { v[j + 1] = v[j]; j--; } v[j + 1] = k; }
  return v[n / 2];
}

void updateThreshold() {
  if (vibRun > vibQuiet) vibThreshold = vibQuiet + THRESH_FRACTION * (vibRun - vibQuiet);
}

// ---------------- pump ----------------
void setPump(bool on) {
  if (on && !pumpOn) { pumpStartMs = millis(); lowCount = 0; }
  pumpOn = on;
  digitalWrite(PIN_IN1, on ? HIGH : LOW);
  digitalWrite(PIN_IN2, LOW);
  ledcWrite(PIN_ENA, on ? PUMP_DUTY : 0);
}

int zoneOf(float level) {
  if (level >= startPct) return 2;
  if (level <= stopPct) return 0;
  return 1;
}

// Automatic control acts only on setpoint crossings, so a BOOT-button toggle sticks.
void autoControl(float level) {
  int z = zoneOf(level);
  if (z != levelZone) {
    if (z == 2 && !pumpOn) { setPump(true); Serial.println("auto: level reached start setpoint, pump ON"); }
    if (z == 0 && pumpOn) { setPump(false); Serial.println("auto: level reached stop setpoint, pump OFF"); }
    levelZone = z;
  }
}

void updateStatus(float vib) {
  // EOG pump status: 0 stopped, 1 running, 2 fault (commanded on but not vibrating).
  // Fault needs STALL_CONFIRM low readings in a row, and never within STALL_IGNORE_MS of a start.
  bool settled = millis() - pumpStartMs > STALL_IGNORE_MS;
  if (pumpOn && settled && mpuOk && vib >= 0 && vib < vibThreshold) lowCount++; else lowCount = 0;
  pumpStatus = !pumpOn ? 0 : (lowCount >= STALL_CONFIRM ? 2 : 1);
}

void checkButton() {
  bool btn = digitalRead(PIN_BTN);
  if (btn != lastBtn && millis() - lastBtnChange > 50) {  // debounce
    lastBtnChange = millis();
    if (lastBtn == HIGH && btn == LOW) {
      setPump(!pumpOn);
      Serial.println(pumpOn ? "BOOT button: pump ON" : "BOOT button: pump OFF");
    }
    lastBtn = btn;
  }
}

// ---------------- network ----------------
void connectWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.printf("Wi-Fi: connecting to %s", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 10000) {
    delay(300);
    Serial.print('.');
    checkButton();
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\nWi-Fi: %s -> %s\n", WiFi.localIP().toString().c_str(), serverUrl.c_str());
    configTime(0, 0, "pool.ntp.org", "time.google.com");  // UTC; the server stamps samples if this fails
  } else {
    Serial.println("\nWi-Fi: not connected, will retry (pump control keeps running)");
  }
}

bool jsonNumber(const String &body, const char *key, float &out) {
  String k = String("\"") + key + "\":";
  int i = body.indexOf(k);
  if (i < 0) return false;
  out = body.substring(i + k.length()).toFloat();
  return true;
}

void postSample(float level, int status, float vib) {
  if (WiFi.status() != WL_CONNECTED) return;
  if (!timeSynced) timeSynced = time(nullptr) > 1600000000;
  float v = vib < 0 ? 0 : vib;  // the server expects >= 0; IMU errors read as quiet
  char body[160];
  if (timeSynced) {
    snprintf(body, sizeof(body), "{\"level_pct\":%.2f,\"pump_status\":%d,\"vibration\":%.4f,\"ts\":%lu}",
             level, status, v, (unsigned long)time(nullptr));
  } else {
    snprintf(body, sizeof(body), "{\"level_pct\":%.2f,\"pump_status\":%d,\"vibration\":%.4f}", level, status, v);
  }
  HTTPClient http;
  http.setTimeout(1500);
  http.begin(serverUrl);
  http.addHeader("Content-Type", "application/json");
  int code = http.POST((uint8_t *)body, strlen(body));
  if (code == 200) {
    String reply = http.getString();
    float f;
    if (jsonNumber(reply, "start_level_pct", f)) startPct = f;
    if (jsonNumber(reply, "stop_level_pct", f)) stopPct = f;
  } else {
    Serial.printf("POST %s -> %d\n", serverUrl.c_str(), code);
  }
  http.end();
}

// ---------------- calibration ----------------
void autoCalibrate() {
  rawEmpty = readLevelRaw();
  Serial.printf("auto: empty raw = %d (keep sensor dry at power-up), full raw = %d (RAW_FULL)\n", rawEmpty, rawFull);
  if (!mpuOk) {
    Serial.println("auto: IMU missing, running without stall detection");
    return;
  }
  float q[5];
  for (int i = 0; i < 5; i++) q[i] = vibration();
  vibQuiet = medianOf(q, 5);
  setPump(true);
  delay(3000);  // let the motor settle, skip the spin-up jolt
  float r[7];
  for (int i = 0; i < 7; i++) r[i] = vibration();
  vibRun = medianOf(r, 7);  // typical steady running vibration
  updateThreshold();
  Serial.printf("auto: quiet vib = %.4f, running vib = %.4f, threshold = %.4f\n", vibQuiet, vibRun, vibThreshold);
  if (vibRun <= vibQuiet) Serial.println("warning: running vibration not above quiet; check the IMU is glued to the motor");
  setPump(false);  // hand over to automatic control
}

// ---------------- optional serial keys ----------------
void printStatus() {
  Serial.printf("level %.1f%% raw %d (empty %d full %d) | pump %s status %d | vib %.4f thr %.4f "
                "(quiet %.4f run %.4f) | setpoints %.0f/%.0f | %s\n",
                lastLevel, lastRaw, rawEmpty, rawFull, pumpOn ? "ON" : "off", pumpStatus, lastVib,
                vibThreshold, vibQuiet, vibRun, startPct, stopPct,
                WiFi.status() == WL_CONNECTED ? serverUrl.c_str() : "Wi-Fi down");
}

void handleSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == 'e') { rawEmpty = readLevelRaw(); Serial.printf("empty raw = %d\n", rawEmpty); }
    if (c == 'f') { rawFull = readLevelRaw(); Serial.printf("full raw = %d (put this in RAW_FULL)\n", rawFull); }
    if (c == 'q') { vibQuiet = vibration(); updateThreshold(); Serial.printf("quiet vib = %.4f, threshold %.4f\n", vibQuiet, vibThreshold); }
    if (c == 'r') { vibRun = vibration(); updateThreshold(); Serial.printf("running vib = %.4f, threshold %.4f\n", vibRun, vibThreshold); }
    if (c == 's') printStatus();
    if (c == 'h') Serial.println("e empty, f full, q quiet vib, r running vib, s status; BOOT toggles the pump");
  }
}

// ---------------- setup / loop ----------------
void setup() {
  Serial.begin(115200);
  delay(1500);
  Serial.println("\nBeat the Shutdown twin (Wi-Fi)");
  pinMode(PIN_LVL_PWR, OUTPUT);
  digitalWrite(PIN_LVL_PWR, LOW);
  pinMode(PIN_IN1, OUTPUT);
  pinMode(PIN_IN2, OUTPUT);
  ledcAttach(PIN_ENA, PWM_FREQ, PWM_BITS);  // core 3.x LEDC API
  analogReadResolution(12);
  pinMode(PIN_BTN, INPUT_PULLUP);
  Wire.begin(PIN_SDA, PIN_SCL);
  mpuOk = mpuInit();
  Serial.println(mpuOk ? "IMU found at 0x68" : "IMU NOT found - check SDA=8, SCL=9, VCC=3V3, GND");
  setPump(false);

  autoCalibrate();
  levelZone = zoneOf(levelPct(readLevelRaw()));

  serverUrl = String("http://") + SERVER_HOST + ":" + String(SERVER_PORT) + "/twin/ingest";
  connectWifi();
  Serial.println("Ready. BOOT button toggles the pump. Hold the wheel to simulate a failure.");
}

void loop() {
  handleSerial();
  checkButton();

  lastRaw = readLevelRaw();
  lastLevel = levelPct(lastRaw);
  lastVib = mpuOk ? vibration() : -1;  // ~200 ms
  autoControl(lastLevel);
  updateStatus(lastVib);

  if (millis() - lastPost >= POST_INTERVAL_MS) {
    lastPost = millis();
    if (WiFi.status() != WL_CONNECTED) connectWifi();
    postSample(lastLevel, pumpStatus, lastVib);
    Serial.printf("level_pct=%.1f raw=%d vib=%.4f thr=%.4f pump_cmd=%d status=%d%s\n",
                  lastLevel, lastRaw, lastVib, vibThreshold, pumpOn, pumpStatus,
                  pumpStatus == 2 ? "  <-- PUMP FAILURE" : "");
  }
  delay(LOOP_DELAY_MS);
}
