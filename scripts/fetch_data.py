"""Build board_data.json - the live US heatmap snapshot.

One sweep covers everything: 503 S&P 500 stocks, 112 ETFs (which double as the
eleven sector reference funds) and four headline indices. The sector funds are
fetched as ordinary ETF rows and then ALSO attached to their sector, so a
ticker serving two consumers is fetched once, not twice.

Cadence. This is the per-minute board, driven by the same external pinger as
the Indian ones, gated to US market hours. The RRG build is a separate daily
job - it reads five years of history and would be pointless at this frequency.
"""

import json
import os
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "nifty-heatmap-core"))

import us_market_core as U
from us_market_core import board as B
from nifty_heatmap_core import fetch_all, compute_movers, summarize_group

OUT = os.path.join(REPO_ROOT, "board_data.json")
SPOT_OUT = os.path.join(REPO_ROOT, "data", "spot.json")


def write_spot(stocks, generated_at):
    """Write the EMA board's spot file from the sweep THIS script already did.

    refresh_spot.py used to re-fetch the same 614 symbols through yfinance,
    which measured 41s against the 12s this sweep takes for 615 - more than
    half the minute job's whole runtime, spent fetching prices we already had
    in memory. Two sweeps for one set of prices also meant the heatmap and the
    EMA board could show different prices for the same name at the same
    moment, which no amount of care in either script would have fixed.

    refresh_spot.py is kept for standalone use (it can rebuild spot.json
    without touching the heatmap), but it is no longer in the minute chain.

    Carry-forward is preserved from it: a symbol that failed keeps its previous
    price and is listed as stale, because a board showing a ten-minute-old
    price as if it were live is worse than one that admits it is behind.
    """
    prev = {}
    try:
        with open(SPOT_OUT) as f:
            prev = (json.load(f) or {}).get("spot", {})
    except Exception:
        prev = {}

    want = []
    try:
        with open(os.path.join(REPO_ROOT, "data", "levels.json")) as f:
            want = sorted({r["symbol"] for r in json.load(f)["rows"]})
    except Exception:
        # levels.json is built by the daily job; before its first run fall back
        # to the whole sweep rather than writing nothing.
        want = sorted(stocks)

    spot, stale, fresh = {}, [], 0
    for sym in want:
        px = stocks.get(sym, (None,))[0]
        if px is not None:
            spot[sym] = round(px, 2)
            fresh += 1
        elif sym in prev:
            spot[sym] = prev[sym]
            stale.append(sym)

    os.makedirs(os.path.dirname(SPOT_OUT), exist_ok=True)
    with open(SPOT_OUT, "w") as f:
        json.dump({"generated_at": generated_at, "fresh": fresh,
                   "total": len(want), "stale": sorted(stale),
                   "spot": spot}, f, separators=(",", ":"))
    return fresh, len(want), len(stale)


def sort_by_pct(rows):
    return sorted(rows,
                  key=lambda r: (r["pct"] if r["pct"] is not None else -999),
                  reverse=True)


def main():
    force = "--force" in sys.argv
    if not force and U.market_phase() == "stop":
        now = U.now_et()
        print(f"outside the refresh window ({now:%a %H:%M %Z}) — skipping")
        return

    universe = list(U.SPX_ALL) + list(U.ETF_ALL)
    stocks, indices = fetch_all(universe, dict(B.INDICES), max_workers=25)
    generated_at = datetime.now(timezone.utc).isoformat()

    if "sp500" not in indices:
        print("S&P 500 index missing, aborting rather than writing a bad "
              "snapshot", file=sys.stderr)
        sys.exit(1)

    loaded = sum(1 for t in universe if stocks.get(t, (None,))[0] is not None)
    if loaded < 0.8 * len(universe):
        print(f"only {loaded}/{len(universe)} tickers loaded, aborting",
              file=sys.stderr)
        sys.exit(1)

    # Decide the corporate-action question ONCE, before anything reads a price.
    flagged = B.flag_corporate_moves(stocks)

    stock_rows = sort_by_pct(B.build_rows(U.SPX_ALL, stocks, flagged))
    etf_rows = sort_by_pct(B.build_rows(U.ETF_ALL, stocks, flagged))

    # Movers are drawn from the STOCKS only. Mixing a leveraged or single-sector
    # fund into "top gainers" would mean the list is usually telling you about
    # the same handful of volatile ETFs rather than about the market.
    gainers, losers = compute_movers(stock_rows)

    board_pcts = [r["pct"] for r in stock_rows if r.get("pct") is not None]
    sectors = B.build_sectors(stock_rows, summarize_group, board_pcts)
    sectors.sort(key=lambda s: (s["avgPct"] if s["avgPct"] is not None else -999),
                 reverse=True)
    for s in sectors:
        s["pinned"] = False

    fund_snaps = {t: {"price": stocks[t][0], "pct": stocks[t][1],
                      "pts": stocks[t][2], "dayHigh": stocks[t][3],
                      "dayLow": stocks[t][4]}
                  for t in set(U.SECTOR_ETF.values()) if t in stocks}
    sectors, missing_funds = B.attach_sector_funds(sectors, fund_snaps)
    if missing_funds:
        print(f"  sector fund(s) did not fetch: {missing_funds} — those "
              "sectors show no reference fund this run")

    etf_pcts = [r["pct"] for r in etf_rows if r.get("pct") is not None]
    etf_groups = B.build_etf_groups(etf_rows, summarize_group, etf_pcts)
    etf_groups.sort(key=lambda s: (s["avgPct"] if s["avgPct"] is not None else -999),
                    reverse=True)

    market = B.build_market_block(stock_rows, summarize_group, indices.get("sp500"))

    advancers = sum(1 for r in stock_rows if r.get("pct") is not None and r["pct"] > 0)
    decliners = sum(1 for r in stock_rows if r.get("pct") is not None and r["pct"] < 0)

    payload = {
        "generatedAt": generated_at,
        "market": "US",
        "indices": indices,
        "indexLabels": B.INDEX_LABELS,
        "sectors": [market] + sectors,
        "etfGroups": etf_groups,
        "gainers": gainers,
        "losers": losers,
        "breadth": {"advancers": advancers, "decliners": decliners,
                    "total": len([r for r in stock_rows if r["pct"] is not None])},
        "universe": {"stocks": len(U.SPX_ALL), "etfs": len(U.ETF_ALL),
                     "loaded": loaded},
    }
    with open(OUT, "w") as f:
        json.dump(payload, f, separators=(",", ":"))

    # The EMA board's spot file, from the sweep just done rather than a second
    # one. Must never fail the heatmap write that precedes it.
    try:
        sf, st, ss = write_spot(stocks, U.now_et().isoformat())
        print(f"  spot.json: {sf}/{st} fresh"
              + (f", {ss} carried forward as stale" if ss else ""))
    except Exception as e:
        print(f"  spot.json NOT written ({type(e).__name__}: {e}) — the EMA "
              "board will fall back to its last daily close")

    print(f"wrote board_data.json — {loaded}/{len(universe)} loaded, "
          f"{len(sectors)} sectors, {len(etf_groups)} ETF groups, "
          f"breadth {advancers} up / {decliners} down "
          f"({os.path.getsize(OUT)/1024:.0f} KB)")


if __name__ == "__main__":
    main()
