"""Build rrg_data.json - Relative Rotation points for the US board.

Runs once a day after the close: it pulls 5y of daily history for ~615 symbols
and RRG is read off daily/weekly bars, so a minute cadence would buy nothing.

Staleness is decided HERE, not by a GitHub cron, for the reason established on
the Indian boards: GitHub throttles scheduled workflows hard (observed 1-2h
late, once 7h30m). The external pinger calls this with --if-stale and it exits
immediately unless the file predates the most recent post-close boundary.

FOUR THINGS ARE PLOTTED, and they are different kinds of object:
  sectors     the eleven Select Sector SPDRs - real funds, real weights
  stocks      the 503 S&P 500 constituents
  etfs        112 curated ETFs, grouped
  etfGroups   equal-weight aggregates of those groups (no fund exists)
Each symbol is z-scored over its own history, so these can be read on one
scale; they are separate views because they answer separate questions.
"""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "nifty-heatmap-core"))

import us_market_core as U
from us_market_core import corp_events as CE
from nifty_heatmap_core.rrg import (
    DAILY, WEEKLY, to_weekly, rrg_tail, equal_weight_series, min_bars,
    vol_tail, ret_tail, outlier_indices,
)

OUT = os.path.join(REPO_ROOT, "rrg_data.json")


def is_stale(path):
    if not os.path.exists(path):
        return True
    try:
        with open(path) as f:
            gen = datetime.fromisoformat(json.load(f)["generatedAt"])
    except Exception:
        return True
    return gen.astimezone(U.EXCHANGE_TZ) < U.last_post_close()


def fetch(ticker, rng="5y"):
    """Closes AND split events in one request - 615 symbols is enough that a
    second sweep purely for events would double the build for no new data."""
    sym = ticker.replace("^", "%5E")
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
           f"?interval=1d&range={rng}&events=split")
    try:
        r = requests.get(url, headers=U.HEADERS, timeout=30)
        d = r.json()["chart"]["result"][0]
        q = d["indicators"]["quote"][0]
        pairs = [(t, c) for t, c in zip(d["timestamp"], q["close"]) if c is not None]
        events = []
        for v in (d.get("events", {}).get("splits", {}) or {}).values():
            num, den = CE._parse_ratio(v.get("splitRatio"), v.get("numerator"),
                                       v.get("denominator"))
            if num and den:
                day = datetime.fromtimestamp(v["date"], timezone.utc).date().isoformat()
                events.append((day, num, den, v.get("splitRatio")))
        return ticker, pairs, sorted(events)
    except Exception:
        return ticker, [], []


def fetch_all(tickers, workers=16, retries=2):
    """Fetch every ticker, retrying the empties.

    A transient Yahoo failure returns no history, which downstream looks
    identical to a genuinely short series - the name lands in `excluded` with
    the reason "only 0 of 192 daily bars", which is both wrong and plausible
    enough that nobody would question it. EXC did exactly this on the first
    run of this script while returning a full series moments earlier. So the
    empties are retried, and whatever is still empty afterwards is PRINTED as
    a fetch failure rather than dressed up as a history problem.
    """
    out = {}
    todo = list(tickers)
    for attempt in range(retries + 1):
        if not todo:
            break
        if attempt:
            print(f"  retrying {len(todo)} empty result(s): "
                  f"{sorted(todo)[:12]}{'…' if len(todo) > 12 else ''}")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for f in as_completed([ex.submit(fetch, t) for t in todo]):
                t, pairs, events = f.result()
                out[t] = (pairs, events)
        todo = [t for t in todo if not out.get(t, ([], []))[0]]
    return out


def verdicts(ticker, events):
    """corp_events.classify, but on events already in hand, plus any
    hand-verified override for a separation Yahoo has not posted yet."""
    out = list(CE.overrides_for(ticker))
    for day, num, den, raw in events:
        r = num / den
        if CE.is_declared_split(num, den):
            out.append({"date": day, "ratio": raw, "kind": "split",
                        "removed": None,
                        "what": f"{raw} split, already reflected in close"})
            continue
        removed = 1 - 1 / r if r > 0 else 0.0
        economic = removed >= CE.ECONOMIC_SPLIT
        out.append({"date": day, "ratio": raw,
                    "kind": "economic" if economic else "cosmetic",
                    "removed": removed,
                    "what": (f"spin-off removing {removed*100:.1f}% of the company"
                             + ("; history starts here" if economic
                                else "; back-adjusted, history kept"))})
    return out


