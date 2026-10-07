"""Shared universe + fetch logic for the US market boards.

WHAT IS DELIBERATELY DIFFERENT FROM THE INDIAN BOARDS - read this before
porting anything across, because two of the Indian boards' largest pieces of
machinery are absent here ON PURPOSE, not by omission.

1. NO CORPORATE-ACTION REPAIR FOR SPLITS. The Indian boards carry a curated
   CORPORATE_ACTIONS table, matched into the series by RATIO rather than date,
   because Yahoo's Indian `close` is NOT split-adjusted and its split RECORDS
   carry the wrong date (every one lands on 1 January of some year). Measured
   on 2026-10-05 against eight known US splits - NVDA 10:1 2024-06-10,
   AAPL 4:1 2020-08-31, AMZN 20:1, GOOGL 20:1, TSLA 3:1, CMG 50:1, WMT 3:1,
   MSTR 10:1 - the US series behaves the opposite way: the close-to-close gap
   across every ex-date is an ordinary daily move (0.975 to 1.091), so the
   series is ALREADY adjusted, and Yahoo's split event dates are correct. A
   repair layer here would therefore double-adjust and INVENT the very cliff
   it exists to remove. SPIN-OFFS are a separate question - see corp_events.py.

2. NO CASH-ONLY MARKING. The Indian F&O board marks names without derivatives
   because its universe IS the derivatives list. This universe is the S&P 500,
   which is not a derivatives list, so the distinction carries no meaning.

3. THE SECTOR SERIES IS A REAL FUND, NOT A BASKET. The Indian board must build
   equal-weight baskets for sectors with no liquid sectoral index. Here the
   eleven Select Sector SPDRs are exactly the eleven GICS sectors, so the
   sector view plots the funds themselves - properly weighted, tradeable, and
   not an artefact of our own arithmetic. equal_weight_series is still used
   for the ETF GROUP aggregates, which have no fund of their own.

WHAT IS SHARED. The RRG mathematics - RS-Ratio, RS-Momentum, the z-score on a
100-centred scale, the common-window rule, the volatility and absolute-return
tails - lives in nifty-heatmap-core and is imported, not copied. It contains no
market assumptions. Copying it was the obvious shortcut and would have been the
wrong one: hand-copying shared logic between repos is what produced the
web-vs-Android parity bug that created that package in the first place.
"""

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - py<3.9
    raise

from .universe import SPX_SECTORS, COMPANY
from .etf_universe import ETF_GROUPS, SECTOR_ETF, ETF_NAME

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

BENCHMARK = "^GSPC"
BENCH_LABEL = "S&P 500"

# The exchange timezone, NOT a fixed offset. The Indian boards can hard-code
# +05:30 because India has no DST; the US has two, and they do not move with
# each other, so a fixed offset would put the post-close boundary an hour wrong
# for several weeks twice a year.
EXCHANGE_TZ = ZoneInfo("America/New_York")
MARKET_OPEN = (9, 30)
MARKET_CLOSE = (16, 0)
POST_CLOSE_HOUR = 17  # ET; regular session ends 16:00

SPX_ALL = sorted({t for v in SPX_SECTORS.values() for t in v})
ETF_ALL = [t for g in ETF_GROUPS.values() for t in g]
SECTOR_OF = {t: sec for sec, ts in SPX_SECTORS.items() for t in ts}

# Broad industries exist only for the 3D view's connecting threads: they group
# the eleven sectors so a thread joins things that actually move together.
# Every sector must appear exactly once - build_rrg.py asserts it.
BROAD_INDUSTRIES = {
    "Technology & Communication": ["Information Technology", "Communication Services"],
    "Cyclicals": ["Consumer Discretionary", "Industrials"],
    "Defensives": ["Consumer Staples", "Health Care", "Utilities"],
    "Finance & Property": ["Financials", "Real Estate"],
    "Resources": ["Energy", "Materials"],
}
INDUSTRY_OF = {s: ind for ind, ss in BROAD_INDUSTRIES.items() for s in ss}


def short_name(ticker):
    """Plot label. US tickers are already short; indices lose the caret."""
    return ticker.lstrip("^")[:10]


def display_name(ticker):
    """Full name for tooltips: company for stocks, fund name for ETFs."""
    return COMPANY.get(ticker) or ETF_NAME.get(ticker) or ticker


def quote_url(ticker):
    return f"https://finance.yahoo.com/quote/{ticker.replace('^', '%5E')}"


def now_et():
    return datetime.now(EXCHANGE_TZ)


def last_post_close(now=None):
    """Most recent POST_CLOSE_HOUR boundary in exchange time."""
    now = now or now_et()
    boundary = now.replace(hour=POST_CLOSE_HOUR, minute=0, second=0, microsecond=0)
    if now < boundary:
        boundary -= timedelta(days=1)
    return boundary


