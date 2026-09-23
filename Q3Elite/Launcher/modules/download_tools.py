import shutil
from socket import timeout

from sys import argv
import zipfile
from shutil import rmtree

import os
import time
import urllib.request
import urllib.error
import http.client
import socket
from math import floor
from pathlib import Path

# Backend paths. This module intentionally has no GUI dependency.
APPDATA_DIR = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
Q3ELITE_APPDATA_DIR = APPDATA_DIR / "Quake 3 Elite"
Q3ELITE_LAUNCHER_DATA_DIR = Q3ELITE_APPDATA_DIR / "Launcher"
CACHE_DIR = Q3ELITE_LAUNCHER_DATA_DIR / "cache"
Q3ELITE_TEMP_DIR = Q3ELITE_APPDATA_DIR / "Temp"
TEMP_FILES_DIR = Q3ELITE_TEMP_DIR

def get_relative_paths(root):
    """Compatibility helper used by legacy unziper()."""
    root = os.path.abspath(root)
    result = []
    for current, dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(current, name)
            result.append(os.path.relpath(full, root))
    return result

def caption():
    """Legacy compatibility hook."""
    return None


# Shared launcher-wide controller/callback used by the unified Q3Elite
# downloader pipeline.
_DEFAULT_DOWNLOAD_CONTROL = None
_DEFAULT_PROGRESS_CALLBACK = None


def set_default_download_control(control):
    global _DEFAULT_DOWNLOAD_CONTROL
    _DEFAULT_DOWNLOAD_CONTROL = control


def set_default_progress_callback(callback):
    global _DEFAULT_PROGRESS_CALLBACK
    _DEFAULT_PROGRESS_CALLBACK = callback


class DownloadControl:
    """
    Thread-safe cooperative download controller.

    The same controller can be shared by Q3Elite, PAK and OSP workers:
        control.pause()
        control.resume()
        control.cancel()

    Pause does not delete downloaded data. The active downloader stops reading
    new chunks while paused and continues when resume() is called.
    """

    def __init__(self):
        import threading
        self._resume_event = threading.Event()
        self._resume_event.set()
        self._cancel_event = threading.Event()

    def pause(self):
        self._resume_event.clear()

    def resume(self):
        self._resume_event.set()

    def cancel(self):
        self._cancel_event.set()
        # Wake a paused downloader so it can notice cancellation.
        self._resume_event.set()

    def reset(self):
        self._cancel_event.clear()
        self._resume_event.set()

    @property
    def paused(self):
        return not self._resume_event.is_set()

    @property
    def cancelled(self):
        return self._cancel_event.is_set()

    def wait_if_paused(self):
        while not self._resume_event.wait(0.2):
            if self._cancel_event.is_set():
                return False
        return not self._cancel_event.is_set()


class DownloadCancelled(Exception):
    pass


def _remote_size(file_url, headers, timeout_value):
    """Best-effort remote size lookup. Returns None when the server hides it."""
    try:
        req = urllib.request.Request(
            file_url,
            headers=headers,
            method="HEAD"
        )
        with urllib.request.urlopen(req, timeout=timeout_value) as response:
            value = response.headers.get("Content-Length")
            if value is not None:
                return int(value)
    except Exception:
        pass
    return None


def _content_range_total(response):
    """Parse total size from Content-Range, e.g. 'bytes 100-199/1000'."""
    value = response.headers.get("Content-Range")
    if not value or "/" not in value:
        return None

    total = value.rsplit("/", 1)[1].strip()
    if total == "*":
        return None

    try:
        return int(total)
    except (TypeError, ValueError):
        return None


def _call_progress(callback, downloaded, total, speed, file_name):
    if callback is None:
        return

    try:
        callback(downloaded, total, speed, file_name)
    except TypeError:
        # Keep it convenient for older/simple callbacks.
        try:
            callback(downloaded, total)
        except Exception:
            pass
    except Exception:
        pass


