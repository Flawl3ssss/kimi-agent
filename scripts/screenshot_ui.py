#!/usr/bin/env python3
"""Screenshot the real console UI at phone width via headless chromium.

Chromium in this guest only survives with --single-process --no-zygote (no
/dev/shm, no sandbox), so the flags are pinned here rather than guessed per run.
"""
import sys
from playwright.sync_api import sync_playwright

ARGS = ["--no-sandbox", "--disable-gpu", "--single-process", "--no-zygote",
        "--disable-dev-shm-usage", "--disable-software-rasterizer"]

url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8799/"
out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/ui-phone.png"
width = int(sys.argv[3]) if len(sys.argv) > 3 else 412
height = int(sys.argv[4]) if len(sys.argv) > 4 else 915

with sync_playwright() as p:
    b = p.chromium.launch(args=ARGS)
    pg = b.new_page(viewport={"width": width, "height": height}, device_scale_factor=2)
    pg.goto(url, wait_until="networkidle")
    # app.js filters events by the active session, so open one first, exactly
    # like a user would; the mock delays its script until we have done so.
    pg.click(".sessions li")
    pg.wait_for_timeout(9000)
    pg.screenshot(path=out, full_page=(height == 0))
    print("saved", out)
    b.close()
