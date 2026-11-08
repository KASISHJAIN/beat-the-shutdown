/*
  Beat the Shutdown: ESP32-S3 hardware twin (Arduino core 3.x)

  The tank is a water-level sensor, the pump is a TT motor on an L298N, and a GY-521 (MPU-6050)
  on the motor mount senses vibration. The board runs the pump between the twin's start/stop
  setpoints and every 2 s POSTs {level_pct, pump_status, vibration, ts} to the laptop at
  http://<SERVER_HOST>:<SERVER_PORT>/twin/ingest. The reply carries the setpoints, so editing
  twin_config.json on the laptop retunes the board.

  pump_status: 0 stopped, 1 running, 2 fault (pump commanded on but not vibrating = stalled)
  vibration:   standard deviation of the acceleration magnitude over ~200 ms, in g

  Wiring
    Water sensor   power GPIO 5 (only on while reading, slows probe corrosion), signal GPIO 4
    MPU-6050       SDA GPIO 8, SCL GPIO 9, address 0x68, VCC 3V3
    L298N          ENA GPIO 6 (PWM), IN1 GPIO 15, IN2 GPIO 16, motor on OUT1/OUT2

  Serial (115200): e = tank empty here, f = tank full here,
                   q = learn quiet vibration (motor off), r = learn running vibration (motor on),
                   s = status, p = toggle pump override (off/auto), h = help.
  Calibration is saved in flash (NVS) and survives reboots.

  Wi-Fi name, password and laptop IP go in secrets.h (copy secrets.example.h).
*/
#include <Arduino.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <WiFi.h>
#include <Wire.h>
#include <math.h>
#include <time.h>

#include "secrets.h"  // WIFI_SSID, WIFI_PASSWORD, SERVER_HOST, SERVER_PORT

// ---------------- pins ----------------
constexpr int PIN_WATER_PWR = 5;
constexpr int PIN_WATER_SIG = 4;
constexpr int PIN_SDA = 8;
constexpr int PIN_SCL = 9;
constexpr uint8_t MPU_ADDR = 0x68;
constexpr int PIN_ENA = 6;
constexpr int PIN_IN1 = 15;
constexpr int PIN_IN2 = 16;

// ---------------- tuning ----------------
constexpr uint32_t POST_INTERVAL_MS = 2000;
constexpr uint32_t VIB_WINDOW_MS = 200;     // vibration window
constexpr uint32_t VIB_SAMPLE_US = 2000;    // ~100 samples per window
constexpr uint32_t SPINUP_GRACE_MS = 800;   // ignore low vibration right after the motor starts
constexpr int STALL_CONFIRM = 2;            // consecutive low windows before declaring a stall
constexpr int PWM_FREQ = 20000;             // above hearing
constexpr int PWM_BITS = 8;
constexpr int MOTOR_DUTY = 230;             // 0-255
constexpr int WATER_SAMPLES = 16;

// ---------------- state ----------------
Preferences prefs;
int calEmpty = 200;          // raw ADC at an empty tank
int calFull = 2600;          // raw ADC at a full tank
float vibQuiet = 0.01f;      // g, motor off
float vibRunning = 0.25f;    // g, motor running freely
float startPct = 70.0f;      // pump on at or above (updated from the server)
float stopPct = 30.0f;       // pump off at or below

bool pumpCommanded = false;  // what the controller asked the motor to do
bool overrideOff = false;    // 'p' toggles: hold the pump off (for maintenance / demos)
uint32_t motorOnSince = 0;
int lowVibCount = 0;
int pumpStatus = 0;          // 0 stopped, 1 running, 2 fault
float lastLevel = 0, lastVib = 0;
uint32_t lastPost = 0;
bool timeSynced = false;
String serverUrl;

// ---------------- helpers ----------------
float vibThreshold() {
  // halfway between the learned quiet and running levels
  return vibQuiet + 0.5f * (vibRunning - vibQuiet);
}

void saveCal() {
  prefs.putInt("calEmpty", calEmpty);
  prefs.putInt("calFull", calFull);
  prefs.putFloat("vibQuiet", vibQuiet);
  prefs.putFloat("vibRun", vibRunning);
}

void loadCal() {
  calEmpty = prefs.getInt("calEmpty", calEmpty);
  calFull = prefs.getInt("calFull", calFull);
  vibQuiet = prefs.getFloat("vibQuiet", vibQuiet);
  vibRunning = prefs.getFloat("vibRun", vibRunning);
}

