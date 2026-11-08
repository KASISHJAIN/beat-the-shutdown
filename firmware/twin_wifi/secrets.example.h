// Copy this file to secrets.h (same folder) and fill it in. secrets.h is gitignored.
#pragma once

#define WIFI_SSID "your-wifi-name"
#define WIFI_PASSWORD "your-wifi-password"

// The laptop running `uvicorn backend.api:app --host 0.0.0.0 --port 8000`.
// Find its address with `ipconfig` (Windows) or `ipconfig getifaddr en0` (macOS).
#define SERVER_HOST "192.168.1.20"
#define SERVER_PORT 8000
