"""US corporate events: what needs doing, what explicitly does not, and why.

THE SHORT VERSION. US splits need NO repair - Yahoo has already applied them
and dated them correctly. Spin-offs need a DECISION, not a repair, and Yahoo
hands us the number the decision turns on. Everything here is computed from
Yahoo's own event feed on every build, so there is no curated table to go
stale - which is the one real upkeep hazard on the Indian boards, where an
entry added after an ex-date buys nothing.

MEASURED, 2026-10-05, over the S&P 500 + 112 ETFs, 5 years:

  88 split events: 59 declared splits, 29 fractional price adjustments.

  Declared splits (2:1, 3:2, 25:1, 1:5 ...) are ALREADY in `close`. Verified
  on eight of them - NVDA 10:1, AAPL 4:1, AMZN 20:1, GOOGL 20:1, TSLA 3:1,
  CMG 50:1, WMT 3:1, MSTR 10:1 - where the close-to-close gap across the
  ex-date is an ordinary daily move (0.975-1.091), not a cliff. Repairing
  these would INVENT the gap it is meant to remove.

  Fractional adjustments (1253:1000, 239:100, 1907:2000 ...) are how Yahoo
  prices in a SPIN-OFF: shareholders received stock in a new company, so the
  parent's earlier prices are scaled. The ratio IS the size of the separation:
  value removed from the parent = 1 - 1/ratio.

TELLING THEM APART. A declared split is a simple rational - small numerator
AND small denominator. A spin-off adjustment is an ugly one, because it is a
measured value ratio rather than an announced exchange. Hence SIMPLE_NUM /
SIMPLE_DEN below. This matters: PCAR and WRB both do routine 3:2 splits, and
a naive "is it an integer?" test files them as spin-offs.

THE DECISION, AND WHY IT IS A JUDGEMENT. Cosmetic (share count changed,
business did not) means carry on; economic (the company itself changed) means
no arithmetic makes the earlier bars describe the thing now being plotted, so
usable history STARTS at the event. A spin-off is a matter of DEGREE, so the
line is a choice, and it is named here rather than buried:

    ECONOMIC_SPLIT = 0.20

Above 20% of value removed, you are comparing two different companies and the
relative-strength path before the event belongs to the larger one. Below it,
the surviving business still dominates its own path. The measured 5-year
distribution either side of that line, so the choice is inspectable:

    ECONOMIC   DD 58.2% (Qnity)      DELL 49.3% (VMware)   EXC 28.7% (Constellation)
               FTV 24.6% (Ralliant)  FLEX 24.6% (Nextracker)
               T   24.5% (Warner Bros Discovery)           WDC 24.4% (Sandisk)
               GE  21.9% (HealthCare) BDX 21.4%            GE  20.2% (Vernova)
    COSMETIC   FDX 19.4%  J 16.5%  MMM 16.4%  LH 14.1%  DHR 11.3%  CMCSA 6.3%
               HON 5.7%  SPGI 5.4%  IBM 4.4%  LEN 3.2%  O 3.1%  ZBH 2.9%
               ILMN 2.7%  BDX 2.4%  J 1.0%

Truncating is cheap and SELF-CLEARING. The RRG needs a full normalisation
window (about 192 daily bars, or 142 weekly ones), so an old separation sits
far outside the window and costs nothing, while a recent one excludes the name
with a printed reason until the window rolls past it. No dated code to remove.

WHAT IS LEFT ALONE, DELIBERATELY. A large gap with no split record is not
evidence of anything. Measured across 5 years, the gaps beyond -25%/+40% are
overwhelmingly real: NFLX -35.1% (2022-04-20), META -26.4% (2022-02-03),
INTC -26.1% (2024-08-02), GL -53.1%, FISV -44.0%, CNC -40.4%. Smoothing those
would delete the largest true information on the board. `unexplained()` reports
them so they cannot be silent, and changes nothing.
"""

from datetime import datetime, timezone

import requests

from . import HEADERS

# ── Events Yahoo has not posted ────────────────────────────────────────
# The automatic rule above reads Yahoo's own event feed, which is reliable for
# the US but NOT instant: a separation can trade for days before the feed
# carries it, and until then the series holds a cliff that no rule here can
# see. That window is exactly when the board is most wrong, so this table
# exists for it - narrow, sourced, and checked against the company's own
# filing rather than inferred from the price.
#
# IT IS AN EXCEPTION LIST, NOT THE MECHANISM. On the Indian boards a curated
# table IS the mechanism, and its standing hazard is that an entry added after
# an ex-date buys nothing. Here the table should normally be EMPTY: once Yahoo
# posts the event, classify() finds it, and truncation is idempotent (the
# latest economic date wins), so a stale entry is harmless and can simply be
# deleted at leisure.
#
# WHAT DOES NOT GO IN HERE. A large move is not evidence. MRNA +177.0% on
# 2026-08-19 looks exactly like a corporate action by size and is entirely
# real - the first positive Phase 3 for a personalised mRNA cancer vaccine,
# $62.96 to $174.38, its best session on record. It stays untouched. The test
# for an entry is a filing, never a number.
OVERRIDES = {
    "CTVA": [{
        "date": "2026-10-01",
        "kind": "economic",
        "ratio": "approx 6.18:1",
        "removed": 0.838,
        "what": "seed business separated as Vylor (VYLR); history starts here",
        "source": ("Corteva 8-K, SEC CIK 1755672: record date 2026-09-24, "
                   "distribution 2026-10-01 before 09:30 ET, Vylor listed NYSE "
                   "'VYLR'. Price went 77.65 -> 12.57 on 2026-10-01 and Yahoo "
                   "had posted no split event as of 2026-10-05."),
    }],
}


