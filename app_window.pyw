"""Web Scraping as a desktop app: opens the dashboard in its own window - no CMD screen.

Double-click this file (or the "Web Scraping" desktop shortcut). It
* starts the local dashboard (127.0.0.1 only) inside this process, with no console window
* opens it as an app window (Microsoft Edge / Chrome "app mode": no tabs or address bar),
  or in your default browser when neither is installed
* stops by itself a couple of minutes after the last window closes - never while a scan is running -
  or right away with the "יציאה" (exit) button in the app

Output and errors go to logs/app.log.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOST, PORT = "127.0.0.1", 8765
URL = f"http://{HOST}:{PORT}/"
IDLE_SECONDS = 150          # minimised windows only poll about once a minute
FIRST_PAGE_SECONDS = 90     # the window must open within this time
BROWSERS = [
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
]


def _log_to_file() -> None:
    (HERE / "logs").mkdir(exist_ok=True)
    log = open(HERE / "logs" / "app.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - lives as long as the app
    sys.stdout = sys.stderr = log
    print(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} app started")


def _message(text: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, "Web Scraping", 0x10)
    except Exception:  # not on Windows
        print(text)


def _dashboard_answers() -> bool:
    try:
        with urllib.request.urlopen(URL + "api/ping", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _port_taken() -> bool:
    with socket.socket() as s:
        return s.connect_ex((HOST, PORT)) == 0


def _open_window() -> None:
    browser = next((b for b in BROWSERS if b.is_file()), None)
    if browser is None:
        webbrowser.open(URL)
        return
    # its own profile folder: a separate, clean app window that remembers its size and zoom
    subprocess.Popen([str(browser), f"--app={URL}", f"--user-data-dir={HERE / '.app-window'}",
                      "--window-size=1360,900", "--no-first-run", "--no-default-browser-check"])


def main() -> int:
    os.chdir(HERE)
    sys.path.insert(0, str(HERE))
    _log_to_file()

    if _dashboard_answers():          # already running (e.g. a second double-click): just show it
        _open_window()
        return 0
    if _port_taken():
        _message(f"Port {PORT} is used by another program, so the dashboard cannot start.\n"
                 "Close that program (or a dashboard started from CMD) and try again.")
        return 1

    import uvicorn

    from affiliate_scraper.dashboard.app import create_app

    holder: dict = {}

    def stop() -> None:
        holder["server"].should_exit = True

    app = create_app(str(HERE / "data" / "web_scraping.db"), automation_config=str(HERE / "automation.json"),
                     project_dir=str(HERE), on_shutdown=stop)
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="warning"))
    holder["server"] = server
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    for _ in range(60):
        if _dashboard_answers():
            break
        time.sleep(0.25)
    else:
        _message("The dashboard did not start. Details are in logs\\app.log.")
        return 1
    started = time.monotonic()
    app.state.last_ping = None        # our own readiness check is not a window
    _open_window()

    while thread.is_alive() and not server.should_exit:
        time.sleep(2)
        last = app.state.last_ping
        idle = time.monotonic() - (last if last is not None else started)
        limit = IDLE_SECONDS if last is not None else FIRST_PAGE_SECONDS
        if idle > limit and not app.state.busy():
            print("no open window - stopping")
            server.should_exit = True
    thread.join(timeout=10)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # pythonw has no console: say what happened
        import traceback
        traceback.print_exc()
        _message(f"Web Scraping could not start: {exc}\nDetails are in logs\\app.log.")
        sys.exit(1)
