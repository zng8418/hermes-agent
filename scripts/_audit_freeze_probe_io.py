"""Physical-IO differential probe + WAL growth watch.

Uses /proc/<pid>/io: `write_bytes`/`read_bytes` are bytes handed to the block
layer (the physical-IO proxy), `wchar`/`rchar` are syscall-level bytes.
Usage: python scripts/_audit_freeze_probe_io.py <seconds> <out.json>
"""
import json
import os
import sys
import time

HOME = os.path.expanduser("~/.hermes")
DB = os.path.join(HOME, "state.db")
WAL = DB + "-wal"


def snapshot():
    out = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/io") as fh:
                d = {}
                for line in fh:
                    k, _, v = line.partition(": ")
                    d[k] = int(v)
            with open(f"/proc/{pid}/comm") as fh:
                comm = fh.read().strip()
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")[:200]
        except (OSError, ValueError):
            continue
        out[pid] = {"comm": comm, "cmd": cmd, **d}
    return out


def main():
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    dest = sys.argv[2] if len(sys.argv) > 2 else "io.json"

    def wal_size():
        try:
            return os.path.getsize(WAL)
        except OSError:
            return 0

    def db_size():
        try:
            return os.path.getsize(DB)
        except OSError:
            return 0

    t0 = time.time()
    wal0, db0 = wal_size(), db_size()
    s0 = snapshot()
    time.sleep(secs)
    t1 = time.time()
    wal1, db1 = wal_size(), db_size()
    s1 = snapshot()
    span = t1 - t0

    rows = []
    for pid, b in s1.items():
        a = s0.get(pid)
        if a is None:
            continue
        d = {k: b[k] - a.get(k, 0) for k in
             ("wchar", "write_bytes", "rchar", "read_bytes", "syscr", "syscw")}
        if not any(d[k] for k in ("write_bytes", "read_bytes")):
            continue
        rows.append({
            "pid": int(pid), "comm": b["comm"], "cmd": b["cmd"],
            "write_bytes_s": d["write_bytes"] / span,
            "read_bytes_s": d["read_bytes"] / span,
            "wchar_s": d["wchar"] / span,
            "rchar_s": d["rchar"] / span,
            "write_bytes_total": d["write_bytes"],
            "read_bytes_total": d["read_bytes"],
        })
    rows.sort(key=lambda r: -(r["write_bytes_s"] + r["read_bytes_s"]))

    out = {
        "_ts": t0, "_span_s": span,
        "wal_bytes_at_start": wal0, "wal_bytes_at_end": wal1,
        "wal_growth_s": (wal1 - wal0) / span,
        "db_bytes_at_start": db0, "db_bytes_at_end": db1,
        "db_growth_s": (db1 - db0) / span,
        "procs": rows,
        "totals": {
            "write_bytes_s": sum(r["write_bytes_s"] for r in rows),
            "read_bytes_s": sum(r["read_bytes_s"] for r in rows),
            "wchar_s": sum(r["wchar_s"] for r in rows),
        },
    }
    with open(dest, "w") as fh:
        json.dump(out, fh, indent=1)
    print(f"span={span:.1f}s wal {wal0}->{wal1} ({(wal1-wal0)/span/1024:.1f} KB/s) "
          f"db {db0}->{db1}")
    for r in rows[:20]:
        print(f"pid={r['pid']:<8} wr={r['write_bytes_s']/1024:9.1f}KB/s "
              f"rd={r['read_bytes_s']/1024:9.1f}KB/s "
              f"wchar={r['wchar_s']/1024:9.1f}KB/s {r['comm']}")
    print("TOTALS", json.dumps(out["totals"]))


if __name__ == "__main__":
    main()
