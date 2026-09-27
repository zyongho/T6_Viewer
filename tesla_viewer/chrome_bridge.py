"""Local Chrome DevTools bridge for detecting a Tesla Bearer token.

The bridge never reads Chrome passwords or cookies.  It only observes request
headers from a browser that the user explicitly starts with a localhost CDP
port, and returns the Authorization value in memory to the application.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

import requests

try:
    import websocket
except ImportError:  # pragma: no cover - exercised by the UI when dependency is missing
    websocket = None

DEBUG_HOST = "127.0.0.1"
DEBUG_PORT = 9222
TESLA_HOST = "dashcam.tesla.com"


def is_receive_timeout(error: BaseException) -> bool:
    """A quiet CDP socket is normal while the user is choosing/uploading a file."""
    if isinstance(error, (TimeoutError, socket.timeout)):
        return True
    return websocket is not None and isinstance(error, websocket.WebSocketTimeoutException)


def extract_bearer(headers: dict) -> str | None:
    """Return only the token portion of a case-insensitive Authorization header."""
    for name, value in headers.items():
        if str(name).lower() != "authorization":
            continue
        value = str(value).strip()
        scheme, separator, token = value.partition(" ")
        if separator and scheme.lower() == "bearer" and token.strip():
            return token.strip()
    return None


def chrome_executable() -> Path | None:
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    return next((path for path in candidates if path.is_file()), None)


def debug_json(path: str) -> list | dict:
    response = requests.get(f"http://{DEBUG_HOST}:{DEBUG_PORT}{path}", timeout=2)
    response.raise_for_status()
    return response.json()


def debug_available() -> bool:
    try:
        debug_json("/json/version")
        return True
    except (OSError, requests.RequestException, ValueError):
        return False


def start_debug_chrome() -> subprocess.Popen:
    executable = chrome_executable()
    if executable is None:
        raise FileNotFoundError("Google Chrome 설치 경로를 찾지 못했습니다.")
    profile = Path(os.environ.get("LOCALAPPDATA", Path.cwd())) / "MyTeslaViewer" / "ChromeProfile"
    profile.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [
            str(executable),
            f"--remote-debugging-address={DEBUG_HOST}",
            f"--remote-debugging-port={DEBUG_PORT}",
            "--remote-allow-origins=http://localhost",
            f"--user-data-dir={profile}",
            "--new-window",
            "https://dashcam.tesla.com/",
        ],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_for_page(timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            pages = debug_json("/json/list")
            for page in pages:
                if page.get("type") == "page" and TESLA_HOST in page.get("url", ""):
                    return page
        except (OSError, requests.RequestException, ValueError):
            pass
        time.sleep(0.25)
    raise TimeoutError("Chrome에서 dashcam.tesla.com 탭을 찾지 못했습니다.")


class BearerWatcher:
    """Watch one explicitly connected Chrome page until a Tesla token appears."""

    def __init__(self, stop_event: threading.Event, status: Callable[[str], None], found: Callable[[str], None]):
        self.stop_event = stop_event
        self.status = status
        self.found = found
        self.browser_process: subprocess.Popen | None = None

    def run(self) -> None:
        if websocket is None:
            raise RuntimeError("websocket-client가 설치되어 있지 않습니다.")
        if not debug_available():
            self.status("전용 Chrome 창을 여는 중…")
            self.browser_process = start_debug_chrome()
            deadline = time.monotonic() + 20
            while not debug_available() and time.monotonic() < deadline and not self.stop_event.is_set():
                time.sleep(0.25)
        self.status("Chrome 탭을 기다리는 중…")
        page = wait_for_page()
        self.status("로그인 후 암호화 영상을 선택하거나 드래그하세요…")
        try:
            socket = websocket.create_connection(
                page["webSocketDebuggerUrl"], timeout=1, origin="http://localhost"
            )
        except Exception as first_error:
            # Chrome instances started before the allow-origins flag was added
            # may still reject the Origin header. Some versions accept a
            # no-Origin CDP handshake, so try that before showing a restart hint.
            try:
                socket = websocket.create_connection(
                    page["webSocketDebuggerUrl"], timeout=1, suppress_origin=True
                )
            except Exception as second_error:
                raise RuntimeError(
                    "Chrome가 localhost CDP 연결을 거부했습니다. "
                    "전용 Chrome 창을 모두 닫고 자동 감지를 다시 눌러 주세요. "
                    f"({second_error})"
                ) from first_error
        try:
            self._send(socket, 1, "Network.enable")
            while not self.stop_event.is_set():
                try:
                    raw = socket.recv()
                except Exception as exc:
                    if self.stop_event.is_set():
                        break
                    if is_receive_timeout(exc):
                        # No network event arrived during the socket timeout.
                        # Keep watching until the user's upload/Batch request.
                        continue
                    raise RuntimeError(f"Chrome 연결이 끊겼습니다: {exc}") from exc
                if not raw:
                    continue
                message = json.loads(raw)
                if message.get("method") != "Network.requestWillBeSent":
                    continue
                params = message.get("params", {})
                request = params.get("request", {})
                url = request.get("url", "")
                token = extract_bearer(request.get("headers", {}))
                if token and TESLA_HOST in url and "/api/" in url:
                    self.found(token)
                    self.status("Bearer 토큰을 감지했습니다.")
                    break
        finally:
            socket.close()

    @staticmethod
    def _send(socket, identifier: int, method: str) -> None:
        socket.send(json.dumps({"id": identifier, "method": method}))