def downloader(
    file_url,
    file_path,
    file_name,
    skip=False,
    max_attempts=10,
    control=None,
    progress_callback=None,
    expected_size=None,
    use_part_file=True,
    timeout_value=10,
    probe_remote_size=True,
    dynamic_stream=False,
    stall_timeout=900,
):
    """
    Download a file with resume, pause/resume control and progress reporting.

    Backwards compatible:
        downloader(url, path, name, skip=True)

    New optional arguments:
        control             DownloadControl shared with GUI/workers.
        progress_callback   callback(downloaded, total, bytes_per_second, name)
        expected_size       trusted total size (pCloud metadata is ideal).
        use_part_file       store incomplete data as <name>.part.
        probe_remote_size     use a separate HEAD request to discover size.
                              Disable for dynamically generated ZIP streams.
        dynamic_stream         tolerate temporary read stalls without reconnecting.
                              Intended for pCloud getpubzip streams.
        stall_timeout          maximum cumulative no-data stall before giving up.

    Incomplete downloads use <name>.part by default for every launcher download
    (Q3Elite, official PAKs, OSP2-BE and updater files). The final filename is
    created only after the transfer reaches the expected remote size.
    """

    final_path = os.path.join(file_path, file_name)
    working_path = final_path + ".part" if use_part_file else final_path

    os.makedirs(file_path, exist_ok=True)

    print(f"Downloading {file_name}...")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }

    chunk_size = 256 * 1024
    attempt = 0

    if control is None:
        control = _DEFAULT_DOWNLOAD_CONTROL

    if control is None:
        control = DownloadControl()

    if progress_callback is None:
        progress_callback = _DEFAULT_PROGRESS_CALLBACK

    # A trusted caller-provided size wins over HEAD.
    total_length = None
    if expected_size is not None:
        try:
            total_length = int(expected_size)
        except (TypeError, ValueError):
            total_length = None

    if (total_length is None or total_length <= 0) and probe_remote_size:
        total_length = _remote_size(file_url, headers, timeout_value)

    # If an old final file already exists and we now use .part, accept it when
    # complete; otherwise move it to .part so it can be resumed safely.
    if use_part_file and os.path.exists(final_path):
        final_size = os.path.getsize(final_path)

        if total_length and final_size == total_length:
            print(f"\nFile {file_name} already exists and is complete. Skipping.")
            return final_path

        if skip and not os.path.exists(working_path):
            try:
                os.replace(final_path, working_path)
            except OSError:
                pass

    # Complete cached legacy file: never send Range at EOF. This fixes the
    # common HTTP 416 case seen with OSP2-BE/mod metadata.
    if skip and os.path.exists(working_path) and total_length:
        local_size = os.path.getsize(working_path)

        if local_size == total_length:
            if use_part_file:
                os.replace(working_path, final_path)
            print(f"\nFile {file_name} already exists and is complete. Skipping.")
            return final_path

        if local_size > total_length:
            print(
                f"\nLocal {file_name} is larger than the remote file. "
                "Restarting download."
            )
            with open(working_path, "wb"):
                pass

    while attempt < max_attempts:
        if control.cancelled:
            print(f"\nDownload cancelled: {file_name}")
            return None

        if not control.wait_if_paused():
            print(f"\nDownload cancelled: {file_name}")
            return None

        downloaded = (
            os.path.getsize(working_path)
            if skip and os.path.exists(working_path)
            else 0
        )

        request_headers = headers.copy()
        append_requested = skip and downloaded > 0

        if append_requested:
            request_headers["Range"] = f"bytes={downloaded}-"

        request = urllib.request.Request(file_url, headers=request_headers)

        try:
            with urllib.request.urlopen(
                request,
                timeout=timeout_value
            ) as response:

                # pCloud getpubzip can temporarily stop producing bytes while
                # continuing to generate the archive. Keep the SAME HTTP
                # response alive across read timeouts; reconnecting is unsafe
                # because a newly generated ZIP is not byte-identical.
                stall_started = None

                status = response.getcode()

                # Server ignored Range. Restart instead of appending duplicate
                # bytes to the existing file.
                if append_requested and status != 206:
                    print(
                        f"\nServer ignored resume request for {file_name}; "
                        "restarting from zero."
                    )
                    downloaded = 0
                    write_mode = "wb"
                else:
                    write_mode = "ab" if append_requested else "wb"

                range_total = _content_range_total(response)
                if range_total:
                    total_length = range_total

                if not total_length:
                    content_length = response.headers.get("Content-Length")
                    if content_length is not None:
                        content_length = int(content_length)
                        total_length = (
                            downloaded + content_length
                            if status == 206
                            else content_length
                        )

                percent = -1
                speed_window_start = time.monotonic()
                speed_window_bytes = downloaded
                current_speed = 0.0

                with open(working_path, write_mode) as out_file:
                    while True:
                        if control.cancelled:
                            raise DownloadCancelled()

                        if not control.wait_if_paused():
                            raise DownloadCancelled()

                        try:
                            chunk = response.read(chunk_size)
                            stall_started = None
                        except (socket.timeout, TimeoutError) as error:
                            if not dynamic_stream:
                                raise

                            now = time.monotonic()
                            if stall_started is None:
                                stall_started = now

                            stalled_for = now - stall_started
                            _call_progress(
                                progress_callback,
                                downloaded,
                                total_length,
                                0.0,
                                file_name,
                            )

                            print(
                                f"\rWaiting for pCloud ZIP stream... "
                                f"{stalled_for:.0f}s "
                                f"({downloaded / 1048576:.1f} MB received)   ",
                                end="",
                                flush=True,
                            )

                            if stalled_for >= stall_timeout:
                                raise ConnectionError(
                                    f"pCloud ZIP stream stalled for "
                                    f"{stall_timeout} seconds."
                                )

                            # Stay on this exact HTTP response. Do NOT retry with
                            # Range, because getpubzip may regenerate a different ZIP.
                            continue
                        except http.client.IncompleteRead as error:
                            chunk = error.partial
                            if not chunk:
                                raise ConnectionError(
                                    "pCloud closed the ZIP stream before EOF."
                                )

                        if not chunk:
                            break

                        out_file.write(chunk)
                        downloaded += len(chunk)

                        now = time.monotonic()
                        elapsed = now - speed_window_start

                        if elapsed >= 0.5:
                            current_speed = (
                                downloaded - speed_window_bytes
                            ) / elapsed
                            speed_window_start = now
                            speed_window_bytes = downloaded

                            _call_progress(
                                progress_callback,
                                downloaded,
                                total_length,
                                current_speed,
                                file_name,
                            )

                        if total_length:
                            new_percent = min(
                                100,
                                int(downloaded * 100 / total_length)
                            )

                            if new_percent != percent:
                                percent = new_percent
                                mb_done = downloaded / 1048576
                                mb_total = total_length / 1048576
                                bar_count = min(20, int(percent / 5))
                                bar = "#" * bar_count
                                spaces = " " * (20 - bar_count)

                                print(
                                    f"\r[{bar}{spaces}] {percent}% "
                                    f"({mb_done:.1f}/{mb_total:.1f} MB)   ",
                                    end="",
                                    flush=True,
                                )
                        else:
                            print(
                                f"\rDownloaded: {downloaded / 1048576:.1f} MB",
                                end="",
                                flush=True,
                            )

                # Validate completion whenever the total is known.
                if total_length is not None and downloaded < total_length:
                    raise ConnectionError(
                        "Connection closed prematurely (size mismatch)."
                    )

                if (
                    expected_size is not None
                    and downloaded != int(expected_size)
                ):
                    raise ConnectionError(
                        f"Downloaded size mismatch. "
                        f"Expected {int(expected_size)}, got {downloaded}."
                    )

                _call_progress(
                    progress_callback,
                    downloaded,
                    total_length,
                    current_speed,
                    file_name,
                )

                if use_part_file:
                    os.replace(working_path, final_path)

                print("\nDownloaded successfully.")
                return final_path

        except DownloadCancelled:
            print(f"\nDownload cancelled: {file_name}")
            return None

        except urllib.error.HTTPError as error:
            # Range at EOF / stale local partial. If sizes match, the file is
            # actually complete. Otherwise restart safely from zero.
            if error.code == 416 and os.path.exists(working_path):
                local_size = os.path.getsize(working_path)

                remote_total = total_length
                content_range = error.headers.get("Content-Range")
                if content_range and "/" in content_range:
                    try:
                        remote_total = int(content_range.rsplit("/", 1)[1])
                    except (TypeError, ValueError):
                        pass

                if remote_total and local_size == remote_total:
                    if use_part_file:
                        os.replace(working_path, final_path)
                    print(
                        f"\nFile {file_name} is already complete "
                        "(HTTP 416 at EOF)."
                    )
                    return final_path

                print(
                    f"\nResume position for {file_name} is invalid "
                    "(HTTP 416). Restarting from zero."
                )
                try:
                    with open(working_path, "wb"):
                        pass
                except OSError:
                    pass

                attempt += 1
                continue

            attempt += 1
            print(
                f"\nNetwork issue: HTTP {error.code}: {error.reason}. "
                f"Retrying ({attempt}/{max_attempts})..."
            )
            time.sleep(1)

        except (
            urllib.error.URLError,
            ConnectionError,
            TimeoutError,
            http.client.HTTPException,
            OSError,
        ) as error:
            attempt += 1
            print(
                f"\nNetwork issue: {error}. "
                f"Retrying ({attempt}/{max_attempts})..."
            )
            time.sleep(1)

    print(
        f"\nFailed to download {file_name} "
        f"after {max_attempts} attempts."
    )
    return None


