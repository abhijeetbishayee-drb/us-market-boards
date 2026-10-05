"""Live snapshot logic for the US heatmap board.

WHAT IS REUSED AND WHAT IS NOT. nifty-heatmap-core's `fetch_one`/`fetch_all`,
`summarize_group`, `constituent_range` and `outlier_indices` carry no market
assumptions, so they are imported. Its `build_rows`, `build_sectors`,
`attach_sector_indices` and `build_pinned_groups` all reach into Indian tables
(CASH_ONLY, the NSE sectoral index map, the Bank Nifty constituent list, the
corporate-action table), so those four have US counterparts here rather than
being bent into shape.

THE SECTOR'S REFERENCE SERIES IS A REAL FUND. The Indian board attaches an NSE
sectoral index where one genuinely matches the group and falls back to a
constituent-derived range for the eleven groups where none does. Here every one
of the eleven GICS sectors has an exact, liquid fund - its Select Sector SPDR -
so the fallback never fires and the comparison is always like-for-like: the
equal-weighted breadth of our constituents against the cap-weighted fund.

THE LIVE SNAPSHOT GETS ONE CHANCE. A historical series can be repaired at any
time and the next rebuild fixes the whole thing; an ex-date print is wrong for
that session only. The Indian board handles this with a curated table that must
be populated BEFORE the ex-date. Here the equivalent risk is much smaller - US
closes are split-adjusted - but it is not zero, because an adjustment can lag
the event by a session (Corteva traded for days at a 6x gap before Yahoo
published anything). So `flag_corporate_moves` checks any name printing beyond
LIVE_JUMP against Yahoo's own split feed and the hand-verified overrides, and
where it finds a match it NULLS the percentage rather than averaging a cliff
into the sector. Where it finds nothing it leaves the number alone and says so.
"""

import sys
from datetime import date, datetime, timedelta, timezone

from . import (BENCHMARK, ETF_GROUPS, ETF_NAME, SECTOR_ETF, SECTOR_OF,
               SPX_SECTORS, display_name, short_name)
from . import corp_events as CE

# A one-day move big enough to be worth CHECKING against the event feed. It is
# a trigger for a lookup, never a verdict: the measured 5-year distribution of
# US gaps beyond this is overwhelmingly real news (MRNA +177%, NFLX -35%,
# META -26%, INTC -26%), and those must print exactly as they are.
LIVE_JUMP = 0.25

# Headline indices shown above the board. Price indices, matching the
# benchmark the RRG is built against.
INDICES = {
    "^GSPC": "sp500",
    "^NDX": "nasdaq100",
    "^DJI": "dow",
    "^RUT": "russell2000",
}
INDEX_LABELS = {
    "sp500": "S&P 500",
    "nasdaq100": "NASDAQ 100",
    "dow": "DOW JONES",
    "russell2000": "RUSSELL 2000",
}


def build_rows(tickers, stocks, flagged=None):
    """Turn {ticker: (price, pct, pts, high, low)} into the row shape the board
    renders. `flagged` maps ticker -> corporate-action note; a flagged name has
    its percentage nulled so sector averages, movers and the day-range bar all
    skip it for the session instead of averaging in an ex-date cliff.
    """
    flagged = flagged or {}
    rows = []
    for ticker in tickers:
        price, pct, pts, day_high, day_low = stocks.get(
            ticker, (None, None, None, None, None))
        ca = None
        note = flagged.get(ticker)
        if note is not None:
            ca = {"what": note["what"], "kind": note["kind"],
                  "date": note["date"], "rawPct": pct, "adjusted": False}
            pct, pts = None, None
        off_low = (price - day_low) / day_low * 100 if price is not None and day_low else None
        off_high = (price - day_high) / day_high * 100 if price is not None and day_high else None
        rows.append({
            "ticker": ticker,
            "name": short_name(ticker),
            "full": display_name(ticker),
            "price": price,
            "pct": pct,
            "pts": pts,
            "offLow": off_low,
            "offHigh": off_high,
            "dayHigh": day_high,
            "dayLow": day_low,
            "ca": ca,
        })
    return rows


