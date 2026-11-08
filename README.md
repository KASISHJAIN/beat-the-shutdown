# Beat the Shutdown

Operator console for EOG's "Beat the Shutdown" challenge (HackUTD 2026). It reads water-facility
telemetry, detects **pump failures, pipeline leaks and worn pumps**, counts down to each tank's
shutdown limit, and recommends the cheapest response: trucks, well shut-ins, a mix, or accepting
the trip, with all failures in a system sharing its trucks.

![Alerts view: pump failures in SPEC order with countdowns and plans, the live hardware twin, and the network map](docs/alerts.png)

## Screenshots

| | |
|---|---|
| ![What-if replay](docs/whatif.png) | ![Shift handoff](docs/handoff.png) |
| **What-if replay.** Tank level under the recommended plan (a truck, then a late shut-in) vs doing nothing, against the warning and shutdown lines | **Shift handoff.** Written by the Nemotron agent from the console's own tools; every time and cost is checked against them |
| ![Live hardware twin](docs/twin.png) | ![Phone](docs/phone.png) |
| **Live hardware twin.** The ESP32 facility mid-stall: LIVE badge, countdown and plan, vibration-based fault | **Phone.** The same console as tabs |

## Architecture

```mermaid
flowchart LR
    subgraph data["Dataset folder (history/*, test/)"]
        N[network.json]
        O[ops.json]
        T[telemetry.csv]
    end
    ESP["ESP32-S3 twin<br/>level, pump status, vibration"] -- "POST /twin/ingest every 2 s" --> TW[twin.py<br/>live facility]
    N & O & T --> L[loader.py<br/>"now" = last ts_unix]
    L --> D[detect.py<br/>failures, leaks, worn pumps]
    TW --> D
    D --> F[forecast.py<br/>hours_to_limit p10/p50/p90]
    F --> P[optimize.py<br/>cheapest plan, shared trucks]
    P --> A[alerts.py<br/>SPEC order, events.json]
    A --> API[api.py<br/>FastAPI + ?now= time travel]
    A --> AG[agent.py<br/>Nemotron tool-using handoff]
    AG -- "llm_chat()" --> NV[(NVIDIA API)]
    AG --> API
    API --> UI[React console<br/>alerts, map, what-if, handoff]
    K[answer_key.json] -. "scorer only" .-> S[scripts/score.py]
    D --> S
    P --> S
```

## What's in it

| Part | What it does |
|---|---|
| `backend/loader.py` | Loads a dataset folder (`network.json`, `ops.json`, `telemetry.csv`). "Now" is the last `ts_unix` |
| `backend/detect.py` | The three detectors, relative to each asset's own healthy baseline and noise |
| `backend/forecast.py` | Tank level and `hours_to_limit` (p10/p50/p90) for ongoing pump failures |
| `backend/optimize.py` | Exhaustive cheapest-plan search per system with a shared truck fleet, plus what-if trajectories |
| `backend/alerts.py` | Alerts ordering (SPEC "Alerts view") and the `events.json` export |
| `backend/api.py` | FastAPI: `/api/datasets`, `/network`, `/alerts`, `/events`, `/series`, `/plan` |
| `frontend/` | React + Vite + TypeScript console with a Leaflet map and Recharts trends |
| `scripts/score.py` | Grades detection, forecasts, plans and alert order against every `answer_key.json` |

`answer_key.json` is only ever read by `scripts/score.py`, never by the app.

## Run it

Requirements: Python 3.13 and Node.js 20 or newer. The challenge data folder `HackUTD-ZeroDay/`
must sit in the project root (or point `BTS_DATA_ROOT` at it).

```bash
# 1. backend
pip install -r requirements.txt
uvicorn backend.api:app --reload            # http://127.0.0.1:8000

# 2. frontend (second terminal)
cd frontend
npm install
npm run dev                                  # http://localhost:5173, proxies /api to :8000
```

