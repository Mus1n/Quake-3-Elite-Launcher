#!/usr/bin/env python3
"""Detached Q3Elite playtime tracker; counts only foreground engine minutes."""
import argparse
import csv
import ctypes
import datetime as dt
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import uuid


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def processes():
    if os.name != "nt":
        return []
    try:
        result = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
            text=True, timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        out=[]
        for row in csv.reader((result.stdout or "").splitlines()):
            if len(row) < 2 or "xq3e" not in str(row[0]).casefold():
                continue
            try: out.append((int(str(row[1]).replace(",", "").strip()), str(row[0])))
            except ValueError: pass
        return out
    except Exception:
        return []


def foreground_pid():
    if os.name != "nt": return 0
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        pid = ctypes.c_ulong(0)
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--ignore-pids", default="")
    ap.add_argument("--started-at", default="")
    ap.add_argument("--wait-seconds", type=int, default=35)
    ap.add_argument("--heartbeat-seconds", type=int, default=60)
    args=ap.parse_args()
    ignored={int(x) for x in args.ignore_pids.split(",") if x.strip().isdigit()}
    db=Path(args.db)
    db.parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(str(db), timeout=15)
    con.execute("""CREATE TABLE IF NOT EXISTS game_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, ended_at TEXT,
        last_heartbeat TEXT NOT NULL, duration_seconds INTEGER NOT NULL DEFAULT 0,
        engine_process TEXT NOT NULL DEFAULT '', process_id INTEGER NOT NULL DEFAULT 0,
        tracker_token TEXT NOT NULL DEFAULT '', uploaded INTEGER NOT NULL DEFAULT 0)""")
    cols={r[1] for r in con.execute("PRAGMA table_info(game_sessions)")}
    if "process_id" not in cols: con.execute("ALTER TABLE game_sessions ADD COLUMN process_id INTEGER NOT NULL DEFAULT 0")
    if "tracker_token" not in cols: con.execute("ALTER TABLE game_sessions ADD COLUMN tracker_token TEXT NOT NULL DEFAULT ''")
    con.commit()
    deadline=time.monotonic()+max(5,args.wait_seconds)
    found=[]
    while time.monotonic() < deadline:
        found=[p for p in processes() if p[0] not in ignored]
        if found: break
        time.sleep(2)
    if not found:
        con.close(); return
    pid,name=found[0]
    token="foreground-"+uuid.uuid4().hex
    now=utcnow()
    cur=con.execute("INSERT INTO game_sessions(started_at,ended_at,last_heartbeat,duration_seconds,engine_process,process_id,tracker_token,uploaded) VALUES (?,NULL,?,0,?,?,?,0)",
        (now,now,name,pid,token))
    session_id=cur.lastrowid; con.commit()
    duration=0
    sample=max(60,int(args.heartbeat_seconds or 60))
    try:
        while True:
            time.sleep(sample)
            alive={p[0]:p[1] for p in processes()}
            if pid not in alive:
                # If a second XQ3E process exists, keep tracking it in the same session.
                candidates=[p for p in alive.items() if p[0] not in ignored]
                if not candidates: break
                pid,name=candidates[0]
            if foreground_pid() == pid:
                duration += sample
            stamp=utcnow()
            con.execute("UPDATE game_sessions SET last_heartbeat=?, duration_seconds=?, engine_process=?, process_id=? WHERE id=?",
                (stamp,duration,name,pid,session_id))
            con.commit()
    except Exception:
        pass
    finally:
        stamp=utcnow()
        try:
            con.execute("UPDATE game_sessions SET ended_at=?,last_heartbeat=?,duration_seconds=?,uploaded=0 WHERE id=?",
                (stamp,stamp,duration,session_id)); con.commit()
        except Exception: pass
        con.close()

if __name__ == "__main__": main()