def flag_corporate_moves(stocks, within_days=4, log=print):
    """Find names whose SNAPSHOT move is large AND explained by a real event.

    Returns {ticker: note}. Deliberately two-stage: the size test only decides
    whom to look up, and the verdict comes from Yahoo's split feed or the
    hand-verified override table. A big move with no event behind it is left
    completely alone and reported, because on this market that is nearly always
    genuine news and smoothing it would delete the best information on the
    board.
    """
    suspects = []
    for t, (price, pct, pts, hi, lo) in stocks.items():
        if pct is None:
            continue
        if abs(pct) / 100.0 >= LIVE_JUMP:
            suspects.append((t, pct))
    if not suspects:
        return {}

    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=within_days)).isoformat()
    flagged, unexplained = {}, []
    for t, pct in sorted(suspects, key=lambda x: -abs(x[1])):
        recent = [e for e in CE.classify(t, rng="1mo") if e["date"] >= cutoff]
        if recent:
            ev = max(recent, key=lambda e: e["date"])
            flagged[t] = ev
            log(f"  {t}: {pct:+.1f}% today is {ev['what']} ({ev['date']}) — "
                "percentage withheld for this session so it is not averaged "
                "into its sector")
        else:
            unexplained.append((t, pct))
    if unexplained:
        log(f"  {len(unexplained)} large move(s) with no corporate event behind "
            "them, printed as they are: "
            + ", ".join(f"{t} {p:+.1f}%" for t, p in unexplained[:10]))
    return flagged


def build_sectors(rows, summarize_group, board_pcts):
    """The eleven GICS sectors, equal-weighted across our constituents."""
    by_sector = {sector: [] for sector in SPX_SECTORS}
    for r in rows:
        sector = SECTOR_OF.get(r["ticker"])
        if sector is not None:
            by_sector[sector].append(r)
    return [summarize_group(sector, srows, board_pcts)
            for sector, srows in by_sector.items()]


def build_etf_groups(rows, summarize_group, board_pcts):
    """The eight curated ETF groups, built exactly as the sectors are so the
    two kinds of block can never drift apart in how they are computed."""
    by_group = {g: [] for g in ETF_GROUPS}
    member_of = {t: g for g, ts in ETF_GROUPS.items() for t in ts}
    for r in rows:
        g = member_of.get(r["ticker"])
        if g is not None:
            by_group[g].append(r)
    out = []
    for g, grows in by_group.items():
        block = summarize_group(g, grows, board_pcts)
        block["kind"] = "etf"
        out.append(block)
    return out


def attach_sector_funds(sectors, fund_snaps):
    """Attach each sector's Select Sector SPDR snapshot.

    Unlike the Indian board there is no fallback branch, because there is no
    sector without a fund - so if one is missing that is a FETCH failure, not a
    mapping gap, and it is reported as such rather than quietly rendering "no
    index".
    """
    missing = []
    for s in sectors:
        fund = SECTOR_ETF.get(s["sector"])
        snap = fund_snaps.get(fund) if fund else None
        if fund and snap and snap.get("price") is not None:
            s["index"] = dict(snap, label=fund, ticker=fund,
                              name=ETF_NAME.get(fund, fund))
        else:
            s["index"] = None
            if fund:
                missing.append(fund)
    return sectors, missing


def build_market_block(rows, summarize_group, index_snap):
    """One block for the whole index, pinned above the sectors - the direct
    analogue of the Indian board's pinned NIFTY 50 group."""
    block = summarize_group("S&P 500", rows)
    block["index"] = (dict(index_snap, label="S&P 500", ticker=BENCHMARK)
                      if index_snap and index_snap.get("price") is not None else None)
    block["pinned"] = True
    return block