def is_market_hours(now=None):
    """Regular session, weekdays only. Holidays are NOT handled here on
    purpose: the only caller is a refresh gate, and refreshing on a closed
    holiday costs one wasted run, whereas a holiday list that goes stale
    silently stops the board. A missing holiday firing a job on a closed
    exchange has bitten this stack before (the armed recovery, item 60)."""
    now = now or now_et()
    if now.weekday() >= 5:
        return False
    o = now.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    c = now.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0, microsecond=0)
    return o <= now <= c


def chart_url(ticker, interval="1d", rng="5y"):
    sym = ticker.replace("^", "%5E")
    return (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
            f"?interval={interval}&range={rng}")


def fetch_closes(ticker, rng="5y", interval="1d", timeout=25):
    """(ticker, [(epoch, close), ...]) with None closes dropped."""
    try:
        r = requests.get(chart_url(ticker, interval, rng), headers=HEADERS,
                         timeout=timeout)
        d = r.json()["chart"]["result"][0]
        q = d["indicators"]["quote"][0]
        pairs = [(t, c) for t, c in zip(d["timestamp"], q["close"]) if c is not None]
        return ticker, pairs
    except Exception:
        return ticker, []


def fetch_all_closes(tickers, workers=16, rng="5y", interval="1d"):
    out = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(fetch_closes, t, rng, interval) for t in tickers]
        for f in as_completed(futs):
            t, pairs = f.result()
            out[t] = pairs
    return out


# ── Chain phase, for the self-chaining refresh ──────────────────────────
# A self-chaining workflow needs a STOP condition or it runs for ever. The
# phases are deliberately wider than `is_market_hours`: the chain must already
# be alive before the opening print and must outlive the closing one, while
# the fetchers themselves stay gated to the regular session.
#
#   warm   09:00-09:30 ET   chain running, fetchers skipping
#   live   09:30-16:05 ET   chain running, fetchers fetching
#   stop   otherwise        chain ends; the morning cron starts the next one
#
# Weekends stop. US market holidays are NOT handled, on purpose and for the
# reason the rest of this stack settled on: a holiday list that silently goes
# stale STOPS a board, whereas a chain running on a closed exchange costs a
# few no-op runs. A missing holiday firing a job has bitten this stack before.
# The fetchers gate on market_phase() != "stop", NOT on is_market_hours(), and
# the five minutes past 16:00 are the reason. Gated on the session alone, the
# last snapshot of the day is taken a minute or so BEFORE the close and the
# settled closing print is never captured - on 2026-10-06 the board's final
# reading was 15:47 ET. The pre-open half of the window is harmless: outside
# the session Yahoo returns the previous close, which is what the board should
# show before the opening bell anyway.
CHAIN_WARM = (9, 0)
CHAIN_STOP = (16, 5)

# How long the NEXT run should wait before taking over.
#
# The chain used to end at `stop` and rely on a morning cron to start the next
# one. That does not work. Measured on this repo: the starter asks for roughly
# 60 firings a day (`*/10 12-21 * * 1-5`) and delivered 2 on 2026-10-06 and 0
# on 2026-10-07 up to 16:11Z -- so on 2026-10-07 the board sat on an 02:18 ET
# snapshot while the session ran. The chain itself was never the problem; its
# ignition was.
#
# So the chain no longer stops, it IDLES. Outside the session a run does no
# fetching and simply waits, and the wait is sized to land on the next 09:00 ET
# warm boundary rather than to tick pointlessly through the night: a handful of
# long hops cover a weekend. The cap keeps any single sleep short enough that a
# DST shift, a missed holiday or a clock error self-corrects within one hop
# instead of overshooting an entire session.
CHAIN_IDLE_MAX = 45 * 60


def _next_warm(now):
    """The next weekday 09:00 ET strictly after `now`."""
    nxt = now.replace(hour=CHAIN_WARM[0], minute=CHAIN_WARM[1],
                      second=0, microsecond=0)
    while nxt <= now or nxt.weekday() >= 5:
        nxt += timedelta(days=1)
        nxt = nxt.replace(hour=CHAIN_WARM[0], minute=CHAIN_WARM[1],
                          second=0, microsecond=0)
    return nxt


def chain_wait_seconds(now=None):
    """Seconds the next run should sleep before handing off.

    live: none -- the job's own runtime is the ~60s cadence.
    warm: a short tick, so the chain is already hot at the opening print.
    stop: long hops onto the next warm boundary.
    """
    now = now or now_et()
    phase = market_phase(now)
    if phase == "live":
        return 0
    if phase == "warm":
        return 60
    return max(60, min(CHAIN_IDLE_MAX, int((_next_warm(now) - now).total_seconds())))


def market_phase(now=None):
    now = now or now_et()
    if now.weekday() >= 5:
        return "stop"
    mins = now.hour * 60 + now.minute
    if mins < CHAIN_WARM[0] * 60 + CHAIN_WARM[1]:
        return "stop"
    if mins >= CHAIN_STOP[0] * 60 + CHAIN_STOP[1]:
        return "stop"
    return "live" if is_market_hours(now) else "warm"
