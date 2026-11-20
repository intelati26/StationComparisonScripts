#!/usr/bin/env python3
"""Forecast column water (PWAT, mm) from Iowa State's BUFKIT archive: the HRRR run nearest 22Z on each night's date at the home airport (HYPERLOCAL_AIRPORT in .env), valid hours -> log/bufkit_pw.json {night: {valid UTC hour: PWAT}}. Nights = those of log/nbs_nights.json. Resumable.   python bufkit_pull.py"""
import json, os, re, sys, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coldpool as c
import places
AIRPORT = places.airport()
F = c.LOG / "bufkit_pw.json"; OUT = json.load(open(F)) if F.exists() else {}
nights = [r["E"] for r in json.load(open(c.LOG / "nbs_nights.json"))]
def get(url):
    for i in range(3):
        try: return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "hyperlocal-research"}), timeout=60).read().decode("latin1")
        except Exception as e:
            if getattr(e, "code", 0) == 404: return None
            time.sleep(5 * (i + 1))
for E in nights:
    if E in OUT: continue
    y, m, d = E.split("-")
    for hh in ("22", "21", "23", "20"):
        txt = get(f"https://mtarchive.geol.iastate.edu/{y}/{m}/{d}/bufkit/{hh}/hrrr/hrrr_k{AIRPORT}.buf")
        if txt: break
    if not txt: print(E, "no file", flush=True); OUT[E] = None; continue
    pw = {}
    for blk in txt.split("STID =")[1:]:
        t = re.search(r"TIME = (\d{6})/(\d{4})", blk); p = re.search(r"PWAT = ([-\d.]+)", blk)
        if t and p and float(p.group(1)) > -999: pw[f"20{t.group(1)[:2]}-{t.group(1)[2:4]}-{t.group(1)[4:6]}T{t.group(2)[:2]}"] = float(p.group(1))
    OUT[E] = {"run": hh, "pw": pw}; F.write_text(json.dumps(OUT)); print(E, hh, len(pw), flush=True); time.sleep(0.3)
print("done", sum(v is not None for v in OUT.values()), "nights")
