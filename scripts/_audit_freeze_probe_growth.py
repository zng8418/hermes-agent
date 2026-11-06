"""File-growth differential: which files on the box are actually growing, and how fast.

Usage: python scripts/_audit_freeze_probe_growth.py <seconds> <out.json>
"""
import json
import os
import sys
import time

TARGETS = [
    ("hermes_state_db", os.path.expanduser("~/.hermes/state.db")),
    ("hermes_state_db_wal", os.path.expanduser("~/.hermes/state.db-wal")),
    ("hermes_gateway_log", os.path.expanduser("~/.hermes/logs/gateway.log")),
    ("hermes_agent_log", os.path.expanduser("~/.hermes/logs/agent.log")),
    ("hermes_errors_log", os.path.expanduser("~/.hermes/logs/errors.log")),
    ("trading_db", os.path.expanduser("~/smart_trading_v10/data/trading.db")),
    ("trading_db_wal", os.path.expanduser("~/smart_trading_v10/data/trading.db-wal")),
    ("pm2_smart_out", os.path.expanduser("~/.pm2/logs/smart-trading-out.log")),
    ("pm2_smart_err", os.path.expanduser("~/.pm2/logs/smart-trading-error.log")),
    ("pm2_pm2log", os.path.expanduser("~/.pm2/pm2.log")),
]


def size(path):
    try:
        st = os.stat(path)
        return st.st_size, st.st_mtime
    except OSError:
        return None, None


def main():
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    dest = sys.argv[2] if len(sys.argv) > 2 else "growth.json"
    t0 = time.time()
    before = {name: size(p) for name, p in TARGETS}
    time.sleep(secs)
    t1 = time.time()
    after = {name: size(p) for name, p in TARGETS}
    span = t1 - t0
    rows = []
    for name, path in TARGETS:
        b, bm = before[name]
        a, am = after[name]
        rows.append({
            "name": name, "path": path,
            "start": b, "end": a,
            "delta": (a - b) if (a is not None and b is not None) else None,
            "per_s": ((a - b) / span) if (a is not None and b is not None) else None,
            "mtime_start": bm, "mtime_end": am,
        })
    out = {"_ts": t0, "_span_s": span, "files": rows}
    with open(dest, "w") as fh:
        json.dump(out, fh, indent=1)
    for r in rows:
        d = r["delta"]
        ps = r["per_s"]
        rate = f"{ps/1024:.1f}KB/s" if ps is not None else "-"
        print(f"{r['name']:22} {r['start']!s:>12} -> {r['end']!s:>12} "
              f"delta={d} rate={rate}")


if __name__ == "__main__":
    main()
