"""Datacenter-IP award-extraction validation harness (throwaway recon, NOT a scraper). v2.

For each candidate no-login airline, on the GitHub Actions (Azure) IP: launch headful Chrome
(nodriver), warm the award-search page, inject a fetch()/XHR interceptor (via
addScriptToEvaluateOnNewDocument so it survives the search's navigation), best-effort DRIVE the
award search with robust cookie-dismissal + real keystrokes, and record the full request flow.
Then classify per airline: did any response carry real AWARD DATA (miles/points/Avios + a
number)? was there a SESSION-MINT call? — settling stateless-vs-session and proving end-to-end
extraction works on the Azure IP. Writes nothing to the DB; dumps captures + screenshots.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import time
import urllib.request

import nodriver as uc

INTERCEPT = r"""
(()=>{ if(window.__cap)return 'already'; window.__cap=[];
  const push=o=>{try{if(window.__cap.length<500)window.__cap.push(o);}catch(e){}};
  const of=window.fetch;
  if(of) window.fetch=function(){const a=arguments; let url='',m='GET';
    try{url=(a[0]&&a[0].url)?a[0].url:(''+a[0]); m=(a[1]&&a[1].method)||(a[0]&&a[0].method)||'GET';}catch(e){}
    return of.apply(this,a).then(r=>{try{r.clone().text().then(t=>push({k:'f',u:String(url).slice(0,200),m,s:r.status,n:(t||'').length,b:(t||'').slice(0,700)})).catch(()=>{});}catch(e){} return r;});};
  const oo=XMLHttpRequest.prototype.open, os=XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open=function(m,u){this.__m=m;this.__u=u;return oo.apply(this,arguments);};
  XMLHttpRequest.prototype.send=function(){const x=this; x.addEventListener('load',()=>{try{push({k:'x',u:String(x.__u).slice(0,200),m:x.__m,s:x.status,n:(x.responseText||'').length,b:(x.responseText||'').slice(0,700)});}catch(e){}}); return os.apply(this,arguments);};
  return 'installed';
})()
"""

FUTURE_DAY = "20"   # day-of-month to click in calendars (run date ~ mid-June 2026)


# ------------------------------------------------------------------ interaction helpers
async def click_exact(tab, *texts, allow_nav=False):
    """Click the most specific *clickable* element whose trimmed text/value/aria equals (then
    contains) one of `texts`. Skips elements inside <nav>/<header> megamenus unless allow_nav."""
    js = (
        "(()=>{const ts=" + json.dumps([t.lower() for t in texts]) + ";"
        "const sel='button,a,[role=button],[role=tab],[role=option],[role=radio],label,li,input[type=radio],input[type=checkbox],input[type=submit]';"
        "const els=[...document.querySelectorAll(sel)].filter(e=>{if(!e.offsetParent)return false;"
        + ("" if allow_nav else "if(e.closest('nav,header,[role=navigation]'))return false;") +
        "return true;});"
        "const txt=e=>((e.textContent||'')+' '+(e.value||'')+' '+(e.getAttribute&&(e.getAttribute('aria-label')||'')||'')).toLowerCase().replace(/\\s+/g,' ').trim();"
        "for(const t of ts){const e=els.find(x=>txt(x)===t);if(e){e.scrollIntoView({block:'center'});e.click();return 'exact:'+txt(e).slice(0,40);}}"
        "for(const t of ts){const m=els.filter(x=>txt(x).includes(t)&&txt(x).length<70).sort((a,b)=>txt(a).length-txt(b).length);"
        "if(m[0]){m[0].scrollIntoView({block:'center'});m[0].click();return 'contains:'+txt(m[0]).slice(0,40);}}return null;})()"
    )
    try:
        return await tab.evaluate(js)
    except Exception:
        return None


async def accept_cookies(tab):
    """Dismiss a consent banner, preferring privacy-preserving options. Retried (banners load
    late). Returns what it clicked, if anything."""
    for _ in range(4):
        r = await click_exact(
            tab, "i accept only necessary cookies", "accept only necessary cookies",
            "only necessary cookies", "reject all", "reject all cookies", "necessary cookies only",
            "i accept", "accept all cookies", "accept all", "accept", "agree", "ok", "got it",
            "confirm my choices", allow_nav=True,
        )
        if r:
            await tab.sleep(1.5)
            return r
        await tab.sleep(2)
    return None


async def click_field(tab, *labels):
    """Click an airport/date field by its label/placeholder, returning whether it focused an
    input. Matches input placeholder/aria, or a div/span field container by visible label."""
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
    """Type `text` into the currently-focused input via REAL keystrokes (CDP), which trigger
    React/autocomplete handlers the native-setter misses. Falls back to insert_text, then JS."""
    # real per-char key events to the focused element
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
        pass
    js = ("(()=>{const a=document.activeElement;if(!a||!('value' in a))return 'no';"
          "const s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value');"
          "if(s&&s.set)s.set.call(a," + json.dumps(text) + ");a.dispatchEvent(new Event('input',{bubbles:true}));return 'js';})()")
    try:
        return await tab.evaluate(js)
    except Exception:
        return None


async def fill_airport(tab, label, city, code):
    """click field by `label` -> type `city` (real keys) -> pick the option containing `code`."""
    await click_field(tab, label)
    await tab.sleep(1)
    await type_focused(tab, city)
    await tab.sleep(2.5)
    await click_exact(tab, f"({code})", code.lower(), city.lower(), allow_nav=True)
    await tab.sleep(1)


def classify(cap):
    award, session = [], []
    award_re = ("miles", "avios", '"points"', "milesamount", "awardprice", "milevalue",
                "redemption", "fareawards", "pointsprice", "milerequired", "awardfare")
    sess_re = ("/session", "/cart", "conversation", "shoppingid", "basketid", "createsession",
               "shoppingsession", "correlationid", "offercacheid", "shoppingcart", "/booking/init")
    skip = ("google", "doubleclick", "adsrvr", "facebook", "tiktok", "optimizely", "tealium",
            "qualtric", "onetrust", "px-cloud", "useinsider", "pisano", "demdex", "branch.io",
            "quantummetric", "kampyle", "sojern", "bing", "pinterest", "applicationinsights")
    for r in cap:
        u = (r.get("u") or "").lower()
        b = (r.get("b") or "").lower()
        s, n = r.get("s"), r.get("n") or 0
        if any(k in u for k in skip):
            continue
        if s == 200 and n > 400 and any(k in b for k in award_re) and any(c.isdigit() for c in b):
            award.append({"u": r.get("u"), "s": s, "n": n})
        if any(k in u for k in sess_re) or any(k in b[:400] for k in ("sessionid", "shoppingid", "conversationid", "basketid", "cartid")):
            session.append({"u": r.get("u"), "m": r.get("m"), "s": s})
    return award[:6], session[:6]


# ------------------------------------------------------------------ per-airline drivers
async def drv_turkish(tab):
    await accept_cookies(tab)
    await click_exact(tab, "award ticket - buy a ticket with miles", "award ticket")
    await tab.sleep(2)
    await fill_airport(tab, "to", "Istanbul", "IST")
    await click_field(tab, "dates", "departure", "select date")
    await tab.sleep(1)
    await click_exact(tab, FUTURE_DAY, allow_nav=True)
    await tab.sleep(1)
    await click_exact(tab, "ok", "done")
    await click_exact(tab, "search flights", "search")
    await tab.sleep(12)


async def drv_etihad(tab):
    await accept_cookies(tab)
    await fill_airport(tab, "flying to", "Abu Dhabi", "AUH")
    await click_exact(tab, "continue")
    await tab.sleep(1.5)
    await click_exact(tab, "continue")
    await tab.sleep(1.5)
    await click_exact(tab, "one way")
    await tab.sleep(0.5)
    await click_exact(tab, FUTURE_DAY, allow_nav=True)
    await tab.sleep(1)
    await click_exact(tab, "continue", "search")
    await tab.sleep(13)


async def drv_generic(tab):
    await accept_cookies(tab)
    await click_exact(tab, "redeem miles", "pay with miles", "book with miles", "use points",
                      "use miles", "search for reward seats", "rewards", "redeem", "award")
    await tab.sleep(1.5)
    await fill_airport(tab, "from", "New York", "JFK")
    await fill_airport(tab, "to", "London", "LHR")
    await click_field(tab, "dates", "departure", "when", "select date")
    await tab.sleep(1)
    await click_exact(tab, FUTURE_DAY, allow_nav=True)
    await tab.sleep(1)
    await click_exact(tab, "search", "find flight", "search flights", "continue")
    await tab.sleep(13)


DRIVERS = {
    "turkish":         ("https://www.turkishairlines.com/en-us/", drv_turkish),
    "etihad":          ("https://www.etihad.com/en-us/etihadguest/spend-miles/fly-with-miles", drv_etihad),
    "air_france":      ("https://www.airfrance.us/", drv_generic),
    "avianca":         ("https://www.lifemiles.com/fly/find", drv_generic),
    "cathay":          ("https://www.cathaypacific.com/cx/en_US/book-a-trip/redeem-flights/redeem-flight-awards.html", drv_generic),
    "qantas":          ("https://www.qantas.com/us/en/book-a-trip/flights.html", drv_generic),
    "virgin_atlantic": ("https://www.virginatlantic.com/", drv_generic),
    "tap":             ("https://www.flytap.com/en-us/", drv_generic),
}


async def main():
    from nodriver.core.config import find_chrome_executable
    from nodriver.core.util import free_port

    port = free_port()
    profile = tempfile.mkdtemp(prefix="testscrape_")
    flags = ["--remote-allow-origins=*", "--remote-debugging-host=127.0.0.1",
             f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
             "--no-first-run", "--no-default-browser-check", "--no-service-autorun",
             "--homepage=about:blank", "--no-pings", "--password-store=basic",
             "--disable-breakpad", "--disable-dev-shm-usage", "--disable-infobars",
             "--disable-session-crashed-bubble", "--disable-search-engine-choice-screen",
             "--disable-features=IsolateOrigins,site-per-process", "--no-sandbox"]
    proc = subprocess.Popen([find_chrome_executable(), *flags],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1).read(); break
        except Exception:
            await asyncio.sleep(0.5)
    browser = await uc.start(host="127.0.0.1", port=port)

    summary = []
    for name, (url, driver) in DRIVERS.items():
        rec = {"airline": name}
        try:
            tab = await browser.get("about:blank")
            try:
                await tab.send(uc.cdp.page.add_script_to_evaluate_on_new_document(INTERCEPT))
            except Exception as e:
                rec["inject_err"] = str(e)[:60]
            await tab.get(url)
            await tab.sleep(9)
            try:
                await tab.evaluate(INTERCEPT)
            except Exception:
                pass
            try:
                await driver(tab)
            except Exception as e:
                rec["drive_err"] = f"{type(e).__name__}: {str(e)[:90]}"
            try:
                raw = await tab.evaluate("JSON.stringify(window.__cap||[])")
                cap = json.loads(raw) if isinstance(raw, str) else []
            except Exception as e:
                cap = []; rec["cap_err"] = str(e)[:60]
            award, session = classify(cap)
            rec.update({"final_url": (await tab.evaluate("location.href") or "")[:95],
                        "xhr_count": len(cap), "award_data": bool(award),
                        "award_calls": award, "session_calls": session})
            try:
                with open(f"cap_{name}.json", "w") as f:
                    json.dump(cap, f)
                await tab.save_screenshot(f"scrape_{name}.png")
            except Exception:
                pass
        except Exception as e:
            rec["fatal"] = f"{type(e).__name__}: {str(e)[:100]}"
        print("RESULT " + json.dumps(rec), flush=True)
        summary.append(rec)

    print("\n===== SUMMARY =====", flush=True)
    for r in summary:
        print(f"{r['airline']:18} award_data={str(r.get('award_data')):5} "
              f"xhr={r.get('xhr_count','-'):>4} sess={len(r.get('session_calls',[]))} "
              f"url={r.get('final_url','')[:55]} {('DRVERR' if r.get('drive_err') else '')}", flush=True)
    try:
        browser.stop()
    except Exception:
        pass
    proc.terminate()


if __name__ == "__main__":
    uc.loop().run_until_complete(main())
