from __future__ import annotations

import base64
import hashlib
import http.server
import ctypes
import datetime as _dt
import json
import os
import platform
import secrets
import socket
import sqlite3
import threading
import time
import urllib.parse
import webbrowser
import uuid
from pathlib import Path
from typing import Any

from supabase_client import AuthSession, SupabaseClient, SupabaseError


class AccountError(RuntimeError):
    pass


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _unix_now() -> int:
    return int(_dt.datetime.now(_dt.timezone.utc).timestamp())


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _dpapi_protect(data: bytes) -> bytes:
    if os.name != "nt":
        # Development fallback. Q3Elite production builds are Windows-only and
        # therefore use DPAPI. Do not treat this branch as secure storage.
        return b"plain:" + base64.b64encode(data)

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buffer = ctypes.create_string_buffer(data)
    in_blob = _DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    out_blob = _DataBlob()
    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        raise ctypes.WinError()
    try:
        return b"dpapi:" + ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))


def _dpapi_unprotect(data: bytes) -> bytes:
    if data.startswith(b"plain:"):
        return base64.b64decode(data[6:])
    if not data.startswith(b"dpapi:"):
        raise AccountError("Unknown account-session storage format.")
    if os.name != "nt":
        raise AccountError("This protected account session can only be opened on Windows.")

    encrypted = data[6:]
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buffer = ctypes.create_string_buffer(encrypted)
    in_blob = _DataBlob(
        len(encrypted),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    out_blob = _DataBlob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))


