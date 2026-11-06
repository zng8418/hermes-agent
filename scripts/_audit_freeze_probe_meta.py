"""Read-only probe of state_meta key families + FTS/maintenance markers."""
import json
import os
import sqlite3
import sys
import time

DB = os.path.expanduser("~/.hermes/state.db")


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    out = {"_ts": time.time()}

    rows = cur.execute("select key, length(value) n from state_meta").fetchall()
    out["total_rows"] = len(rows)
    out["total_bytes"] = sum(r["n"] for r in rows)

    marks = {}
    for r in rows:
        k = r["key"]
        if k.startswith("gateway.admission.result"):
            continue
        if k.startswith("fts_") or "rebuild" in k or "teardown" in k:
            marks[k] = r["n"]
    out["fts_markers"] = marks

    adm = cur.execute(
        "select key, length(value) n from state_meta where key like 'gateway.admission.result.v1.%'").fetchall()
    out["admission_blobs"] = {
        "count": len(adm),
        "bytes": sum(r["n"] for r in adm),
        "min": min((r["n"] for r in adm), default=0),
        "max": max((r["n"] for r in adm), default=0),
        "mean": (sum(r["n"] for r in adm) // len(adm)) if adm else 0,
        "size_hist": [
            {"bucket": r[0], "n": r[1], "bytes": r[2]} for r in cur.execute(
                "select case when length(value)<50000 then '<50k' "
                "when length(value)<200000 then '50k-200k' "
                "when length(value)<1000000 then '200k-1M' "
                "when length(value)<3000000 then '1M-3M' else '>=3M' end b, count(*), sum(length(value)) "
                "from state_meta where key like 'gateway.admission.result.v1.%' group by b").fetchall()],
    }
    out["admission_top"] = [{"key": r["key"], "n": r["n"]} for r in
                            cur.execute("select key, length(value) n from state_meta "
                                        "where key like 'gateway.admission.result.v1.%' "
                                        "order by n desc limit 15").fetchall()]

    try:
        out["session_admissions_status"] = [
            {"status": r[0], "outcome": r[1], "n": r[2]} for r in cur.execute(
                "select status, outcome, count(*) from session_admissions group by status, outcome").fetchall()]
    except Exception as exc:
        out["session_admissions_status"] = repr(exc)

    out["messages_columns"] = [
        {"name": r[1], "type": r[2]} for r in cur.execute("pragma table_info(messages)").fetchall()]
    try:
        cols = {}
        for c in ("content", "api_content", "tool_calls", "display_metadata", "tool_name"):
            cols[c] = cur.execute(f"select sum(length({c})) from messages").fetchone()[0]
        out["messages_col_bytes"] = {k: v for k, v in cols.items() if v}
    except Exception as exc:
        out["messages_col_bytes"] = repr(exc)

    try:
        out["sessions_by_source"] = [{"source": r[0], "n": r[1]} for r in cur.execute(
            "select source, count(*) from sessions group by source order by 2 desc").fetchall()]
        out["sessions_columns"] = [r[1] for r in cur.execute("pragma table_info(sessions)").fetchall()]
    except Exception as exc:
        out["sessions_by_source"] = repr(exc)

    con.close()
    dest = sys.argv[1]
    with open(dest, "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    print("wrote", dest)


if __name__ == "__main__":
    main()
