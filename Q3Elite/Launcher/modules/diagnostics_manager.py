from __future__ import annotations

import datetime as _dt
import json
import os
import platform
import re
import sqlite3
import sys
import threading
import traceback as _traceback
import uuid
from collections import deque
from pathlib import Path
from typing import Any


_EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])", re.I)
_IPV4_RE = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _safe_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:2000]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in list(value.items())[:80]:
            out[str(key)[:80]] = _safe_json(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [_safe_json(item, depth=depth + 1) for item in list(value)[:80]]
    return str(value)[:2000]


class _CapturedStream:
    """Small stdout/stderr tee used only for the in-memory crash context ring."""

    def __init__(self, manager: "DiagnosticsManager", original, stream_name: str):
        self.manager = manager
        self.original = original
        self.stream_name = stream_name
        self.encoding = getattr(original, "encoding", "utf-8") or "utf-8"
        self.errors = getattr(original, "errors", "replace") or "replace"

    def write(self, text):
        value = str(text or "")
        if value:
            self.manager._capture_log_text(value, self.stream_name)
        if self.original is not None:
            try:
                return self.original.write(value)
            except Exception:
                pass
        return len(value)

    def flush(self):
        if self.original is not None:
            try:
                self.original.flush()
            except Exception:
                pass

    def isatty(self):
        if self.original is not None:
            try:
                return bool(self.original.isatty())
            except Exception:
                pass
        return False

    def fileno(self):
        if self.original is not None and hasattr(self.original, "fileno"):
            return self.original.fileno()
        raise OSError("captured stream has no file descriptor")


class DiagnosticsManager:
    """Offline-first diagnostics/crash queue for Q3Elite Launcher.

    Nothing is uploaded unless diagnostics are enabled or the user explicitly
    sends a locally saved crash report. Anonymous mode uses a random install id
    that is separate from the Q3Elite account/device id. The launcher never asks
    an external IP service for the player's public address.
    """

    QUEUE_LIMIT = 500
    CRASH_KEEP = 8

    def __init__(self, data_dir: Path, account_manager, *, launcher_version: str = "unknown"):
        self.data_dir = Path(data_dir)
        self.account_manager = account_manager
        self.launcher_version = str(launcher_version or "unknown")[:80]
        self.root = self.data_dir / "diagnostics"
        self.crash_dir = self.root / "crashes"
        self.db_path = self.root / "diagnostics.sqlite3"
        self.identity_file = self.root / "identity.json"
        self.root.mkdir(parents=True, exist_ok=True)
        self.crash_dir.mkdir(parents=True, exist_ok=True)

        self.enabled = False
        self.auto_crash = False
        self.link_account = False
        self.install_id = self._load_or_create_install_id()
        self._lock = threading.RLock()
        self._flush_lock = threading.Lock()
        self._flush_thread: threading.Thread | None = None
        self._log_buffer: deque[str] = deque(maxlen=100)
        self._output_capture_installed = False
        self._last_crash_fingerprint = ""
        self._last_crash_at = 0.0
        self.last_sync_result = "Not synced yet."
        self._init_db()
        self._cleanup_old_crashes()

    # ------------------------------------------------------------------
    # Local storage / preferences
    # ------------------------------------------------------------------
    def _connect(self):
        con = sqlite3.connect(self.db_path, timeout=8)
        con.row_factory = sqlite3.Row
        return con

    def _init_db(self):
        with self._connect() as con:
            con.execute(
                """
                create table if not exists queue (
                    id text primary key,
                    kind text not null,
                    payload_json text not null,
                    created_at text not null,
                    attempts integer not null default 0,
                    last_error text not null default '',
                    force_send integer not null default 0
                )
                """
            )
            con.execute("create index if not exists queue_created_idx on queue(created_at)")

    def _load_or_create_install_id(self) -> str:
        try:
            if self.identity_file.is_file():
                raw = json.loads(self.identity_file.read_text(encoding="utf-8"))
                value = str(raw.get("install_id") or "").strip()
                if value:
                    return value
        except Exception:
            pass
        value = str(uuid.uuid4())
        try:
            self.identity_file.parent.mkdir(parents=True, exist_ok=True)
            self.identity_file.write_text(
                json.dumps({"install_id": value}, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass
        return value

    def set_preferences(self, *, enabled: bool, auto_crash: bool, link_account: bool):
        self.enabled = bool(enabled)
        self.auto_crash = bool(auto_crash) and self.enabled
        self.link_account = bool(link_account) and self.enabled
        if not self.enabled:
            # Opt-out means unsent automatic telemetry/crashes are discarded.
            # Explicitly queued crash reports (force_send=1) remain because the
            # player already pressed SEND for those reports.
            try:
                with self._connect() as con:
                    con.execute("delete from queue where force_send = 0")
            except Exception:
                pass

    def install_output_capture(self):
        if self._output_capture_installed:
            return
        self._output_capture_installed = True
        try:
            sys.stdout = _CapturedStream(self, sys.stdout, "stdout")
        except Exception:
            pass
        try:
            sys.stderr = _CapturedStream(self, sys.stderr, "stderr")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Sanitization
    # ------------------------------------------------------------------
    def _sanitize_text(self, text: str, *, limit: int = 16000) -> str:
        value = str(text or "")
        replacements = []
        try:
            replacements.append((str(Path.home()), "%USERPROFILE%"))
        except Exception:
            pass
        try:
            appdata = os.environ.get("APPDATA", "")
            if appdata:
                replacements.append((appdata, "%APPDATA%"))
        except Exception:
            pass
        for old, new in replacements:
            if old:
                value = value.replace(old, new).replace(old.replace("\\", "/"), new)
        value = _EMAIL_RE.sub("<email>", value)
        # Automatic diagnostic logs should never accidentally carry IP strings.
        value = _IPV4_RE.sub("<ip>", value)
        return value[:limit]

    def _sanitize_payload(self, value: Any) -> Any:
        value = _safe_json(value)
        if isinstance(value, str):
            return self._sanitize_text(value, limit=2000)
        if isinstance(value, list):
            return [self._sanitize_payload(x) for x in value]
        if isinstance(value, dict):
            return {str(k)[:80]: self._sanitize_payload(v) for k, v in value.items()}
        return value

    def _capture_log_text(self, text: str, stream_name: str):
        chunks = str(text).replace("\r", "\n").split("\n")
        with self._lock:
            for chunk in chunks:
                line = chunk.strip()
                if line:
                    self._log_buffer.append(
                        self._sanitize_text(f"[{stream_name}] {line}", limit=1200)
                    )

    def recent_logs(self, limit: int = 50) -> list[str]:
        with self._lock:
            return list(self._log_buffer)[-max(1, min(100, int(limit))):]

    # ------------------------------------------------------------------
    # Queue
    # ------------------------------------------------------------------
    def _queue(self, kind: str, payload: dict[str, Any], *, item_id: str | None = None, force_send: bool = False):
        item_id = str(item_id or uuid.uuid4())
        encoded = json.dumps(self._sanitize_payload(payload), ensure_ascii=False, separators=(",", ":"))
        with self._connect() as con:
            con.execute(
                """
                insert or ignore into queue(id, kind, payload_json, created_at, attempts, last_error, force_send)
                values(?,?,?,?,0,'',?)
                """,
                (item_id, str(kind), encoded, _utc_now_iso(), 1 if force_send else 0),
            )
            # Bound disk growth during long offline periods.
            con.execute(
                """
                delete from queue where id in (
                    select id from queue order by created_at desc limit -1 offset ?
                ) and force_send = 0
                """,
                (self.QUEUE_LIMIT,),
            )
        return item_id

    def pending_count(self) -> int:
        try:
            with self._connect() as con:
                row = con.execute("select count(*) as n from queue").fetchone()
                return int(row["n"] if row else 0)
        except Exception:
            return 0

    def record_event(self, event_name: str, payload: dict[str, Any] | None = None):
        if not self.enabled:
            return False
        event_name = re.sub(r"[^a-z0-9_.-]+", "_", str(event_name or "").strip().lower())[:64]
        if not event_name:
            return False
        self._queue(
            "telemetry",
            {
                "event_name": event_name,
                "payload": dict(payload or {}),
                "created_at": _utc_now_iso(),
            },
        )
        return True

    # ------------------------------------------------------------------
    # Crash reports
    # ------------------------------------------------------------------
    def _crash_files(self) -> list[Path]:
        try:
            return sorted(self.crash_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            return []

    def _cleanup_old_crashes(self):
        for path in self._crash_files()[self.CRASH_KEEP:]:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

    def capture_exception(self, exc_type, exc_value, exc_tb, *, context: dict[str, Any] | None = None):
        try:
            type_name = getattr(exc_type, "__name__", str(exc_type or "Exception"))[:120]
            message = self._sanitize_text(str(exc_value or ""), limit=4000)
            trace = self._sanitize_text(
                "".join(_traceback.format_exception(exc_type, exc_value, exc_tb)),
                limit=60000,
            )
            fingerprint = f"{type_name}|{message}|{trace[-800:]}"
            import time
            now = time.monotonic()
            if fingerprint == self._last_crash_fingerprint and now - self._last_crash_at < 4.0:
                return None
            self._last_crash_fingerprint = fingerprint
            self._last_crash_at = now

            crash_id = str(uuid.uuid4())
            payload = {
                "crash_id": crash_id,
                "created_at": _utc_now_iso(),
                "exception_type": type_name,
                "exception_message": message,
                "traceback": trace,
                "context": {
                    **dict(context or {}),
                    "platform": platform.platform(),
                    "python": platform.python_version(),
                    "recent_log": self.recent_logs(50),
                },
                "uploaded": False,
            }
            path = self.crash_dir / f"{payload['created_at'].replace(':', '-').replace('+', '_')}_{crash_id}.json"
            path.write_text(json.dumps(self._sanitize_payload(payload), ensure_ascii=False, indent=2), encoding="utf-8")
            self._cleanup_old_crashes()
            if self.enabled and self.auto_crash:
                self._queue("crash", payload, item_id=f"crash:{crash_id}", force_send=False)
                self.flush_async()
            return path
        except Exception:
            return None

    def latest_crash(self, *, include_uploaded: bool = False) -> dict[str, Any]:
        for path in self._crash_files():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    continue
                if raw.get("uploaded") and not include_uploaded:
                    continue
                raw["_path"] = str(path)
                return raw
            except Exception:
                continue
        return {}

    def send_latest_crash(self) -> bool:
        crash = self.latest_crash(include_uploaded=False)
        if not crash:
            return False
        crash_id = str(crash.get("crash_id") or uuid.uuid4())
        crash.pop("_path", None)
        self._queue("crash", crash, item_id=f"crash:{crash_id}", force_send=True)
        self.flush_async(force=True)
        return True

    def _mark_crash_uploaded(self, crash_id: str):
        for path in self._crash_files():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if str(raw.get("crash_id") or "") != str(crash_id):
                    continue
                raw["uploaded"] = True
                raw["uploaded_at"] = _utc_now_iso()
                path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
                return
            except Exception:
                continue

    # ------------------------------------------------------------------
    # Cloud upload
    # ------------------------------------------------------------------
    def _cloud_identity(self) -> tuple[str, str | None, str]:
        """Return access_token, optional user_id, and cloud device id."""
        if self.link_account:
            try:
                session = self.account_manager._ensure_session()
                user_id = str((session.user or {}).get("id") or "").strip()
                if user_id:
                    return session.access_token, user_id, str(self.account_manager.device_id)
            except Exception:
                pass
        return "", None, f"anon-{self.install_id}"

    def _load_queue(self, limit: int = 100, *, force: bool = False) -> list[sqlite3.Row]:
        with self._connect() as con:
            if force or self.enabled:
                return list(con.execute(
                    "select * from queue order by created_at asc limit ?",
                    (max(1, min(200, int(limit))),),
                ).fetchall())
            return list(con.execute(
                "select * from queue where force_send = 1 order by created_at asc limit ?",
                (max(1, min(200, int(limit))),),
            ).fetchall())

    def flush_pending(self, *, force: bool = False) -> dict[str, Any]:
        if not self._flush_lock.acquire(blocking=False):
            return {"busy": True, "pending": self.pending_count()}
        sent = 0
        try:
            rows = self._load_queue(force=force)
            if not rows:
                self.last_sync_result = "Nothing waiting to send."
                return {"sent": 0, "pending": 0}

            access_token, user_id, device_id = self._cloud_identity()
            client = self.account_manager._get_client()

            for row in rows:
                item_id = str(row["id"])
                kind = str(row["kind"])
                try:
                    payload = json.loads(str(row["payload_json"]))
                    if kind == "telemetry":
                        cloud_row = {
                            "user_id": user_id,
                            "device_id": device_id,
                            "event_name": str(payload.get("event_name") or "unknown")[:64],
                            "launcher_version": self.launcher_version,
                            "payload": self._sanitize_payload(payload.get("payload") or {}),
                            "created_at": str(payload.get("created_at") or _utc_now_iso()),
                        }
                        client.insert(
                            "telemetry_events",
                            cloud_row,
                            access_token=access_token,
                        )
                    elif kind == "crash":
                        context = dict(payload.get("context") or {})
                        context["crash_id"] = str(payload.get("crash_id") or "")
                        cloud_row = {
                            "user_id": user_id,
                            "device_id": device_id,
                            "launcher_version": self.launcher_version,
                            "exception_type": str(payload.get("exception_type") or "Exception")[:120],
                            "exception_message": str(payload.get("exception_message") or "")[:4000],
                            "traceback": str(payload.get("traceback") or "")[:60000],
                            "context": self._sanitize_payload(context),
                            "created_at": str(payload.get("created_at") or _utc_now_iso()),
                        }
                        client.insert(
                            "crash_reports",
                            cloud_row,
                            access_token=access_token,
                        )
                        self._mark_crash_uploaded(str(payload.get("crash_id") or ""))
                    else:
                        raise RuntimeError(f"Unknown diagnostics queue type: {kind}")

                    with self._connect() as con:
                        con.execute("delete from queue where id = ?", (item_id,))
                    sent += 1
                except Exception as error:
                    detail = self._sanitize_text(str(error), limit=1200)
                    with self._connect() as con:
                        con.execute(
                            "update queue set attempts = attempts + 1, last_error = ? where id = ?",
                            (detail, item_id),
                        )
                    self.last_sync_result = f"Upload paused: {detail}"
                    # Network/RLS failure will usually affect every remaining row.
                    break

            pending = self.pending_count()
            if sent:
                self.last_sync_result = f"Sent {sent} report(s). {pending} waiting."
            return {"sent": sent, "pending": pending, "status": self.last_sync_result}
        finally:
            self._flush_lock.release()

    def flush_async(self, *, force: bool = False):
        if self._flush_thread is not None and self._flush_thread.is_alive():
            return False

        def run():
            try:
                self.flush_pending(force=force)
            except Exception as error:
                self.last_sync_result = f"Upload failed: {self._sanitize_text(str(error), limit=800)}"

        self._flush_thread = threading.Thread(target=run, name="Q3EliteDiagnostics", daemon=True)
        self._flush_thread.start()
        return True

    def status_snapshot(self) -> dict[str, Any]:
        latest = self.latest_crash(include_uploaded=False)
        return {
            "enabled": self.enabled,
            "auto_crash": self.auto_crash,
            "link_account": self.link_account,
            "pending": self.pending_count(),
            "last_sync": self.last_sync_result,
            "unsent_crash": bool(latest),
            "crash_created_at": str(latest.get("created_at") or ""),
            "install_id": self.install_id,
        }
