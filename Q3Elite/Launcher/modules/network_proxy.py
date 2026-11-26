"""Scoped HTTP/HTTPS proxy support for Q3Elite update/download traffic.

The proxy is opt-in and never changes urllib's process-wide default opener.
Credentials are protected with Windows DPAPI before being written to disk.
"""
from __future__ import annotations
import base64
import ctypes
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

import ssl_config

APPDATA = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
CONFIG_PATH = APPDATA / "Quake 3 Elite" / "Launcher" / "network_proxy.json"


def _dpapi(data: bytes, protect: bool) -> bytes:
    if os.name != "nt":
        raise OSError("Windows DPAPI is unavailable")
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]
    source_buf = ctypes.create_string_buffer(data)
    source = DATA_BLOB(len(data), ctypes.cast(source_buf, ctypes.POINTER(ctypes.c_ubyte)))
    output = DATA_BLOB()
    crypt = ctypes.windll.crypt32
    kernel = ctypes.windll.kernel32
    if protect:
        ok = crypt.CryptProtectData(ctypes.byref(source), "Q3Elite Proxy", None, None, None, 0, ctypes.byref(output))
    else:
        ok = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel.LocalFree(output.pbData)


def _encrypt_password(password: str) -> str:
    if not password:
        return ""
    return base64.b64encode(_dpapi(password.encode("utf-8"), True)).decode("ascii")


def _decrypt_password(value: str) -> str:
    if not value:
        return ""
    try:
        return _dpapi(base64.b64decode(value), False).decode("utf-8")
    except Exception:
        return ""


def load_settings() -> dict:
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {"enabled": False, "url": "", "username": "", "password": ""}
        return {
            "enabled": bool(raw.get("enabled", False)),
            "url": str(raw.get("url") or "").strip(),
            "username": str(raw.get("username") or ""),
            "password": _decrypt_password(str(raw.get("password_dpapi") or "")),
        }
    except Exception:
        return {"enabled": False, "url": "", "username": "", "password": ""}


def save_settings(enabled: bool, url: str, username: str = "", password: str = "") -> None:
    url = str(url or "").strip()
    username = str(username or "")
    password = str(password or "")
    if enabled:
        if password and not username:
            raise ValueError("A proxy password requires a username.")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname or parsed.port is None:
            raise ValueError("Proxy URL must use http:// or https:// and include a host and port.")
        if parsed.username or parsed.password:
            raise ValueError("Enter proxy credentials in the separate username/password fields.")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("Proxy URL must contain only scheme, host and port.")
        if password and os.name != "nt":
            raise OSError("Secure proxy password storage requires Windows DPAPI.")
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "enabled": bool(enabled), "url": url, "username": username,
        "password_dpapi": _encrypt_password(password),
    }
    temp = CONFIG_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temp, CONFIG_PATH)


def proxy_url() -> str | None:
    settings = load_settings()
    if not settings["enabled"] or not settings["url"]:
        return None
    base = settings["url"]
    if settings["username"]:
        parsed = urllib.parse.urlsplit(base)
        user = urllib.parse.quote(settings["username"], safe="")
        password = urllib.parse.quote(settings["password"], safe="")
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = f"{user}:{password}@{host}:{parsed.port}"
        base = urllib.parse.urlunsplit((parsed.scheme, netloc, "", "", ""))
    return base


def build_opener():
    """Build a proxy-aware opener using the launcher's shared strict TLS context."""
    return ssl_config.build_opener(proxy=proxy_url())


def open_url(request, timeout=30):
    return ssl_config.open_url(request, timeout=timeout, proxy=proxy_url())


def test_connection(url: str, username: str = "", password: str = "", timeout: int = 15) -> tuple[bool, str]:
    """Test the configured proxy against pCloud's API, not a generic website.

    getapiserver is a lightweight unauthenticated pCloud API endpoint. A valid
    JSON response with result == 0 confirms that HTTPS CONNECT through this
    proxy reached pCloud and that pCloud accepted the request.
    """
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname or parsed.port is None:
        return False, "Invalid proxy URL. Use http://host:port or https://host:port."
    if parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return False, "Proxy URL must contain only scheme, host and port; credentials go in separate fields."
    if password and not username:
        return False, "A proxy password requires a username."

    proxy = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    if username:
        user = urllib.parse.quote(username, safe="")
        pwd = urllib.parse.quote(password or "", safe="")
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        proxy = f"{parsed.scheme}://{user}:{pwd}@{host}:{parsed.port}"

    opener = ssl_config.build_opener(proxy=proxy)
    request = urllib.request.Request(
        "https://eapi.pcloud.com/getapiserver",
        headers={
            "User-Agent": "Q3Elite-Launcher/1.0",
            "Accept": "application/json",
        },
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.getcode()
            body = response.read(64 * 1024)

        if status != 200:
            return False, f"pCloud API returned HTTP {status}."

        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return False, f"Proxy reached pCloud, but its API response was invalid: {exc}"

        if not isinstance(data, dict):
            return False, "Proxy reached pCloud, but the API returned an unexpected response."

        result = data.get("result")
        if result == 0:
            api_host = str(data.get("api") or "pCloud API").strip()
            return True, f"pCloud connection successful (HTTP 200, API: {api_host})."

        detail = data.get("error") or data.get("message") or f"result={result}"
        return False, f"Proxy reached pCloud, but pCloud returned an API error: {detail}."

    except Exception as exc:
        return False, f"pCloud connection through proxy failed: {exc}"
