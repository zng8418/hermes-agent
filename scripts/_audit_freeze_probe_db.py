"""Read-only forensic probe of ~/.hermes/state.db for the freeze audit.

Every connection is opened with mode=ro. No VACUUM / REINDEX / write txn.
Usage: python scripts/_audit_freeze_probe_db.py <out.json>
"""
import json
import os
import sqlite3
import sys
import time
from collections import Counter

HOME = os.path.expanduser("~/.hermes")
DB = os.path.join(HOME, "state.db")


def ro():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def main():
    out = {"_ts": time.time(), "_db": DB}
    con = ro()
    cur = con.cursor()

    out["page_size"] = cur.execute("pragma page_size").fetchone()[0]
    out["page_count"] = cur.execute("pragma page_count").fetchone()[0]
    out["freelist_count"] = cur.execute("pragma freelist_count").fetchone()[0]
    out["auto_vacuum"] = cur.execute("pragma auto_vacuum").fetchone()[0]
    out["journal_mode"] = cur.execute("pragma journal_mode").fetchone()[0]
    out["wal_autocheckpoint"] = cur.execute("pragma wal_autocheckpoint").fetchone()[0]
    out["page_size_bytes"] = out["page_size"] * out["page_count"]

    # ---- dbstat per-object size -------------------------------------------
    try:
        rows = cur.execute(
            "select name, sum(pgsize) sz, count(*) pages "
            "from dbstat group by name order by sz desc").fetchall()
        out["dbstat"] = [{"name": r["name"], "bytes": r["sz"], "pages": r["pages"]} for r in rows]
    except Exception as exc:  # pragma: no cover
        out["dbstat_error"] = repr(exc)

    # ---- schema objects ---------------------------------------------------
    out["sqlite_master"] = [
        {"type": r[0], "name": r[1], "tbl": r[2], "sql": (r[3] or "")[:600]}
        for r in cur.execute(
            "select type,name,tbl_name,sql from sqlite_master order by type,name").fetchall()]

    # ---- state_meta -------------------------------------------------------
    out["state_meta_rows"] = cur.execute("select count(*) from state_meta").fetchone()[0]
    out["state_meta_total_bytes"] = cur.execute(
        "select sum(length(value)) from state_meta").fetchone()[0] or 0
    rows = cur.execute(
        "select key, length(value) n from state_meta order by n desc").fetchall()
    out["state_meta_top"] = [{"key": r["key"], "n": r["n"]} for r in rows[:60]]
    byprefix = Counter()
    cnt = Counter()
    for r in rows:
        k = r["key"]
        p = ".".join(k.split(".")[:4]) if "." in k else k
        byprefix[p] += r["n"]
        cnt[p] += 1
    out["state_meta_by_prefix"] = sorted(
        [{"prefix": p, "bytes": byprefix[p], "rows": cnt[p]} for p in byprefix],
        key=lambda d: -d["bytes"])[:40]

    # ---- messages ---------------------------------------------------------
    out["messages_rows"] = cur.execute("select count(*) from messages").fetchone()[0]
    out["messages_bytes"] = cur.execute(
        "select sum(length(content)) from messages").fetchone()[0] or 0
    out["messages_len_hist"] = [
        {"bucket": b, "n": n} for b, n in cur.execute(
            "select case when length(content)<1000 then '<1k' "
            "when length(content)<10000 then '1k-10k' "
            "when length(content)<50000 then '10k-50k' "
            "else '>=50k' end b, count(*) from messages group by b").fetchall()]
    out["messages_ts"] = dict(cur.execute(
        "select min(timestamp) mn, max(timestamp) mx from messages").fetchone())
    out["messages_ts_formatted"] = {
        "min": time.strftime("%Y-%m-%d %H:%M", time.localtime(out["messages_ts"]["mn"])),
        "max": time.strftime("%Y-%m-%d %H:%M", time.localtime(out["messages_ts"]["mx"]))}
    out["messages_per_month"] = [
        {"month": r[0], "n": r[1]} for r in cur.execute(
            "select strftime('%Y-%m', timestamp, 'unixepoch') m, count(*) from messages "
            "group by m order by m").fetchall()]
    out["messages_by_session"] = [
        {"session_id": r[0], "n": r[1]} for r in cur.execute(
            "select session_id, count(*) from messages group by session_id order by 2 desc").fetchall()[:20]]

    # ---- fts tables -------------------------------------------------------
    for t in ("messages_fts", "messages_fts_trigram"):
        try:
            out[f"{t}_rows"] = cur.execute(f"select count(*) from {t}").fetchone()[0]
        except Exception as exc:
            out[f"{t}_rows"] = repr(exc)
    for t in ("trash_messages", "fts_v22_trash_messages_fts", "fts_v22_trash_messages_fts_trigram"):
        try:
            out[f"{t}_rows"] = cur.execute(f"select count(*) from {t}").fetchone()[0]
        except Exception as exc:
            out[f"{t}_rows"] = "ERR: " + str(exc)

    # ---- other big tables -------------------------------------------------
    for t in ("sessions", "session_admissions", "system_prompts", "todos", "cron_jobs"):
        try:
            out[f"{t}_rows"] = cur.execute(f"select count(*) from {t}").fetchone()[0]
        except Exception:
            pass

    con.close()
    dest = sys.argv[1]
    with open(dest, "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    print("wrote", dest, "keys:", len(out))


if __name__ == "__main__":
    main()