def align(pairs, dates):
    """Project a series onto `dates`, carrying the last known close forward.
    Returns (values_from_first, first_index) or None."""
    if not pairs:
        return None
    m = dict(pairs)
    out, last = [], None
    for d in dates:
        if d in m:
            last = m[d]
        out.append(last)
    first = next((i for i, v in enumerate(out) if v is not None), None)
    if first is None:
        return None
    return out[first:], first


def prepare(aligned_pair, bench_vals, weekly, dates):
    if aligned_pair is None:
        return None
    vals, first = aligned_pair
    bench = bench_vals[first:]
    d = dates[first:]
    if weekly:
        wd, vals = to_weekly(d, vals)
        _, bench = to_weekly(d, bench)
        d = wd
    return vals, bench, d


def points(prepped, cfg):
    if prepped is None:
        return None
    vals, bench = prepped[0], prepped[1]
    if len(vals) < min_bars(cfg):
        return None
    return rrg_tail(vals, bench, cfg)


def record(name, ticker, group, industry, prepped, tail, cfg, extra=None):
    r = {"name": name, "ticker": ticker, "sector": group, "industry": industry,
         "tail": tail, "vol": vol_tail(prepped[0], cfg, len(tail)),
         "ret": ret_tail(prepped[0], cfg, len(tail)),
         "full": U.display_name(ticker)}
    if extra:
        r.update(extra)
    return r


