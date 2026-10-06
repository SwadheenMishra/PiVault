"""Optional browser test of the web UI (needs: pip install playwright && playwright install chromium).

Run from the project root:  python tests/ui_test.py [screenshot_dir]
"""
import socket
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp())
SHOTS.mkdir(parents=True, exist_ok=True)
PASSED = 0


def check(name, cond):
    global PASSED
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        sys.exit(1)
    PASSED += 1


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_png(path, w, h, fn):
    raw = b"".join(b"\x00" + b"".join(bytes(fn(x, y, w, h)) for x in range(w)) for y in range(h))
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) \
        + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    Path(path).write_bytes(png)


def wait_js(page, expr, tries=60):
    """Poll with evaluate(): wait_for_function uses eval(), which our strict CSP forbids."""
    for _ in range(tries):
        if page.evaluate(expr):
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"timed out waiting for: {expr}")


def main():
    tmp = Path(tempfile.mkdtemp())
    src = tmp / "src"
    (src / "Holiday 2026" / "beach").mkdir(parents=True)
    make_png(src / "sunset.png", 320, 240, lambda x, y, w, h: (255 - y * 200 // h, 90 + x * 60 // w, 120 + y * 100 // h))
    make_png(src / "forest.png", 320, 240, lambda x, y, w, h: (30 + x * 40 // w, 120 + y * 100 // h, 70))
    make_png(src / "ocean.png", 320, 240, lambda x, y, w, h: (20, 80 + y * 60 // h, 160 + x * 90 // w))
    (src / "notes.txt").write_text("remember the sunscreen")
    (src / "<img src=x onerror=window.__xss=1>.txt").write_text("xss attempt")
    make_png(src / "Holiday 2026" / "beach" / "shell.png", 200, 200, lambda x, y, w, h: (240, 200 - y // 3, 190))
    (src / "Holiday 2026" / "itinerary.txt").write_text("day 1: beach")

    tcp, http = free_port(), free_port()
    server = subprocess.Popen(
        [sys.executable, str(ROOT / "server" / "server.py"), "--port", str(tcp), "--web-port", str(http),
         "--data-dir", str(tmp / "data"), "--invite-code", "family123"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{http}"
    for _ in range(60):
        try:
            socket.create_connection(("127.0.0.1", http), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)

    problems = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 820}, accept_downloads=True)
            page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") and "400 (Bad Request)" not in m.text else None)
            page.on("pageerror", lambda e: problems.append(str(e)))
            page.goto(base)

            print("auth")
            expect(page.locator("#auth")).to_be_visible()
            check("invite field hidden on sign-in tab", not page.locator("#row-invite").is_visible())
            page.screenshot(path=SHOTS / "1-auth.png")
            page.click("#tab-register")
            check("invite field appears on register tab", page.locator("#row-invite").is_visible())
            page.fill("#f-user", "dev"); page.fill("#f-pass", "password-1"); page.fill("#f-pass2", "different"); page.fill("#f-invite", "family123")
            page.click("#auth-submit")
            check("mismatched passwords caught", "don't match" in page.inner_text("#auth-error"))
            page.fill("#f-pass2", "password-1"); page.fill("#f-invite", "wrong")
            page.click("#auth-submit")
            expect(page.locator("#auth-error")).to_contain_text("invite")
            page.fill("#f-invite", "family123")
            page.click("#auth-submit")
            expect(page.locator("#app")).to_be_visible()
            check("registered and signed in", page.inner_text("#whoami") == "dev")
            expect(page.locator("#empty")).to_be_visible()
            page.screenshot(path=SHOTS / "2-empty.png")

            print("uploads")
            files = [str(src / n) for n in ("sunset.png", "forest.png", "ocean.png", "notes.txt", "<img src=x onerror=window.__xss=1>.txt")]
            page.set_input_files("#pick-files", files)
            expect(page.locator("#up-title")).to_contain_text("Uploaded 5 files")
            expect(page.locator(".item")).to_have_count(5)
            check("5 files listed", True)
            check("hostile filename shown as plain text, not executed",
                  page.evaluate("window.__xss") is None and page.locator(".item .name", has_text="onerror").count() == 1)

            print("folders")
            page.click("#btn-mkdir")
            page.fill("#dlg-input", "Documents")
            page.keyboard.press("Enter")
            expect(page.locator(".item .name", has_text="Documents")).to_be_visible()
            check("new-folder dialog submits with Enter", True)
            page.set_input_files("#pick-folder", str(src / "Holiday 2026"))
            expect(page.locator(".item .name", has_text="Holiday 2026")).to_be_visible(timeout=10000)
            check("folder upload keeps the folder", True)
            page.screenshot(path=SHOTS / "3-list.png")

            page.click("#v-grid")
            expect(page.locator(".files.grid")).to_be_visible()
            wait_js(page, "[...document.images].filter(i=>i.closest('.thumb')).every(i=>i.complete && i.naturalWidth>0)")
            check("image thumbnails load in grid view", page.locator(".thumb img").count() == 3)
            page.screenshot(path=SHOTS / "4-grid.png")

            print("navigate")
            page.locator(".item", has_text="Holiday 2026").click()
            expect(page.locator("#title")).to_have_text("Holiday 2026")
            expect(page.locator(".item .name", has_text="beach")).to_be_visible()
            page.locator(".item", has_text="beach").click()
            expect(page.locator(".item .name", has_text="shell.png")).to_be_visible()
            check("nested folders open", page.url.endswith("#Holiday%202026%2Fbeach"))
            page.go_back()
            expect(page.locator("#title")).to_have_text("Holiday 2026")
            check("browser back button works", True)
            page.click("#crumbs button:first-child")
            expect(page.locator("#title")).to_have_text("Your files")

            print("viewer")
            page.locator(".item", has_text="forest.png").click()
            expect(page.locator("#viewer")).to_be_visible()
            check("viewer opens", page.locator("#v-name").inner_text() == "forest.png")
            wait_js(page, "document.querySelector('#v-stage img').naturalWidth > 0")
            page.screenshot(path=SHOTS / "5-viewer.png")
            page.keyboard.press("ArrowRight")
            check("arrow key moves to the next image", page.locator("#v-name").inner_text() != "forest.png")
            page.keyboard.press("Escape")
            expect(page.locator("#viewer")).to_be_hidden()

            print("rename / delete / download")
            page.click("#v-list")
            row = page.locator(".item", has_text="notes.txt")
            row.hover()
            row.locator('button[title="Rename"]').click()
            page.fill("#dlg-input", "trip-notes.txt")
            page.keyboard.press("Enter")
            expect(page.locator(".item .name", has_text="trip-notes.txt")).to_be_visible()
            check("rename works", True)
            row = page.locator(".item", has_text="trip-notes.txt")
            row.locator('button[title="Delete"]').click()
            page.screenshot(path=SHOTS / "6-dialog.png")
            page.click("#dlg-ok")
            expect(page.locator(".item .name", has_text="trip-notes.txt")).to_have_count(0)
            check("delete works", True)
            with page.expect_download() as dl:
                page.locator(".item", has_text="Holiday 2026").locator('button[title="Download as zip"]').click()
            check("folder downloads as zip", dl.value.suggested_filename == "Holiday 2026.zip")
            usage = page.inner_text("#usage-text")
            check("usage meter updates", "0 B of" not in usage)

            print("session + mobile")
            page.reload()
            expect(page.locator("#app")).to_be_visible()
            check("session survives reload", page.inner_text("#whoami") == "dev")
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=SHOTS / "7-mobile-list.png")
            page.click("#v-grid")
            page.wait_for_timeout(300)
            page.screenshot(path=SHOTS / "8-mobile-grid.png")
            overflow = page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth")
            check("no horizontal scroll on phone", not overflow)
            page.click("#btn-logout")
            expect(page.locator("#auth")).to_be_visible()
            check("sign out returns to login", True)
            browser.close()

        check("no console errors / CSP violations", not problems)
        if problems:
            print(problems)
        print(f"\nall {PASSED} checks passed; screenshots in {SHOTS}")
    finally:
        server.terminate()
        server.wait()
        if problems:
            print("console problems:", problems)


if __name__ == "__main__":
    main()
