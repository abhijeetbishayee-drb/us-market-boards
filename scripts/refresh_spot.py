#!/usr/bin/env python3
"""1-minute job: last traded price only, for every name on the EMA board.

WHY THIS IS SEPARATE FROM daily_levels.py. EMAs, 52-week extremes and the PnF
column are daily values. Only spot moves intraday, so only spot is fetched
here. The page recomputes distance-to-44-EMA and above/below in the browser
from (spot, daily EMA), which is why a minute-cadence board costs one small
sweep instead of 615 full histories.

WHY yfinance AND NOT RAW HTTP - MEASURED ON THE INDIAN BOARD, NOT ASSUMED. The
first version of that script called Yahoo's JSON endpoints directly
(v7/finance/quote in batches with a cookie+crumb pair, falling back to
v8/finance/chart per symbol). On a GitHub Actions runner it scored fresh=0/500
in 7.5 seconds - every request refused at the edge, because Actions IP ranges
are heavily used for Yahoo scraping. In the same workflow, on the same runners,
yf.download() pulled 500 symbols successfully. yfinance maintains the session
those raw calls could not establish. Reachability was the deciding factor, not
speed.

Note this board therefore uses a DIFFERENT transport from the heatmap, which
reads the chart endpoint over plain requests and does work from Actions. The
difference is the endpoint, not the runner: v8/chart one symbol at a time is
tolerated where a batched v7/quote is not.

STALENESS IS REPORTED, NEVER HIDDEN. A symbol that fails keeps its previous
price and is marked stale. The page greys those rows and the header shows
fresh/total. A board that shows a ten-minute-old price as if it were live is
worse than one that admits it is behind.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))

import us_market_core as U  # noqa: E402

CHUNK = 50
PERIOD = "1d"       # just today's bar; its Close is the live price intraday


def fetch_spot(symbols):
    """{symbol: last price}. Missing symbols are simply absent."""
    out = {}
    for i in range(0, len(symbols), CHUNK):
        batch = symbols[i:i + CHUNK]
        try:
            raw = yf.download(batch, period=PERIOD, interval="1d",
                              progress=False, auto_adjust=False,
                              group_by="ticker", threads=True)
        except Exception as e:
            print(f"  batch {i // CHUNK}: {e}", flush=True)
            continue
        if raw is None or len(raw) == 0:
            continue
        for s in batch:
            try:
                df = raw[s] if isinstance(raw.columns, pd.MultiIndex) else raw
                close = df["Close"].dropna()
                if len(close):
                    out[s] = float(close.iloc[-1])
            except Exception:
                pass
    return out


def main() -> int:
    force = "--force" in sys.argv
    if not force and U.market_phase() == "stop":
        print(f"outside the refresh window ({U.now_et():%a %H:%M %Z}) — skipping")
        return 0

    levels_path = DATA / "levels.json"
    if not levels_path.exists():
        print("data/levels.json missing — run daily_levels.py first",
              file=sys.stderr)
        return 4
    levels = json.loads(levels_path.read_text())
    symbols = sorted({r["symbol"] for r in levels["rows"]})

    prev = {}
    spot_path = DATA / "spot.json"
    if spot_path.exists():
        try:
            prev = (json.loads(spot_path.read_text()) or {}).get("spot", {})
        except Exception:
            prev = {}

    fresh = fetch_spot(symbols)
    spot, stale = {}, []
    for s in symbols:
        if s in fresh:
            spot[s] = round(fresh[s], 2)
        elif s in prev:
            spot[s] = prev[s]
            stale.append(s)

    DATA.mkdir(exist_ok=True)
    spot_path.write_text(json.dumps({
        "generated_at": datetime.now(U.EXCHANGE_TZ).isoformat(),
        "fresh": len(fresh),
        "total": len(symbols),
        "stale": sorted(stale),
        "spot": spot,
    }, separators=(",", ":")))

    print(f"spot: fresh {len(fresh)}/{len(symbols)}"
          + (f", {len(stale)} carried forward as stale" if stale else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