// ---------------- water level ----------------
int readWaterRaw() {
  digitalWrite(PIN_WATER_PWR, HIGH);
  delay(10);  // let the sensor settle
  long sum = 0;
  for (int i = 0; i < WATER_SAMPLES; i++) {
    sum += analogRead(PIN_WATER_SIG);
    delayMicroseconds(200);
  }
  digitalWrite(PIN_WATER_PWR, LOW);
  return (int)(sum / WATER_SAMPLES);
}

float rawToPct(int raw) {
  if (calFull == calEmpty) return 0;
  float pct = 100.0f * (raw - calEmpty) / (float)(calFull - calEmpty);
  return constrain(pct, 0.0f, 100.0f);
}

// ---------------- MPU-6050 (raw registers, no library) ----------------
bool mpuInit() {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x6B);  // PWR_MGMT_1
  Wire.write(0x00);  // wake up, internal oscillator
  if (Wire.endTransmission() != 0) return false;
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x1C);  // ACCEL_CONFIG
  Wire.write(0x00);  // +-2 g, 16384 LSB/g
  Wire.endTransmission();
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x1A);  // CONFIG: DLPF ~44 Hz keeps motor vibration, drops high-freq noise
  Wire.write(0x03);
  Wire.endTransmission();
  return true;
}

bool mpuAccel(float &ax, float &ay, float &az) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x3B);  // ACCEL_XOUT_H
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((int)MPU_ADDR, 6) != 6) return false;
  int16_t x = (Wire.read() << 8) | Wire.read();
  int16_t y = (Wire.read() << 8) | Wire.read();
  int16_t z = (Wire.read() << 8) | Wire.read();
  ax = x / 16384.0f;
  ay = y / 16384.0f;
  az = z / 16384.0f;
  return true;
}

/** Standard deviation of |a| over VIB_WINDOW_MS, in g (gravity drops out as the mean). */
float readVibration() {
  double sum = 0, sumSq = 0;
  int n = 0;
  uint32_t t0 = millis();
  uint32_t next = micros();
  while (millis() - t0 < VIB_WINDOW_MS) {
    float ax, ay, az;
    if (mpuAccel(ax, ay, az)) {
      double m = sqrt((double)ax * ax + (double)ay * ay + (double)az * az);
      sum += m;
      sumSq += m * m;
      n++;
    }
    next += VIB_SAMPLE_US;
    while ((int32_t)(micros() - next) < 0) { /* wait for the next sample slot */ }
  }
  if (n < 2) return 0;
  double mean = sum / n;
  double var = sumSq / n - mean * mean;
  return var > 0 ? (float)sqrt(var) : 0.0f;
}

// ---------------- motor ----------------
void motor(bool on) {
  if (on && !pumpCommanded) motorOnSince = millis();
  pumpCommanded = on;
  digitalWrite(PIN_IN1, on ? HIGH : LOW);
  digitalWrite(PIN_IN2, LOW);
  ledcWrite(PIN_ENA, on ? MOTOR_DUTY : 0);
}

// ---------------- control loop ----------------
void updatePump(float level, float vib) {
  // hysteresis between the twin's setpoints
  if (overrideOff) {
    if (pumpCommanded) motor(false);
  } else if (level >= startPct && !pumpCommanded) {
    motor(true);
  } else if (level <= stopPct && pumpCommanded) {
    motor(false);
  }

  if (!pumpCommanded) {
    pumpStatus = 0;
    lowVibCount = 0;
    return;
  }
  // commanded on: a healthy pump vibrates; a stalled one doesn't
  if (millis() - motorOnSince < SPINUP_GRACE_MS) {
    pumpStatus = 1;
    return;
  }
  if (vib < vibThreshold()) {
    lowVibCount++;
  } else {
    lowVibCount = 0;
  }
  pumpStatus = (lowVibCount >= STALL_CONFIRM) ? 2 : 1;
}

// ---------------- network ----------------
void connectWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.printf("Wi-Fi: connecting to %s", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 10000) {
    delay(300);
    Serial.print('.');
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\nWi-Fi: %s\n", WiFi.localIP().toString().c_str());
    configTime(0, 0, "pool.ntp.org", "time.google.com");  // UTC; optional, the server falls back to receive time
  } else {
    Serial.println("\nWi-Fi: not connected, will retry (pump control keeps running)");
  }
}

