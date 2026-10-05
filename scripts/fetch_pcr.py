#!/usr/bin/env python3
"""Build data/pcr.json - put/call ratios for the US index options.

SOURCE, AND WHY IT IS BETTER THAN THE INDIAN BOARD'S. The Nifty tracker scrapes
Sensibull with Playwright because NSE's own site is Akamai-blocked, and that
scrape returns only what the page RENDERS - a window around ATM, measured at
about +/-6.7% for NIFTY and +/-4.2% for SENSEX. Its band PCR is therefore not a
choice so much as a limitation, and its "bandStrikes/bandWanted" fields exist to
admit when even that window came back short.

CBOE publishes the entire chain as plain JSON on a public CDN - 29,282 SPX
contracts in one request, with open interest and volume on every one, no
browser and no key. So here the FULL-CHAIN ratio is the headline number and the
band is the secondary one, which is the right way round and the opposite of
what the Indian board can manage.

OI *AND* VOLUME, BECAUSE THEY ANSWER DIFFERENT QUESTIONS. Open interest is
positioning that has accumulated and only settles overnight; volume is what
traded today. An OI ratio barely moves intraday and a volume ratio is noisy
early in the session. Both are published rather than one being chosen for you.

WHAT IS NOT CLAIMED. A put/call ratio is a positioning statistic, not a signal.
Index puts are bought as hedges against portfolios that the ratio cannot see,
so a high reading is at least as likely to mean "hedged" as "bearish". The page
says so.
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))

import us_market_core as U  # noqa: E402

CDN = "https://cdn.cboe.com/api/global/delayed_quotes/options/{}.json"

# CBOE prefixes a cash index with an underscore; ETFs are bare.
UNDERLYINGS = [
    {"key": "spx",  "cboe": "_SPX", "label": "S&P 500",     "ticker": "^SPX",
     "kind": "index"},
    {"key": "ndx",  "cboe": "_NDX", "label": "NASDAQ 100",  "ticker": "^NDX",
     "kind": "index"},
    {"key": "rut",  "cboe": "_RUT", "label": "RUSSELL 2000", "ticker": "^RUT",
     "kind": "index"},
    {"key": "spy",  "cboe": "SPY",  "label": "SPY",          "ticker": "SPY",
     "kind": "etf"},
    {"key": "qqq",  "cboe": "QQQ",  "label": "QQQ",          "ticker": "QQQ",
     "kind": "etf"},
]

# Strikes either side of ATM for the band reading. 11 total, matching the
# Indian tracker so the two boards' band numbers are read the same way.
BAND_EACH_SIDE = 5

OSI = re.compile(r"^([A-Z0-9^_]+?)(\d{6})([CP])(\d{8})$")


def parse(sym):
    """OSI contract symbol -> (expiry date, 'C'|'P', strike)."""
    m = OSI.match(sym)
    if not m:
        return None
    _root, yymmdd, cp, strike = m.groups()
    try:
        exp = datetime.strptime(yymmdd, "%y%m%d").date()
    except ValueError:
        return None
    return exp, cp, int(strike) / 1000.0


def third_friday(d: date) -> date:
    """The standard monthly expiry: the third Friday of d's month."""
    first = d.replace(day=1)
    offset = (4 - first.weekday()) % 7          # 4 = Friday
    return first.replace(day=1 + offset + 14)


def ratio(puts, calls):
    return round(puts / calls, 3) if calls else None


def summarise(contracts, spot, label):
    """OI and volume PCR for one expiry, full chain and ATM band."""
    by_strike = defaultdict(lambda: {"C": [0.0, 0.0], "P": [0.0, 0.0]})
    for exp, cp, strike, oi, vol in contracts:
        slot = by_strike[strike][cp]
        slot[0] += oi
        slot[1] += vol

    strikes = sorted(by_strike)
    if not strikes:
        return None

    c_oi = sum(by_strike[k]["C"][0] for k in strikes)
    p_oi = sum(by_strike[k]["P"][0] for k in strikes)
    c_vol = sum(by_strike[k]["C"][1] for k in strikes)
    p_vol = sum(by_strike[k]["P"][1] for k in strikes)

    # ATM band: the nearest strike to spot, plus BAND_EACH_SIDE either side.
    atm_i = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
    lo = max(0, atm_i - BAND_EACH_SIDE)
    hi = min(len(strikes), atm_i + BAND_EACH_SIDE + 1)
    band = strikes[lo:hi]
    bc_oi = sum(by_strike[k]["C"][0] for k in band)
    bp_oi = sum(by_strike[k]["P"][0] for k in band)
    bc_vol = sum(by_strike[k]["C"][1] for k in band)
    bp_vol = sum(by_strike[k]["P"][1] for k in band)

    return {
        "label": label,
        "strikes": len(strikes),
        "pcr": ratio(p_oi, c_oi),
        "pcrVolume": ratio(p_vol, c_vol),
        "callOI": int(c_oi), "putOI": int(p_oi),
        "callVolume": int(c_vol), "putVolume": int(p_vol),
        "band": {
            # Stated, not assumed: the Indian board has to report when its
            # window came back short of what it asked for. Here it never
            # should, because the whole chain is in hand - so if these ever
            # disagree something upstream changed.
            "strikes": len(band), "wanted": BAND_EACH_SIDE * 2 + 1,
            "low": band[0], "high": band[-1],
            "atm": strikes[atm_i],
            "pcr": ratio(bp_oi, bc_oi),
            "pcrVolume": ratio(bp_vol, bc_vol),
        },
    }