class AccountManager:
    """Offline-safe Q3Elite account/session + playtime sync manager.

    The launcher ships only Supabase's publishable key. Authentication tokens
    are user-scoped and the database is protected by RLS. The refresh token is
    encrypted with Windows DPAPI before it is written to AppData.
    """

    def __init__(self, launcher_dir: Path, data_dir: Path, playtime_db: Path):
        self.launcher_dir = Path(launcher_dir)
        self.data_dir = Path(data_dir)
        self.playtime_db = Path(playtime_db)
        self.backend_file = self.launcher_dir / "settings" / "backend.json"
        self.session_file = self.data_dir / "account_session.dat"
        self.device_file = self.data_dir / "device.json"
        self.pending_confirmation_file = self.data_dir / "pending_email_confirmation.json"
        self._lock = threading.RLock()
        self._config = self._load_backend_config()
        self._client: SupabaseClient | None = None
        self.session: AuthSession | None = None
        self.profile: dict[str, Any] = {}
        self.device_id = self._load_or_create_device_id()

    # ------------------------------------------------------------------
    # Configuration / local identity
    # ------------------------------------------------------------------
    def _load_backend_config(self) -> dict[str, Any]:
        try:
            if not self.backend_file.is_file():
                return {}
            raw = json.loads(self.backend_file.read_text(encoding="utf-8"))
            return dict(raw) if isinstance(raw, dict) else {}
        except Exception as error:
            print(f"[account] Could not read backend.json: {error}")
            return {}

    @property
    def configured(self) -> bool:
        url = str(self._config.get("url") or "").strip()
        key = str(
            self._config.get("publishable_key")
            or self._config.get("anon_key")
            or ""
        ).strip()
        enabled = bool(self._config.get("enabled", True))
        return enabled and url.startswith("https://") and bool(key)

    @property
    def config_status(self) -> str:
        if not self.backend_file.is_file():
            return "settings/backend.json is missing"
        if not bool(self._config.get("enabled", True)):
            return "cloud backend is disabled in settings/backend.json"
        if not str(self._config.get("url") or "").startswith("https://"):
            return "Supabase project URL is missing"
        if not str(self._config.get("publishable_key") or self._config.get("anon_key") or "").strip():
            return "Supabase publishable key is missing"
        return "ready"

    def _get_client(self) -> SupabaseClient:
        if not self.configured:
            raise AccountError(f"Q3Elite Cloud is not configured: {self.config_status}.")
        if self._client is None:
            self._client = SupabaseClient(
                str(self._config.get("url") or ""),
                str(self._config.get("publishable_key") or self._config.get("anon_key") or ""),
                timeout=float(self._config.get("timeout_seconds") or 12),
            )
        return self._client

    def _load_or_create_device_id(self) -> str:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            if self.device_file.is_file():
                raw = json.loads(self.device_file.read_text(encoding="utf-8"))
                value = str(raw.get("device_id") or "").strip()
                if value:
                    return value
        except Exception as error:
            print(f"[account] Could not read device identity: {error}")

        value = str(uuid.uuid4())
        try:
            self.device_file.write_text(
                json.dumps({"device_id": value}, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception as error:
            print(f"[account] Could not persist device identity: {error}")
        return value

    # ------------------------------------------------------------------
    # Pending email confirmation
    # ------------------------------------------------------------------
    @property
    def pending_confirmation_email(self) -> str:
        try:
            if not self.pending_confirmation_file.is_file():
                return ""
            raw = json.loads(self.pending_confirmation_file.read_text(encoding="utf-8"))
            return str(raw.get("email") or "").strip() if isinstance(raw, dict) else ""
        except Exception:
            return ""

    def _save_pending_confirmation(self, email: str) -> None:
        email = str(email or "").strip()
        if not email:
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.pending_confirmation_file.write_text(
            json.dumps({"email": email, "created_at": _utc_now_iso()}, indent=2) + "\n",
            encoding="utf-8",
        )

    def clear_pending_confirmation(self) -> None:
        try:
            self.pending_confirmation_file.unlink(missing_ok=True)
        except Exception as error:
            print(f"[account] Could not clear pending email confirmation: {error}")

    # ------------------------------------------------------------------
    # Protected persistent session
    # ------------------------------------------------------------------
    @property
    def has_saved_session(self) -> bool:
        return self.session_file.is_file()

    def _save_session(self, session: AuthSession) -> None:
        payload = {
            "refresh_token": session.refresh_token,
            "user": session.user,
            "saved_at": _utc_now_iso(),
        }
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        protected = _dpapi_protect(raw)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.session_file.with_suffix(".tmp")
        temporary.write_bytes(protected)
        os.replace(temporary, self.session_file)

    def _read_saved_session(self) -> dict[str, Any]:
        if not self.session_file.is_file():
            return {}
        try:
            raw = _dpapi_unprotect(self.session_file.read_bytes())
            parsed = json.loads(raw.decode("utf-8"))
            return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception as error:
            raise AccountError(f"Could not unlock the saved Q3Elite account session: {error}") from error

    def clear_saved_session(self) -> None:
        try:
            self.session_file.unlink(missing_ok=True)
        except Exception as error:
            print(f"[account] Could not remove saved session: {error}")

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------
    def _finish_authenticated_session(self, session: AuthSession) -> dict[str, Any]:
        with self._lock:
            self.session = session
            self._save_session(session)
        # A successful authenticated session proves any pending signup for this
        # launcher has been completed (or the player chose another verified
        # identity). Do not leave a stale verification banner behind.
        self.clear_pending_confirmation()
        self.profile = self.fetch_profile()

        # Older accounts may have been created before OAuth avatar metadata was
        # copied into public.profiles. Fill it lazily from the authenticated user
        # without overwriting a profile avatar the player already owns.
        if not str(self.profile.get("avatar_url") or "").strip():
            metadata = (session.user or {}).get("user_metadata") or (session.user or {}).get("raw_user_meta_data") or {}
            if isinstance(metadata, dict):
                avatar = str(metadata.get("avatar_url") or metadata.get("picture") or "").strip()
                if avatar:
                    try:
                        user_id = self._user_id(session)
                        self._get_client().update(
                            "profiles",
                            {"avatar_url": avatar, "updated_at": _utc_now_iso()},
                            access_token=session.access_token,
                            query=urllib.parse.urlencode({"id": f"eq.{user_id}"}),
                        )
                        self.profile["avatar_url"] = avatar
                    except Exception as error:
                        print(f"[account] Could not import provider avatar: {error}")

        self.register_device()
        return self.account_snapshot()

    def restore(self) -> dict[str, Any]:
        if not self.has_saved_session:
            return {"restored": False}
        saved = self._read_saved_session()
        refresh = str(saved.get("refresh_token") or "")
        if not refresh:
            self.clear_saved_session()
            return {"restored": False}
        try:
            session = self._get_client().refresh_session(refresh)
        except SupabaseError as error:
            # Network outages, a paused Free project, or temporary 5xx errors
            # must NOT log the player out. Clear the local refresh token only
            # when Supabase explicitly tells us that the token itself is dead.
            text = str(error).casefold()
            token_dead = (
                ("refresh token" in text or "refresh_token" in text or "session_not_found" in text)
                and any(word in text for word in ("invalid", "expired", "not found", "already used", "revoked"))
            )
            if token_dead:
                self.clear_saved_session()
                self.session = None
                self.profile = {}
            raise
        snapshot = self._finish_authenticated_session(session)
        snapshot["restored"] = True
        return snapshot

    def sign_in(self, email: str, password: str) -> dict[str, Any]:
        session = self._get_client().sign_in(email, password)
        snapshot = self._finish_authenticated_session(session)
        snapshot["signed_in"] = True
        return snapshot

    def _oauth_callback_settings(self) -> tuple[str, int, int]:
        host = str(self._config.get("oauth_callback_host") or "127.0.0.1").strip()
        # Keep the callback loopback-only. Do not allow backend.json to expose
        # a listener on LAN/WAN interfaces.
        if host not in {"127.0.0.1", "localhost"}:
            host = "127.0.0.1"
        try:
            port = int(self._config.get("oauth_callback_port") or 8765)
        except (TypeError, ValueError):
            port = 8765
        if not 1024 <= port <= 65535:
            port = 8765
        try:
            timeout = int(self._config.get("oauth_timeout_seconds") or 180)
        except (TypeError, ValueError):
            timeout = 180
        timeout = max(30, min(timeout, 600))
        return host, port, timeout

    def sign_in_oauth(self, provider: str) -> dict[str, Any]:
        """Authenticate with a Supabase social provider using browser + PKCE.

        A short-lived HTTP callback is bound only to loopback. The provider never
        sends credentials to Q3Elite; the browser authenticates with Google,
        Discord or Telegram, Supabase returns a one-time auth code, and the
        launcher exchanges that code with the locally-held PKCE verifier.
        """
        requested_provider = str(provider or "").strip().lower()
        provider_aliases = {
            "google": "google",
            "discord": "discord",
            "telegram": "custom:telegram",
            "custom:telegram": "custom:telegram",
        }
        provider = provider_aliases.get(requested_provider, "")
        if not provider:
            raise AccountError(
                f"Unsupported Q3Elite OAuth provider: {requested_provider or 'unknown'}"
            )

        provider_label = {
            "google": "Google",
            "discord": "Discord",
            "custom:telegram": "Telegram",
        }.get(provider, provider.replace("custom:", "").title())

        client = self._get_client()
        # /auth/v1/settings reliably exposes the built-in providers. Custom
        # OAuth/OIDC providers are managed separately by Supabase, so attempting
        # the real authorize endpoint is the authoritative availability check.
        if not provider.startswith("custom:"):
            try:
                settings = client.auth_settings()
                external = settings.get("external") if isinstance(settings, dict) else {}
                if isinstance(external, dict) and external.get(provider) is False:
                    raise AccountError(
                        f"{provider_label} sign-in is not enabled in Supabase Auth providers."
                    )
            except AccountError:
                raise
            except Exception as error:
                # Provider discovery is useful, but a temporary settings-endpoint
                # failure should not prevent attempting the real OAuth endpoint.
                print(f"[account] Could not read OAuth provider settings: {error}")

        host, port, timeout = self._oauth_callback_settings()
        callback_nonce = secrets.token_urlsafe(18)
        callback_path = f"/q3elite-auth/{callback_nonce}"
        redirect_host = "127.0.0.1" if host == "localhost" else host
        redirect_to = f"http://{redirect_host}:{port}{callback_path}"

        # RFC 7636 verifier: URL-safe and comfortably inside the 43..128 range.
        code_verifier = secrets.token_urlsafe(64)[:96]
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

        result: dict[str, str] = {}

        class CallbackHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path != callback_path:
                    self.send_response(404)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b"Q3Elite OAuth callback not found.")
                    return

                params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                code = str((params.get("code") or [""])[0])
                error = str((params.get("error_description") or params.get("error") or [""])[0])
                if code:
                    result["code"] = code
                    title = "Q3Elite sign-in complete"
                    body = "Authentication completed. You can close this browser tab and return to Q3Elite Launcher."
                else:
                    result["error"] = error or "OAuth provider returned no authorization code."
                    title = "Q3Elite sign-in failed"
                    body = result["error"]

                page = (
                    "<!doctype html><html><head><meta charset='utf-8'><title>" + title + "</title>"
                    "<style>body{font-family:Segoe UI,Arial,sans-serif;background:#0b0b0d;color:#eee;"
                    "display:grid;place-items:center;min-height:100vh;margin:0}.box{max-width:620px;"
                    "padding:32px;border:1px solid #444;border-radius:14px;background:#151518}"
                    "h1{font-size:24px;margin-top:0}p{color:#bbb;line-height:1.5}</style></head>"
                    "<body><div class='box'><h1>" + title + "</h1><p>" + body + "</p></div></body></html>"
                ).encode("utf-8", errors="replace")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)

        try:
            server = http.server.HTTPServer((host, port), CallbackHandler)
        except OSError as error:
            raise AccountError(
                f"Could not start the local Q3Elite OAuth callback on {host}:{port}. "
                "Another application may already be using this port."
            ) from error

        server.timeout = 0.75
        try:
            authorize_url = client.oauth_authorize_url(
                provider,
                redirect_to=redirect_to,
                code_challenge=code_challenge,
            )
            # Social authentication always happens in the user's normal browser;
            # no Google/Discord password ever enters the launcher process.
            webbrowser.open(authorize_url, new=2, autoraise=True)

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and not result:
                server.handle_request()
        finally:
            server.server_close()

        if not result:
            raise AccountError(
                f"{provider_label} sign-in timed out. Start the sign-in again from Q3Elite Launcher."
            )
        if result.get("error"):
            raise AccountError(result["error"])

        session = client.exchange_pkce_code(result.get("code", ""), code_verifier)
        snapshot = self._finish_authenticated_session(session)
        snapshot["signed_in"] = True
        snapshot["oauth_provider"] = provider
        return snapshot

    def link_oauth_identity(self, provider: str) -> dict[str, Any]:
        """Link Google/Discord/Telegram OAuth to the currently signed-in Q3Elite user."""
        session = self._ensure_session()
        requested_provider = str(provider or "").strip().lower()
        provider_aliases = {
            "discord": "discord",
            "telegram": "custom:telegram",
            "custom:telegram": "custom:telegram",
            "google": "google",
        }
        provider = provider_aliases.get(requested_provider, "")
        if not provider:
            raise AccountError(f"Unsupported identity provider: {requested_provider or 'unknown'}")

        providers = {str(x or "").strip().lower() for x in (self.account_snapshot().get("providers") or [])}
        if provider in providers or (provider == "custom:telegram" and "telegram" in providers):
            return {**self.account_snapshot(), "linked_provider": provider, "already_linked": True}

        host, port, timeout = self._oauth_callback_settings()
        callback_nonce = secrets.token_urlsafe(18)
        callback_path = f"/q3elite-link/{callback_nonce}"
        redirect_host = "127.0.0.1" if host == "localhost" else host
        redirect_to = f"http://{redirect_host}:{port}{callback_path}"

        code_verifier = secrets.token_urlsafe(64)[:96]
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        result: dict[str, str] = {}

        class CallbackHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path != callback_path:
                    self.send_response(404); self.end_headers(); return
                params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                code = str((params.get("code") or [""])[0])
                error = str((params.get("error_description") or params.get("error") or [""])[0])
                if code:
                    result["code"] = code
                    title = "Q3Elite identity linked"
                    body = "Authorization completed. You can close this tab and return to Q3Elite Launcher."
                else:
                    result["error"] = error or "OAuth provider returned no authorization code."
                    title = "Q3Elite identity linking failed"
                    body = result["error"]
                page = (
                    "<!doctype html><html><head><meta charset='utf-8'><title>" + title + "</title>"
                    "<style>body{font-family:Segoe UI,Arial,sans-serif;background:#0b0b0d;color:#eee;"
                    "display:grid;place-items:center;min-height:100vh;margin:0}.box{max-width:620px;"
                    "padding:32px;border:1px solid #444;border-radius:14px;background:#151518}"
                    "h1{font-size:24px;margin-top:0}p{color:#bbb;line-height:1.5}</style></head>"
                    "<body><div class='box'><h1>" + title + "</h1><p>" + body + "</p></div></body></html>"
                ).encode("utf-8", errors="replace")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers(); self.wfile.write(page)

        try:
            server = http.server.HTTPServer((host, port), CallbackHandler)
        except OSError as error:
            raise AccountError(f"Could not start the local Q3Elite OAuth callback on {host}:{port}.") from error
        server.timeout = 0.75
        try:
            scopes = "identify email" if provider == "discord" else ""
            authorize_url = self._get_client().oauth_link_authorize_url(
                provider,
                access_token=session.access_token,
                redirect_to=redirect_to,
                code_challenge=code_challenge,
                scopes=scopes,
            )
            webbrowser.open(authorize_url, new=2, autoraise=True)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and not result:
                server.handle_request()
        finally:
            server.server_close()

        if not result:
            raise AccountError("Identity linking timed out. Start it again from Q3Elite Launcher.")
        if result.get("error"):
            raise AccountError(result["error"])

        linked_session = self._get_client().exchange_pkce_code(result.get("code", ""), code_verifier)
        snapshot = self._finish_authenticated_session(linked_session)
        snapshot["linked_provider"] = provider
        return snapshot

    def unlink_oauth_identity(self, provider: str) -> dict[str, Any]:
        """Unlink one Google/Discord/Telegram identity from the signed-in user."""
        session = self._ensure_session()
        requested = str(provider or "").strip().lower()
        aliases = {
            "google": {"google"},
            "discord": {"discord"},
            "telegram": {"telegram", "custom:telegram"},
        }
        accepted = aliases.get(requested)
        if not accepted:
            raise AccountError(f"Unsupported identity provider: {requested or 'unknown'}")

        client = self._get_client()
        user = client.get_user(session.access_token)
        identities = [item for item in (user.get("identities") or []) if isinstance(item, dict)]
        if len(identities) < 2:
            raise AccountError("Keep at least one other sign-in method connected before unlinking this account.")

        target = next(
            (item for item in identities if str(item.get("provider") or "").strip().lower() in accepted),
            None,
        )
        if target is None:
            raise AccountError(f"No linked {requested.title()} identity was found on this account.")
        identity_id = str(target.get("identity_id") or target.get("id") or "").strip()
        if not identity_id:
            raise AccountError("Supabase did not return an ID for the selected identity.")

        client.unlink_identity(identity_id, access_token=session.access_token)
        user = client.get_user(session.access_token)
        with self._lock:
            session.user = user
            self.session = session
            self._save_session(session)
        snapshot = self.account_snapshot()
        snapshot["unlinked_provider"] = requested
        return snapshot

    def sign_up(self, nickname: str, email: str, password: str) -> dict[str, Any]:
        payload = self._get_client().sign_up(email, password, nickname=nickname)
        if payload.get("access_token"):
            session = AuthSession.from_payload(payload)
            snapshot = self._finish_authenticated_session(session)
            snapshot["signed_up"] = True
            snapshot["confirmation_required"] = False
            return snapshot

        # Confirm-email mode returns a user but no session. Persist the pending
        # email so PROFILE can keep showing the verification state after a
        # launcher restart and offer a resend action.
        pending_email = str(email).strip()
        self._save_pending_confirmation(pending_email)
        return {
            "signed_up": True,
            "confirmation_required": True,
            "email": pending_email,
            "user": dict(payload.get("user") or {}),
        }

    def resend_confirmation(self) -> dict[str, Any]:
        email = self.pending_confirmation_email
        if not email:
            raise AccountError("There is no pending email confirmation to resend.")
        self._get_client().resend_signup_confirmation(email)
        return {
            "resent": True,
            "email": email,
            **self.account_snapshot(),
        }

    def sign_out(self) -> dict[str, Any]:
        session = self.session
        try:
            if session is not None and session.access_token:
                self._get_client().sign_out(session.access_token)
        except Exception as error:
            # Logout is still completed locally if the network is unavailable.
            print(f"[account] Remote sign-out failed: {error}")
        finally:
            with self._lock:
                self.session = None
                self.profile = {}
                self.clear_saved_session()
        return {"signed_out": True}

    def _ensure_session(self) -> AuthSession:
        with self._lock:
            session = self.session
        if session is None:
            raise AccountError("Sign in to Q3Elite Cloud first.")
        if session.expires_at and session.expires_at <= _unix_now() + 120:
            refreshed = self._get_client().refresh_session(session.refresh_token)
            with self._lock:
                self.session = refreshed
                self._save_session(refreshed)
            session = refreshed
        return session

    # ------------------------------------------------------------------
    # Profile / device
    # ------------------------------------------------------------------
    @staticmethod
    def _user_id(session: AuthSession | None) -> str:
        if session is None:
            return ""
        return str((session.user or {}).get("id") or "")

    def fetch_profile(self) -> dict[str, Any]:
        session = self._ensure_session()
        user_id = self._user_id(session)
        if not user_id:
            return {}
        query = urllib.parse.urlencode({
            "select": "id,nickname,avatar_url,created_at,updated_at",
            "id": f"eq.{user_id}",
            "limit": "1",
        })
        rows = self._get_client().select(
            "profiles",
            access_token=session.access_token,
            query=query,
        )
        return dict(rows[0]) if rows else {}

    def register_device(self) -> None:
        session = self._ensure_session()
        user_id = self._user_id(session)
        if not user_id:
            raise AccountError("Authenticated account has no user id.")
        device_name = str(socket.gethostname() or "Windows PC").strip()[:80] or "Windows PC"
        row = {
            "user_id": user_id,
            "device_id": self.device_id,
            "device_name": device_name,
            "os_name": platform.platform(),
            "last_seen": _utc_now_iso(),
        }
        try:
            self._get_client().upsert(
                "devices",
                row,
                access_token=session.access_token,
                on_conflict="user_id,device_id",
            )
        except SupabaseError as error:
            # V23.3 databases do not have device_name yet. Keep authentication
            # working before the optional V23.4 SQL migration is applied.
            if "device_name" not in str(error).casefold():
                raise
            row.pop("device_name", None)
            self._get_client().upsert(
                "devices",
                row,
                access_token=session.access_token,
                on_conflict="user_id,device_id",
            )

    def update_profile(self, nickname: str) -> dict[str, Any]:
        session = self._ensure_session()
        user_id = self._user_id(session)
        nickname = str(nickname or "").strip()
        if len(nickname) < 2 or len(nickname) > 28:
            raise AccountError("Q3Elite nickname must contain 2 to 28 characters.")
        try:
            self._get_client().update(
                "profiles",
                {"nickname": nickname, "updated_at": _utc_now_iso()},
                access_token=session.access_token,
                query=urllib.parse.urlencode({"id": f"eq.{user_id}"}),
            )
        except SupabaseError as error:
            text = str(error)
            if "duplicate" in text.casefold() or "unique" in text.casefold():
                raise AccountError("That Q3Elite nickname is already in use.") from error
            raise
        self.profile = self.fetch_profile()
        return {"updated": True, "overview": self.account_overview(), **self.account_snapshot()}

    def upload_avatar(self, image_bytes: bytes, content_type: str = "image/jpeg") -> dict[str, Any]:
        session = self._ensure_session()
        user_id = self._user_id(session)
        if not user_id:
            raise AccountError("Authenticated account has no user id.")
        data = bytes(image_bytes or b"")
        if not data:
            raise AccountError("Profile image is empty.")
        if len(data) > 2 * 1024 * 1024:
            raise AccountError("Profile image is larger than 2 MB after processing.")

        content_type = str(content_type or "image/jpeg").lower().strip()
        extensions = {
            "image/jpeg": "jpg",
            "image/jpg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
        }
        extension = extensions.get(content_type, "jpg")
        if extension == "jpg":
            content_type = "image/jpeg"

        # Upload the new object first. Only after the profile points to it do we
        # remove older revisions, so a network/database failure can never leave
        # the account without its previous working avatar.
        stamp = int(time.time() * 1000)
        object_path = f"{user_id}/avatar-{stamp}.{extension}"
        client = self._get_client()
        client.upload_storage_object(
            "avatars",
            object_path,
            data,
            access_token=session.access_token,
            content_type=content_type,
        )
        avatar_url = client.public_storage_url("avatars", object_path)
        try:
            client.update(
                "profiles",
                {"avatar_url": avatar_url, "updated_at": _utc_now_iso()},
                access_token=session.access_token,
                query=urllib.parse.urlencode({"id": f"eq.{user_id}"}),
            )
        except Exception:
            # Keep the previous profile intact. The just-uploaded object may be
            # cleaned by the next successful avatar replacement.
            raise

        self.profile = self.fetch_profile()

        # Keep only the active avatar in this user's Storage folder. Existing
        # V23.5 installations may already contain several timestamped files; the
        # first successful replacement after this update cleans them all.
        try:
            objects = client.list_storage_objects(
                "avatars",
                prefix=user_id,
                access_token=session.access_token,
                limit=1000,
            )
            for item in objects:
                raw_name = str((item or {}).get("name") or "").strip().lstrip("/")
                if not raw_name:
                    continue
                candidate = raw_name if "/" in raw_name else f"{user_id}/{raw_name}"
                filename = candidate.rsplit("/", 1)[-1]
                if candidate == object_path or not filename.startswith("avatar-"):
                    continue
                try:
                    client.delete_storage_object(
                        "avatars",
                        candidate,
                        access_token=session.access_token,
                    )
                except Exception as error:
                    print(f"[account] Could not delete old avatar {candidate}: {error}")
        except Exception as error:
            print(f"[account] Could not enumerate old avatars: {error}")

        return {"updated": True, "avatar_url": avatar_url, **self.account_snapshot()}

    def list_devices(self) -> list[dict[str, Any]]:
        session = self._ensure_session()
        user_id = self._user_id(session)
        query = urllib.parse.urlencode({
            "select": "device_id,device_name,os_name,first_seen,last_seen",
            "user_id": f"eq.{user_id}",
            "order": "last_seen.desc",
        })
        try:
            return self._get_client().select("devices", access_token=session.access_token, query=query)
        except SupabaseError as error:
            if "device_name" not in str(error).casefold():
                raise
            legacy_query = urllib.parse.urlencode({
                "select": "device_id,os_name,first_seen,last_seen",
                "user_id": f"eq.{user_id}",
                "order": "last_seen.desc",
            })
            return self._get_client().select("devices", access_token=session.access_token, query=legacy_query)

    def remove_device(self, device_id: str) -> dict[str, Any]:
        session = self._ensure_session()
        user_id = self._user_id(session)
        device_id = str(device_id or "").strip()
        if not device_id:
            raise AccountError("Device id is missing.")
        if device_id == self.device_id:
            raise AccountError("The current device cannot be removed while it is signed in.")
        query = urllib.parse.urlencode({
            "user_id": f"eq.{user_id}",
            "device_id": f"eq.{device_id}",
        })
        self._get_client().delete("devices", access_token=session.access_token, query=query)
        return {"removed": True, "device_id": device_id}

    def _cloud_session_stats(self) -> dict[str, Any]:
        session = self._ensure_session()
        user_id = self._user_id(session)
        query = urllib.parse.urlencode({
            "select": "duration_seconds,started_at,ended_at",
            "user_id": f"eq.{user_id}",
            "order": "started_at.desc",
            "limit": "5000",
        })
        rows = self._get_client().select("game_sessions", access_token=session.access_token, query=query)
        total = sum(max(0, int(row.get("duration_seconds") or 0)) for row in rows if isinstance(row, dict))
        last_played = ""
        for row in rows:
            if not isinstance(row, dict):
                continue
            candidate = str(row.get("ended_at") or row.get("started_at") or "")
            if candidate and (not last_played or candidate > last_played):
                last_played = candidate
        return {
            "total_playtime_seconds": total,
            "session_count": len(rows),
            "last_played": last_played,
        }

    def save_cloud_state(
        self,
        state: dict[str, Any],
        *,
        user_config_kind: str = "auto",
    ) -> dict[str, Any]:
        """Upsert the user's portable Q3Elite configuration snapshot.

        The cloud row is intentionally one-current-snapshot-per-account rather
        than an append-only history. Repeated SYNC SETTINGS updates the same row.
        UserConfig.cfg is transferred as base64 so non-UTF8 Quake configs are
        restored byte-for-byte on another PC.
        """
        session = self._ensure_session()
        user_id = self._user_id(session)
        state = dict(state or {})
        matchmaking = state.get("matchmaking") if isinstance(state.get("matchmaking"), dict) else {}
        user_config_b64 = str(state.get("user_config_b64") or "")
        query = urllib.parse.urlencode({
            "select": "preferences,user_config_b64,user_config_sha256,user_config_size,source_device_id,updated_at",
            "user_id": f"eq.{user_id}",
            "limit": "1",
        })
        existing_rows = self._get_client().select(
            "cloud_state", access_token=session.access_token, query=query
        )
        existing = dict(existing_rows[0]) if existing_rows and isinstance(existing_rows[0], dict) else {}
        preferences = state.get("preferences") if isinstance(state.get("preferences"), dict) else {}
        preferences = dict(preferences)
        slot_key = "_q3elite_user_config_slots"
        previous_preferences = existing.get("preferences") if isinstance(existing.get("preferences"), dict) else {}
        slots = preferences.get(slot_key)
        if not isinstance(slots, list):
            slots = previous_preferences.get(slot_key, [])
        slots = [dict(item) for item in slots if isinstance(item, dict)] if isinstance(slots, list) else []
        if not slots and existing.get("user_config_b64"):
            slots.append({
                "kind": "auto",
                "b64": str(existing.get("user_config_b64") or ""),
                "sha256": str(existing.get("user_config_sha256") or ""),
                "size": max(0, int(existing.get("user_config_size") or 0)),
                "saved_at": str(existing.get("updated_at") or ""),
                "device_id": str(existing.get("source_device_id") or ""),
            })
        if user_config_b64:
            new_slot = {
                "kind": "manual" if user_config_kind == "manual" else "auto",
                "b64": user_config_b64,
                "sha256": str(state.get("user_config_sha256") or ""),
                "size": max(0, int(state.get("user_config_size") or 0)),
                "saved_at": _utc_now_iso(),
                "device_id": self.device_id,
            }
            auto_slot = next((item for item in slots if str(item.get("kind") or "auto") == "auto"), None)
            manual_slots = [item for item in slots if str(item.get("kind") or "auto") == "manual"]
            if new_slot["kind"] == "manual":
                manual_slots = [new_slot] + manual_slots
            else:
                auto_slot = new_slot
            manual_limit = 2 if auto_slot else 3
            slots = ([auto_slot] if auto_slot else []) + manual_slots[:manual_limit]
        if slots:
            preferences[slot_key] = slots[:3]
        row = {
            "user_id": user_id,
            "preferences": preferences,
            "servers": state.get("servers") if isinstance(state.get("servers"), list) else [],
            "matchmaking": matchmaking,
            "user_config_b64": user_config_b64 or None,
            "user_config_sha256": str(state.get("user_config_sha256") or "") or None,
            "user_config_size": max(0, int(state.get("user_config_size") or 0)),
            "theme_id": str(state.get("theme_id") or ""),
            "state_version": 2,
            "source_device_id": self.device_id,
            "updated_at": _utc_now_iso(),
        }
        result = self._get_client().upsert(
            "cloud_state",
            row,
            access_token=session.access_token,
            on_conflict="user_id",
            return_rows=True,
        )
        saved = dict(result[0]) if isinstance(result, list) and result else row
        if slots:
            saved["user_config_slots"] = slots[:3]
        return {"saved": True, "cloud_state": saved, **self.account_snapshot()}

    def save_user_config_snapshot(self, state: dict[str, Any]) -> dict[str, Any]:
        """Save a manual UserConfig.cfg restore point, retaining two older ones."""
        return self.save_cloud_state(state, user_config_kind="manual")

    def load_cloud_state(self) -> dict[str, Any]:
        session = self._ensure_session()
        user_id = self._user_id(session)
        query = urllib.parse.urlencode({
            "select": (
                "preferences,servers,matchmaking,user_config_b64,user_config_sha256,"
                "user_config_size,theme_id,state_version,source_device_id,updated_at"
            ),
            "user_id": f"eq.{user_id}",
            "limit": "1",
        })
        rows = self._get_client().select("cloud_state", access_token=session.access_token, query=query)
        if not rows:
            return {"found": False, **self.account_snapshot()}
        row = dict(rows[0])
        preferences = dict(row.get("preferences") or {})
        slots = preferences.get("_q3elite_user_config_slots")
        if not isinstance(slots, list):
            slots = []
        slots = [dict(item) for item in slots if isinstance(item, dict)]
        legacy_b64 = str(row.get("user_config_b64") or "")
        if not slots and legacy_b64:
            slots = [{
                "kind": "auto",
                "b64": legacy_b64,
                "sha256": str(row.get("user_config_sha256") or ""),
                "size": max(0, int(row.get("user_config_size") or 0)),
                "saved_at": str(row.get("updated_at") or ""),
                "device_id": str(row.get("source_device_id") or ""),
            }]
        return {
            "found": True,
            "preferences": preferences,
            "servers": list(row.get("servers") or []),
            "matchmaking": dict(row.get("matchmaking") or {}),
            "user_config_b64": str(row.get("user_config_b64") or ""),
            "user_config_sha256": str(row.get("user_config_sha256") or ""),
            "user_config_size": max(0, int(row.get("user_config_size") or 0)),
            "user_config_slots": slots[:3],
            "theme_id": str(row.get("theme_id") or ""),
            "state_version": int(row.get("state_version") or 1),
            "source_device_id": str(row.get("source_device_id") or ""),
            "updated_at": str(row.get("updated_at") or ""),
            **self.account_snapshot(),
        }

    def account_overview(self) -> dict[str, Any]:
        session = self._ensure_session()
        # Touch this device whenever the account page is opened/refreshed.
        self.register_device()
        self.profile = self.fetch_profile()
        overview = {**self.account_snapshot()}
        overview.update(self._cloud_session_stats())
        overview["devices"] = self.list_devices()
        try:
            cloud = self.load_cloud_state()
            overview["cloud_state"] = ({
                "preferences": cloud.get("preferences", {}),
                "servers": cloud.get("servers", []),
                "matchmaking": cloud.get("matchmaking", {}),
                "user_config_size": cloud.get("user_config_size", 0),
                "user_config_slots": cloud.get("user_config_slots", []),
                "user_config_sha256": cloud.get("user_config_sha256", ""),
                "theme_id": cloud.get("theme_id", ""),
                "state_version": cloud.get("state_version", 1),
                "source_device_id": cloud.get("source_device_id", ""),
                "updated_at": cloud.get("updated_at", ""),
            } if cloud.get("found") else {})
        except SupabaseError as error:
            # Account/profile/playtime remain usable if the V23.4 cloud_state
            # migration has not been applied yet. Backup/restore will surface the
            # database error when the user explicitly tries those actions.
            overview["cloud_state"] = {}
            overview["cloud_state_error"] = str(error)
        return overview

    # ------------------------------------------------------------------
    # Local playtime -> cloud
    # ------------------------------------------------------------------
    def pending_playtime_count(self) -> int:
        try:
            if not self.playtime_db.is_file():
                return 0
            with sqlite3.connect(str(self.playtime_db), timeout=10) as db:
                row = db.execute(
                    "SELECT COUNT(*) FROM game_sessions WHERE ended_at IS NOT NULL AND uploaded = 0"
                ).fetchone()
            return max(0, int((row or [0])[0] or 0))
        except Exception:
            return 0

    def _pending_playtime_rows(self, limit: int = 100) -> list[dict[str, Any]]:
        if not self.playtime_db.is_file():
            return []
        with sqlite3.connect(str(self.playtime_db), timeout=10) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                """
                SELECT id, started_at, ended_at, duration_seconds,
                       engine_process, tracker_token
                  FROM game_sessions
                 WHERE ended_at IS NOT NULL AND uploaded = 0
                 ORDER BY id ASC
                 LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def sync_playtime(self) -> dict[str, Any]:
        session = self._ensure_session()
        user_id = self._user_id(session)
        if not user_id:
            raise AccountError("Authenticated account has no user id.")

        # Refresh last_seen even when there are no new sessions.
        self.register_device()
        rows = self._pending_playtime_rows(100)
        if not rows:
            return {"uploaded": 0, "pending": 0, **self.account_snapshot()}

        remote_rows = []
        local_ids = []
        for row in rows:
            local_id = int(row.get("id") or 0)
            token = str(row.get("tracker_token") or "").strip()
            client_session_id = token or f"{self.device_id}:{local_id}"
            remote_rows.append({
                "user_id": user_id,
                "device_id": self.device_id,
                "client_session_id": client_session_id,
                "started_at": row.get("started_at"),
                "ended_at": row.get("ended_at"),
                "duration_seconds": max(0, int(row.get("duration_seconds") or 0)),
                "engine_process": str(row.get("engine_process") or ""),
            })
            local_ids.append(local_id)

        self._get_client().upsert(
            "game_sessions",
            remote_rows,
            access_token=session.access_token,
            on_conflict="user_id,client_session_id",
        )

        # Mark local rows only after the remote transaction returned success.
        with sqlite3.connect(str(self.playtime_db), timeout=10) as db:
            db.executemany(
                "UPDATE game_sessions SET uploaded = 1 WHERE id = ?",
                [(row_id,) for row_id in local_ids],
            )
            db.commit()

        return {
            "uploaded": len(local_ids),
            "pending": self.pending_playtime_count(),
            **self.account_snapshot(),
        }

    # ------------------------------------------------------------------
    # UI snapshot
    # ------------------------------------------------------------------
    def account_snapshot(self) -> dict[str, Any]:
        session = self.session
        user = dict(session.user or {}) if session is not None else {}

        app_metadata = user.get("app_metadata") or user.get("raw_app_meta_data") or {}
        app_metadata = app_metadata if isinstance(app_metadata, dict) else {}
        provider = str(app_metadata.get("provider") or "").strip().lower()
        providers = app_metadata.get("providers") or []
        identities = user.get("identities") or []
        identity_summaries = []
        identity_providers = set()
        if isinstance(identities, list):
            for identity in identities:
                if isinstance(identity, dict):
                    p = str(identity.get("provider") or "").strip().lower()
                    if p:
                        identity_providers.add(p)
                    if p and p not in providers:
                        providers = list(providers) + [p]
                    identity_id = str(identity.get("identity_id") or identity.get("id") or "").strip()
                    if p:
                        identity_summaries.append({"id": identity_id, "provider": p})
        # Supabase's live identity list is authoritative after a link/unlink;
        # app_metadata can otherwise retain a stale provider for a short time.
        if identity_providers:
            providers = sorted(identity_providers)
        if not provider and isinstance(providers, list) and len(providers) == 1:
            provider = str(providers[0] or "").strip().lower()
        if not provider and isinstance(identities, list) and identities:
            identity = identities[-1] if isinstance(identities[-1], dict) else {}
            provider = str(identity.get("provider") or "").strip().lower()

        metadata = user.get("user_metadata") or user.get("raw_user_meta_data") or {}
        metadata = metadata if isinstance(metadata, dict) else {}
        avatar = str(self.profile.get("avatar_url") or metadata.get("avatar_url") or metadata.get("picture") or "")
        email_verified = bool(
            user.get("email_confirmed_at")
            or user.get("confirmed_at")
            or metadata.get("email_verified")
        )

        return {
            "authenticated": session is not None,
            "user_id": self._user_id(session),
            "email": str(user.get("email") or ""),
            "email_verified": email_verified,
            "pending_confirmation_email": self.pending_confirmation_email,
            "nickname": str(self.profile.get("nickname") or metadata.get("nickname") or metadata.get("user_name") or ""),
            "avatar_url": avatar,
            "created_at": str(self.profile.get("created_at") or user.get("created_at") or ""),
            "updated_at": str(self.profile.get("updated_at") or user.get("updated_at") or ""),
            "provider": provider,
            "providers": list(dict.fromkeys(providers)) if isinstance(providers, list) else [],
            "identities": identity_summaries,
            "device_id": self.device_id,
            "pending": self.pending_playtime_count(),
        }