/** Pull "key": number out of the server's JSON reply without a JSON library. */
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
  char body[160];
  if (timeSynced) {
    snprintf(body, sizeof(body), "{\"level_pct\":%.2f,\"pump_status\":%d,\"vibration\":%.4f,\"ts\":%lu}",
             level, status, vib, (unsigned long)time(nullptr));
  } else {
    snprintf(body, sizeof(body), "{\"level_pct\":%.2f,\"pump_status\":%d,\"vibration\":%.4f}",
             level, status, vib);
  }
  HTTPClient http;
  http.setTimeout(1500);
  http.begin(serverUrl);
  http.addHeader("Content-Type", "application/json");
  int code = http.POST((uint8_t *)body, strlen(body));
  if (code == 200) {
    String reply = http.getString();
    float v;
    if (jsonNumber(reply, "start_level_pct", v)) startPct = v;
    if (jsonNumber(reply, "stop_level_pct", v)) stopPct = v;
  } else {
    Serial.printf("POST %s -> %d\n", serverUrl.c_str(), code);
  }
  http.end();
}

// ---------------- serial commands ----------------
float averageVibration(int windows) {
  float s = 0;
  for (int i = 0; i < windows; i++) s += readVibration();
  return s / windows;
}

void printStatus() {
  Serial.printf("level %.1f%% (raw %d, empty %d, full %d) | pump %s status %d | vib %.4f g "
                "(quiet %.4f, running %.4f, threshold %.4f) | setpoints %.0f/%.0f | %s\n",
                lastLevel, readWaterRaw(), calEmpty, calFull, pumpCommanded ? "ON" : "off", pumpStatus,
                lastVib, vibQuiet, vibRunning, vibThreshold(), startPct, stopPct,
                WiFi.status() == WL_CONNECTED ? serverUrl.c_str() : "Wi-Fi down");
}

void printHelp() {
  Serial.println("e = tank EMPTY now   f = tank FULL now");
  Serial.println("q = learn QUIET vibration (motor off)   r = learn RUNNING vibration (motor spins ~2 s)");
  Serial.println("s = status   p = toggle pump hold-off   h = help");
}

void handleSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    switch (c) {
      case 'e':
        calEmpty = readWaterRaw();
        saveCal();
        Serial.printf("tank EMPTY = raw %d\n", calEmpty);
        break;
      case 'f':
        calFull = readWaterRaw();
        saveCal();
        Serial.printf("tank FULL = raw %d\n", calFull);
        break;
      case 'q': {
        bool was = pumpCommanded;
        motor(false);
        delay(300);
        vibQuiet = averageVibration(5);
        saveCal();
        Serial.printf("vibration QUIET = %.4f g, threshold now %.4f g\n", vibQuiet, vibThreshold());
        motor(was);
        break;
      }
      case 'r': {
        bool was = pumpCommanded;
        motor(true);
        delay(SPINUP_GRACE_MS);
        vibRunning = averageVibration(5);
        motor(was);
        saveCal();
        Serial.printf("vibration RUNNING = %.4f g, threshold now %.4f g\n", vibRunning, vibThreshold());
        if (vibRunning < vibQuiet * 2) Serial.println("warning: running is barely above quiet; check the MPU mounting");
        break;
      }
      case 's': printStatus(); break;
      case 'p':
        overrideOff = !overrideOff;
        Serial.printf("pump %s\n", overrideOff ? "held OFF" : "back on AUTO");
        break;
      case 'h': case '?': printHelp(); break;
      default: break;  // ignore newlines etc.
    }
  }
}

// ---------------- setup / loop ----------------
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\nBeat the Shutdown twin");

  pinMode(PIN_WATER_PWR, OUTPUT);
  digitalWrite(PIN_WATER_PWR, LOW);
  analogReadResolution(12);
  analogSetPinAttenuation(PIN_WATER_SIG, ADC_11db);

  pinMode(PIN_IN1, OUTPUT);
  pinMode(PIN_IN2, OUTPUT);
  ledcAttach(PIN_ENA, PWM_FREQ, PWM_BITS);  // core 3.x LEDC API
  motor(false);

  Wire.begin(PIN_SDA, PIN_SCL, 400000);
  if (!mpuInit()) Serial.println("MPU-6050 not found at 0x68: check SDA 8 / SCL 9 / 3V3");

  prefs.begin("twin", false);
  loadCal();
  serverUrl = String("http://") + SERVER_HOST + ":" + String(SERVER_PORT) + "/twin/ingest";
  printHelp();
  connectWifi();
}

void loop() {
  handleSerial();

  lastVib = readVibration();             // ~200 ms
  lastLevel = rawToPct(readWaterRaw());  // ~12 ms
  updatePump(lastLevel, lastVib);

  if (millis() - lastPost >= POST_INTERVAL_MS) {
    lastPost = millis();
    if (WiFi.status() != WL_CONNECTED) connectWifi();
    postSample(lastLevel, pumpStatus, lastVib);
    Serial.printf("level %5.1f%%  pump %d  vib %.4f g\n", lastLevel, pumpStatus, lastVib);
  }
}