def fetch(spec):
    r = requests.get(CDN.format(spec["cboe"]), headers=U.HEADERS, timeout=40)
    r.raise_for_status()
    payload = r.json()
    d = payload["data"]
    # `close` on this feed is the PREVIOUS day's close - it equals
    # prev_day_close field-for-field - so current_price is the live one and
    # must come first. The other way round centres the ATM band on yesterday.
    spot = d.get("current_price") or d.get("close")
    prev = d.get("prev_day_close")
    rows = []
    for c in d.get("options", []):
        got = parse(c.get("option", ""))
        if not got:
            continue
        exp, cp, strike = got
        rows.append((exp, cp, strike,
                     float(c.get("open_interest") or 0),
                     float(c.get("volume") or 0)))
    # The feed timestamp is at the TOP level, not inside `data`; `data`'s own
    # last_trade_time is the underlying's last print, which for a cash index
    # can still read Friday on a Monday morning.
    return spot, rows, payload.get("timestamp"), prev


def main() -> int:
    force = "--force" in sys.argv
    if not force and not U.is_market_hours():
        print(f"outside US market hours ({U.now_et():%a %H:%M %Z}) — skipping")
        return 0

    today = U.now_et().date()
    out = {"generatedAt": datetime.now(U.EXCHANGE_TZ).isoformat(),
           "bandEachSide": BAND_EACH_SIDE, "underlyings": {}}

    for spec in UNDERLYINGS:
        try:
            spot, rows, stamp, prev = fetch(spec)
        except Exception as e:
            print(f"  {spec['key']}: FAILED {type(e).__name__}: {e}")
            continue
        if not rows or not spot:
            print(f"  {spec['key']}: no contracts")
            continue

        by_exp = defaultdict(list)
        for exp, cp, strike, oi, vol in rows:
            if exp >= today:
                by_exp[exp].append((exp, cp, strike, oi, vol))
        if not by_exp:
            print(f"  {spec['key']}: no live expiries")
            continue

        nearest = min(by_exp)
        # The monthly is the third Friday of this month if it has not passed,
        # otherwise next month's. Picked from the expiries that EXIST rather
        # than assumed, so a holiday-shifted expiry still resolves.
        tf = third_friday(today)
        if tf < today:
            nxt = (today.replace(day=28) + __import__("datetime").timedelta(days=4))
            tf = third_friday(nxt.replace(day=1))
        monthly = min(by_exp, key=lambda e: (abs((e - tf).days), e))

        block = {
            "label": spec["label"], "ticker": spec["ticker"],
            "kind": spec["kind"], "spot": round(spot, 2),
            "prevClose": round(prev, 2) if prev else None,
            # NO CHANGE FIGURE UNLESS THE QUOTE HAS CLEARLY TICKED. On this
            # delayed feed a cash index can still carry Friday's print on a
            # Monday morning, and publishing that as "+0.00%" is a fabricated
            # zero sitting next to the heatmap board showing the S&P up 0.73%.
            #
            # The test is "rounds to zero", not "exactly equal", because the
            # first version used exact equality and NDX and RUT slipped past
            # it on a sub-tick difference - still printing +0.00% beside a
            # Nasdaq that was up 1%. A genuine dead-flat index is vanishingly
            # rare and a lagging feed is common, so suppressing both is the
            # right trade. last_trade_time cannot arbitrate this either: SPY
            # carried Friday's trade time while its price had plainly moved.
            #
            # The index LEVEL still shows - it is the strike reference the
            # band is centred on, which is what this board needs it for. The
            # session's change belongs to the heatmap board, which has a feed
            # that ticks.
            "chgPct": (None if (not prev or not spot
                                or abs(spot / prev - 1) * 100 < 0.005)
                       else round((spot / prev - 1) * 100, 2)),
            "quoteStale": bool(prev and spot
                               and abs(spot / prev - 1) * 100 < 0.005),
            "sourceTime": stamp,
            "expiries": len(by_exp),
            "nearest": dict(summarise(by_exp[nearest], spot,
                                      nearest.isoformat()) or {},
                            expiry=nearest.isoformat(),
                            dte=(nearest - today).days),
            "monthly": dict(summarise(by_exp[monthly], spot,
                                      monthly.isoformat()) or {},
                            expiry=monthly.isoformat(),
                            dte=(monthly - today).days),
        }
        # Every live expiry, so the page can show the term structure - the one
        # thing the Indian board cannot offer at all.
        term = []
        for exp in sorted(by_exp)[:14]:
            s = summarise(by_exp[exp], spot, exp.isoformat())
            if s:
                term.append({"expiry": exp.isoformat(),
                             "dte": (exp - today).days,
                             "pcr": s["pcr"], "pcrVolume": s["pcrVolume"],
                             "callOI": s["callOI"], "putOI": s["putOI"]})
        block["term"] = term
        out["underlyings"][spec["key"]] = block
        print(f"  {spec['key']:<4} spot {spot:>10.2f}  "
              f"nearest {nearest} PCR {block['nearest']['pcr']}  "
              f"monthly {monthly} PCR {block['monthly']['pcr']}  "
              f"({len(by_exp)} expiries)")

    if not out["underlyings"]:
        print("no underlying produced data, refusing to write", file=sys.stderr)
        return 4

    DATA.mkdir(exist_ok=True)
    (DATA / "pcr.json").write_text(json.dumps(out, separators=(",", ":")))
    print(f"wrote data/pcr.json ({len(out['underlyings'])} underlyings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
