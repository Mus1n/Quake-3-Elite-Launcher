from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class SupportFeedError(RuntimeError):
    pass


class SupportManager:
    """Public Q3Elite support/donations feed.

    Only the Supabase publishable/anon key is used by the launcher. Provider
    DonationAlerts credentials stay server-side and write sanitized public
    rows into support_donations. Payment-method links such as PayPal are display
    only and do not require provider API credentials in the launcher.
    """

    def __init__(self, launcher_dir: Path, data_dir: Path):
        self.launcher_dir = Path(launcher_dir)
        self.data_dir = Path(data_dir)
        self.backend_file = self.launcher_dir / "settings" / "backend.json"
        self.support_file = self.launcher_dir / "settings" / "support.json"
        self.cache_file = self.data_dir / "support_cache.json"
        self._backend = self._load_json(self.backend_file)
        self._support = self._load_json(self.support_file)

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        try:
            if path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return dict(raw)
        except Exception as error:
            print(f"[support] Could not read {path.name}: {error}")
        return {}

    @property
    def configured(self) -> bool:
        url = str(self._backend.get("url") or "").strip()
        key = str(self._backend.get("publishable_key") or self._backend.get("anon_key") or "").strip()
        return url.startswith("https://") and bool(key) and bool(self._backend.get("enabled", True))

    @property
    def methods(self) -> dict[str, Any]:
        raw = self._support.get("methods")
        return dict(raw) if isinstance(raw, dict) else {}

    @property
    def fallback_motd(self) -> str:
        return str(self._support.get("fallback_motd") or "Frag now, debug later. 🙃").strip()

    def _rest_get(self, table: str, query: str) -> Any:
        if not self.configured:
            raise SupportFeedError("Cloud backend is not configured.")
        base = str(self._backend.get("url") or "").rstrip("/")
        key = str(self._backend.get("publishable_key") or self._backend.get("anon_key") or "").strip()
        url = f"{base}/rest/v1/{urllib.parse.quote(table, safe='')}?{query}"
        req = urllib.request.Request(
            url,
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
                "User-Agent": "Q3Elite-Launcher/Support",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=float(self._backend.get("timeout_seconds") or 10)) as response:
                return json.loads(response.read().decode("utf-8", errors="replace") or "null")
        except urllib.error.HTTPError as error:
            try:
                detail = error.read().decode("utf-8", errors="replace")
            except Exception:
                detail = ""
            raise SupportFeedError(f"Support feed HTTP {error.code}: {detail[:240] or error.reason}") from error
        except Exception as error:
            raise SupportFeedError(str(error)) from error

    def load_cache(self) -> dict[str, Any]:
        try:
            if self.cache_file.is_file():
                raw = json.loads(self.cache_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return raw
        except Exception as error:
            print(f"[support] Could not read cache: {error}")
        return {"motd": self.fallback_motd, "donations": []}

    def _save_cache(self, payload: dict[str, Any]) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.cache_file)
        except Exception as error:
            print(f"[support] Could not save cache: {error}")

    def fetch(self) -> dict[str, Any]:
        motd = self.fallback_motd
        donations: list[dict[str, Any]] = []

        content = self._rest_get(
            "support_content",
            urllib.parse.urlencode({"select": "value,updated_at", "key": "eq.motd", "limit": "1"}),
        )
        if isinstance(content, list) and content:
            motd = str(content[0].get("value") or motd).strip() or motd

        rows = self._rest_get(
            "support_donations",
            urllib.parse.urlencode(
                {
                    "select": "provider,nickname,amount,currency,message,donated_at",
                    "visible": "eq.true",
                    "order": "donated_at.desc",
                    "limit": "4",
                }
            ),
        )
        if isinstance(rows, list):
            for row in rows[:4]:
                if not isinstance(row, dict):
                    continue
                donations.append(
                    {
                        "provider": str(row.get("provider") or "Support"),
                        "nickname": str(row.get("nickname") or "Anonymous"),
                        "amount": row.get("amount"),
                        "currency": str(row.get("currency") or ""),
                        "message": str(row.get("message") or ""),
                        "donated_at": str(row.get("donated_at") or ""),
                    }
                )

        payload = {"motd": motd, "donations": donations}
        self._save_cache(payload)
        return payload
