"""OSP2-BE updater using the public website changelog and GitHub Releases."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
import zipfile
from pathlib import Path

import download_tools as dt
from base_methods import BASEQ3_DIR

OSP_ASSET_NAME = "zz-osp-pak8be.pk3"
OSP_FILE = BASEQ3_DIR / "mods" / "osp" / OSP_ASSET_NAME
OSP_CHANGELOG_URLS = (
    "https://osp2.q3a.space/assets/version_changelog.txt",
    "https://osp2.q3a.space/en/assets/version_changelog.txt",
)
OSP_RELEASE_HISTORY_URLS = (
    "https://osp2.q3a.space/assets/changelog_be.txt",
    "https://osp2.q3a.space/en/assets/changelog_be.txt",
)
OSP_DOWNLOAD_URL = (
    "https://github.com/scoqx/osp2-be/releases/latest/download/"
    "zz-osp-pak8be.pk3"
)
OSP_CACHE_FILE = (
    Path(os.environ.get("APPDATA", Path.home()))
    / "Quake 3 Elite" / "Launcher" / "osp2be_changelog_cache.json"
)


def _read_text(url: str, timeout: int = 12) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Q3Elite-Launcher", "Cache-Control": "no-cache"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8-sig", errors="replace").strip()


def _read_cached_changelog() -> tuple[str, str] | None:
    try:
        data = json.loads(OSP_CACHE_FILE.read_text(encoding="utf-8"))
        text = str(data.get("text") or "").strip()
        version = str(data.get("version") or _parse_version(text) or "").strip()
        if text and version:
            return text, version
    except (OSError, ValueError, TypeError):
        pass
    return None


def _save_cached_changelog(text: str, version: str) -> None:
    try:
        existing = {}
        try:
            existing = json.loads(OSP_CACHE_FILE.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except (OSError, ValueError, TypeError):
            pass
        payload = dict(existing)
        payload.update({
            "text": str(text or "").strip(),
            "version": str(version or "").strip(),
            "cached_at": int(time.time()),
        })
        OSP_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = OSP_CACHE_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(OSP_CACHE_FILE)
    except OSError as error:
        print(f"[osp2be] Could not cache website changelog: {error}")


def _read_remote_changelog() -> tuple[str, str]:
    errors = []
    for url in OSP_CHANGELOG_URLS:
        try:
            text = _read_text(url)
            version = _parse_version(text)
            if version:
                _save_cached_changelog(text, version)
                return text, version
            errors.append(f"{url}: no version found")
        except Exception as error:
            errors.append(f"{url}: {error}")
    cached = _read_cached_changelog()
    if cached:
        print("[osp2be] Website unavailable; using cached changelog.")
        return cached
    raise RuntimeError("Could not read the OSP2-BE website changelog: " + " | ".join(errors))


def _parse_version(text: str) -> str | None:
    """Extract the release version from entries such as '... be v1.10c-beta5'."""
    value = str(text or "").replace("\ufeff", "")
    match = re.search(
        r"\bv\s*([0-9]+(?:\.[0-9]+)+(?:[a-z])?(?:[-._][a-z0-9]+)*)",
        value,
        re.IGNORECASE,
    )
    if not match:
        match = re.search(
            r"\bbe[-_ ]?([0-9]+(?:\.[0-9]+)+(?:[a-z])?(?:[-._][a-z0-9]+)*)",
            value,
            re.IGNORECASE,
        )
    return match.group(1).strip() if match else None


_RELEASE_HEADER_RE = re.compile(
    r"^\s*(\d{1,2}\.\d{1,2}\.\d{4})\s+be\s+v?([0-9][A-Za-z0-9._-]*)\s*$",
    re.IGNORECASE,
)


def _parse_release_history(text: str) -> list[dict]:
    """Parse changelog_be.txt, whose release posts are ordered oldest-first."""
    releases = []
    current = None

    def finish():
        if current is None:
            return
        notes = current["notes"]
        # Preserve headings (UI:, SHUD:, MISC:) and nested descriptions, but
        # remove the site's visual bullet markers from the displayed list.
        clean = []
        for raw in notes:
            line = raw.strip()
            if not line:
                continue
            line = re.sub(r"^[+*•-]\s*", "", line).strip()
            if line and (not clean or clean[-1] != line):
                clean.append(line)
        releases.append({
            "type": "OSP2-BE",
            "version": current["version"],
            "date": current["date"],
            "changelog": clean,
            "text": "",
            "link": "https://osp2.q3a.space/en/index.html",
            "list": True,
        })

    for raw in str(text or "").replace("\ufeff", "").splitlines():
        if raw.strip() in {"```", "~~~"}:
            continue
        match = _RELEASE_HEADER_RE.match(raw)
        if match:
            finish()
            try:
                date_value = time.strftime("%Y-%m-%d", time.strptime(match.group(1), "%d.%m.%Y"))
            except ValueError:
                date_value = ""
            current = {"date": date_value, "version": match.group(2), "notes": []}
            continue
        if current is not None:
            # Ignore non-release notes before the first header; all content
            # between two release headers belongs to the preceding post.
            current["notes"].append(raw)

    finish()

    # Launcher changelog baseline: include beta2 and newer only.
    # Older development history is intentionally excluded from this feed.
    baseline_date = "2026-11-06"
    releases = [
        release for release in releases
        if str(release.get("date") or "") >= baseline_date
    ]

    # Some historical versions were posted more than once (e.g. v0.032).
    # They are separate dated posts, so only collapse exact duplicate blocks.
    unique = {}
    for release in releases:
        key = (release["version"].casefold(), release["date"], tuple(release["changelog"]))
        unique[key] = release

    def sort_key(item):
        date = str(item.get("date") or "")
        try:
            date_num = int(date.replace("-", ""))
        except ValueError:
            date_num = 0
        return date_num

    return sorted(unique.values(), key=sort_key, reverse=True)


def _read_cached_release_history() -> list[dict]:
    try:
        data = json.loads(OSP_CACHE_FILE.read_text(encoding="utf-8"))
        releases = data.get("releases", [])
        if not isinstance(releases, list):
            return []
        baseline_date = "2026-11-06"
        return [
            item for item in releases
            if isinstance(item, dict) and str(item.get("date") or "") >= baseline_date
        ]
    except (OSError, ValueError, TypeError):
        return []


def _read_remote_release_history() -> list[dict]:
    errors = []
    for url in OSP_RELEASE_HISTORY_URLS:
        try:
            text = _read_text(url, timeout=18)
            releases = _parse_release_history(text)
            if releases:
                _save_cached_changelog_history(releases)
                print(f"[osp2be] Parsed {len(releases)} release post(s) from changelog_be.txt.")
                return releases
            errors.append(f"{url}: no release headers found")
        except Exception as error:
            errors.append(f"{url}: {error}")
    cached = _read_cached_release_history()
    if cached:
        print(f"[osp2be] History endpoint unavailable; using {len(cached)} cached release post(s).")
        return cached
    print("[osp2be] Full release history unavailable: " + " | ".join(errors))
    return []


def _save_cached_changelog_history(releases: list[dict]) -> None:
    try:
        data = {}
        try:
            data = json.loads(OSP_CACHE_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError, TypeError):
            pass
        data["releases"] = list(releases or [])
        data["history_cached_at"] = int(time.time())
        OSP_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = OSP_CACHE_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(OSP_CACHE_FILE)
    except OSError as error:
        print(f"[osp2be] Could not cache full release history: {error}")




def _changelog_metadata(text: str) -> dict:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    first = lines[0] if lines else ""
    date_match = re.search(r"\b(\d{1,2}\.\d{1,2}\.\d{4})\b", first)
    return {
        "changelog_text": str(text or "").strip(),
        "changelog_date": date_match.group(1) if date_match else "",
        "changelog_summary": first,
        "changelog_url": "https://osp2.q3a.space/en/index.html",
    }

def _version_key(value: str) -> tuple:
    tokens = re.findall(r"\d+|[a-z]+", str(value or "").casefold())
    return tuple((0, int(token)) if token.isdigit() else (1, token) for token in tokens)


def _read_pk3_version(pk3_path: Path) -> str | None:
    if not pk3_path.is_file():
        return None
    try:
        with zipfile.ZipFile(pk3_path, "r") as archive:
            candidates = [
                name for name in archive.namelist()
                if Path(name.replace("\\", "/")).name.casefold() == "version.txt"
            ]
            if not candidates:
                return None
            # Prefer a top-level version.txt if present; otherwise use the first
            # matching entry in the PK3. The name matching is case-insensitive.
            candidates.sort(key=lambda name: ("/" in name.replace("\\", "/"), len(name)))
            raw = archive.read(candidates[0]).decode("utf-8-sig", errors="replace").strip()
            return _parse_version(raw) or raw.removeprefix("be-").strip() or None
    except (OSError, zipfile.BadZipFile, KeyError):
        return None


def _changelog_lines(text: str) -> list[str]:
    lines = []
    for raw in str(text or "").splitlines()[1:]:
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^[+\-•*]\s*", "", line)
        if line:
            lines.append(line)
        if len(lines) >= 8:
            break
    return lines


def check_and_update(
    *,
    force: bool = False,
    auto_install: bool = True,
    control=None,
    progress_callback=None,
    status_callback=None,
) -> dict:
    """Check the version embedded in the installed PK3 against the site.

    When auto_install=False, return update_available without downloading.
    force=True downloads regardless of the automatic-update preference.
    """
    changelog_text, remote_version = _read_remote_changelog()
    local_version = _read_pk3_version(OSP_FILE)
    exists = OSP_FILE.is_file()
    notes = _changelog_lines(changelog_text)
    changelog_meta = _changelog_metadata(changelog_text)
    changelog_releases = _read_remote_release_history()

    needs_update = (not exists) or (not local_version) or (
        _version_key(local_version) != _version_key(remote_version)
    )
    if not needs_update:
        return {
            "action": "current", "status": "current", "update_available": False,
            "local_version": local_version, "remote_version": remote_version,
            "changelog": notes, "changelog_releases": changelog_releases,
            "file": str(OSP_FILE), **changelog_meta,
        }

    if not auto_install and not force:
        return {
            "action": "available", "status": "update_available", "update_available": True,
            "local_version": local_version or "missing", "remote_version": remote_version,
            "changelog": notes, "changelog_releases": changelog_releases,
            "file": str(OSP_FILE), **changelog_meta,
        }

    OSP_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_name = OSP_ASSET_NAME + ".update"
    temp_file = OSP_FILE.parent / temp_name
    part_file = Path(str(temp_file) + ".part")
    for stale in (temp_file, part_file):
        try:
            stale.unlink(missing_ok=True)
        except OSError as error:
            raise RuntimeError(f"Could not clear stale OSP2-BE download: {stale}: {error}") from error

    if status_callback:
        try:
            status_callback("download_start", file_name=OSP_ASSET_NAME, url=OSP_DOWNLOAD_URL, host="github.com")
        except Exception:
            pass
    downloaded = dt.downloader(
        OSP_DOWNLOAD_URL,
        str(OSP_FILE.parent),
        temp_name,
        skip=False,
        control=control,
        progress_callback=progress_callback,
        use_part_file=True,
        status_callback=status_callback,
    )
    if not downloaded or not temp_file.is_file() or temp_file.stat().st_size <= 0:
        raise RuntimeError("OSP2-BE release download failed or produced an empty PK3.")

    downloaded_version = _read_pk3_version(temp_file)
    if not downloaded_version:
        raise RuntimeError("Downloaded OSP2-BE PK3 does not contain a readable version.txt.")
    if _version_key(downloaded_version) != _version_key(remote_version):
        raise RuntimeError(
            f"Downloaded OSP2-BE version mismatch: website={remote_version}, PK3={downloaded_version}."
        )

    # Keep the currently installed PK3 until the new package is fully downloaded
    # and the embedded version has been verified.
    temp_file.replace(OSP_FILE)
    return {
        "action": "updated" if exists else "installed",
        "status": "current", "update_available": False,
        "local_version": downloaded_version, "remote_version": remote_version,
        "previous_version": local_version, "changelog": notes,
        "changelog_releases": changelog_releases,
        "file": str(OSP_FILE), **changelog_meta,
    }