def basket(members, aligned, bench_vals, dates, need, cfg, weekly):
    """Equal-weight aggregate of whatever covers the whole window.

    Constituents not present across the WHOLE window are dropped rather than
    shortening the basket - on the Indian board a single recent listing cut an
    entire sector's history to 75 bars before this rule existed.
    """
    start = max(0, len(dates) - need)
    cons, used, dropped = [], [], []
    for t in members:
        a = aligned.get(t)
        if a is not None and a[1] <= start:
            cons.append(a[0][start - a[1]:])
            used.append(t)
        else:
            dropped.append(U.short_name(t))
    if not cons:
        return None, [], dropped, None
    synth = equal_weight_series(cons)
    if not synth:
        return None, used, dropped, None
    prepped = prepare((synth, start), bench_vals, weekly, dates)
    return points(prepped, cfg), used, dropped, prepped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--if-stale", action="store_true",
                    help="exit without fetching unless the file predates the last post-close")
    ap.add_argument("--force", action="store_true", help="build regardless")
    args = ap.parse_args()

    if args.if_stale and not args.force and not is_stale(OUT):
        print("rrg_data.json is current for the latest close; skipping")
        return

    orphans = sorted(set(U.SPX_SECTORS) - set(U.INDUSTRY_OF))
    if orphans:
        print(f"ERROR: sectors with no broad industry: {orphans}", file=sys.stderr)
        sys.exit(1)
    missing_etf = sorted(set(U.SECTOR_ETF.values()) - set(U.ETF_ALL))
    if missing_etf:
        print(f"ERROR: sector funds not in the ETF universe: {missing_etf}",
              file=sys.stderr)
        sys.exit(1)

    universe = sorted(set(U.SPX_ALL) | set(U.ETF_ALL) | {U.BENCHMARK})
    print(f"fetching 5y daily history + split events for {len(universe)} symbols…")
    raw = fetch_all(universe)
    empty = sorted(t for t, (p, _) in raw.items() if not p)
    if empty:
        print(f"  FETCH FAILED for {len(empty)} symbol(s) after retries "
              f"(not a history problem): {empty}")
    if len(empty) > 25:
        print(f"ERROR: {len(empty)} symbols failed to fetch - that is a Yahoo "
              "or network problem, not a market one; refusing to publish a "
              "board with a hole this size", file=sys.stderr)
        sys.exit(1)

    bench_pairs = raw.get(U.BENCHMARK, ([], []))[0]
    if len(bench_pairs) < 300:
        print("benchmark history missing/too short, aborting", file=sys.stderr)
        sys.exit(1)

    # Yahoo serves the FORMING candle. A build started before the close would
    # carry a partial session as if it were a whole one - the trap that made
    # the live scan score a bar with ~1% of its normal volume. The scheduled
    # run is post-close and never sees it; --force and any early dispatch do.
    last_et = datetime.fromtimestamp(bench_pairs[-1][0], U.EXCHANGE_TZ)
    now = U.now_et()
    if last_et.date() == now.date() and now.hour < U.MARKET_CLOSE[0]:
        print(f"  dropping the forming {last_et.date()} bar "
              f"(session still open at {now:%H:%M %Z})")
        bench_pairs = bench_pairs[:-1]

    dates = [t for t, _ in bench_pairs]
    bench_vals = [c for _, c in bench_pairs]
    print(f"  benchmark {U.BENCHMARK}: {len(dates)} sessions through "
          f"{datetime.fromtimestamp(dates[-1], U.EXCHANGE_TZ).date()}")

    # ── corporate events ────────────────────────────────────────────────
    aligned, repaired, unexplained = {}, {}, []
    for t, (pairs, events) in raw.items():
        if not pairs:
            continue
        evs = verdicts(t, events)
        cut = CE.truncate_from(evs)
        if cut:
            keep = [(ts, c) for ts, c in pairs
                    if datetime.fromtimestamp(ts, timezone.utc).date().isoformat() >= cut]
            ev = max((e for e in evs if e["kind"] == "economic"),
                     key=lambda e: e["date"])
            repaired[t] = {"what": ev["what"], "date": cut, "kind": "economic",
                           "ratio": ev["ratio"], "removed": ev["removed"],
                           "bars": len(keep)}
            pairs = keep
        unexplained.extend(CE.unexplained(t, pairs, evs))
        a = align(pairs, dates)
        if a is not None:
            aligned[t] = a

    cos = [t for t, (_, e) in raw.items()
           if any(v["kind"] == "cosmetic" for v in verdicts(t, e))]
    print(f"  corporate events: {len(repaired)} economic (history restarts), "
          f"{len(cos)} cosmetic (back-adjusted, kept)")
    for t, n in sorted(repaired.items()):
        print(f"     {t:<7} {n['date']}  {n['ratio']:<11} "
              f"removed {n['removed']*100:5.1f}%  {n['bars']} bars kept")
    if unexplained:
        print(f"  {len(unexplained)} large day move(s) with no split record, "
              "LEFT ALONE (most are real news):")
        for u in sorted(unexplained, key=lambda x: -abs(x["move"]))[:12]:
            print(f"     {u['ticker']:<7} {u['date']}  {u['move']*100:+7.1f}%")

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "lastBar": datetime.fromtimestamp(dates[-1], U.EXCHANGE_TZ).date().isoformat(),
        "market": "US",
        "benchmark": {"ticker": U.BENCHMARK, "label": U.BENCH_LABEL},
        "repaired": {t: {"what": n["what"], "date": n["date"], "kind": n["kind"]}
                     for t, n in sorted(repaired.items())},
        "note": ("Approximation of JdK RS-Ratio/RS-Momentum, not the licensed "
                 "formula. Every symbol is z-scored over an identical window; "
                 "symbols with less history are excluded, not rescaled. vol is "
                 "trailing annualised realised volatility in percent, one value "
                 "per tail point; ret is the trailing absolute return over the "
                 "stretch the tail covers. Either can be the Z axis of the 3D "
                 "view. US splits need no repair - Yahoo's close is already "
                 "split-adjusted and correctly dated, unlike its Indian series. "
                 "Spin-offs are judged by size: above 20% of the parent's value "
                 "removed, usable history starts at the event, because the "
                 "earlier bars belong to a larger company; below it the series "
                 "is kept on Yahoo's back-adjusted basis."),
        "config": {"daily": DAILY, "weekly": WEEKLY},
        "industries": U.BROAD_INDUSTRIES,
        "etfGroupNames": list(U.ETF_GROUPS),
        "sectors": {}, "stocks": {}, "etfs": {}, "etfGroups": {}, "excluded": {},
        "universe": {},
    }

    for period, cfg, weekly in (("daily", DAILY, False), ("weekly", WEEKLY, True)):
        excluded = []

        def exclude(ticker, name, group, prepped, kind=None):
            note = repaired.get(ticker)
            if note:
                have = len(prepped[2]) if prepped else 0
                reason = (f"{note['what']} on {note['date']} — only {have} of "
                          f"{min_bars(cfg)} bars are post-event, so there is "
                          "not yet enough of this company's own history to "
                          "compare it with its peers")
            else:
                have = len(prepped[0]) if prepped else 0
                unit = "weekly bars" if weekly else "daily bars"
                reason = (f"only {have} of the {min_bars(cfg)} {unit} every "
                          "symbol is normalised over")
            e = {"name": name, "ticker": ticker, "sector": group,
                 "reason": reason, "ca": bool(note)}
            if kind:
                e["kind"] = kind
            excluded.append(e)

        # ── stocks ──────────────────────────────────────────────────────
        stocks = []
        for sector, tickers in U.SPX_SECTORS.items():
            for t in tickers:
                prepped = prepare(aligned.get(t), bench_vals, weekly, dates)
                tail = points(prepped, cfg)
                if tail is None:
                    exclude(t, U.short_name(t), sector, prepped)
                    continue
                stocks.append(record(U.short_name(t), t, sector,
                                     U.INDUSTRY_OF.get(sector), prepped, tail, cfg))

        # ── individual ETFs ─────────────────────────────────────────────
        etfs = []
        for group, tickers in U.ETF_GROUPS.items():
            for t in tickers:
                prepped = prepare(aligned.get(t), bench_vals, weekly, dates)
                tail = points(prepped, cfg)
                if tail is None:
                    exclude(t, t, group, prepped, kind="etf")
                    continue
                etfs.append(record(t, t, group, group, prepped, tail, cfg,
                                   {"isEtf": True}))

        # ── sectors: the eleven Select Sector SPDRs, as funds ───────────
        # Not equal-weight baskets. The funds exist, are liquid, carry real
        # index weights and are the thing a reader can actually buy; the
        # Indian board only builds baskets because no such fund exists there.
        sectors = []
        for sector, fund in U.SECTOR_ETF.items():
            prepped = prepare(aligned.get(fund), bench_vals, weekly, dates)
            tail = points(prepped, cfg)
            if tail is None:
                excluded.append({"name": sector, "kind": "sector", "ticker": fund,
                                 "reason": f"{fund} does not cover the window"})
                continue
            sectors.append(record(sector, fund, sector, U.INDUSTRY_OF.get(sector),
                                  prepped, tail, cfg,
                                  {"kind": "fund",
                                   "label": f"{sector} ({fund})",
                                   "count": len(U.SPX_SECTORS.get(sector, [])),
                                   "full": U.display_name(fund)}))

        # ── ETF groups: equal-weight, because no fund covers a group ────
        need = min_bars(cfg) if not weekly else min_bars(cfg) * 5 + 60
        groups = []
        for group, tickers in U.ETF_GROUPS.items():
            tail, used, dropped, prepped = basket(tickers, aligned, bench_vals,
                                                  dates, need, cfg, weekly)
            if tail is None:
                excluded.append({"name": group, "kind": "etfGroup",
                                 "reason": "no member covers the window"})
                continue
            groups.append({"name": group, "kind": "synthetic", "industry": group,
                           "label": f"{group} (equal-weight)",
                           "count": len(tickers), "basis": len(used),
                           "dropped": dropped, "tail": tail,
                           "vol": vol_tail(prepped[0], cfg, len(tail)),
                           "ret": ret_tail(prepped[0], cfg, len(tail))})

        # Nothing may vanish silently. A name could fall through a gap in this
        # script and simply not appear, which no one would notice on a
        # 500-bubble board - so every S&P and ETF ticker must be either
        # plotted or excluded WITH A REASON the page can print.
        accounted = ({s["ticker"] for s in stocks} | {e["ticker"] for e in etfs}
                     | {e["ticker"] for e in excluded if e.get("ticker")})
        gone = (set(U.SPX_ALL) | set(U.ETF_ALL)) - accounted
        if gone:
            print(f"  ERROR: {len(gone)} symbol(s) neither plotted nor excluded "
                  f"on {period}: {sorted(gone)}", file=sys.stderr)
            sys.exit(1)

        out["stocks"][period] = stocks
        out["sectors"][period] = sectors
        out["etfs"][period] = etfs
        out["etfGroups"][period] = groups
        out["excluded"][period] = excluded
        out["universe"][period] = {
            "stocksTotal": len(U.SPX_ALL), "stocksPlotted": len(stocks),
            "etfsTotal": len(U.ETF_ALL), "etfsPlotted": len(etfs),
            "excluded": len([e for e in excluded if e.get("ticker")]),
        }
        print(f"  {period:6}: {len(sectors)} sectors, {len(stocks)} stocks, "
              f"{len(etfs)} ETFs, {len(groups)} ETF groups, "
              f"{len(excluded)} excluded")

    with open(OUT, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"wrote {OUT} ({os.path.getsize(OUT)/1024:.0f} KB)")


if __name__ == "__main__":
    main()
