"""Cathay Asia Miles award-API capture (throwaway recon, NOT a scraper).

Runs on the GH Actions / Azure IP. Warms Cathay's "Redeem Flight Awards" page (hosts the
redibe-v3 redemption widget), installs a page-context fetch()/XHR interceptor AND a CDP
Network-layer capture (bypassing the service worker so SW-handled requests hit the network
stack), drives a JFK->HKG one-way award search (which submits to flights.cathaypacific.com),
then post-processes both captures to surface the availability call — the recon says it's served
by api.cathaypacific.com — its url, request payload, and where the per-cabin miles live.

Dumps cap_virgin_full.json + cap_virgin_net.json (artifacts) and a log-readable summary to stdout.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import time
import urllib.request

import nodriver as uc

# Cathay's "Redeem Flight Awards" page hosts the redibe-v3 redemption booking widget (From /
# Going to / One way). Filling + searching it submits to the flights.cathaypacific.com IBE, whose
# availability is served by api.cathaypacific.com — the CDP Network capture (below) catches those
# calls regardless of how they're fetched.
WARM_URL = "https://www.virginatlantic.com/"
ORIGIN_CITY, ORIGIN_CODE = "New York", "JFK"
DEST_CITY, DEST_CODE = "London", "LHR"
FUTURE_DAY = "22"  # day-of-month to click in the calendar (run ~mid-June 2026)
# Virgin's date picker day cells are button[data-cy="day-component-YYYY-MM-DD"] — target exact dates
FUTURE_DEP = "2026-07-15"
FUTURE_RET = "2026-07-22"

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
        await tab.save_screenshot(f"cathay_{stage}.png")
    except Exception:
        pass


async def _fill_redibe(tab, input_id, city, code):
    """Type `city` into a redibe input by id, wait for the autocomplete, then pick option `code`."""
    js = (
        "(async()=>{const el=document.getElementById(" + json.dumps(input_id) + ");"
        "if(!el)return 'no-input';el.scrollIntoView({block:'center'});el.focus();el.click();return 'focused';})()"
    )
    await tab.evaluate(js, await_promise=True)
    await tab.sleep(0.6)
    await type_focused(tab, city)
    await tab.sleep(2.8)  # autocomplete XHR + render
    # pick the dropdown option containing the IATA code (redibe options show "City (CODE)")
    picked = await tab.evaluate(
        "(()=>{const opts=[...document.querySelectorAll('[role=option],li,[class*=option],[class*=suggestion],[class*=result]')]"
        ".filter(e=>e.offsetParent&&e.textContent&&e.textContent.toUpperCase().includes('(" + code.upper() + ")'));"
        "if(opts[0]){opts[0].scrollIntoView({block:'center'});opts[0].click();return 'picked:'+opts[0].textContent.replace(/\\s+/g,' ').trim().slice(0,40);}return 'noopt';})()"
    )
    print(f"[FILL {input_id} {code}] {picked}", flush=True)
    await tab.sleep(1)


async def _redibe_state(tab, stage):
    js = (
        "(()=>{const ins=[...document.querySelectorAll('input[id^=redibe-v3-text]')].map(e=>e.id+'='+JSON.stringify(e.value));"
        "const dates=[...document.querySelectorAll('input[id^=redibe-v3]')].map(e=>e.id+'='+JSON.stringify(e.value)).filter(s=>!s.includes('text'));"
        "const btns=[...document.querySelectorAll('button,[role=button],input[type=submit]')].filter(e=>e.offsetParent&&/search|continue|find|show flights/i.test((e.textContent||'')+(e.value||'')))"
        ".map(e=>({tx:((e.textContent||'')+(e.value||'')).replace(/\\s+/g,' ').trim().slice(0,30),cls:(e.className||'').slice(0,50),inRedibe:!!e.closest('[class*=redibe],[id*=redibe]')}));"
        "return JSON.stringify({inputs:ins,dateInputs:dates,searchBtns:btns});})()"
    )
    try:
        print(f"[REDIBE {stage}] {await tab.evaluate(js)}", flush=True)
    except Exception as e:
        print(f"[REDIBE {stage}] err {str(e)[:80]}", flush=True)


async def _kill_overlays(tab):
    """Remove the auto-opening Notification centre overlay + backdrops that intercept clicks."""
    try:
        r = await tab.evaluate(
            "(()=>{let n=0;document.querySelectorAll('[class*=notificationCenter__overlay],"
            "[class*=notificationCenter__backdrop],[class*=Backdrop],[class*=backdrop]')"
            ".forEach(e=>{e.remove();n++;});return 'killed:'+n;})()"
        )
        print(f"[NOTIF] {r}", flush=True)
    except Exception as e:
        print(f"[NOTIF] err {str(e)[:60]}", flush=True)


async def _fill_vs_airport(tab, label, city, code):
    """Focus a Virgin autocomplete field by aria-label, type the city, dump the dropdown, then
    click the option matching the IATA code."""
    await click_field(tab, label)
    await tab.sleep(0.8)
    await type_focused(tab, city)
    await tab.sleep(3)
    opts = await tab.evaluate(
        "JSON.stringify([...document.querySelectorAll('[role=option],li,[class*=suggestion],[id*=option],[class*=autocomplete] li')]"
        ".filter(e=>e.offsetParent&&(e.textContent||'').trim().length>2&&(e.textContent||'').length<70)"
        ".map(e=>(e.textContent||'').replace(/\\s+/g,' ').trim()).slice(0,8))"
    )
    print(f"[VS OPTS {label} {code}] {str(opts)[:280]}", flush=True)
    picked = await tab.evaluate(
        "(()=>{const o=[...document.querySelectorAll('[role=option],li,[class*=suggestion],[id*=option],button,a')]"
        ".find(e=>e.offsetParent&&(e.textContent||'').toUpperCase().includes('" + code.upper() + "'));"
        "if(o){o.scrollIntoView({block:'center'});o.click();return 'picked:'+o.textContent.replace(/\\s+/g,' ').trim().slice(0,40);}return 'noopt';})()"
    )
    print(f"[VS PICK {label} {code}] {picked}", flush=True)
    await tab.sleep(1.2)


async def _real_click(tab, find_expr, label, is_fn=False):
    """Find an element via a JS expression, scroll it into view, and dispatch a REAL CDP mouse
    click at its center (synthetic .click() often won't open date pickers / submit redibe-style
    widgets)."""
    js = (
        "(()=>{const e=(" + find_expr + ");if(!e||!e.getBoundingClientRect)return 'no-el';"
        "e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();"
        "return JSON.stringify({x:r.x+r.width/2,y:r.y+r.height/2,w:r.width});})()"
    )
    try:
        rect = await tab.evaluate(js)
        if not isinstance(rect, str) or not rect.startswith("{"):
            print(f"[CLICK {label}] {rect}", flush=True)
            return
        p = json.loads(rect)
        if p.get("w", 0) <= 0:
            print(f"[CLICK {label}] zero-size/offscreen", flush=True)
            return
        x, y = float(p["x"]), float(p["y"])
        await tab.send(uc.cdp.input_.dispatch_mouse_event(
            type_="mousePressed", x=x, y=y, button=uc.cdp.input_.MouseButton.LEFT, click_count=1))
        await tab.send(uc.cdp.input_.dispatch_mouse_event(
            type_="mouseReleased", x=x, y=y, button=uc.cdp.input_.MouseButton.LEFT, click_count=1))
        print(f"[CLICK {label}] real click ({x:.0f},{y:.0f})", flush=True)
    except Exception as e:
        print(f"[CLICK {label}] err {str(e)[:70]}", flush=True)


async def _vs_state(tab, stage):
    """Dump visible fields (label+value) + candidate date/search buttons for the AF widget."""
    js = (
        "(()=>{const vis=e=>e.offsetParent;"
        "const fields=[...document.querySelectorAll('input,[role=combobox],[role=textbox]')].filter(vis)"
        ".map(e=>({al:(e.getAttribute('aria-label')||'').slice(0,45),val:((e.value||e.textContent||'')+'').slice(0,30)}))"
        ".filter(f=>f.al);"
        "const btns=[...document.querySelectorAll('button,[role=button]')].filter(e=>vis(e)&&"
        "/search|date|depart|when|continue|find|outbound/i.test((e.textContent||'')+(e.getAttribute('aria-label')||'')))"
        ".map(e=>(((e.textContent||'')+'|'+(e.getAttribute('aria-label')||'')).replace(/\\s+/g,' ').trim()).slice(0,45));"
        "return JSON.stringify({fields:fields.slice(0,14),btns:[...new Set(btns)].slice(0,12)});})()"
    )
    try:
        print(f"[VS {stage}] " + str(await tab.evaluate(js))[:700], flush=True)
    except Exception as e:
        print(f"[VS {stage}] err {str(e)[:60]}", flush=True)


async def drive_virgin(tab):
    """Discovery drive of the LifeMiles award search (virgin.com/fly/find): dismiss consent,
    fill From=JFK + To=BOG, pick a date, Search. Dumps widget/field/button state + CDP-net so we
    can find the availability API/DOM."""
    # dismiss the cookie consent so the search widget renders
    for _ in range(4):
        c = await tab.evaluate(
            "(()=>{const bs=[...document.querySelectorAll('button,a,[role=button]')].filter(e=>e.offsetParent);"
            "const norm=e=>(e.textContent||'').replace(/\\s+/g,' ').trim().toLowerCase();"
            # discovery: accept to make the widget render (privacy revisited once it's understood)
            "const pref=['accept all','accept all cookies','accept','agree'];"
            "for(const p of pref){const e=bs.find(x=>norm(x)===p);if(e){e.click();return 'consent:'+p;}}"
            "return 'no-consent';})()"
        )
        print(f"[CONSENT] {c}", flush=True)
        if c == "no-consent":
            break
        await tab.sleep(2.5)
    await accept_cookies(tab)
    # close any feedback/survey modal that covers the page
    await tab.evaluate(
        "(()=>{const b=[...document.querySelectorAll('button,[role=button],[aria-label]')]"
        ".find(e=>e.offsetParent&&/close feedback|close|dismiss|no thanks/i.test((e.textContent||'')+(e.getAttribute('aria-label')||'')));"
        "if(b){b.click();return 'closed';}return 'none';})()"
    )
    await _kill_overlays(tab)
    # poll up to ~40s for the booking widget to render (slow-render vs withheld decides tractability)
    for w in range(8):
        await tab.sleep(5)
        cnt = await tab.evaluate(
            "(()=>{const ins=document.querySelectorAll('input').length;"
            "const all=document.querySelectorAll('*').length;"
            "const widget=document.querySelectorAll('[class*=bw0],[class*=booking],[class*=flight-search],"
            "[class*=search-engine],[data-test*=search],[class*=bookingFlightSearch]').length;"
            "const b=document.body?document.body.innerText.length:0;"
            "return JSON.stringify({sec:0,inputs:ins,allEls:all,widgetEls:widget,bodyLen:b});})()"
        )
        print(f"[VS WAIT {(w+1)*5}s] {cnt}", flush=True)
        try:
            import json as _j
            if _j.loads(cnt).get("inputs", 0) >= 2:
                break
        except Exception:
            pass
    await diag(tab, "00warm")
    # rich probe: is the widget present? body text, iframes, miles/booking elements
    probe = await tab.evaluate(
        "(()=>{const b=document.body?document.body.innerText:'';"
        "const frames=[...document.querySelectorAll('iframe')].map(f=>(f.src||'').slice(0,70)).filter(Boolean);"
        "const hits=[...document.querySelectorAll('label,button,input,[role=combobox]')].filter(e=>e.offsetParent&&"
        "/miles|departing|going to|flying blue|book|where|from|to/i.test((e.textContent||'')+(e.getAttribute('aria-label')||'')))"
        ".map(e=>((e.textContent||'')+'|'+(e.getAttribute('aria-label')||'')).replace(/\\s+/g,' ').trim().slice(0,40));"
        "return JSON.stringify({bodyLen:b.length,bodySnip:b.replace(/[0-9]/g,'#').slice(0,200),frames,hits:[...new Set(hits)].slice(0,14)});})()"
    )
    print(f"[VS PROBE] {str(probe)[:900]}", flush=True)
    # dump the booking widget HTML for offline structure analysis
    try:
        wh = await tab.evaluate(
            "(()=>{const f=document.querySelector('form[name],[class*=bookingFlightSearch],"
            "[class*=flightSearch],[class*=search-engine],[class*=ibe],[data-test*=search]');"
            "return f?f.outerHTML:'no-form';})()"
        )
        if isinstance(wh, str) and wh != 'no-form':
            with open('cap_virgin_widget.html', 'w') as fh:
                fh.write(wh[:600000])
            print(f"WIDGET_HTML saved ({len(wh)} chars)", flush=True)
        else:
            print("WIDGET_HTML: no-form-found", flush=True)
    except Exception as e:
        print(f"WIDGET_DUMP_ERR {str(e)[:80]}", flush=True)
    # Virgin defaults to cash — switch to reward/miles, pick one-way, fill From+To, date, search.
    rw = await tab.evaluate(
        "(()=>{const l=[...document.querySelectorAll('label,button,[role=switch],[role=checkbox],[role=tab],a,span')]"
        ".find(e=>e.offsetParent&&/reward|use miles|pay with miles|book with miles|redeem|spend miles/i.test(e.textContent||''));"
        "if(l){l.scrollIntoView({block:'center'});l.click();return 'reward:'+(l.textContent||'').replace(/\\s+/g,' ').trim().slice(0,30);}return 'no-reward-toggle';})()"
    )
    print(f"[REWARD] {rw}", flush=True)
    await tab.sleep(2)
    await click_exact(tab, "one way", "one-way")
    await tab.sleep(1)
    await _vs_state(tab, "after-oneway")
    # Virgin's "Flying from"/"Flying to" are autocomplete comboboxes — fill each separately,
    # dump its dropdown, and pick the option matching the IATA code.
    await _fill_vs_airport(tab, "flying from", ORIGIN_CITY, ORIGIN_CODE)
    await _vs_state(tab, "after-from")
    await _fill_vs_airport(tab, "flying to", DEST_CITY, DEST_CODE)
    await _vs_state(tab, "after-to")
    # dump the search form's HTML so we can design the date interaction + verify the airports are
    # committed to hidden state (the visible inputs blank after selection).
    try:
        form = await tab.evaluate(
            "(()=>{const fs=[...document.querySelectorAll('form,[class*=search],[class*=Search],[class*=flightSearch],[data-test*=search]')];"
            "let best=null,bn=0;for(const f of fs){const n=f.querySelectorAll('input,button,[role=button]').length;"
            "if(n>bn&&n<200){bn=n;best=f;}}return best?best.outerHTML:'no-form';})()"
        )
        if isinstance(form, str) and form != 'no-form':
            with open('cap_virgin_widget.html', 'w') as fh:
                fh.write(form[:600000])
            print(f"FORM_HTML saved ({len(form)} chars)", flush=True)
        else:
            print("FORM_HTML: no-form", flush=True)
        # specifically: the When/date trigger + any hidden airport state
        whenstruct = await tab.evaluate(
            "(()=>{const w=[...document.querySelectorAll('*')].find(e=>e.offsetParent&&/^\\s*When\\s*$/.test(e.textContent||'')&&e.querySelectorAll('*').length<6);"
            "const hid=[...document.querySelectorAll('input[type=hidden]')].map(e=>(e.name||e.id||'')+'='+(e.value||'').slice(0,12)).filter(s=>/orig|dest|from|to|airport|date/i.test(s)).slice(0,8);"
            "return JSON.stringify({when:w?(w.tagName+'.'+w.className+' :: '+(w.outerHTML||'').slice(0,300)):'no-when-el',hidden:hid});})()"
        )
        print(f"[WHEN STRUCT] {str(whenstruct)[:600]}", flush=True)
    except Exception as e:
        print(f"FORM_DUMP_ERR {str(e)[:80]}", flush=True)
    # date: #flights_departing is a readonly input that opens #popover-when. Real-click it, then
    # click the exact day buttons (data-cy="day-component-YYYY-MM-DD"). Pick a return too in case
    # the journey is round-trip (harmless if one-way).
    await _real_click(tab, "document.getElementById('flights_departing')", "departing-field")
    await tab.sleep(2)
    popover = await tab.evaluate(
        "(()=>{const p=document.getElementById('popover-when');"
        "const days=document.querySelectorAll('button[data-cy^=day-component]:not([disabled])').length;"
        "return JSON.stringify({popoverOpen:!!(p&&p.offsetParent),enabledDays:days});})()"
    )
    print(f"[POPOVER] {popover}", flush=True)
    await _real_click(tab,
        "document.querySelector('button[data-cy=\"day-component-" + FUTURE_DEP + "\"]:not([disabled])')",
        "dep-" + FUTURE_DEP)
    await tab.sleep(1.2)
    await _real_click(tab,
        "document.querySelector('button[data-cy=\"day-component-" + FUTURE_RET + "\"]:not([disabled])')",
        "ret-" + FUTURE_RET)
    await tab.sleep(1)
    await click_exact(tab, "search for flights", "search dates", "done", "apply", "confirm")
    await _vs_state(tab, "after-date")
    # search
    await _real_click(tab,
        "[...document.querySelectorAll('button,[role=button],input[type=submit]')]"
        ".find(e=>e.offsetParent&&/search flights|search for reward|^\\s*search/i.test"
        "((e.textContent||'')+(e.getAttribute('aria-label')||'')+(e.value||'')))",
        "search-btn")
    await tab.sleep(28)  # availability render / API (/travelplus/search-panel-api)
    where = await tab.evaluate(
        "JSON.stringify({url:location.href.slice(0,90),sso:/openid-connect|\\/auth\\/|signin|login/i.test(location.href)})"
    )
    print(f"[AFTER SEARCH] {where}", flush=True)
    await diag(tab, "03results")

# --------------------------------------------------------------- post-processing
SKIP = ("google", "doubleclick", "adsrvr", "facebook", "tiktok", "optimizely", "tealium",
        "qualtric", "onetrust", "px-cloud", "useinsider", "pisano", "demdex", "branch.io",
        "quantummetric", "kampyle", "sojern", "bing", "pinterest", "applicationinsights",
        "datadog", "linkedin", "akstat", "akam", "/akam/", "boomerang", "mpulse",
        "newrelic", "nr-data", "cdn-cgi", "fonts.", ".woff", ".css", ".js?", ".svg", ".png",
        # NOTE: do NOT skip "search-panel" — Virgin's availability API is /travelplus/search-panel-api
        "coredata", "/origins/", "/destinations/")
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
    profile = tempfile.mkdtemp(prefix="vscap_")
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
    net_meta: dict = {}
    net_bodies: list = []
    try:
        tab = await browser.get("about:blank")
        try:
            await tab.send(uc.cdp.page.add_script_to_evaluate_on_new_document(INTERCEPT))
        except Exception as e:
            print("inject_err", str(e)[:80], flush=True)

        # CDP Network capture — catches service-worker / SSR traffic that a page-context fetch
        # patch misses. Bypass the service worker so SW-handled requests hit the network stack
        # (and thus fire responseReceived/loadingFinished with retrievable bodies).
        async def _on_response(ev):
            try:
                r = ev.response
                net_meta[str(ev.request_id)] = {
                    "url": r.url, "status": r.status, "mime": r.mime_type,
                    "type": str(getattr(ev, "type_", "")), "rid": ev.request_id,
                }
            except Exception:
                pass

        async def _on_finish(ev):
            try:
                meta = net_meta.get(str(ev.request_id))
                if not meta:
                    return
                u = meta["url"].lower()
                if u.startswith("blob:") or u.startswith("data:") or any(k in u for k in SKIP):
                    return
                mime = (meta.get("mime") or "").lower()
                want = ("json" in mime
                        or ("html" in mime and "/book/" in u)
                        or ("/book/" in u and "search" in u))
                if not want:
                    return
                body, _b64 = await tab.send(uc.cdp.network.get_response_body(meta["rid"]))
                if body and len(body) > 300:
                    net_bodies.append({"url": meta["url"], "status": meta["status"],
                                       "mime": meta["mime"], "type": meta["type"],
                                       "n": len(body), "b": body[:400000]})
            except Exception:
                pass

        try:
            tab.add_handler(uc.cdp.network.ResponseReceived, _on_response)
            tab.add_handler(uc.cdp.network.LoadingFinished, _on_finish)
            await tab.send(uc.cdp.network.enable())
            await tab.send(uc.cdp.network.set_bypass_service_worker(bypass=True))
        except Exception as e:
            print(f"NET_SETUP_ERR {type(e).__name__}: {str(e)[:100]}", flush=True)
        # warm the Redeem Flight Awards page (hosts the redibe-v3 widget), then drive the search
        await tab.get(WARM_URL)
        await tab.sleep(9)
        try:
            await tab.evaluate(INTERCEPT)  # idempotent re-install on this doc
        except Exception:
            pass
        try:
            await drive_virgin(tab)
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
        # Structured DOM dump of the result cards — fallback parser target if the API stays hidden.
        try:
            cards = await tab.evaluate(r"""
            (()=>{
              const seen=new Set(), out=[];
              const els=[...document.querySelectorAll('*')].filter(e=>/From\s*Miles/i.test(e.textContent||''));
              for(const e of els){
                let c=e; for(let i=0;i<5&&c.parentElement;i++){const p=c.parentElement;
                  if((p.textContent||'').length>900)break; c=p;}
                const t=(c.textContent||'').replace(/\s+/g,' ').trim();
                if(t.length<40||t.length>700||seen.has(t))continue; seen.add(t); out.push(t);
                if(out.length>=8)break;
              }
              return JSON.stringify(out);
            })()""")
            print(f"DOM_CARDS: {str(cards)[:3500]}", flush=True)
        except Exception as e:
            print(f"DOM_CARDS_ERR {type(e).__name__}: {str(e)[:100]}", flush=True)
        # CAPTURE=0 → availability is SSR'd or service-worker fetched. Locate where the raw
        # pricing data lives: scan <script> tags + window globals for the known mile figures.
        try:
            probe = await tab.evaluate(r"""
            (()=>{
              const NEEDLES=['540375','83625','82375','540,375','83,625'];
              const out={scripts:[],windowKeys:[],swActive:false,htmlHasRaw:false};
              try{out.swActive=!!(navigator.serviceWorker&&navigator.serviceWorker.controller);}catch(e){}
              const html=document.documentElement.innerHTML;
              out.htmlHasRaw=NEEDLES.some(n=>html.indexOf(n)>=0);
              for(const s of document.querySelectorAll('script')){
                const t=s.textContent||''; const hit=NEEDLES.find(n=>t.indexOf(n)>=0);
                if(t.length>200||s.id||s.type){
                  out.scripts.push({id:s.id||'',type:s.type||'',len:t.length,hit:hit||'',
                    head:t.slice(0,80).replace(/\s+/g,' ')});
                }
              }
              for(const k of Object.keys(window)){
                try{const v=window[k];
                  if(v&&typeof v==='object'){const j=JSON.stringify(v);
                    if(j&&j.length>1000&&NEEDLES.some(n=>j.indexOf(n)>=0)) out.windowKeys.push({k,len:j.length});}
                }catch(e){}
              }
              return JSON.stringify(out);
            })()""")
            print(f"STATE_PROBE: {str(probe)[:7000]}", flush=True)
        except Exception as e:
            print(f"STATE_PROBE_ERR {type(e).__name__}: {str(e)[:100]}", flush=True)
        try:
            await tab.save_screenshot("cathay_capture.png")
        except Exception:
            pass
        # Dump the full results HTML + per-card outerHTML as artifacts for offline structure
        # analysis (precise selectors, cabin labels, aria-labels) — avoids more CI round-trips.
        try:
            html = await tab.evaluate("document.documentElement.outerHTML")
            if isinstance(html, str):
                with open("cap_virgin_results.html", "w") as f:
                    f.write(html[:4000000])
                print(f"RESULTS_HTML saved ({len(html)} chars)", flush=True)
        except Exception as e:
            print(f"HTML_DUMP_ERR {type(e).__name__}: {str(e)[:100]}", flush=True)
        try:
            cards_html = await tab.evaluate(r"""
            (()=>{const out=[];const seen=new Set();
              for(const e of document.querySelectorAll('*')){
                if(!/From\s*Miles/i.test(e.textContent||''))continue;
                let c=e;for(let i=0;i<6&&c.parentElement;i++){const p=c.parentElement;
                  if((p.textContent||'').length>1400)break;c=p;}
                const h=c.outerHTML;if(h.length<200||h.length>40000||seen.has(h))continue;
                seen.add(h);out.push(h);if(out.length>=3)break;}
              return JSON.stringify(out);})()""")
            if isinstance(cards_html, str):
                with open("cap_virgin_cards.json", "w") as f:
                    f.write(cards_html)
                print(f"CARDS_HTML saved ({len(cards_html)} chars)", flush=True)
        except Exception as e:
            print(f"CARDS_DUMP_ERR {type(e).__name__}: {str(e)[:100]}", flush=True)
    finally:
        try:
            browser.stop()
        except Exception:
            pass
        proc.terminate()

    with open("cap_virgin_full.json", "w") as f:
        json.dump(cap, f)
    analyze(cap)

    # CDP Network-layer capture (catches service-worker / SSR responses)
    with open("cap_virgin_net.json", "w") as f:
        json.dump(net_bodies, f)
    print(f"\n===== CDP NET: {len(net_bodies)} captured bodies =====", flush=True)
    # Cathay's award API is on api.cathaypacific.com — surface those bodies directly (we don't
    # know the mile figures up front), plus anything with award-ish keywords.
    AWARD_TOKENS = ("mile", "asiamiles", '"award"', "redemption", "milesrequired", "cabinclass",
                    "availability", "flightsegment")
    award_net = []
    for r in net_bodies:
        bl = r["b"].lower()
        ul = r["url"].lower()
        print(f"  {r['type']:10} {r['status']} {r['mime'][:24]:24} n={r['n']:>7}  {r['url'][:120]}",
              flush=True)
        if "api.cathaypacific.com" in ul or (any(t in bl for t in AWARD_TOKENS)
                                             and any(c.isdigit() for c in r["b"])):
            award_net.append(r)
    award_net.sort(key=lambda r: ("api.cathaypacific.com" not in r["url"].lower(), -r["n"]))
    for idx, r in enumerate(award_net[:5]):
        print(f"\n========== CDP AWARD BODY #{idx} ==========", flush=True)
        print(f"URL: {r['url']}\nMIME: {r['mime']}  TYPE: {r['type']}  LEN: {r['n']}", flush=True)
        try:
            data = json.loads(r["b"])
            print("--- SKELETON ---\n" + skeleton(data)[:7000], flush=True)
            print("--- MILE PATHS ---", flush=True)
            for line in find_mile_paths(data)[:60]:
                print("  " + line, flush=True)
        except Exception:
            # not JSON (e.g. SSR HTML) — show context around the first mile figure
            b = r["b"]
            i = next((b.find(n) for n in ("540375", "83625", "82375") if b.find(n) >= 0), -1)
            if i >= 0:
                print(f"--- HTML context @ {i} ---\n{b[max(0,i-400):i+400]}", flush=True)
    print("\n=== DONE ===", flush=True)


if __name__ == "__main__":
    uc.loop().run_until_complete(main())