SIMPLE_NUM = 50     # a declared split's numerator, e.g. 25:1, 3:2
SIMPLE_DEN = 20     # ... and denominator, e.g. 1:5, 3:2
ECONOMIC_SPLIT = 0.20   # fraction of parent value removed -> history restarts
LARGE_MOVE = 0.25       # one-day move reported for review, never acted on


def _parse_ratio(raw, num, den):
    """Yahoo gives numerator/denominator and a 'a:b' string; prefer the pair."""
    if num and den:
        return float(num), float(den)
    if raw and ":" in str(raw):
        a, b = str(raw).split(":", 1)
        try:
            return float(a), float(b)
        except ValueError:
            return None, None
    return None, None


def is_declared_split(num, den):
    """A small rational on both sides is an announced split, already in close."""
    return (num is not None and den is not None
            and num <= SIMPLE_NUM and den <= SIMPLE_DEN
            and float(num).is_integer() and float(den).is_integer())


def split_events(ticker, rng="5y", timeout=25):
    """[(iso_date, numerator, denominator, raw_ratio)] from Yahoo's event feed."""
    sym = ticker.replace("^", "%5E")
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
           f"?interval=1d&range={rng}&events=split")
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        d = r.json()["chart"]["result"][0]
    except Exception:
        return []
    out = []
    for v in (d.get("events", {}).get("splits", {}) or {}).values():
        num, den = _parse_ratio(v.get("splitRatio"), v.get("numerator"),
                                v.get("denominator"))
        if not num or not den:
            continue
        day = datetime.fromtimestamp(v["date"], timezone.utc).date().isoformat()
        out.append((day, num, den, v.get("splitRatio")))
    return sorted(out)


def overrides_for(ticker):
    """Hand-verified events for `ticker`, shaped like classify()'s output."""
    return [dict(e, override=True) for e in OVERRIDES.get(ticker, [])]


def classify(ticker, rng="5y"):
    """Verdict per event for one ticker.

    Returns [{date, ratio, kind, removed, what}] where kind is one of
    'split' (nothing to do), 'cosmetic' (small spin-off, nothing to do) or
    'economic' (history must start at `date`).
    """
    out = list(overrides_for(ticker))
    for day, num, den, raw in split_events(ticker, rng):
        r = num / den
        if is_declared_split(num, den):
            out.append({"date": day, "ratio": raw, "kind": "split",
                        "removed": None,
                        "what": f"{raw} split, already reflected in close"})
            continue
        removed = 1 - 1 / r if r > 0 else 0.0
        economic = removed >= ECONOMIC_SPLIT
        out.append({
            "date": day, "ratio": raw,
            "kind": "economic" if economic else "cosmetic",
            "removed": removed,
            "what": (f"spin-off removing {removed*100:.1f}% of the company"
                     + ("; history starts here" if economic
                        else "; price series back-adjusted, history kept")),
        })
    return out


def truncate_from(events):
    """The date history must start at, or None. The LATEST economic event wins:
    two separations mean the pre-second-one bars are still the wrong company."""
    dates = [e["date"] for e in events if e["kind"] == "economic"]
    return max(dates) if dates else None


def unexplained(ticker, pairs, events, threshold=LARGE_MOVE):
    """Day moves beyond `threshold` that no split event accounts for.

    Reported, never repaired - see the module docstring. A date within one
    session of a known event is attributed to it rather than flagged.
    """
    known = {e["date"] for e in events}
    out = []
    for i in range(1, len(pairs)):
        a, b = pairs[i - 1][1], pairs[i][1]
        if not a or not b:
            continue
        r = b / a
        if abs(1 - r) < threshold:
            continue
        day = datetime.fromtimestamp(pairs[i][0], timezone.utc).date().isoformat()
        prev = datetime.fromtimestamp(pairs[i - 1][0], timezone.utc).date().isoformat()
        if day in known or prev in known:
            continue
        out.append({"ticker": ticker, "date": day, "move": r - 1})
    return out
