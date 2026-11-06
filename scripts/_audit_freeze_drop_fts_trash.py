"""Reclaim the demoted v22 FTS shadow tables (Q2 / remediation B1).

WHAT IT DOES
    `hermes sessions optimize-storage` demotes the legacy v22 FTS shadow
    tables by renaming them to ``fts_v22_trash_*`` (hermes_state_search.py
    `_demote_legacy_fts_to_trash`) and is supposed to drain + DROP them in
    chunks.  On this host the rename committed but the drain never ran a
    single chunk (no ``fts_teardown_*_progress`` marker exists), so 8 orphan
    shadow tables still hold 168.6 MB.

    Those tables are dead weight: no trigger references them and the WAL frame
    decode shows zero frames written to them.  Dropping them is cheap (no row
    deletion, no blob rewrite) and puts ~43,700 pages back on SQLite's
    freelist, so the DB stops growing.  The file itself does NOT shrink --
    that would need a VACUUM, which on this ROTA=1 disk would rewrite 774 MB.
    Do NOT run VACUUM afterwards (see the report, section 3.4).

SAFETY
    * Refuses to run while the gateway looks alive, unless --force.
    * Dry run by default; --apply performs the DROP.
    * Only touches tables whose name matches ``fts_v22_trash_%``.
    * Takes its own exclusive transaction; rolls back on any error.

USAGE
    python scripts/_audit_freeze_drop_fts_trash.py              # dry run
    python scripts/_audit_freeze_drop_fts_trash.py --apply      # do it
    python scripts/_audit_freeze_drop_fts_trash.py --apply --force
"""
import argparse
import os
import sqlite3
import sys
import time

HOME = os.path.expanduser("~/.hermes")
DB = os.path.join(HOME, "state.db")
TRASH_PREFIX = "fts_v22_trash_"
LIKE = TRASH_PREFIX.replace("_", "\\_") + "%"


def _open(readonly):
    if readonly:
        return sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    return sqlite3.connect(DB, timeout=60)


def _trash_tables(conn):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ? ESCAPE '\\' ORDER BY name",
        (LIKE,)).fetchall()]


def _size_report(conn):
    return conn.execute(
        "SELECT name, SUM(pgsize) FROM dbstat WHERE name LIKE ? ESCAPE '\\' GROUP BY name ORDER BY 2 DESC",
        (LIKE,)).fetchall()


def _gateway_alive(conn):
    """A gateway heartbeat row younger than 10 minutes means a live writer."""
    try:
        row = conn.execute(
            "SELECT MAX(last_heartbeat) FROM gateway_heartbeats").fetchone()
    except sqlite3.Error:
        return None
    if not row or row[0] is None:
        return None
    return (time.time() - float(row[0])) < 600


def _stale_heartbeats(conn):
    """Dead gateway instances whose heartbeat rows were never pruned."""
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM gateway_heartbeats WHERE last_heartbeat < ?",
            (time.time() - 600,)).fetchone()[0]
    except sqlite3.Error:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually DROP (default: dry run)")
    ap.add_argument("--force", action="store_true", help="proceed even if the gateway looks alive")
    args = ap.parse_args()

    con = _open(readonly=not args.apply)
    con.row_factory = sqlite3.Row
    try:
        tables = _trash_tables(con)
        print(f"state.db: {DB}")
        print("page_count={} page_size={} freelist_count={} auto_vacuum={}".format(
            con.execute("PRAGMA page_count").fetchone()[0],
            con.execute("PRAGMA page_size").fetchone()[0],
            con.execute("PRAGMA freelist_count").fetchone()[0],
            con.execute("PRAGMA auto_vacuum").fetchone()[0]))
        rows = _size_report(con)
        total = sum(r[1] for r in rows)
        print(f"\ntrash shadow tables: {len(tables)}  total {total / 1048576:.2f} MB")
        for name, size in rows:
            print(f"  {size / 1048576:9.3f} MB  {name}")
        if not tables:
            print("\nnothing to do: no fts_v22_trash_* tables.")
            return 0

        alive = _gateway_alive(con)
        stale = _stale_heartbeats(con)
        print(f"\ngateway heartbeat fresh (<10min): {alive}")
        if stale:
            print(f"stale gateway_heartbeats rows (dead instances, never pruned): {stale}")
        if alive and not args.force:
            print("REFUSING: the gateway looks alive. Stop it first "
                  "(systemctl --user stop hermes-gateway.service) or pass --force.")
            return 2

        if not args.apply:
            print("\nDRY RUN. Re-run with --apply to DROP these tables.")
            print("After the drop: freelist_count should jump from ~7 to ~43,700 and the DB "
                  "stops growing. DO NOT run VACUUM (see report 3.4).")
            return 0

        print("\napplying...")
        before = con.execute("PRAGMA freelist_count").fetchone()[0]
        con.execute("BEGIN IMMEDIATE")
        try:
            for name in tables:
                con.execute(f'DROP TABLE IF EXISTS "{name}"')
                print(f"  dropped {name}")
            con.commit()
        except Exception:
            con.rollback()
            print("ROLLED BACK")
            raise
        after = con.execute("PRAGMA freelist_count").fetchone()[0]
        print(f"\nfreelist_count: {before} -> {after}  (+{after - before} pages reusable)")
        print(f"remaining trash tables: {_trash_tables(con)}")
        print("\nNext (optional, offline): 'hermes sessions optimize-storage --yes' would now "
              "stamp fts_storage_version. It also VACUUMs -- skip it on this ROTA=1 disk.")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