**Demo / judging (one command, Windows):**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_demo.ps1
```

It builds the console if needed, serves the API and console together on `0.0.0.0:8000`, and
prints the console URL plus the laptop's IPv4 (for phones and for `SERVER_HOST` in the ESP32's
`secrets.h`). Use `-Rebuild` to force a fresh build and `-Port 8001` for another port.

Single-port option by hand: run `npm run build` in `frontend/` once. `uvicorn backend.api:app`
then also serves the built console at http://127.0.0.1:8000. Add `--host 0.0.0.0` for phones.

Any folder under `HackUTD-ZeroDay/` that holds the three data files shows up in the dataset
switcher, including `test/` once it has been pulled (`git -C HackUTD-ZeroDay pull`). The server
re-scans on every request and the switcher refreshes when opened, so no restart or reload is
needed.

## Using the console

- **Header:** dataset switcher, the data's "now" (UTC), and a time-travel slider across the dataset.
  Use −1h/−5m/+5m/+1h to step, **Play** to replay forward, and **Latest** to return to the end.
  Every view is recomputed from the data up to the chosen moment.
- **Alerts:** shown in SPEC order. Pump failures come first, soonest to shutdown first (ties go to
  higher lost revenue per hour). Each failure card has a live countdown, a tank gauge, the
  recommended plan with its plain-English reasoning, and its cost against doing nothing. Leaks are
  marked **Urgent**; worn pumps show their confidence. A leak the data can't confirm as still open
  (the pump hasn't run since) is labelled **Unverified**.
- **Map:** systems, facilities (colored by severity), disposal sites (diamonds), truck yards (T)
  and pipelines; a leaking pipeline is drawn dashed. Click a facility to open its detail.
- **Facility detail:** current readings, a what-if replay (tank level under the recommended plan
  vs doing nothing, against the warning and shutdown lines), and tank level, flow and pressure
  history with event onsets marked.
- **Phone:** the same three views as tabs along the bottom. Light and dark themes are available
  from the header button.

Deep links: `?dataset=history/full_week&now=1791460800&facility=S2_FACILITY_A&theme=dark`.

## Live hardware twin

An ESP32 (see `firmware/`) streams one extra facility, `TWIN_FACILITY` in its own system
`TWIN`. Every ~2 s it posts to `POST /twin/ingest`:

```json
{"level_pct": 54.2, "pump_status": 1, "vibration": 0.31, "ts": 1794101999}
```

`pump_status` is 0 stopped, 1 running, 2 fault. `vibration` is the standard deviation of the
acceleration magnitude in g. `ts` is optional: epoch seconds or ms, and anything else (such as
`millis()`) falls back to the server's receive time. The reply carries the pump setpoints so the
device stays in sync.

The twin's tank size, setpoints, wells, prices, repair time and truck fleet come from
`twin_config.json`, at demo scale so a stall plays out in a couple of minutes. The device has no
flow meter, so inflow is estimated from how fast the level rises while the pump isn't running.
The same detection, countdown and planning code then runs on the twin. The console shows it with
a **LIVE** badge: a status strip above the alerts, its alerts merged in SPEC order, a pulsing ring
on the map, and a detail view with a vibration chart.

No hardware? Simulate it, including a pump stall:

```bash
python scripts/fake_twin.py --reset                         # real time, stalls after ~90 s
python scripts/fake_twin.py --reset --stall-after 20        # stall sooner
python scripts/fake_twin.py --url http://<laptop-ip>:8000/twin/ingest
```

Run uvicorn with `--host 0.0.0.0` so the ESP32 can reach the laptop. Use `POST /twin/reset` to
clear the twin's history and `GET /api/twin` to see its status.

## Shift handoff agent (NVIDIA Nemotron)

The **Shift handoff** button in the header writes a handoff for the moment on screen.
`backend/agent.py` is a multi-step, tool-using agent. Each tool calls the existing modules:

| Tool | What it returns |
|---|---|
| `get_alerts` | Every problem in progress in SPEC order with countdown, plan, costs and evidence; plus events resolved in the last 12 h |
| `get_series` | Summary and ~24 points of one telemetry series |
| `optimize_plan` | The cheapest plan from now for a pump failure, with its reasoning |
| `what_if` | Any custom truck or shut-in plan scored against doing nothing |
| `write_handoff` | Saves the final Markdown (also to `handoffs/`, gitignored) |

To turn it on, copy `.env.example` to `.env` and set `NVIDIA_API_KEY` (from build.nvidia.com).
The model defaults to `nvidia/nemotron-3-ultra-550b-a55b` (a faster option is
`nvidia/nemotron-3.5-lightning-30b-a3b`) and can be changed with `NVIDIA_MODEL`. The only LLM
call is `llm_chat()`. Without a key, if the call fails, or after a 120 s budget, the same tools
fill a fixed template, so the button always works. Every clock time, $ amount and $/h rate in
the handoff is checked against the tool outputs, and anything that doesn't match is flagged in
the panel. The panel also shows which mode was used and the tool calls made.

**Pre-generated for the demo.** A live run takes about 30 s. For instant opening, a Nemotron
handoff for `snapshot_2026-10-10T0900` is saved in `demo/handoffs/`, and the console returns it
immediately when that dataset is shown at its latest time (badge "Pre-generated"). **Regenerate**
always asks the agent again. To save one for another dataset (kept only if Nemotron wrote it and
it passes the number check):

```bash
python -m backend.agent history/snapshot_2026-10-10T0900 --save
```

## Results

Measured with `scripts/score.py` against the practice answer keys (never read by the app).

| Check | Result |
|---|---|
| Detection, `full_week` and all 3 snapshots | 23/23, 13/13, 15/15, 21/21 events; 0 missed, 0 false |
| Onset error | mean 2.8–5.4 min per folder, worst 25 min |
| 15-minute sweep of `full_week` (624 cuts, each treated as "now") | 0 missed, 0 false, 0 wrong status; onset error mean 4.6 min, p95 20 min |
| Plans vs `reference_action` | 13/13 at each onset (`full_week`), 7/7 from "now" (snapshots) |
| `hours_to_limit` vs reference (snapshots) | within 0.8 min |

### How fast each fault is flagged

From the 15-minute sweep. A fault counts as flagged at the first reading that tripped the
detector, using only data up to that moment, measured from the answer key's true onset.

| Fault | Events | Flagged after onset: median | Worst | After the pump first runs: median | Worst |
|---|---|---|---|---|---|
| Pump failure | 13 | 0 min | 0 min | 0 min | 0 min |
| Pipeline leak | 5 | 30 min | 90 min | 25 min | 30 min |
| Worn pump | 5 | 150 min | 170 min | 45 min | 130 min |

Leaks and worn pumps can only be seen while the pump runs (no flow, no signal), so part of each
lag is time the pump sat idle. EVENT_1's 90 min, for example, is 60 min with the pump off plus
30 min of detection. A worn pump loses only about 0.4 % of its flow per hour, so the evidence
builds slowly. Worn pumps are flagged by a CUSUM of the flow-ratio and discharge-pressure drop,
which accumulates a sustained decline. Its threshold is 2x the highest value seen on any
healthy pump, leaving margin for noisier pumps on an unseen network. That cut the median from
170 to 150 min and the worst case from 225 to 170 min, with no new false alarms in the sweep,
the snapshots, `mock_test` or with 3 % of readings dropped. An early flag starts at about 40 % confidence and climbs (86 % an hour
later, 100 % within a few hours) as the trend firms up. Each leak and wear alert in the console shows
"Detected X after onset". There, onset is the app's own estimate, since the true onset isn't
known live.

Reproduce with `python scripts/score.py --sweep 0.25`.

## Check accuracy

```bash
python scripts/score.py                      # every practice folder: detection, plans, forecasts, order
python scripts/score.py --sweep 1            # also cut full_week every hour and grade each cut
python -m backend.alerts HackUTD-ZeroDay/history/mock_test_2026-10-10T1530 -o events.json --alerts
```
