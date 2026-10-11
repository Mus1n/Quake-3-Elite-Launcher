from __future__ import annotations

import argparse
import csv
import ctypes
import datetime as dt
import os
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path


def hide_own_console_if_requested() -> None:
    """Hide an accidentally inherited console when started by Q3Elite Launcher."""
    if os.name != "nt" or os.environ.get("Q3ELITE_TRACKER_HIDDEN") != "1":
        return
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS game_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            last_heartbeat TEXT NOT NULL,
            duration_seconds INTEGER NOT NULL DEFAULT 0,
            engine_process TEXT NOT NULL DEFAULT '',
            process_id INTEGER NOT NULL DEFAULT 0,
            tracker_token TEXT NOT NULL DEFAULT '',
            uploaded INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(game_sessions)").fetchall()
    }
    migrations = {
        "process_id": "ALTER TABLE game_sessions ADD COLUMN process_id INTEGER NOT NULL DEFAULT 0",
        "tracker_token": "ALTER TABLE game_sessions ADD COLUMN tracker_token TEXT NOT NULL DEFAULT ''",
    }
    for column, sql in migrations.items():
        if column not in columns:
            connection.execute(sql)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_game_sessions_uploaded ON game_sessions(uploaded, ended_at)"
    )
    connection.commit()


def connect_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=10)
    ensure_schema(connection)
    return connection


def running_xq3e_processes() -> list[tuple[int, str]]:
    if os.name != "nt":
        return []
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=flags,
            check=False,
        )
    except Exception:
        return []

    found: list[tuple[int, str]] = []
    for row in csv.reader((result.stdout or "").splitlines()):
        if len(row) < 2:
            continue
        image_name = str(row[0] or "").strip()
        if "xq3e" not in image_name.casefold():
            continue
        try:
            pid = int(str(row[1]).replace(",", "").strip())
        except (TypeError, ValueError):
            continue
        found.append((pid, image_name))
    return found


def parse_pid_set(value: str) -> set[int]:
    result: set[int] = set()
    for item in str(value or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            result.add(int(item))
        except ValueError:
            pass
    return result


def wait_for_new_process(ignore_pids: set[int], wait_seconds: float) -> tuple[int, str] | None:
    deadline = time.monotonic() + max(1.0, float(wait_seconds))
    while time.monotonic() < deadline:
        candidates = [
            process for process in running_xq3e_processes()
            if int(process[0]) not in ignore_pids
        ]
        if candidates:
            return candidates[0]
        time.sleep(0.5)
    return None


def wait_for_process_exit(pid: int, heartbeat_seconds: float, heartbeat_callback) -> None:
    """Wait efficiently on Windows, calling heartbeat_callback while the PID lives."""
    heartbeat_seconds = max(5.0, float(heartbeat_seconds))
    try:
        kernel32 = ctypes.windll.kernel32
        synchronize = 0x00100000
        wait_object_0 = 0x00000000
        wait_timeout = 0x00000102
        handle = kernel32.OpenProcess(synchronize, False, int(pid))
        if handle:
            try:
                timeout_ms = max(1000, round(heartbeat_seconds * 1000))
                while True:
                    result = kernel32.WaitForSingleObject(handle, timeout_ms)
                    if result == wait_object_0:
                        return
                    if result == wait_timeout:
                        heartbeat_callback()
                        continue
                    break
            finally:
                kernel32.CloseHandle(handle)
    except Exception:
        pass

    # Conservative fallback for unusual Windows permission/process-handle cases.
    next_heartbeat = time.monotonic()
    while True:
        running = any(
            int(candidate_pid) == int(pid)
            for candidate_pid, _ in running_xq3e_processes()
        )
        if not running:
            return
        now = time.monotonic()
        if now >= next_heartbeat:
            heartbeat_callback()
            next_heartbeat = now + heartbeat_seconds
        time.sleep(1.0)


def main() -> int:
    # pythonw.exe should mean there is no console at all.  This is an extra guard
    # for unusual launch environments or fallback to python.exe.
    hide_own_console_if_requested()

    parser = argparse.ArgumentParser(description="Q3Elite Gametime Tracker")
    parser.add_argument("--db", required=True)
    parser.add_argument("--ignore-pids", default="")
    parser.add_argument("--started-at", default="")
    parser.add_argument("--wait-seconds", type=float, default=35.0)
    parser.add_argument("--heartbeat-seconds", type=float, default=15.0)
    args = parser.parse_args()

    if os.name != "nt":
        return 0

    database = Path(args.db)
    ignore_pids = parse_pid_set(args.ignore_pids)
    process = wait_for_new_process(ignore_pids, args.wait_seconds)
    if process is None:
        return 0

    pid, image_name = process
    tracker_token = uuid.uuid4().hex
    started_at = str(args.started_at or "").strip() or utc_now()
    started_monotonic = time.monotonic()
    session_id: int | None = None

    try:
        with connect_db(database) as connection:
            cursor = connection.execute(
                """
                INSERT INTO game_sessions
                    (started_at, ended_at, last_heartbeat, duration_seconds,
                     engine_process, process_id, tracker_token, uploaded)
                VALUES (?, NULL, ?, 0, ?, ?, ?, 0)
                """,
                (started_at, utc_now(), image_name, int(pid), tracker_token),
            )
            connection.commit()
            session_id = int(cursor.lastrowid)

        heartbeat = max(5.0, float(args.heartbeat_seconds))

        def write_heartbeat() -> None:
            duration = max(0, round(time.monotonic() - started_monotonic))
            with connect_db(database) as connection:
                connection.execute(
                    """
                    UPDATE game_sessions
                       SET last_heartbeat = ?,
                           duration_seconds = ?
                     WHERE id = ? AND tracker_token = ?
                    """,
                    (utc_now(), duration, session_id, tracker_token),
                )
                connection.commit()

        # Write one early checkpoint, then block efficiently on the actual
        # Windows process handle instead of continuously polling tasklist.
        write_heartbeat()
        wait_for_process_exit(pid, heartbeat, write_heartbeat)

        duration = max(0, round(time.monotonic() - started_monotonic))
        ended_at = utc_now()
        with connect_db(database) as connection:
            connection.execute(
                """
                UPDATE game_sessions
                   SET ended_at = ?,
                       last_heartbeat = ?,
                       duration_seconds = ?,
                       uploaded = 0
                 WHERE id = ? AND tracker_token = ?
                """,
                (ended_at, ended_at, duration, session_id, tracker_token),
            )
            connection.commit()
        return 0

    except Exception:
        # Do not fabricate a clean end if the tracker itself fails. The launcher
        # will recover stale NULL-ended sessions at the last valid heartbeat.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