def unziper(file_url, name, file_paths=[], skip=False, wanted_paths=None, control=None, progress_callback=None):
    """wanted_paths: if specified (set of normalized destination paths),
    process only entries whose destination is included in it."""

    installed = []

    cache_file = CACHE_DIR / name
    extract_dir = TEMP_FILES_DIR / f"{name}dir"

    if wanted_paths is not None:
        file_paths = [
            fp for fp in file_paths
            if os.path.normpath(
                os.path.join(fp[1], os.path.basename(fp[0]))
            ) in wanted_paths
        ]

        if not file_paths:
            print(f'[skip] {name}: no files needed, archive not downloaded')
            return installed

    # Remove cached archive when skip=False
    if cache_file.exists() and not skip:
        cache_file.unlink()

    # Create working directories
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_FILES_DIR.mkdir(parents=True, exist_ok=True)

    # Remove old extraction directory if left from interrupted run
    if extract_dir.exists():
        shutil.rmtree(extract_dir, ignore_errors=True)

    # Download archive to AppData cache
    downloaded_file = downloader(
        file_url,
        str(CACHE_DIR),
        name,
        skip=True,
        control=control,
        progress_callback=progress_callback
    )

    if not downloaded_file:
        raise RuntimeError(f"Failed to download archive: {name}")

    # Extract archive
    with zipfile.ZipFile(cache_file, 'r') as zip_ref:
        zip_ref.extractall(extract_dir)

    # Install requested files
    for file_path in file_paths:
        temp_name = extract_dir / file_path[0]
        dest = Path(file_path[1])

        assert temp_name.exists(), f"No such file or directory: {temp_name}"

        if temp_name.is_file():
            print(temp_name)

            dest.mkdir(parents=True, exist_ok=True)

            shutil.copy2(temp_name, dest)

            dest_file = dest / temp_name.name
            installed.append(os.path.normpath(str(dest_file)))

        else:
            if dest.exists():
                shutil.rmtree(dest)

            shutil.copytree(temp_name, dest)

            installed.extend(
                os.path.normpath(str(dest).rstrip('/\\') + p)
                for p in get_relative_paths(str(dest))
            )

    # Delete extraction files, but KEEP archive in AppData cache
    shutil.rmtree(extract_dir, ignore_errors=True)

    return installed


