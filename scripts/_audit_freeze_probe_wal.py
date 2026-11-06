"""Decode the SQLite WAL frame-by-frame and map every written page back to its
table via dbstat's (pageno -> object) map.

This is the decisive forensic for "what does Hermes actually write": a WAL frame
carries the page number it modifies, so the frame stream says exactly which
pages (and therefore which tables) the gateway rewrites.

Read-only: opens state.db with mode=ro and only reads the -wal file bytes.
Usage: python scripts/_audit_freeze_probe_wal.py <out.json>
"""
import collections
import json
import os
import sqlite3
import struct
import sys
import time

HOME = os.path.expanduser("~/.hermes")
DB = os.path.join(HOME, "state.db")
WAL = DB + "-wal"
HDR = 32
FHDR = 24


def main():
    out = {"_ts": time.time(), "_wal": WAL}

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    pageno2obj = {}
    for name, pno in con.execute("select name, pageno from dbstat").fetchall():
        pageno2obj[pno] = name
    con.close()
    out["dbstat_pages"] = len(pageno2obj)

    raw = open(WAL, "rb").read()
    out["wal_bytes"] = len(raw)
    if len(raw) < HDR:
        out["error"] = "wal too small"
        with open(sys.argv[1], "w") as fh:
            json.dump(out, fh, indent=1)
        return
    magic, fmt, pagesize, ckpt, salt1, salt2, _c1, _c2 = struct.unpack(">IIIIIIII", raw[:HDR])
    out["wal_header"] = {"magic": hex(magic), "format": fmt, "page_size": pagesize,
                          "checkpoint_seq": ckpt, "salt1": salt1, "salt2": salt2}

    nframes = (len(raw) - HDR) // (FHDR + pagesize)
    out["frame_count"] = nframes
    by_obj = collections.Counter()
    by_page = collections.Counter()
    frames = []
    commits = 0
    for i in range(nframes):
        off = HDR + i * (FHDR + pagesize)
        pno, dbsize, _s1, _s2, _fc1, _fc2 = struct.unpack(">IIIIII", raw[off:off + FHDR])
        obj = pageno2obj.get(pno, f"page#{pno}")
        by_obj[obj] += 1
        by_page[pno] += 1
        if dbsize:
            commits += 1
        frames.append({"i": i, "pno": pno, "dbsize": dbsize, "obj": obj,
                       "commit": bool(dbsize)})
    out["commit_frames"] = commits
    out["bytes_in_frames"] = nframes * (FHDR + pagesize)
    out["top_objects"] = [{"obj": o, "frames": c, "bytes": c * (FHDR + pagesize)}
                           for o, c in by_obj.most_common(30)]
    out["top_pages"] = [{"pno": p, "frames": c, "obj": pageno2obj.get(p, "?")}
                         for p, c in by_page.most_common(25)]
    out["first_frames"] = frames[:60]
    out["last_frames"] = frames[-60:]
    with open(sys.argv[1], "w") as fh:
        json.dump(out, fh, indent=1)
    print(json.dumps(out["wal_header"]))
    print("frames:", nframes, "commits:", commits, "bytes:", out["bytes_in_frames"])
    for r in out["top_objects"][:15]:
        print(f'  {r["bytes"] / 1048576:8.2f}MB  {r["frames"]:7d} frames  {r["obj"]}')


if __name__ == "__main__":
    main()
