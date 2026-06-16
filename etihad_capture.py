"""Full-body Etihad Guest award-API capture (throwaway recon, NOT a scraper).

Runs on the GH Actions / Azure IP (where Etihad's Amadeus DXP flow clears Akamai+Imperva — a
residential CDP session gets the "Pardon Our Interruption" ABP block on a top-level nav). Warms
the www.etihad.com "Fly with miles" page, installs a fetch()/XHR interceptor that records the
FULL request (method/url/headers/body) and FULL response body, drives a JFK->AUH award search,
then post-processes the capture to surface:
  * the availability endpoint(s): method, url, request headers, request JSON payload
  * a structural skeleton of the award response + every JSON path whose value looks like miles
    (so we can see WHERE per-cabin pricing lives before building scrapers/etihad.py)

Dumps cap_etihad_full.json (artifact) and prints a compact, log-readable summary to stdout.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import time
import urllib.request

import nodriver as uc

WARM_URL = "https://www.etihad.com/en-us/etihadguest/spend-miles/fly-with-miles"
ORIGIN_CITY, ORIGIN_CODE = "New York", "JFK"
DEST_CITY, DEST_CODE = "Abu Dhabi", "AUH"
FUTURE_DAY = "22"  # day-of-month to click in the calendar (run ~mid-June 2026)

# digital.etihad.com root warms the Amadeus DXP Imperva/Akamai session (clears clean on the
# Azure IP); then we top-nav to the award deep-link (the exact param set the real "Fly with
# miles" widget redirects to) and let the SPA fire its availability call, which the persistent
# add_script_to_evaluate_on_new_document interceptor captures on the results document.
DIGITAL_ROOT = "https://digital.etihad.com/"
DEEPLINK = (
    "https://digital.etihad.com/book/search?LANGUAGE=EN&CHANNEL=DESKTOP"
    "&B_LOCATION=JFK&E_LOCATION=AUH&TRIP_TYPE=O&CABIN=E&TRAVELERS=ADT"
    "&TRIP_FLOW_TYPE=AVAILABILITY&SITE_EDITION=EN-US&DATE_1=202607080000"
    "&WDS_ENABLE_MILES_TOGGLE=TRUE&FLOW=AWARD"
)

# Full-fidelity interceptor with sessionStorage persistence — the Amadeus award flow top-navigates
# (/book/search → /book/cart-new/upsell), which resets an in-memory array, so we persist captures
# in sessionStorage (same-origin across the whole digital.etihad.com nav chain) and read it at the
# end. Captures request headers + body and the COMPLETE response text (body capped to keep under
# the sessionStorage quota).
INTERCEPT = r"""
(()=>{ const KEY='__ppcap';
  try{ window.__cap = JSON.parse(sessionStorage.getItem(KEY)||'[]'); }catch(e){ window.__cap=[]; }
  if(window.__ppPatched) return 'already'; window.__ppPatched=true;
  const save=()=>{try{sessionStorage.setItem(KEY, JSON.stringify(window.__cap));}catch(e){}};
  const push=o=>{try{ if((o.b||'').length>300000) o.b=o.b.slice(0,300000)+'…[trunc]';
    if(window.__cap.length<400){window.__cap.push(o);save();}}catch(e){}};
  const hdrs=hh=>{const r={};try{if(!hh)return r;
    if(hh.forEach){hh.forEach((v,k)=>r[k]=v);} else if(Array.isArray(hh)){hh.forEach(p=>r[p[0]]=p[1]);}
    else {for(const k in hh)r[k]=hh[k];}}catch(e){}return r;};
  const of=window.fetch;
  if(of) window.fetch=function(){const a=arguments; let url='',m='GET',rb='',rh={};
    try{url=(a[0]&&a[0].url)?a[0].url:(''+a[0]); m=(a[1]&&a[1].method)||(a[0]&&a[0].method)||'GET';
      rb=(a[1]&&a[1].body)?String(a[1].body):''; rh=hdrs((a[1]&&a[1].headers)||(a[0]&&a[0].headers));}catch(e){}
    return of.apply(this,a).then(r=>{try{r.clone().text().then(t=>push({k:'f',u:String(url),m,rh,rb,s:r.status,n:(t||'').length,b:t||''})).catch(()=>{});}catch(e){} return r;});};
  const oo=XMLHttpRequest.prototype.open, os=XMLHttpRequest.prototype.send, osh=XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.open=function(m,u){this.__m=m;this.__u=u;this.__rh={};return oo.apply(this,arguments);};
  XMLHttpRequest.prototype.setRequestHeader=function(k,v){try{this.__rh[k]=v;}catch(e){} return osh.apply(this,arguments);};
  XMLHttpRequest.prototype.send=function(bd){const x=this; x.__rb=bd?String(bd):'';
    x.addEventListener('load',()=>{try{push({k:'x',u:String(x.__u),m:x.__m,rh:x.__rh||{},rb:x.__rb,s:x.status,n:(x.responseText||'').length,b:x.responseText||''});}catch(e){}});
    return os.apply(this,arguments);};
  return 'installed';
})()
"""

BLOCK = ["sign in", "sign-in", "log in", "login", "register", "join ", "join now", "help",
         "my account", "contact us", "manage my", "manage booking", "logout", "sign up", "sign out"]


async def click_exact(tab, *texts, allow_nav=False):
    block = [b for b in BLOCK if not any(b in t.lower() for t in texts)]
    js = (
        "(()=>{const ts=" + json.dumps([t.lower() for t in texts]) + ";"
        "const block=" + json.dumps(block) + ";"
        "const sel='button,a,[role=button],[role=tab],[role=option],[role=radio],label,li,input[type=radio],input[type=checkbox],input[type=submit]';"
        "const txt=e=>((e.textContent||'')+' '+(e.value||'')+' '+(e.getAttribute&&(e.getAttribute('aria-label')||'')||'')).toLowerCase().replace(/\\s+/g,' ').trim();"
        "const els=[...document.querySelectorAll(sel)].filter(e=>{if(!e.offsetParent)return false;"
        + ("" if allow_nav else "if(e.closest('nav,header,[role=navigation]'))return false;") +
        "const t=txt(e);if(block.some(b=>t.includes(b)))return false;return true;});"
        "for(const t of ts){const e=els.find(x=>txt(x)===t);if(e){e.scrollIntoView({block:'center'});e.click();return 'exact:'+txt(e).slice(0,40);}}"
        "for(const t of ts){const m=els.filter(x=>txt(x).includes(t)&&txt(x).length<70).sort((a,b)=>txt(a).length-txt(b).length);"
        "if(m[0]){m[0].scrollIntoView({block:'center'});m[0].click();return 'contains:'+txt(m[0]).slice(0,40);}}return null;})()"
    )
    try:
        return await tab.evaluate(js)
    except Exception:
        return None


async def accept_cookies(tab):
    for _ in range(4):
        r = await click_exact(
            tab, "i accept only necessary cookies", "accept only necessary cookies",
            "only necessary cookies", "reject all cookies", "reject all", "necessary cookies only",
            "accept all cookies", "accept all", "confirm my choices", "allow all cookies",
            allow_nav=True,
        )
        if r:
            await tab.sleep(1.5)
            return r
        await tab.sleep(2)
    return None


async def click_field(tab, *labels):
    js = (
        "(()=>{const ls=" + json.dumps([t.lower() for t in labels]) + ";"
        "const lab=e=>((e.placeholder||'')+' '+(e.getAttribute&&(e.getAttribute('aria-label')||'')||'')+' '+(e.textContent||'')).toLowerCase().replace(/\\s+/g,' ').trim();"
        "const inputs=[...document.querySelectorAll('input,textarea')].filter(e=>e.offsetParent);"
        "for(const l of ls){const e=inputs.find(x=>((x.placeholder||'')+' '+(x.getAttribute('aria-label')||'')).toLowerCase().includes(l));"
        "if(e){e.scrollIntoView({block:'center'});e.focus();e.click();return 'input:'+l;}}"
        "const divs=[...document.querySelectorAll('div,span,button,[role=combobox],[role=button]')].filter(e=>e.offsetParent&&!e.closest('nav,header'));"
        "for(const l of ls){const m=divs.filter(x=>lab(x).startsWith(l)&&lab(x).length<40).sort((a,b)=>lab(a).length-lab(b).length);"
        "if(m[0]){m[0].scrollIntoView({block:'center'});m[0].click();return 'div:'+l;}}return null;})()"
    )
    try:
        return await tab.evaluate(js)
    except Exception:
        return None


async def type_focused(tab, text):
    try:
        el = await tab.select("input:focus, textarea:focus")
        if el:
            await el.send_keys(text)
            return "sendkeys"
    except Exception:
        pass
    try:
        await tab.send(uc.cdp.input_.insert_text(text=text))
        return "insert"
    except Exception:
        return None


async def fill_airport(tab, labels, city, code):
    """labels: list of candidate field labels to focus, then type `city` + pick option `code`."""
    await click_field(tab, *labels)
    await tab.sleep(1)
    await type_focused(tab, city)
    await tab.sleep(2.5)
    await click_exact(tab, f"({code})", code.lower(), city.lower(), allow_nav=True)
    await tab.sleep(1)


async def diag(tab, stage):
    """Log-readable snapshot of where the drive is: url, title, visible buttons/fields."""
    js = (
        "(()=>{const vis=e=>e.offsetParent;"
        "const btn=[...document.querySelectorAll('button,a[role=button],[role=tab],input[type=submit]')]"
        ".filter(vis).map(e=>((e.textContent||'')+' '+(e.value||'')+' '+(e.getAttribute('aria-label')||''))"
        ".replace(/\\s+/g,' ').trim()).filter(Boolean).slice(0,30);"
        "const fld=[...document.querySelectorAll('input,[role=combobox]')].filter(vis)"
        ".map(e=>((e.placeholder||'')+'|'+(e.getAttribute('aria-label')||'')).slice(0,40)).filter(s=>s!=='|').slice(0,15);"
        "return JSON.stringify({href:location.href.slice(0,110),title:document.title.slice(0,50),"
        "bodyLen:(document.body?document.body.innerText.length:0),buttons:btn,fields:fld});})()"
    )
    try:
        r = await tab.evaluate(js)
        print(f"[DIAG {stage}] {r}", flush=True)
    except Exception as e:
        print(f"[DIAG {stage}] eval_err {type(e).__name__}: {str(e)[:80]}", flush=True)
    try:
        await tab.save_screenshot(f"etihad_{stage}.png")
    except Exception:
        pass


async def drive_etihad(tab):
    """Best-effort drive of the JFK->AUH one-way award search. Handles both the desktop
    single-panel form and the step-wizard layout; heavily instrumented so a failed drive still
    reveals the form shape for the next iteration."""
    await accept_cookies(tab)
    await diag(tab, "00warm")
    await click_exact(tab, "one way")
    await tab.sleep(1)
    await fill_airport(tab, ["flying from", "from", "origin", "leaving from", "where from"],
                       ORIGIN_CITY, ORIGIN_CODE)
    await fill_airport(tab, ["flying to", "to", "destination", "going to", "where to"],
                       DEST_CITY, DEST_CODE)
    await diag(tab, "01airports")
    # wizard layouts gate the date step behind a continue
    await click_exact(tab, "continue", "next")
    await tab.sleep(1.5)
    await click_field(tab, "dates", "departure", "depart", "select date", "when", "travel date")
    await tab.sleep(1)
    await click_exact(tab, FUTURE_DAY, allow_nav=True)
    await tab.sleep(1)
    await click_exact(tab, "confirm", "done", "ok", "apply")
    await diag(tab, "02date")
    await click_exact(tab, "search", "search flights", "find flights", "show flights",
                      "continue", "let's go")
    await tab.sleep(8)
    await click_exact(tab, "search", "search flights", "find flights", "continue")  # 2nd nudge
    await tab.sleep(22)  # availability is slow (session/cart + Amadeus pricing)
    await diag(tab, "03results")


# --------------------------------------------------------------- post-processing
SKIP = ("google", "doubleclick", "adsrvr", "facebook", "tiktok", "optimizely", "tealium",
        "qualtric", "onetrust", "px-cloud", "useinsider", "pisano", "demdex", "branch.io",
        "quantummetric", "kampyle", "sojern", "bing", "pinterest", "applicationinsights",
        "datadog", "linkedin", "akstat", "akam", "/akam/", "boomerang", "mpulse",
        "newrelic", "nr-data", "cdn-cgi", "fonts.", ".woff", ".css", ".js?", ".svg", ".png",
        # reference data, not availability (these are airport lists — false award positives)
        "coredata", "search-panel", "/origins/", "/destinations/", "/airports", "/stations")
# Require an actual award-miles signal, not generic "amount/price/total" that match everything.
AWARD_KW = ("mile", "avios", "milesamount", "fareawards", "rewardseat", "pointsprice",
            "awardprice", "redeemmiles", "milevalue", "redemption")


def is_interesting(u: str) -> bool:
    ul = u.lower()
    if ul.startswith("blob:") or ul.startswith("data:"):
        return False
    return not any(k in ul for k in SKIP)


def looks_award(body: str) -> bool:
    bl = body.lower()
    return any(k in bl for k in AWARD_KW) and any(c.isdigit() for c in body)


def skeleton(obj, depth=0, max_depth=6):
    """Compact structural skeleton: keys + leaf sample values, arrays collapsed to [0]."""
    pad = "  " * depth
    if depth > max_depth:
        return pad + "...\n"
    if isinstance(obj, dict):
        out = ""
        for k, v in list(obj.items())[:40]:
            if isinstance(v, (dict, list)):
                out += f"{pad}{k}:\n" + skeleton(v, depth + 1, max_depth)
            else:
                sv = repr(v)
                if len(sv) > 60:
                    sv = sv[:60] + "…"
                out += f"{pad}{k}: {sv}\n"
        return out
    if isinstance(obj, list):
        out = f"{pad}[{len(obj)} items]\n"
        if obj:
            out += skeleton(obj[0], depth + 1, max_depth)
        return out
    return pad + repr(obj)[:60] + "\n"


def find_mile_paths(obj, path="", out=None):
    """Every JSON path whose key/value smells like an award-miles figure (number > 1000)."""
    if out is None:
        out = []
    if len(out) > 80:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}"
            kl = str(k).lower()
            if isinstance(v, (int, float)) and v > 1000 and any(
                t in kl for t in ("mile", "point", "amount", "fare", "price", "total")
            ):
                out.append(f"{p} = {v}")
            find_mile_paths(v, p, out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:5]):
            find_mile_paths(v, f"{path}[{i}]", out)
    return out


def analyze(cap):
    interesting = [r for r in cap if is_interesting(r.get("u", ""))]
    award = [r for r in interesting if r.get("s") == 200 and r.get("n", 0) > 400
             and looks_award(r.get("b", ""))]
    award.sort(key=lambda r: r.get("n", 0), reverse=True)

    print(f"\n===== CAPTURE: {len(cap)} total, {len(interesting)} interesting, "
          f"{len(award)} award-bearing =====\n", flush=True)

    print("--- interesting endpoints (non-asset, non-analytics) ---", flush=True)
    for r in interesting[:60]:
        u = r.get("u", "")
        print(f"  {r.get('m'):4} {r.get('s')} n={r.get('n'):>7}  {u[:140]}", flush=True)

    for idx, r in enumerate(award[:3]):
        print(f"\n========== AWARD CALL #{idx} ==========", flush=True)
        print(f"METHOD: {r.get('m')}   STATUS: {r.get('s')}   LEN: {r.get('n')}", flush=True)
        print(f"URL: {r.get('u')}", flush=True)
        print(f"REQ HEADERS: {json.dumps(r.get('rh', {}))[:800]}", flush=True)
        rb = r.get("rb", "")
        print(f"REQ BODY ({len(rb)} chars): {rb[:1500]}", flush=True)
        try:
            data = json.loads(r.get("b", ""))
        except Exception:
            print("  (response not JSON)", flush=True)
            continue
        print("--- RESPONSE SKELETON (depth<=6) ---", flush=True)
        print(skeleton(data)[:6000], flush=True)
        print("--- MILE-LIKE PATHS ---", flush=True)
        for line in find_mile_paths(data)[:60]:
            print("  " + line, flush=True)


async def main():
    from nodriver.core.config import find_chrome_executable
    from nodriver.core.util import free_port

    port = free_port()
    profile = tempfile.mkdtemp(prefix="etihadcap_")
    flags = ["--remote-allow-origins=*", "--remote-debugging-host=127.0.0.1",
             f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
             "--no-first-run", "--no-default-browser-check", "--no-service-autorun",
             "--homepage=about:blank", "--no-pings", "--password-store=basic",
             "--disable-breakpad", "--disable-dev-shm-usage", "--disable-infobars",
             "--disable-session-crashed-bubble", "--disable-search-engine-choice-screen",
             "--disable-features=IsolateOrigins,site-per-process", "--no-sandbox",
             "--window-size=1440,900", "--start-maximized"]
    proc = subprocess.Popen([find_chrome_executable(), *flags],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1).read()
            break
        except Exception:
            await asyncio.sleep(0.5)
    browser = await uc.start(host="127.0.0.1", port=port)

    cap = []
    try:
        tab = await browser.get("about:blank")
        try:
            await tab.send(uc.cdp.page.add_script_to_evaluate_on_new_document(INTERCEPT))
        except Exception as e:
            print("inject_err", str(e)[:80], flush=True)
        # warm the Amadeus DXP host so Imperva/Akamai trust the session
        await tab.get(DIGITAL_ROOT)
        await tab.sleep(12)
        await diag(tab, "00warm_digital")
        # top-nav to the award deep-link; persistent interceptor reinstalls on the results doc
        try:
            await tab.get(DEEPLINK)
        except Exception as e:
            print(f"NAV_ERR {type(e).__name__}: {str(e)[:120]}", flush=True)
        await tab.sleep(30)  # availability is slow (session/cart + Amadeus pricing)
        try:
            await tab.evaluate(INTERCEPT)  # idempotent re-install in case the doc was fresh
        except Exception:
            pass
        await tab.sleep(6)
        await diag(tab, "01results")
        # fallback: if the deep-link got Imperva-blocked, drive the widget wizard instead
        try:
            blocked = await tab.evaluate(
                "/Pardon Our Interruption/i.test(document.documentElement.innerHTML)"
            )
        except Exception:
            blocked = False
        if blocked:
            print("DEEPLINK_BLOCKED → falling back to widget drive", flush=True)
            try:
                await tab.get(WARM_URL)
                await tab.sleep(9)
                await drive_etihad(tab)
            except Exception as e:
                print(f"DRIVE_ERR {type(e).__name__}: {str(e)[:120]}", flush=True)
        try:
            final_url = await tab.evaluate("location.href")
            print(f"FINAL_URL: {str(final_url)[:120]}", flush=True)
        except Exception:
            pass
        try:
            raw = await tab.evaluate("sessionStorage.getItem('__ppcap')||'[]'", await_promise=False)
            cap = json.loads(raw) if isinstance(raw, str) else []
        except Exception as e:
            print("cap_read_err", str(e)[:80], flush=True)
        # DOM pricing as a backup signal (proves the page rendered award fares even if the XHR
        # capture misses): every "Miles 83,625 + USD 225" token on the results page.
        try:
            dom = await tab.evaluate(
                "JSON.stringify([...document.querySelectorAll('*')].map(e=>e.textContent||'')"
                ".filter(t=>/Miles[\\s\\S]{0,4}[0-9]/.test(t)&&t.length<60).slice(0,30))"
            )
            print(f"DOM_PRICES: {dom}", flush=True)
        except Exception:
            pass
        try:
            await tab.save_screenshot("etihad_capture.png")
        except Exception:
            pass
    finally:
        try:
            browser.stop()
        except Exception:
            pass
        proc.terminate()

    with open("cap_etihad_full.json", "w") as f:
        json.dump(cap, f)
    analyze(cap)
    print("\n=== DONE ===", flush=True)


if __name__ == "__main__":
    uc.loop().run_until_complete(main())
