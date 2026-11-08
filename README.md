# Beat the Shutdown

Operator console for EOG's "Beat the Shutdown" challenge (HackUTD 2026). It reads water-facility
telemetry, detects **pump failures, pipeline leaks and worn pumps**, counts down to each tank's
shutdown limit, and recommends the cheapest response: trucks, well shut-ins, a mix, or accepting
the trip, with all failures in a system sharing its trucks.

![Console screenshot](docs/screenshot.png)
<!-- Screenshot placeholder: save a capture of the console as docs/screenshot.png -->

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

Single-port option: run `npm run build` in `frontend/` once. `uvicorn backend.api:app` then also
serves the built console at http://127.0.0.1:8000. To try it from a phone on the same network,
start uvicorn with `--host 0.0.0.0`, or open the Vite dev server's network URL.

Any folder under `HackUTD-ZeroDay/` that holds the three data files shows up in the dataset
switcher, including `test/` once it has been pulled (`git -C HackUTD-ZeroDay pull`).

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

## Check accuracy

```bash
python scripts/score.py                      # every practice folder: detection, plans, forecasts, order
python scripts/score.py --sweep 1            # also cut full_week every hour and grade each cut
python -m backend.alerts HackUTD-ZeroDay/history/mock_test_2026-10-10T1530 -o events.json --alerts
```
