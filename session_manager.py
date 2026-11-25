#!/usr/bin/env python3
"""Multi-user session manager, save service, and optional HF Dataset cloud sync."""

from datetime import datetime
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlparse

try:
    from huggingface_hub import HfApi, snapshot_download
except ImportError:
    HfApi = None
    snapshot_download = None

SAVES_ROOT = os.environ.get("SAVES_DIR", "/data/saves")
GAME_TEMPLATE = os.environ.get("GAME_DIR", "/app/game")
REPO_DIR = os.environ.get("REPO_DIR", "")
WINE_TEMPLATE = "/opt/wine-template"
SESSIONS_TMP = "/tmp/fm-sessions"
GAME_READY_LOCK = threading.Lock()
HOST = "127.0.0.1"
PORT = 7861
CHUNK_SIZE = 65536
MAX_SLOTS = 20
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT_SECONDS", "1800"))
PROFILE_RE = re.compile(r"^[a-z0-9_-]{2,24}$")


def detect_max_sessions() -> int:
    """Compute max concurrent sessions from MAX_SESSIONS env or available RAM+swap."""
    raw = os.environ.get("MAX_SESSIONS", "0").strip()
    try:
        override = int(raw)
    except ValueError:
        override = 0
    if override > 0:
        return max(1, min(MAX_SLOTS, override))

    mem_kb = 0
    swap_kb = 0
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    mem_kb = int(line.split()[1])
                elif line.startswith("SwapTotal:"):
                    swap_kb = int(line.split()[1])
    except OSError:
        return 2

    ram_mb = mem_kb // 1024
    swap_mb = swap_kb // 1024
    slots = (ram_mb + swap_mb // 2) // 650
    return max(2, min(MAX_SLOTS, slots))


MAX_SESSIONS = detect_max_sessions()


def safe_profile(raw_profile: str) -> str | None:
    """Normalize and validate profile name (2-24 chars: a-z, 0-9, -, _)."""
    if not raw_profile:
        return None
    norm = unquote(raw_profile).strip().lower()
    if not PROFILE_RE.match(norm):
        return None
    return norm


def safe_filename(raw_name: str) -> str | None:
    """Validate and return a safe single-segment filename, or None if invalid."""
    if not raw_name:
        return None
    name = unquote(raw_name).strip()
    if (
        not name
        or name in (".", "..")
        or name.startswith(".")
        or "/" in name
        or "\\" in name
        or ".." in name
        or "\x00" in name
    ):
        return None
    return name


class CloudSync:
    """Optional automatic save synchronization with a Hugging Face Dataset."""

    def __init__(self) -> None:
        self.token = os.environ.get("HF_TOKEN", "").strip()
        self.repo = os.environ.get("HF_SAVES_REPO", "").strip()
        self.enabled = bool(
            self.token and self.repo and HfApi is not None and snapshot_download is not None
        )
        self.api = HfApi(token=self.token) if self.enabled else None
        self.lock = threading.Lock()
        self.synced_state: dict[str, tuple[int, int]] = {}

    def _iter_local_saves(self) -> list[tuple[str, str, tuple[int, int], float]]:
        items: list[tuple[str, str, tuple[int, int], float]] = []
        if not os.path.isdir(SAVES_ROOT):
            return items
        for prof_entry in os.scandir(SAVES_ROOT):
            if not prof_entry.is_dir() or prof_entry.name.startswith("."):
                continue
            profile = safe_profile(prof_entry.name)
            if not profile:
                continue
            for file_entry in os.scandir(prof_entry.path):
                if not file_entry.is_file() or file_entry.name.startswith("."):
                    continue
                if ".tmp." in file_entry.name:
                    continue
                fname = safe_filename(file_entry.name)
                if not fname:
                    continue
                st = file_entry.stat()
                rel_path = f"{profile}/{fname}"
                items.append(
                    (rel_path, file_entry.path, (st.st_size, int(st.st_mtime)), st.st_mtime)
                )
        return items

    def restore_all(self) -> None:
        if not self.enabled:
            return
        os.makedirs(SAVES_ROOT, exist_ok=True)
        try:
            snapshot_download(
                repo_id=self.repo,
                repo_type="dataset",
                local_dir=SAVES_ROOT,
                cache_dir="/tmp/hf-cache",
                token=self.token,
                ignore_patterns=[".gitattributes", "README.md", ".huggingface*"],
            )
        except Exception as exc:
            print(f"[CloudSync] restore_all warning: {exc}", flush=True)

        with self.lock:
            for rel_path, _, sig, _ in self._iter_local_saves():
                self.synced_state[rel_path] = sig

    def sync_changed(self, force: bool = False) -> None:
        if not self.enabled or self.api is None:
            return
        now = time.time()
        with self.lock:
            for rel_path, full_path, sig, mtime in self._iter_local_saves():
                if not force and (now - mtime < 3):
                    continue
                if self.synced_state.get(rel_path) == sig:
                    continue
                try:
                    self.api.upload_file(
                        path_or_fileobj=full_path,
                        path_in_repo=rel_path,
                        repo_id=self.repo,
                        repo_type="dataset",
                    )
                    self.synced_state[rel_path] = sig
                except Exception as exc:
                    print(f"[CloudSync] upload warning ({rel_path}): {exc}", flush=True)

    def delete_remote(self, profile: str, name: str) -> None:
        if not self.enabled or self.api is None:
            return
        rel_path = f"{profile}/{name}"
        with self.lock:
            try:
                self.api.delete_file(
                    path_in_repo=rel_path,
                    repo_id=self.repo,
                    repo_type="dataset",
                )
            except Exception as exc:
                print(f"[CloudSync] delete warning ({rel_path}): {exc}", flush=True)
            self.synced_state.pop(rel_path, None)

    def start_background(self) -> None:
        if not self.enabled:
            return
        threading.Thread(target=self._sync_loop, daemon=True).start()

    def _sync_loop(self) -> None:
        while True:
            time.sleep(30)
            self.sync_changed()


CLOUD_SYNC = CloudSync()


def wait_for_tcp(host: str, port: int, timeout: float = 10.0) -> bool:
    """Wait until a local TCP port accepts connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def wait_for_unix_socket(path: str, timeout: float = 10.0) -> bool:
    """Wait until a Unix domain socket path exists."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.15)
    return False


def terminate_proc(proc: subprocess.Popen | None, timeout: float = 3.0) -> None:
    """Gracefully terminate a subprocess, then kill if needed."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=1.0)
        except Exception:
            pass


def link_saves_dir(base_dir: str, profile_saves: str) -> None:
    """Link Wine Documents/.../games directly to the user's persistent saves dir."""
    si_dir = os.path.join(base_dir, "Sports Interactive", "Football Manager 2005")
    games_dir = os.path.join(si_dir, "games")
    os.makedirs(si_dir, exist_ok=True)
    if os.path.islink(games_dir):
        os.unlink(games_dir)
    elif os.path.isdir(games_dir):
        for entry in os.scandir(games_dir):
            dst = os.path.join(profile_saves, entry.name)
            if not os.path.exists(dst) and entry.is_file():
                shutil.copy2(entry.path, dst)
        shutil.rmtree(games_dir, ignore_errors=True)
    os.symlink(profile_saves, games_dir)


class Session:
    """Represents one isolated FM 2005 Xvfb + Wine + VNC + Websockify session."""

    def __init__(self, profile: str, slot: int) -> None:
        self.profile = profile
        self.slot = slot
        self.display_num = 10 + slot
        self.display = f":{self.display_num}"
        self.vnc_port = 5900 + slot
        self.ws_port = 6100 + slot
        self.session_dir = os.path.join(SESSIONS_TMP, profile)
        self.wine_prefix = os.path.join(self.session_dir, "wine")
        self.game_dir = os.path.join(self.session_dir, "game")
        self.saves_dir = os.path.join(SAVES_ROOT, profile)
        self.last_seen = time.monotonic()
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.xvfb: subprocess.Popen | None = None
        self.openbox: subprocess.Popen | None = None
        self.vnc: subprocess.Popen | None = None
        self.ws: subprocess.Popen | None = None
        self.game_proc: subprocess.Popen | None = None
        self.watchdog_thread: threading.Thread | None = None

    def wine_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["HOME"] = "/home/gamer"
        env["DISPLAY"] = self.display
        env["WINEARCH"] = "win32"
        env["WINEPREFIX"] = self.wine_prefix
        env["WINEDEBUG"] = "-all"
        env["WINEDLLOVERRIDES"] = "msvcr71=n,b;mscoree,mshtml="
        return env

    def is_healthy(self) -> bool:
        return (
            not self.stop_event.is_set()
            and self.xvfb is not None
            and self.xvfb.poll() is None
            and self.vnc is not None
            and self.vnc.poll() is None
            and self.ws is not None
            and self.ws.poll() is None
        )


def ensure_game_ready() -> tuple[bool, str]:
    """Ensure fm2005.exe exists in GAME_TEMPLATE, downloading from GitHub Releases if needed."""
    exe_path = os.path.join(GAME_TEMPLATE, "fm2005.exe")
    if os.path.isfile(exe_path):
        return True, ""

    with GAME_READY_LOCK:
        if os.path.isfile(exe_path):
            return True, ""
        os.makedirs(GAME_TEMPLATE, exist_ok=True)
        local_tar = os.path.join(REPO_DIR, "game.tar.gz") if REPO_DIR else "/app/game.tar.gz"
        if os.path.isfile(local_tar):
            subprocess.run(
                ["tar", "-xzf", local_tar, "-C", GAME_TEMPLATE],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        elif shutil.which("gh") and REPO_DIR and os.path.isdir(REPO_DIR):
            tmp_tar = "/tmp/game.tar.gz"
            dl = subprocess.run(
                ["gh", "release", "download", "-p", "game.tar.gz", "-D", "/tmp", "--clobber"],
                cwd=REPO_DIR,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if dl.returncode == 0 and os.path.isfile(tmp_tar):
                subprocess.run(
                    ["tar", "-xzf", tmp_tar, "-C", GAME_TEMPLATE],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                try:
                    os.remove(tmp_tar)
                except OSError:
                    pass

        if os.path.isfile(exe_path):
            return True, ""

    return (
        False,
        "Brak plików gry (game.tar.gz). Wgraj plik game.tar.gz jako GitHub Release "
        "w swoim repozytorium (Releases -> Create a new release), a następnie kliknij ponownie 'Wejdź do gry'.",
    )


class SessionManager:
    """Thread-safe lifecycle manager for isolated per-user FM 2005 sessions."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sessions: dict[str, Session] = {}
        self.used_slots: set[int] = set()
        os.makedirs(SAVES_ROOT, exist_ok=True)
        os.makedirs(SESSIONS_TMP, exist_ok=True)
        self._reaper = threading.Thread(target=self._reap_loop, daemon=True)
        self._reaper.start()

    def prepare_user_dirs(self, session: Session) -> None:
        os.makedirs(session.saves_dir, exist_ok=True)
        os.makedirs(session.session_dir, exist_ok=True)

        if not os.path.exists(session.wine_prefix):
            shutil.copytree(WINE_TEMPLATE, session.wine_prefix, symlinks=True)

        user_win_dir = os.path.join(session.wine_prefix, "drive_c", "users", "gamer")
        docs_dir = os.path.join(user_win_dir, "Documents")
        os.makedirs(docs_dir, exist_ok=True)
        link_saves_dir(docs_dir, session.saves_dir)

        my_docs_dir = os.path.join(user_win_dir, "My Documents")
        if os.path.isdir(my_docs_dir) and not os.path.islink(my_docs_dir):
            link_saves_dir(my_docs_dir, session.saves_dir)

        os.makedirs(session.game_dir, exist_ok=True)
        for name in ("temp", "stats"):
            os.makedirs(os.path.join(session.game_dir, name), exist_ok=True)

        if os.path.isdir(GAME_TEMPLATE):
            for entry in os.scandir(GAME_TEMPLATE):
                if entry.name in ("temp", "stats"):
                    continue
                target_link = os.path.join(session.game_dir, entry.name)
                if os.path.lexists(target_link):
                    continue
                os.symlink(entry.path, target_link)

    def _start_processes(self, session: Session) -> None:
        lock_file = f"/tmp/.X{session.display_num}-lock"
        sock_file = f"/tmp/.X11-unix/X{session.display_num}"
        for path in (lock_file, sock_file):
            try:
                if os.path.lexists(path):
                    os.unlink(path)
            except OSError:
                pass

        env = session.wine_env()
        session.xvfb = subprocess.Popen(
            [
                "Xvfb",
                session.display,
                "-screen",
                "0",
                "1024x768x24",
                "-ac",
                "+extension",
                "GLX",
                "+render",
                "-noreset",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not wait_for_unix_socket(sock_file, timeout=10.0):
            raise RuntimeError("Xvfb failed to start")

        session.openbox = subprocess.Popen(
            ["openbox", "--config-file", "/home/gamer/.config/openbox/rc.xml"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        session.vnc = subprocess.Popen(
            [
                "x11vnc",
                "-display",
                session.display,
                "-rfbport",
                str(session.vnc_port),
                "-listen",
                "127.0.0.1",
                "-forever",
                "-shared",
                "-nopw",
                "-noxdamage",
                "-wait",
                "20",
                "-defer",
                "20",
                "-quiet",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not wait_for_tcp("127.0.0.1", session.vnc_port, timeout=10.0):
            raise RuntimeError("x11vnc failed to start")

        session.ws = subprocess.Popen(
            [
                "websockify",
                f"127.0.0.1:{session.ws_port}",
                f"127.0.0.1:{session.vnc_port}",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not wait_for_tcp("127.0.0.1", session.ws_port, timeout=15.0):
            raise RuntimeError("websockify failed to start")

        session.watchdog_thread = threading.Thread(
            target=self._game_watchdog, args=(session,), daemon=True
        )
        session.watchdog_thread.start()

    def _game_watchdog(self, session: Session) -> None:
        exe_path = os.path.join(session.game_dir, "fm2005.exe")
        env = session.wine_env()
        while not session.stop_event.is_set():
            if not os.path.exists(exe_path):
                session.stop_event.wait(10.0)
                continue
            proc = subprocess.Popen(
                ["wine", "explorer", "/desktop=FM2005,1024x768", "fm2005.exe"],
                cwd=session.game_dir,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            with session.lock:
                session.game_proc = proc
            proc.wait()
            with session.lock:
                if session.game_proc is proc:
                    session.game_proc = None
            if not session.stop_event.is_set():
                session.stop_event.wait(2.0)

    def _teardown_session(self, session: Session) -> None:
        session.stop_event.set()
        with session.lock:
            game_proc = session.game_proc
            session.game_proc = None
        terminate_proc(game_proc, timeout=2.0)

        try:
            subprocess.run(
                ["wineserver", "-k"],
                env=session.wine_env(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except Exception:
            pass

        terminate_proc(session.ws, timeout=2.0)
        terminate_proc(session.vnc, timeout=2.0)
        terminate_proc(session.openbox, timeout=2.0)
        terminate_proc(session.xvfb, timeout=2.0)

        shutil.rmtree(session.session_dir, ignore_errors=True)

    def start_session(self, profile: str) -> tuple[int, dict]:
        ready, err_msg = ensure_game_ready()
        if not ready:
            return 503, {"error": err_msg}

        with self.lock:
            existing = self.sessions.get(profile)
            if existing is not None:
                if existing.is_healthy():
                    existing.last_seen = time.monotonic()
                    return 200, {
                        "ok": True,
                        "profile": profile,
                        "ws_port": existing.ws_port,
                        "active_sessions": len(self.sessions),
                        "max_sessions": MAX_SESSIONS,
                    }
                self.sessions.pop(profile, None)
                self.used_slots.discard(existing.slot)
                self._teardown_session(existing)

            if len(self.sessions) >= MAX_SESSIONS:
                return 503, {
                    "error": (
                        f"Serwer osiągnął limit aktywnych graczy "
                        f"({len(self.sessions)}/{MAX_SESSIONS}). Spróbuj ponownie za chwilę."
                    )
                }

            slot = next(s for s in range(1, MAX_SLOTS + 1) if s not in self.used_slots)
            self.used_slots.add(slot)
            session = Session(profile, slot)
            self.sessions[profile] = session

        try:
            self.prepare_user_dirs(session)
            self._start_processes(session)
        except Exception as exc:
            with self.lock:
                if self.sessions.get(profile) is session:
                    self.sessions.pop(profile, None)
                self.used_slots.discard(slot)
            self._teardown_session(session)
            return 500, {"error": f"Nie udało się uruchomić sesji: {exc}"}

        with self.lock:
            session.last_seen = time.monotonic()
            active = len(self.sessions)
        return 200, {
            "ok": True,
            "profile": profile,
            "ws_port": session.ws_port,
            "active_sessions": active,
            "max_sessions": MAX_SESSIONS,
        }

    def heartbeat(self, profile: str) -> bool:
        with self.lock:
            session = self.sessions.get(profile)
            if session is None or not session.is_healthy():
                return False
            session.last_seen = time.monotonic()
            return True

    def stop_session(self, profile: str) -> bool:
        with self.lock:
            session = self.sessions.pop(profile, None)
            if session is None:
                return False
            self.used_slots.discard(session.slot)
        self._teardown_session(session)
        if CLOUD_SYNC.enabled:
            threading.Thread(
                target=CLOUD_SYNC.sync_changed, kwargs={"force": True}, daemon=True
            ).start()
        return True

    def restart_game(self, profile: str) -> bool:
        with self.lock:
            session = self.sessions.get(profile)
        if session is None or not session.is_healthy():
            return False
        session.last_seen = time.monotonic()
        with session.lock:
            game_proc = session.game_proc
        terminate_proc(game_proc, timeout=2.0)
        try:
            subprocess.run(
                ["wineserver", "-k"],
                env=session.wine_env(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except Exception:
            pass
        return True

    def stats(self) -> dict:
        with self.lock:
            return {
                "active_sessions": len(self.sessions),
                "max_sessions": MAX_SESSIONS,
            }

    def _reap_loop(self) -> None:
        while True:
            time.sleep(60)
            now = time.monotonic()
            to_stop: list[str] = []
            with self.lock:
                for profile, session in self.sessions.items():
                    if not session.is_healthy() or (now - session.last_seen > IDLE_TIMEOUT):
                        to_stop.append(profile)
            for profile in to_stop:
                self.stop_session(profile)


MANAGER = SessionManager()


def list_save_files(profile: str) -> list[dict]:
    """List regular non-hidden save files for a specific profile."""
    target_dir = os.path.join(SAVES_ROOT, profile)
    os.makedirs(target_dir, exist_ok=True)
    entries = []
    for entry in os.scandir(target_dir):
        if not entry.is_file() or entry.name.startswith("."):
            continue
        stat = entry.stat()
        entries.append(
            {
                "name": entry.name,
                "size_mb": stat.st_size / (1024 * 1024),
                "mtime": stat.st_mtime,
                "mtime_str": datetime.fromtimestamp(stat.st_mtime).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            }
        )
    entries.sort(key=lambda item: item["mtime"], reverse=True)
    return entries


def render_saves_page(profile: str | None) -> str:
    """Render the Polish dark-themed save management page scoped to one profile."""
    if not profile:
        return """<!DOCTYPE html>
<html lang="pl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Wybierz profil — Football Manager 2005</title>
  <style>
    body { margin:0; padding:32px; background:#0b0f14; color:#e6edf3; font-family:system-ui,sans-serif; }
    .card { max-width:460px; margin:48px auto; background:#131a22; border:1px solid #253242; border-radius:12px; padding:24px; }
    input { width:100%; padding:10px 12px; border-radius:8px; border:1px solid #2d3d52; background:#0b0f14; color:#e6edf3; font-size:1rem; box-sizing:border-box; margin:12px 0; }
    .btn { display:inline-block; padding:10px 16px; border-radius:8px; border:0; background:#22c55e; color:#04130a; font-weight:700; cursor:pointer; text-decoration:none; }
    .link { color:#8b9bb0; margin-left:12px; text-decoration:none; }
  </style>
</head>
<body>
  <div class="card">
    <h2 style="margin-top:0;">⚽ Zapisy gry</h2>
    <p style="color:#8b9bb0;">Podaj swój nick gracza (2–24 znaki: litery, cyfry, <code>-</code>, <code>_</code>), aby otworzyć swoje prywatne zapisy:</p>
    <form method="get" action="/saves">
      <input type="text" name="profile" placeholder="np. marcin" required minlength="2" maxlength="24" pattern="[A-Za-z0-9_-]{2,24}" />
      <button class="btn" type="submit">Otwórz moje zapisy</button>
      <a class="link" href="/">Wróć do gry</a>
    </form>
  </div>
</body>
</html>"""

    safe_prof_html = html.escape(profile)
    url_prof = quote(profile, safe="")
    files = list_save_files(profile)
    rows_html = ""
    for item in files:
        safe_display = html.escape(item["name"])
        url_name = quote(item["name"], safe="")
        size_text = f"{item['size_mb']:.2f} MB"
        rows_html += f"""
        <tr>
          <td class="name">{safe_display}</td>
          <td>{size_text}</td>
          <td>{html.escape(item['mtime_str'])}</td>
          <td class="actions">
            <a class="btn btn-dl" href="/api/download/{url_prof}/{url_name}">Pobierz</a>
            <button class="btn btn-del" data-name="{safe_display}" onclick="deleteSave(this.dataset.name)">Usuń</button>
          </td>
        </tr>"""

    if not rows_html:
        rows_html = (
            '<tr><td colspan="4" class="empty">'
            f"Brak zapisów dla gracza <strong>{safe_prof_html}</strong>. "
            "Zapisz grę w FM 2005 lub wgraj plik .fm poniżej."
            "</td></tr>"
        )

    if CLOUD_SYNC.enabled:
        cloud_banner = (
            '<div class="banner banner-ok">'
            "☁️ Automatyczna kopia zapisów w chmurze (Hugging Face Dataset) jest <strong>WŁĄCZONA</strong>."
            "</div>"
        )
    else:
        cloud_banner = (
            '<div class="banner banner-ok">'
            "💾 Twoje zapisy są przechowywane w trwałym folderze na serwerze i nie znikają po zamknięciu sesji. "
            "W każdej chwili możesz je też pobrać jako kopię zapasową."
            "</div>"
        )

    return f"""<!DOCTYPE html>
<html lang="pl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Zapisy ({safe_prof_html}) — Football Manager 2005</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0b0f14;
      --panel: #131a22;
      --border: #253242;
      --text: #e6edf3;
      --muted: #8b9bb0;
      --accent: #22c55e;
      --accent-hover: #16a34a;
      --blue: #3b82f6;
      --danger: #ef4444;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 24px;
      font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    .wrap {{ max-width: 900px; margin: 0 auto; }}
    .header {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 16px;
    }}
    h1 {{ margin: 0; font-size: 1.35rem; }}
    .badge {{
      display: inline-block;
      margin-left: 8px;
      padding: 3px 10px;
      border-radius: 999px;
      background: #1f2b38;
      border: 1px solid var(--border);
      font-size: 0.85rem;
      color: #4ade80;
    }}
    .banner {{
      padding: 12px 16px;
      border-radius: 10px;
      margin-bottom: 16px;
      font-size: 0.9rem;
      line-height: 1.4;
    }}
    .banner-ok {{
      background: rgba(34, 197, 94, 0.12);
      border: 1px solid rgba(34, 197, 94, 0.35);
      color: #bbf7d0;
    }}
    .banner-warn {{
      background: rgba(245, 158, 11, 0.12);
      border: 1px solid rgba(245, 158, 11, 0.38);
      color: #fde68a;
    }}
    .top-actions {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 18px;
      margin-bottom: 18px;
    }}
    .btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 8px 14px;
      border-radius: 8px;
      border: 1px solid transparent;
      font-size: 0.9rem;
      font-weight: 600;
      cursor: pointer;
      text-decoration: none;
      color: #fff;
      background: #273445;
    }}
    .btn-primary {{ background: var(--accent); color: #04130a; }}
    .btn-primary:hover {{ background: var(--accent-hover); }}
    .btn-dl {{ background: var(--blue); }}
    .btn-del {{ background: var(--danger); }}
    .btn-warn {{ background: #d97706; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{
      padding: 10px 8px;
      text-align: left;
      border-bottom: 1px solid var(--border);
      font-size: 0.92rem;
    }}
    th {{ color: var(--muted); font-weight: 600; }}
    td.name {{ word-break: break-all; font-weight: 500; }}
    td.actions {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    .empty {{ text-align: center; color: var(--muted); padding: 24px 8px; }}
    .upload-row {{ display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }}
    #status {{ margin-top: 10px; font-size: 0.9rem; color: var(--muted); }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="header">
      <h1>⚽ Zapisy gry <span class="badge">👤 {safe_prof_html}</span></h1>
      <div class="top-actions">
        <a class="btn btn-primary" href="/?profile={url_prof}">Wróć do gry</a>
        <button class="btn btn-warn" onclick="restartGame()">Restartuj moją grę</button>
      </div>
    </div>

    {cloud_banner}

    <div class="card">
      <h2 style="margin-top:0;font-size:1.05rem;">Wgraj zapis (.fm) dla profilu {safe_prof_html}</h2>
      <div class="upload-row">
        <input type="file" id="saveFile" />
        <button class="btn btn-primary" onclick="uploadSave()">Wyślij na serwer</button>
      </div>
      <div id="status">Te pliki są widoczne wyłącznie w Twojej sesji ({safe_prof_html}).</div>
    </div>

    <div class="card">
      <h2 style="margin-top:0;font-size:1.05rem;">Twoje zapisy na serwerze</h2>
      <table>
        <thead>
          <tr>
            <th>Nazwa pliku</th>
            <th>Rozmiar</th>
            <th>Zmodyfikowano</th>
            <th>Akcje</th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
  </div>

  <script>
    const PROFILE = {json.dumps(profile)};

    function setStatus(msg) {{
      document.getElementById('status').textContent = msg;
    }}

    async function uploadSave() {{
      const input = document.getElementById('saveFile');
      const file = input.files[0];
      if (!file) {{
        setStatus('Wybierz plik zapisu (.fm) przed wysłaniem.');
        return;
      }}
      setStatus('Wysyłanie ' + file.name + '...');
      try {{
        const url = '/api/upload?profile=' + encodeURIComponent(PROFILE) + '&name=' + encodeURIComponent(file.name);
        const res = await fetch(url, {{ method: 'POST', body: file }});
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Błąd wysyłania');
        setStatus('Wgrano pomyślnie: ' + file.name);
        setTimeout(() => location.reload(), 600);
      }} catch (err) {{
        setStatus('Błąd: ' + err.message);
      }}
    }}

    async function deleteSave(name) {{
      if (!confirm('Usunąć Twój plik zapisu "' + name + '"?')) return;
      try {{
        const url = '/api/delete?profile=' + encodeURIComponent(PROFILE) + '&name=' + encodeURIComponent(name);
        const res = await fetch(url, {{ method: 'POST' }});
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Błąd usuwania');
        location.reload();
      }} catch (err) {{
        setStatus('Błąd: ' + err.message);
      }}
    }}

    async function restartGame() {{
      if (!confirm('Uruchomić ponownie Twoją grę FM 2005? Niezapisany postęp zostanie utracony.')) return;
      setStatus('Restartowanie Twojej sesji...');
      try {{
        const res = await fetch('/api/restart?profile=' + encodeURIComponent(PROFILE), {{ method: 'POST' }});
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Nie udało się zrestartować gry');
        setStatus('Gra zrestartowana. Wróć do okna gry — uruchomi się w ciągu kilku sekund.');
      }} catch (err) {{
        setStatus('Błąd: ' + err.message);
      }}
    }}
  </script>
</body>
</html>"""


class SessionHTTPHandler(BaseHTTPRequestHandler):
    """HTTP handler for isolated user sessions and per-profile save files."""

    def log_message(self, format: str, *args) -> None:
        return

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, status: int, content: str) -> None:
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/saves", "/saves/"):
            raw_prof = (query.get("profile") or [""])[0]
            profile = safe_profile(raw_prof)
            self.send_html(200, render_saves_page(profile))
            return

        if path == "/api/health":
            info = MANAGER.stats()
            info["status"] = "ok"
            info["cloud_sync"] = CLOUD_SYNC.enabled
            self.send_json(200, info)
            return

        if path.startswith("/api/download/"):
            rest = path[len("/api/download/") :]
            parts = rest.split("/", 1)
            if len(parts) != 2:
                self.send_json(400, {"error": "Invalid download path"})
                return
            profile = safe_profile(parts[0])
            name = safe_filename(parts[1])
            if not profile or not name:
                self.send_json(400, {"error": "Invalid profile or filename"})
                return
            target = os.path.join(SAVES_ROOT, profile, name)
            if not os.path.isfile(target):
                self.send_json(404, {"error": "File not found"})
                return
            size = os.path.getsize(target)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(target, "rb") as fh:
                while True:
                    chunk = fh.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return

        self.send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        profile = safe_profile((query.get("profile") or [""])[0])

        if path == "/api/session/start":
            if not profile:
                self.send_json(
                    400,
                    {"error": "Nieprawidłowy nick (użyj 2-24 znaków: litery, cyfry, - lub _)."},
                )
                return
            status, payload = MANAGER.start_session(profile)
            self.send_json(status, payload)
            return

        if path == "/api/session/heartbeat":
            if not profile:
                self.send_json(400, {"error": "Invalid profile"})
                return
            active = MANAGER.heartbeat(profile)
            self.send_json(200, {"ok": True, "active": active})
            return

        if path == "/api/session/stop":
            if not profile:
                self.send_json(400, {"error": "Invalid profile"})
                return
            stopped = MANAGER.stop_session(profile)
            self.send_json(200, {"ok": True, "stopped": stopped})
            return

        if path == "/api/restart":
            if not profile:
                self.send_json(400, {"error": "Invalid profile"})
                return
            restarted = MANAGER.restart_game(profile)
            if not restarted:
                self.send_json(404, {"error": "Sesja nie jest aktywna"})
                return
            self.send_json(200, {"ok": True})
            return

        if path == "/api/upload":
            if not profile:
                self.send_json(400, {"error": "Invalid profile"})
                return
            name = safe_filename((query.get("name") or [""])[0])
            if not name:
                self.send_json(400, {"error": "Invalid filename"})
                return

            length_hdr = self.headers.get("Content-Length")
            if length_hdr is None:
                self.send_json(411, {"error": "Content-Length required"})
                return
            try:
                remaining = int(length_hdr)
            except ValueError:
                self.send_json(400, {"error": "Invalid Content-Length"})
                return
            if remaining < 0:
                self.send_json(400, {"error": "Invalid Content-Length"})
                return

            target_dir = os.path.join(SAVES_ROOT, profile)
            os.makedirs(target_dir, exist_ok=True)
            final_path = os.path.join(target_dir, name)
            tmp_path = os.path.join(target_dir, f".tmp.{name}.{os.getpid()}")

            try:
                with open(tmp_path, "wb") as out:
                    while remaining > 0:
                        chunk = self.rfile.read(min(CHUNK_SIZE, remaining))
                        if not chunk:
                            raise IOError("Unexpected EOF during upload")
                        out.write(chunk)
                        remaining -= len(chunk)
                os.replace(tmp_path, final_path)
            except Exception as exc:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                self.send_json(500, {"error": str(exc)})
                return

            if CLOUD_SYNC.enabled:
                threading.Thread(target=CLOUD_SYNC.sync_changed, daemon=True).start()
            self.send_json(200, {"ok": True, "name": name})
            return

        if path == "/api/delete":
            if not profile:
                self.send_json(400, {"error": "Invalid profile"})
                return
            name = safe_filename((query.get("name") or [""])[0])
            if not name:
                self.send_json(400, {"error": "Invalid filename"})
                return
            target = os.path.join(SAVES_ROOT, profile, name)
            if not os.path.isfile(target):
                self.send_json(404, {"error": "File not found"})
                return
            os.remove(target)
            if CLOUD_SYNC.enabled:
                threading.Thread(
                    target=CLOUD_SYNC.delete_remote, args=(profile, name), daemon=True
                ).start()
            self.send_json(200, {"ok": True})
            return

        self.send_json(404, {"error": "Not found"})


def _shutdown_handler(signum, frame) -> None:
    for profile in list(MANAGER.sessions.keys()):
        MANAGER.stop_session(profile)
    CLOUD_SYNC.sync_changed(force=True)
    raise SystemExit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)
    CLOUD_SYNC.restore_all()
    CLOUD_SYNC.start_background()
    server = ThreadingHTTPServer((HOST, PORT), SessionHTTPHandler)
    server.serve_forever()
