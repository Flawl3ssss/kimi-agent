#!/usr/bin/env python3
"""One-shot UI preview: boot the mock server, shoot the console, exit.

Separate server + shooter kept failing here because background processes die
with their shell session; running both inside one process avoids that.
"""
import subprocess
import sys
import time
from pathlib import Path

import urllib.request
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
ARGS = ["--no-sandbox", "--disable-gpu", "--single-process", "--no-zygote",
        "--disable-dev-shm-usage", "--disable-software-rasterizer"]

srv = subprocess.Popen([sys.executable, str(ROOT / "scripts/mock_ui_server.py")],
                       cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(40):
        try:
            urllib.request.urlopen("http://127.0.0.1:8799/health", timeout=1).read()
            break
        except Exception:
            time.sleep(0.25)
    else:
        sys.exit("mock server never came up")

    shots = [("/tmp/ui-phone.png", 412, 915), ("/tmp/ui-desktop.png", 1280, 800)]
    with sync_playwright() as p:
        b = p.chromium.launch(args=ARGS)
        for out, w, h in shots:
            pg = b.new_page(viewport={"width": w, "height": h}, device_scale_factor=2)
            pg.goto("http://127.0.0.1:8799/", wait_until="networkidle")
            pg.click(".sessions li")          # open the session: events are filtered by it
            pg.wait_for_timeout(9000)         # let the scripted turn play out
            pg.screenshot(path=out, full_page=True)
            print("saved", out)
            pg.close()
        b.close()
finally:
    srv.terminate()
