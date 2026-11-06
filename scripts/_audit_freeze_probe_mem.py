"""Estimate the Python heap cost of a persisted transcript.

The gateway's memory valve measures anon RSS, and the docstring says one cached
agent "pins a full _session_messages transcript (tens of MB)". This script
reconstructs a transcript row set from state.db and measures the deep Python
object size, so the stored-bytes -> resident-bytes multiplier is a number, not
a guess.

Read-only. Usage: python scripts/_audit_freeze_probe_mem.py <session_id> [out.json]
"""
import json
import os
import sqlite3
import sys

DB = os.path.expanduser("~/.hermes/state.db")


def deep_size(obj, seen=None):
    """sys.getsizeof-based deep size (like pympler, in-process)."""
    if seen is None:
        seen = set()
    oid = id(obj)
    if oid in seen:
        return 0
    seen.add(oid)
    size = sys.getsizeof(obj)
    if isinstance(obj, dict):
        for k, v in obj.items():
            size += deep_size(k, seen) + deep_size(v, seen)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for item in obj:
            size += deep_size(item, seen)
    return size


def main():
    session_id = sys.argv[1]
    dest = sys.argv[2] if len(sys.argv) > 2 else None
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    cols = ("role", "content", "tool_calls", "tool_name", "reasoning",
            "reasoning_details", "reasoning_content", "api_content",
            "display_metadata", "codex_message_items", "codex_reasoning_items")
    rows = con.execute(
        "SELECT " + ",".join(cols) + " FROM messages WHERE session_id=?", (session_id,)).fetchall()
    con.close()

    stored = 0
    msgs = []
    for r in rows:
        d = {}
        for c in cols:
            v = r[c]
            if v is None:
                continue
            d[c] = v
            if isinstance(v, str):
                stored += len(v)
        msgs.append(d)
    heap = deep_size(msgs)
    out = {
        "session_id": session_id,
        "messages": len(msgs),
        "stored_text_bytes": stored,
        "python_heap_bytes": heap,
        "multiplier": (heap / stored) if stored else None,
        "heap_mb": heap / 1048576,
    }
    print(json.dumps(out, indent=1))
    if dest:
        with open(dest, "w") as fh:
            json.dump(out, fh, indent=1)


if __name__ == "__main__":
    main()
