"""Google Flights datacenter-IP probe — does GH Actions / Azure get results or a CAPTCHA?

Google Flights is CASH fares (no award/miles). This one-shot probe answers the only open
question before we'd build a real scraper: a real headful Chrome session scrapes it cleanly
from a residential IP, but Google is aggressive about datacenter IPs and may serve an
"unusual traffic" / consent interstitial instead. Run as a manual workflow_dispatch and
inspect the uploaded screenshot + the printed verdict.

Self-contained: only needs nodriver (already in requirements.txt). Headful under xvfb on CI.
Exits 0 if structured flight rows were extracted, 1 otherwise (so the Actions run goes red on
a block). Saves /tmp/gflights_probe.png and /tmp/gflights_probe.html for the artifact upload.
"""

import asyncio
import json
import subprocess
import sys
import tempfile
import time
import urllib.request

import nodriver as uc
from nodriver.core.config import find_chrome_executable
from nodriver.core.util import free_port

ORIGIN, DEST, DATE = "SEA", "BOS", "2026-06-15"
URL = (
    f"https://www.google.com/travel/flights?q=Flights%20from%20{ORIGIN}%20to%20{DEST}"
    f"%20on%20{DATE}%20one%20way&curr=USD&hl=en&gl=US"
)
SHOT = "/tmp/gflights_probe.png"
HTML = "/tmp/gflights_probe.html"

EXTRACT_JS = r"""
(() => {
  const out = [];
  const items = document.querySelectorAll('li[aria-label], div[role="listitem"][aria-label]');
  for (const el of items) {
    const label = el.getAttribute('aria-label') || '';
    if (/dollar|US\$|\$\d/.test(label)) out.push(label);
  }
  const body = document.body ? document.body.innerText : '';
  // Detect Google's anti-bot interstitial / consent wall.
  const blocked = /unusual traffic|not a robot|detected unusual|systems have detected|before you continue|enable JavaScript/i.test(body)
                  && out.length === 0;
  return JSON.stringify({
    count: out.length,
    title: document.title,
    bodyLen: body.length,
    blocked,
    bodyHead: body.slice(0, 400),
    labels: out.slice(0, 6),
  });
})()
"""


async def run() -> int:
    port = free_port()
    profile = tempfile.mkdtemp(prefix="gflights_probe_")
    flags = [
        "--remote-allow-origins=*",
        "--remote-debugging-host=127.0.0.1",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-search-engine-choice-screen",
        "--homepage=about:blank",
        "--window-size=1400,1000",
        "--no-sandbox",  # required on CI (root)
        "--disable-dev-shm-usage",
    ]
    proc = subprocess.Popen(
        [find_chrome_executable(), *flags],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    try:
        ver = f"http://127.0.0.1:{port}/json/version"
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(ver, timeout=1).read()
                break
            except Exception:  # noqa: BLE001
                await asyncio.sleep(0.5)
        else:
            print("[probe] FAIL: Chrome CDP port never opened")
            return 1

        browser = await uc.start(host="127.0.0.1", port=port)
        print(f"[probe] navigating to: {URL}")
        tab = await browser.get(URL)
        await tab.sleep(3)

        # Try to dismiss a consent interstitial if present.
        for sel in ['button[aria-label*="Accept"]', 'button[aria-label*="agree"]', 'form button']:
            try:
                btn = await tab.select(sel, timeout=2)
                if btn:
                    await btn.click()
                    print(f"[probe] clicked consent: {sel}")
                    await tab.sleep(2)
                    break
            except Exception:  # noqa: BLE001
                pass

        data = {}
        for attempt in range(15):
            await tab.sleep(2)
            raw = await tab.evaluate(EXTRACT_JS, await_promise=False)
            data = json.loads(raw) if isinstance(raw, str) else {}
            print(
                f"[probe] attempt {attempt}: count={data.get('count')} "
                f"blocked={data.get('blocked')} bodyLen={data.get('bodyLen')} "
                f"title={data.get('title')!r}"
            )
            if data.get("count", 0) > 0 or data.get("blocked"):
                break

        try:
            await tab.save_screenshot(SHOT)
            html = await tab.get_content()
            with open(HTML, "w") as f:
                f.write(html)
            print(f"[probe] saved {SHOT} + {HTML}")
        except Exception as exc:  # noqa: BLE001
            print(f"[probe] artifact save error: {exc}")

        browser.stop()

        count = data.get("count", 0)
        if count > 0:
            print(f"\n[probe] ✅ SUCCESS from datacenter IP — {count} flight rows extracted:")
            for lbl in data.get("labels", []):
                print(f"   - {lbl[:120]}")
            return 0
        print("\n[probe] ❌ NO rows extracted (blocked or empty).")
        print(f"[probe] body head: {data.get('bodyHead')!r}")
        return 1
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
