"""Simulate the ESP32 twin: POST {level_pct, pump_status, vibration, ts} every 2 s.

The tank fills steadily; the pump runs between the setpoints the server sends back and
drains it. After --stall-after seconds the pump stalls the next time it is running: status
2, vibration drops to almost nothing, and the level climbs until the stall clears after
--stall-for seconds.

  python scripts/fake_twin.py                       # real time against a local server
  python scripts/fake_twin.py --fast --count 120    # no sleeping, synthetic 2 s timestamps
  python scripts/fake_twin.py --url http://192.168.1.20:8000/twin/ingest --reset
"""
from __future__ import annotations

import argparse
import json
import random
import time
import urllib.request


def post(url: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read() or b"{}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8000/twin/ingest")
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between posts")
    ap.add_argument("--fill", type=float, default=0.6, help="tank fill rate, %% per second")
    ap.add_argument("--drain", type=float, default=1.6, help="pump drain rate while running, %% per second")
    ap.add_argument("--start-level", type=float, default=45.0)
    ap.add_argument("--stall-after", type=float, default=90.0, help="seconds before the pump stalls (<0: never)")
    ap.add_argument("--stall-for", type=float, default=150.0, help="seconds the stall lasts")
    ap.add_argument("--count", type=int, default=0, help="stop after this many posts (0 = forever)")
    ap.add_argument("--fast", action="store_true", help="don't sleep; timestamps advance by --interval")
    ap.add_argument("--reset", action="store_true", help="clear the twin's history first")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    rng = random.Random(a.seed)

    if a.reset:
        post(a.url.replace("/twin/ingest", "/twin/reset"))
    start_sp, stop_sp = 70.0, 30.0
    level, pump_on = a.start_level, False
    stall_until = None
    t0 = time.time()
    ts = t0
    n = 0
    while a.count == 0 or n < a.count:
        elapsed = ts - t0
        # controller: run between setpoints (the ESP32 does the same)
        if level >= start_sp:
            pump_on = True
        elif level <= stop_sp:
            pump_on = False
        # stall: the first time the pump is running after --stall-after
        if stall_until is None and a.stall_after >= 0 and elapsed >= a.stall_after and pump_on:
            stall_until = elapsed + a.stall_for
            print(f"  -- pump stalls for {a.stall_for:.0f} s")
        stalled = stall_until is not None and elapsed < stall_until
        if stall_until is not None and not stalled and elapsed - a.interval < stall_until:
            print("  -- stall cleared (repaired)")

        draining = pump_on and not stalled
        level += (a.fill - (a.drain if draining else 0.0)) * a.interval
        level = min(max(level, 0.0), 100.0)
        status = 2 if (pump_on and stalled) else (1 if pump_on else 0)
        vib = rng.gauss(0.30, 0.03) if status == 1 else rng.gauss(0.015, 0.006)
        body = {"level_pct": round(level + rng.gauss(0, 0.3), 2), "pump_status": status,
                "vibration": round(max(vib, 0.0), 4), "ts": round(ts, 3)}
        try:
            r = post(a.url, body)
            start_sp, stop_sp = r.get("start_level_pct", start_sp), r.get("stop_level_pct", stop_sp)
            print(f"{n:4d} level {body['level_pct']:6.2f}%  pump {status}  vib {body['vibration']:.3f} g"
                  f"  -> {r.get('samples', '?')} samples")
        except OSError as e:
            print(f"{n:4d} post failed: {e}")
        n += 1
        if a.fast:
            ts += a.interval
        else:
            time.sleep(a.interval)
            ts = time.time()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
