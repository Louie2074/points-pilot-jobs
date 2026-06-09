"""American Airlines award session-mint datacenter-IP probe — does the Azure/GH-Actions IP
clear Akamai for the *mint*, or only for reads?

Background (2026-06-08 Claude-in-Chrome recon, residential IP — see
docs/superpowers/specs/2026-06-08-american-mint-recon-findings.md in the workspace):
AA's award search session is minted by a **document-level form POST** to
`/booking/find-flights` with a flat x-www-form-urlencoded body (NOT JSON, NOT an in-page
fetch — a fetch POST gets bounced to /booking/search). The server 302s to
`/booking/choose-flights/1?sid=<uuid>` and **server-renders the award data into a
`<script id="ng-state">` blob** (`SearchData.itineraryResult.slices[]`). No separate JSON API.

That whole sequence was proven from a residential IP. This probe answers the ONE open
question before building a real scraper: **does the document-POST mint survive from the
Azure/GH-Actions runner IP** (Akamai already clears Delta *reads* there), or does Akamai
serve an Access-Denied / challenge to a state-changing nav?

Replay primitive (exactly what a real scraper would do): warm aa.com, then inject a hidden
`<form method=POST action=/booking/find-flights>` and call `form.submit()` — a genuine
document navigation. Read `#ng-state` off the resulting page.

Self-contained: only needs nodriver (already in requirements.txt). Headful under xvfb on CI.
Writes NOTHING to MotherDuck. Exits 0 if award slices were extracted (mint cleared on this
IP), 1 otherwise. Saves /tmp/aa_mint_probe.png + .html for the artifact upload.
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

ORIGIN, DEST, DEPART = "SEA", "JFK", "07/29/2026"  # mm/dd/yyyy, ~7 weeks out
WARM_URL = "https://www.aa.com/homePage.do"
SHOT = "/tmp/aa_mint_probe.png"
HTML = "/tmp/aa_mint_probe.html"

# Inject + submit a hidden form = a real top-level document POST (the mint). The flat field
# set is the exact body the live homepage search form sends (redeemMiles=true => award).
SUBMIT_JS = r"""
(() => {
  const fields = {
    flight:'flight', tripType:'oneWay', redeemMiles:'true',
    originAirport:'%s', destinationAirport:'%s',
    adultOrSeniorPassengerCount:'1', departDate:'%s', returnDate:'',
    serviceclass:'coach', dateFormat:'mm/dd/yyyy', showMoreOptions:'false',
    fromSearchPage:'true', accountId:''
  };
  const f = document.createElement('form');
  f.method = 'POST';
  f.action = 'https://www.aa.com/booking/find-flights';
  for (const [k, v] of Object.entries(fields)) {
    const i = document.createElement('input');
    i.type = 'hidden'; i.name = k; i.value = v; f.appendChild(i);
  }
  document.body.appendChild(f);
  f.submit();
  return 'submitted';
})()
""" % (ORIGIN, DEST, DEPART)

# Read the SSR award data out of the choose-flights page + detect an Akamai wall.
EXTRACT_JS = r"""
(() => {
  const body = document.body ? document.body.innerText : '';
  const url = location.href;
  const onChoose = /choose-flights/.test(url);
  const blocked = /Access Denied|Reference&#32;\d|\bReference #\d|您的访问|did not match|unusual/i.test(body);
  let sliceCount = 0, sampleMiles = [], parseErr = null;
  const el = document.getElementById('ng-state');
  if (el) {
    try {
      const slices = JSON.parse(el.textContent).SearchData.itineraryResult.slices || [];
      sliceCount = slices.length;
      sampleMiles = (slices.slice(0, 3).flatMap(s =>
        (s.pricingDetail || []).map(p => p.perPassengerAwardPoints))
      ).filter(m => m && m > 0).slice(0, 6);
    } catch (e) { parseErr = String(e).slice(0, 120); }
  }
  return JSON.stringify({
    onChooseFlights: onChoose, hasNgState: !!el, sliceCount, sampleMiles,
    blocked, parseErr, title: document.title, path: location.pathname,
    bodyLen: body.length, bodyHead: body.slice(0, 300),
  });
})()
"""


async def run() -> int:
    port = free_port()
    profile = tempfile.mkdtemp(prefix="aa_mint_probe_")
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

        # 1) Warm aa.com so Akamai seeds _abck / bm_* / UAC on this IP.
        print(f"[probe] warming: {WARM_URL}")
        tab = await browser.get(WARM_URL)
        await tab.sleep(8)

        # 2) Mint: submit the hidden form (real document POST -> 302 -> choose-flights).
        print(f"[probe] minting via document POST: {ORIGIN}->{DEST} {DEPART} (award)")
        try:
            await tab.evaluate(SUBMIT_JS, await_promise=False)
        except Exception as exc:  # noqa: BLE001 — navigation tears down the JS context; expected
            print(f"[probe] submit eval returned (nav teardown ok): {exc!r}")

        # 3) Read ng-state off the resulting page (retry while the SSR settles).
        data = {}
        for attempt in range(12):
            await tab.sleep(2)
            try:
                raw = await tab.evaluate(EXTRACT_JS, await_promise=False)
            except Exception as exc:  # noqa: BLE001 — context may still be swapping
                print(f"[probe] attempt {attempt}: evaluate retry ({exc!r})")
                continue
            data = json.loads(raw) if isinstance(raw, str) else {}
            print(
                f"[probe] attempt {attempt}: path={data.get('path')!r} "
                f"ngState={data.get('hasNgState')} slices={data.get('sliceCount')} "
                f"blocked={data.get('blocked')} title={data.get('title')!r}"
            )
            if data.get("sliceCount", 0) > 0 or data.get("blocked"):
                break

        try:
            await tab.save_screenshot(SHOT)
            with open(HTML, "w") as fh:
                fh.write(await tab.get_content())
            print(f"[probe] saved {SHOT} + {HTML}")
        except Exception as exc:  # noqa: BLE001
            print(f"[probe] artifact save error: {exc}")

        browser.stop()

        slices = data.get("sliceCount", 0)
        if slices > 0:
            print(
                f"\n[probe] ✅ MINT CLEARED on Azure IP — {slices} award slices extracted. "
                f"Sample miles: {data.get('sampleMiles')}"
            )
            print("[probe] => Phase B is GO: document-POST mint survives the GH-Actions IP.")
            return 0
        if data.get("blocked"):
            print(
                "\n[probe] ❌ AKAMAI BLOCK on Azure IP (Access-Denied / challenge on the mint nav)."
            )
        elif data.get("path", "").endswith("/booking/search") or not data.get("onChooseFlights"):
            print(
                "\n[probe] ⚠️  Mint did not take — bounced to "
                f"{data.get('path')!r} (no choose-flights / no sid). Not an Akamai wall; "
                "the nav was rejected (warm dwell? field set? document-nav?)."
            )
        else:
            print("\n[probe] ❌ No ng-state slices extracted (empty / unexpected page).")
        print(f"[probe] parseErr={data.get('parseErr')!r} bodyHead={data.get('bodyHead')!r}")
        return 1
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
