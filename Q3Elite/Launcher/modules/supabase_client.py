from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class SupabaseError(RuntimeError):
    pass


@dataclass
class AuthSession:
    access_token: str
    refresh_token: str
    expires_in: int
    expires_at: int
    user: dict[str, Any]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "AuthSession":
        expires_in = int(payload.get("expires_in") or 0)
        expires_at = int(payload.get("expires_at") or 0)
        if not expires_at and expires_in:
            expires_at = int(time.time()) + expires_in
        return cls(
            access_token=str(payload.get("access_token") or ""),
            refresh_token=str(payload.get("refresh_token") or ""),
            expires_in=expires_in,
            expires_at=expires_at,
            user=dict(payload.get("user") or {}),
        )


class SupabaseClient:
    """Tiny stdlib-only Supabase REST/Auth client for Q3Elite.

    Distributed builds may contain only a Supabase publishable key
    (sb_publishable_...). Never embed a secret/service-role key.
    """

    def __init__(self, url: str, publishable_key: str, *, timeout: float = 12.0):
        self.url = str(url or "").strip().rstrip("/")
        self.publishable_key = str(publishable_key or "").strip()
        self.timeout = max(2.0, float(timeout))
        if not self.url.startswith("https://"):
            raise ValueError("Supabase URL must use HTTPS.")
        if not self.publishable_key:
            raise ValueError("Supabase publishable key is missing.")

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Any | None = None,
        access_token: str = "",
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        headers = {
            "apikey": self.publishable_key,
            "Accept": "application/json",
        }
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)

        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        request = urllib.request.Request(
            self.url + path,
            data=data,
            headers=headers,
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                raw = error.read().decode("utf-8", errors="replace")
                parsed = json.loads(raw) if raw else {}
                detail = (
                    parsed.get("msg")
                    or parsed.get("message")
                    or parsed.get("error_description")
                    or parsed.get("error")
                    or raw
                ) if isinstance(parsed, dict) else raw
            except Exception:
                detail = str(error)
            raise SupabaseError(str(detail or f"HTTP {error.code}")) from error
        except urllib.error.URLError as error:
            raise SupabaseError(f"Network error: {error.reason}") from error


    def _binary_request(
        self,
        path: str,
        data: bytes,
        *,
        method: str = "POST",
        access_token: str = "",
        content_type: str = "application/octet-stream",
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        headers = {
            "apikey": self.publishable_key,
            "Accept": "application/json",
            "Content-Type": str(content_type or "application/octet-stream"),
        }
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        if extra_headers:
            headers.update(extra_headers)

        request = urllib.request.Request(
            self.url + path,
            data=bytes(data),
            headers=headers,
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                if not raw:
                    return None
                try:
                    return json.loads(raw.decode("utf-8"))
                except Exception:
                    return raw
        except urllib.error.HTTPError as error:
            try:
                raw = error.read().decode("utf-8", errors="replace")
                parsed = json.loads(raw) if raw else {}
                detail = (
                    parsed.get("msg")
                    or parsed.get("message")
                    or parsed.get("error_description")
                    or parsed.get("error")
                    or raw
                ) if isinstance(parsed, dict) else raw
            except Exception:
                detail = str(error)
            raise SupabaseError(str(detail or f"HTTP {error.code}")) from error
        except urllib.error.URLError as error:
            raise SupabaseError(f"Network error: {error.reason}") from error

    def resend_signup_confirmation(self, email: str) -> None:
        self._request(
            "/auth/v1/resend",
            method="POST",
            payload={"type": "signup", "email": str(email).strip()},
        )

    def upload_storage_object(
        self,
        bucket: str,
        object_path: str,
        data: bytes,
        *,
        access_token: str,
        content_type: str = "application/octet-stream",
    ) -> Any:
        bucket_q = urllib.parse.quote(str(bucket), safe="")
        path_q = "/".join(urllib.parse.quote(part, safe="") for part in str(object_path).split("/") if part)
        return self._binary_request(
            f"/storage/v1/object/{bucket_q}/{path_q}",
            bytes(data),
            method="POST",
            access_token=access_token,
            content_type=content_type,
            extra_headers={"cache-control": "max-age=3600"},
        )

    def list_storage_objects(
        self,
        bucket: str,
        *,
        prefix: str = "",
        access_token: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        bucket_q = urllib.parse.quote(str(bucket), safe="")
        result = self._request(
            f"/storage/v1/object/list/{bucket_q}",
            method="POST",
            payload={
                "prefix": str(prefix or "").strip("/"),
                "limit": max(1, min(1000, int(limit))),
                "offset": max(0, int(offset)),
                "sortBy": {"column": "created_at", "order": "desc"},
            },
            access_token=access_token,
        )
        return list(result or [])

    def delete_storage_object(
        self,
        bucket: str,
        object_path: str,
        *,
        access_token: str,
    ) -> Any:
        bucket_q = urllib.parse.quote(str(bucket), safe="")
        path_q = "/".join(
            urllib.parse.quote(part, safe="")
            for part in str(object_path).split("/")
            if part
        )
        return self._request(
            f"/storage/v1/object/{bucket_q}/{path_q}",
            method="DELETE",
            access_token=access_token,
        )

    def public_storage_url(self, bucket: str, object_path: str) -> str:
        bucket_q = urllib.parse.quote(str(bucket), safe="")
        path_q = "/".join(urllib.parse.quote(part, safe="") for part in str(object_path).split("/") if part)
        return f"{self.url}/storage/v1/object/public/{bucket_q}/{path_q}"

    def auth_settings(self) -> dict[str, Any]:
        result = self._request("/auth/v1/settings")
        return dict(result or {})

    def oauth_authorize_url(
        self,
        provider: str,
        *,
        redirect_to: str,
        code_challenge: str,
        scopes: str = "",
    ) -> str:
        provider = str(provider or "").strip().lower()
        # Built-in providers are simple names (google, discord, ...), while
        # Supabase custom OAuth/OIDC providers use identifiers such as
        # "custom:telegram". Keep the validation strict while allowing the
        # colon/hyphen characters documented for custom provider identifiers.
        allowed = set("abcdefghijklmnopqrstuvwxyz0123456789_:-")
        if not provider or any(ch not in allowed for ch in provider):
            raise ValueError("Invalid OAuth provider name.")
        params = {
            "provider": provider,
            "redirect_to": str(redirect_to),
            "code_challenge": str(code_challenge),
            "code_challenge_method": "s256",
        }
        if scopes:
            params["scopes"] = str(scopes)
        return self.url + "/auth/v1/authorize?" + urllib.parse.urlencode(params)

    def oauth_link_authorize_url(
        self,
        provider: str,
        *,
        access_token: str,
        redirect_to: str,
        code_challenge: str,
        scopes: str = "",
    ) -> str:
        """Create a Supabase manual identity-linking OAuth URL.

        This uses the authenticated /user/identities/authorize endpoint rather
        than the ordinary sign-in authorize endpoint, so the provider identity
        is attached to the current user instead of creating a second account.
        """
        provider = str(provider or "").strip().lower()
        allowed = set("abcdefghijklmnopqrstuvwxyz0123456789_:-")
        if not provider or any(ch not in allowed for ch in provider):
            raise ValueError("Invalid OAuth provider name.")
        params = {
            "provider": provider,
            "redirect_to": str(redirect_to),
            "code_challenge": str(code_challenge),
            "code_challenge_method": "s256",
            "skip_http_redirect": "true",
        }
        if scopes:
            params["scopes"] = str(scopes)
        result = self._request(
            "/auth/v1/user/identities/authorize?" + urllib.parse.urlencode(params),
            access_token=str(access_token),
        )
        if isinstance(result, dict):
            url = str(result.get("url") or result.get("redirect_url") or "").strip()
            if url:
                return url
        raise SupabaseError("Supabase did not return an identity-link authorization URL.")

    def exchange_pkce_code(self, auth_code: str, code_verifier: str) -> AuthSession:
        payload = self._request(
            "/auth/v1/token?grant_type=pkce",
            method="POST",
            payload={
                "auth_code": str(auth_code),
                "code_verifier": str(code_verifier),
            },
        )
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise SupabaseError("OAuth code exchange completed without an access token.")
        return AuthSession.from_payload(payload)

    def sign_up(self, email: str, password: str, *, nickname: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {
            "email": str(email).strip(),
            "password": str(password),
        }
        if nickname:
            body["data"] = {"nickname": str(nickname).strip()}
        result = self._request("/auth/v1/signup", method="POST", payload=body)
        return dict(result or {})

    def sign_in(self, email: str, password: str) -> AuthSession:
        payload = self._request(
            "/auth/v1/token?grant_type=password",
            method="POST",
            payload={"email": str(email).strip(), "password": str(password)},
        )
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise SupabaseError("Authentication succeeded without an access token.")
        return AuthSession.from_payload(payload)

    def refresh_session(self, refresh_token: str) -> AuthSession:
        payload = self._request(
            "/auth/v1/token?grant_type=refresh_token",
            method="POST",
            payload={"refresh_token": str(refresh_token)},
        )
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise SupabaseError("Could not refresh the authentication session.")
        return AuthSession.from_payload(payload)

    def sign_out(self, access_token: str) -> None:
        self._request(
            "/auth/v1/logout",
            method="POST",
            access_token=str(access_token),
        )

    def get_user(self, access_token: str) -> dict[str, Any]:
        result = self._request("/auth/v1/user", access_token=str(access_token))
        return dict(result or {})

    def unlink_identity(self, identity_id: str, *, access_token: str) -> Any:
        identity_id = str(identity_id or "").strip()
        if not identity_id:
            raise SupabaseError("Supabase identity is missing its identity ID.")
        encoded_id = urllib.parse.quote(identity_id, safe="")
        return self._request(
            f"/auth/v1/user/identities/{encoded_id}",
            method="DELETE",
            access_token=str(access_token),
        )

    def select(
        self,
        table: str,
        *,
        access_token: str = "",
        query: str = "select=*",
    ) -> list[dict[str, Any]]:
        path = f"/rest/v1/{urllib.parse.quote(str(table), safe='')}?{query}"
        result = self._request(path, access_token=access_token)
        return list(result or [])

    def insert(
        self,
        table: str,
        rows: dict[str, Any] | list[dict[str, Any]],
        *,
        access_token: str,
        return_rows: bool = False,
    ) -> Any:
        headers = {"Prefer": "return=representation" if return_rows else "return=minimal"}
        return self._request(
            f"/rest/v1/{urllib.parse.quote(str(table), safe='')}",
            method="POST",
            payload=rows,
            access_token=access_token,
            extra_headers=headers,
        )

    def upsert(
        self,
        table: str,
        rows: dict[str, Any] | list[dict[str, Any]],
        *,
        access_token: str,
        on_conflict: str = "",
        return_rows: bool = False,
    ) -> Any:
        suffix = ""
        if on_conflict:
            suffix = "?on_conflict=" + urllib.parse.quote(on_conflict, safe=",")
        prefer = "resolution=merge-duplicates"
        prefer += ",return=representation" if return_rows else ",return=minimal"
        return self._request(
            f"/rest/v1/{urllib.parse.quote(str(table), safe='')}{suffix}",
            method="POST",
            payload=rows,
            access_token=access_token,
            extra_headers={"Prefer": prefer},
        )
    def update(
        self,
        table: str,
        values: dict[str, Any],
        *,
        access_token: str,
        query: str,
        return_rows: bool = False,
    ) -> Any:
        suffix = "?" + str(query).lstrip("?") if query else ""
        headers = {"Prefer": "return=representation" if return_rows else "return=minimal"}
        return self._request(
            f"/rest/v1/{urllib.parse.quote(str(table), safe='')}{suffix}",
            method="PATCH",
            payload=values,
            access_token=access_token,
            extra_headers=headers,
        )

    def delete(
        self,
        table: str,
        *,
        access_token: str,
        query: str,
        return_rows: bool = False,
    ) -> Any:
        suffix = "?" + str(query).lstrip("?") if query else ""
        headers = {"Prefer": "return=representation" if return_rows else "return=minimal"}
        return self._request(
            f"/rest/v1/{urllib.parse.quote(str(table), safe='')}{suffix}",
            method="DELETE",
            access_token=access_token,
            extra_headers=headers,
        )