def download(conf_file, skip=False, wanted_paths=None, control=None, progress_callback=None):
    """wanted_paths: если задан (set нормализованных путей назначения) —
    из .dconf обрабатываются только записи, дающие хотя бы один из этих
    путей. 'f'-записи вне набора пропускаются вообще без сети,
    'a'-записи — без скачивания и распаковки архива, если внутри него
    нет ни одного нужного файла."""

    arr = [None]

    try:
        all_installed = []

        with open(conf_file, 'r') as pack_file:
            pack_list = pack_file.read().split('\n')

            for (i, _) in enumerate(pack_list):
                installed = []

                if ';' in _:
                    arr = _.split(';')

                    if arr[0] == "a":

                        files = []
                        url, name = arr[2], arr[1]

                        for start, end in zip(arr[3::2], arr[4::2]):
                            files.append([start, end])

                        installed = unziper(url, name, files, skip=skip, wanted_paths=wanted_paths, control=control, progress_callback=progress_callback)

                    elif arr[0] == 'f':

                        file_name = arr[1]
                        file_url = arr[2]
                        dest_dir = Path(arr[3])

                        dest_path = os.path.normpath(
                            os.path.join(arr[3], file_name)
                        )

                        if wanted_paths is not None and dest_path not in wanted_paths:
                            print(f'[skip] {file_name}: file not needed')

                        else:
                            # ----------------------------------------------------
                            # 1. Download/store file in persistent AppData cache
                            # ----------------------------------------------------

                            CACHE_DIR.mkdir(parents=True, exist_ok=True)

                            cached_file = downloader(
                                file_url,
                                str(CACHE_DIR),
                                file_name,
                                skip=skip,
                                control=control,
                                progress_callback=progress_callback
                            )

                            if not cached_file:
                                raise RuntimeError(
                                    f"Failed to download file: {file_name}"
                                )

                            cached_file = Path(cached_file)

                            # ----------------------------------------------------
                            # 2. Copy cached file to its actual destination
                            # ----------------------------------------------------

                            dest_dir.mkdir(parents=True, exist_ok=True)

                            destination = dest_dir / file_name

                            print(f"Installing {file_name} -> {destination}")

                            shutil.copy2(
                                cached_file,
                                destination
                            )

                            installed.append(
                                os.path.normpath(str(destination))
                            )

                    else:

                        raise TypeError (f"Incorrect datatype: {arr[0]} in {arr[1]}")

                all_installed.extend(installed)

        return all_installed

    except Exception as err:
        print(f'[log] {err}')
        print(f"[error] not installed {arr[1]}")
        if arr[0] == 'a':
            extract_dir = TEMP_FILES_DIR / f"{arr[1]}dir"
            if extract_dir.exists():
                shutil.rmtree(extract_dir, ignore_errors=True)
        caption()


if __name__ == "__main__":
    download_conf = argv[1]
    if len(argv) >= 3 and argv[2] == "skip":
        s = True
    else:
        s = False

    download(download_conf, skip=s)
