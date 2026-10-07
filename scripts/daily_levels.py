#!/usr/bin/env python3
"""Daily job: EMAs, 52-week extremes and Point & Figure state for the US board.

CADENCE IS THE WHOLE DESIGN. Every value here is a DAILY value - it can only
change once a day, after the close. Re-deriving it every minute would mean
hundreds of thousands of Yahoo requests a day for 615 names and would get the
IP throttled (Yahoo returns HTTP 429 on both v7/quote and v8/chart once pushed).

So the board is split, exactly as the Indian one is:
    this script       once daily, after close   -> data/levels.json  (heavy)
    refresh_spot.py   every minute in-session   -> data/spot.json    (light)

The page joins the two in the browser: distance-to-44-EMA and above/below are
computed client-side from (live spot, daily 44 EMA), so they move every minute
without anyone re-fetching a single candle.

EMA SEEDING. EMAs are seeded with a simple mean of the first `span` closes and
then run recursively, which is what charting packages do. A 200 EMA therefore
needs more than 200 sessions before its first value means anything; two years
(~500 sessions) is pulled and an EMA whose warm-up is short is not emitted.
`ema_ok` records which spans had enough history - a value that is present but
unreliable is worse than one that is absent.

CORPORATE ACTIONS. Unlike the Indian board this needs no repair layer for
splits: Yahoo's US close is already split-adjusted (measured - see
us_market_core/corp_events.py). What it DOES need is the economic spin-off
rule, and for the same reason the Indian board needs it: an unadjusted or
semantically-wrong series is not only a PnF problem. It runs through the EMAs
and the 52-week range too, which is how that board came to publish a 44 EMA of
587 for a stock trading at 260. Here history simply starts at a large spin-off,
so the EMAs describe the company that exists now.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import pnf_column                                   # noqa: E402
import yfinance as yf                               # noqa: E402
import us_market_core as U                          # noqa: E402
from us_market_core import corp_events as CE        # noqa: E402

EMA_SPANS = [9, 14, 25, 44, 50, 100, 200]

PNF_BOX_PCT = pnf_column.BOX_PCT    # 0.25 - pinned to pnf-charts "short"
PNF_REVERSAL = pnf_column.REVERSAL  # 3

# TWO SCALES, NOT ONE. "short" reverses on 0.25% x 3 = 0.75%, well inside a
# normal daily range, so that column is a one-to-two-day flag rather than a
# trend read and saying only "X demand" oversells it. The "medium" preset
# (1% x 3) is emitted alongside so the fast and slow readings sit side by side.
# Both come from the same ported state machine; only (pct, reversal) differ.
PNF_MED_BOX_PCT = 1.0
PNF_MED_REVERSAL = 3

PNF_CHART_COLUMNS = 25
HISTORY = "2y"
MIN_BARS_FOR_52W = 200           # ~10 months; below this a 52w range is a lie


def ema(series: pd.Series, span: int) -> pd.Series:
    """Mean-seeded EMA, the convention charting packages use."""
    if len(series) < span:
        return pd.Series(dtype="float64")
    seed = series.iloc[:span].mean()
    out = [seed]
    k = 2.0 / (span + 1.0)
    for px in series.iloc[span:]:
        out.append(px * k + out[-1] * (1 - k))
    return pd.Series(out, index=series.index[span - 1:])


def fetch_many(symbols, period=HISTORY, chunk=40):
    """OHLC for many symbols, batched.

    615 separate downloads is 615 round-trips and Yahoo throttles well before
    the end of that. yfinance batches a list into far fewer calls and handles
    the cookie/crumb dance itself. US tickers need no suffix.
    """
    out = {}
    for i in range(0, len(symbols), chunk):
        batch = symbols[i:i + chunk]
        print(f"  [data] {i + 1}-{i + len(batch)} of {len(symbols)}", flush=True)
        try:
            # actions=True carries the split ratios on the frame itself, so
            # the spin-off question is answered without a second sweep.
            raw = yf.download(batch, period=period, interval="1d",
                              progress=False, auto_adjust=False,
                              group_by="ticker", actions=True, threads=True)
        except Exception as e:
            print(f"  [data] batch failed: {e}", flush=True)
            continue
        for s in batch:
            try:
                df = raw[s] if isinstance(raw.columns, pd.MultiIndex) else raw
                keep = [c for c in ("Open", "High", "Low", "Close", "Volume",
                                    "Stock Splits") if c in df.columns]
                df = df[keep].dropna(how="all")
                if not df.empty:
                    out[s] = df
            except Exception:
                pass
    return out


def pnf_col(df, pct=PNF_BOX_PCT, rev=PNF_REVERSAL, key="pnf"):
    """Current PnF column: X = demand, O = supply."""
    null = {key: None, f"{key}_boxes": None}
    if "High" not in df or "Low" not in df:
        return null
    try:
        res = pnf_column.last_column(df["High"].astype(float).tolist(),
                                     df["Low"].astype(float).tolist(), pct, rev)
        if res is None:
            return null
        direction, boxes = res
        return {key: direction, f"{key}_boxes": boxes}
    except Exception:
        return null


def pnf_series(df, pct, rev, keep=PNF_CHART_COLUMNS):
    """The last `keep` columns, packed flat for the browser:
    [dir_of_first, base_box, then two ints per column: bottom-base, height].

    Directions strictly alternate, so only the first is carried. Box INDICES,
    never prices - the page rebuilds the price axis from (base, pct) with the
    same geometric formula, so the two sides cannot round differently.
    """
    if "High" not in df or "Low" not in df:
        return None
    try:
        cols = pnf_column.columns(df["High"].astype(float).tolist(),
                                  df["Low"].astype(float).tolist(), pct, rev)
    except Exception:
        return None
    if not cols:
        return None
    cols = cols[-keep:]
    base = cols[0][1]
    out = [1 if cols[0][0] == pnf_column.X else 0, base]
    for _d, bottom, top in cols:
        out += [bottom - base, top - bottom + 1]
    return out


def truncate_at_spinoff(sym, df, cuts):
    """Start history at a large spin-off, so every level below describes the
    company that exists now rather than the larger one that used to."""
    cut = cuts.get(sym)
    if not cut or df is None or df.empty:
        return df, None
    kept = df[df.index >= pd.Timestamp(cut)]
    if kept.empty:
        return df, None
    return kept, {"date": cut, "bars": len(kept), "dropped": len(df) - len(kept)}


def build_row(sym, meta, df):
    if df is None or df.empty or "Close" not in df:
        return None
    df = df.drop(columns=["Stock Splits"], errors="ignore").dropna(subset=["Close"])
    if len(df) < 60:
        return None

    close = df["Close"].astype(float)
    row = {
        "symbol": sym,
        "name": meta["name"],
        "group": meta["group"],
        "kind": meta["kind"],
        "bars": len(df),
        "prev_close": round(float(close.iloc[-1]), 2),
        # The last COMPLETED daily bar and its own move - i.e. the last trading
        # day, not today. Dated explicitly because the board is read on
        # holidays and before the open, when "latest bar" and "today" are
        # different days and an undated % change silently misleads.
        "last_date": str(df.index[-1])[:10],
        "chg_pct": (round((float(close.iloc[-1]) / float(close.iloc[-2]) - 1) * 100, 2)
                    if len(close) >= 2 and float(close.iloc[-2]) else None),
    }

    ema_ok = {}
    for span in EMA_SPANS:
        s = ema(close, span)
        ok = len(s) >= span
        ema_ok[str(span)] = bool(ok)
        row[f"ema{span}"] = round(float(s.iloc[-1]), 2) if len(s) else None
    row["ema_ok"] = ema_ok

    if len(df) >= MIN_BARS_FOR_52W:
        win = df.iloc[-252:] if len(df) >= 252 else df
        row["high52"] = round(float(win["High"].astype(float).max()), 2)
        row["low52"] = round(float(win["Low"].astype(float).min()), 2)
    else:
        row["high52"] = row["low52"] = None

    row.update(pnf_col(df))
    row.update(pnf_col(df, PNF_MED_BOX_PCT, PNF_MED_REVERSAL, "pnfm"))
    row["_cols"] = {"pnf": pnf_series(df, PNF_BOX_PCT, PNF_REVERSAL),
                    "pnfm": pnf_series(df, PNF_MED_BOX_PCT, PNF_MED_REVERSAL)}
    return row


# Share of rows allowed to lag the newest session before the file counts as
# incomplete. Yahoo posts daily bars PER SYMBOL, so a build run soon after the
# close legitimately catches only some of them.
INCOMPLETE_FRACTION = 0.01


def staleness() -> tuple[bool, str]:
    """(rebuild?, why). Covers BOTH ways levels.json can be out of date.

    Time is the obvious one. Completeness is the one that bit on 2026-10-06:
    the file was written at 20:55 ET, comfortably after that day's 17:00
    boundary, so a time-only check called it current - while 88 of its 613
    rows still carried the PREVIOUS session's bar. Yahoo publishes daily bars
    per symbol and the stragglers arrive over the following hours (the Indian
    board sees the same thing, 414 of 747 the next morning). A time-only gate
    therefore locks the gaps in until the next boundary, which is precisely
    backwards: the later runs exist to collect exactly those stragglers.

    No infinite-rebuild risk: this only decides whether an ALREADY TRIGGERED
    run does its work, and the triggers are a handful of crons plus one kick
    per day. A name that is permanently behind costs a few extra runs, not a
    loop.
    """
    path = DATA / "levels.json"
    if not path.exists():
        return True, "no levels.json yet"
    try:
        doc = json.loads(path.read_text())
        gen = datetime.fromisoformat(doc["generated_at"])
        rows = doc["rows"]
    except Exception:
        return True, "levels.json unreadable"

    if gen.astimezone(U.EXCHANGE_TZ) < U.last_post_close():
        return True, f"written {gen:%Y-%m-%d %H:%M} ET, before the last close"

    dates = [r.get("last_date") for r in rows if r.get("last_date")]
    if not dates:
        return True, "no dated rows"
    newest = max(dates)
    behind = sum(1 for d in dates if d < newest)
    if behind > max(1, int(len(dates) * INCOMPLETE_FRACTION)):
        return True, (f"{behind} of {len(dates)} rows still lag {newest} "
                      "— Yahoo posts daily bars per symbol, so the stragglers "
                      "are collected by re-running")
    return False, f"current for {newest}, {behind} row(s) behind"


def is_stale() -> bool:
    return staleness()[0]


def main() -> int:
    if "--if-stale" in sys.argv:
        rebuild, why = staleness()
        if not rebuild:
            print(f"levels.json {why}; skipping")
            return 0
        print(f"rebuilding: {why}")
    meta = {}
    for sym in U.SPX_ALL:
        meta[sym] = {"name": U.display_name(sym), "kind": "stock",
                     "group": U.SECTOR_OF.get(sym, "Unclassified")}
    member_of = {t: g for g, ts in U.ETF_GROUPS.items() for t in ts}
    for sym in U.ETF_ALL:
        meta[sym] = {"name": U.display_name(sym), "kind": "etf",
                     "group": member_of.get(sym, "ETF")}
    symbols = sorted(meta)

    print(f"fetching {len(symbols)} symbols x {HISTORY} daily ...", flush=True)
    frames = fetch_many(symbols, period=HISTORY)

    # Retry whatever the batch dropped. A yfinance batch can lose a symbol for
    # no durable reason - COST built on one run and vanished on the next - and
    # downstream that is indistinguishable from a name with no history, so it
    # would quietly leave the board. Retried in small batches, and whatever is
    # still absent is named as a FETCH failure rather than filed under
    # "missing", which reads like a data problem.
    for attempt in (1, 2):
        gone = [s_ for s_ in symbols if s_ not in frames]
        if not gone:
            break
        print(f"  [retry {attempt}] {len(gone)} symbol(s): "
              f"{', '.join(gone[:12])}{' …' if len(gone) > 12 else ''}", flush=True)
        frames.update(fetch_many(gone, period=HISTORY, chunk=10))
    still = [s_ for s_ in symbols if s_ not in frames]
    if still:
        print(f"  FETCH FAILED after retries (not a history problem): {still}")

    # Decide the spin-off question ONCE, before anything reads a price.
    #
    # NOT by the size of a gap. Yahoo BACK-ADJUSTS a spin-off, so the parent's
    # series is continuous and a move-size filter never fires - the first
    # version of this script used one and let DuPont keep two years of history
    # belonging to a company 58% larger, feeding straight into its 44 EMA. The
    # verdict comes from the split RATIO on the frame, classified by the same
    # rule the RRG uses, so the two boards cannot disagree about a name.
    cuts = {}
    for sym, df in frames.items():
        if df is None or df.empty or "Stock Splits" not in df.columns:
            continue
        sp = df["Stock Splits"]
        for ts, r in sp[sp.fillna(0) != 0].items():
            kind, _removed = CE.classify_ratio(float(r))
            if kind == "economic":
                day = str(ts)[:10]
                # the LATEST economic event wins: after two separations the
                # bars before the second one are still the wrong company
                if day > cuts.get(sym, ""):
                    cuts[sym] = day
    # A separation that has happened but which Yahoo has not published yet is
    # invisible above, so the hand-verified table is applied on top.
    for sym in frames:
        cut = CE.truncate_from(CE.overrides_for(sym))
        if cut and cut > cuts.get(sym, ""):
            cuts[sym] = cut

    # DROP THE FORMING BAR. Yahoo serves today's partial candle while the
    # session is open, and every value here is labelled as the last COMPLETED
    # day - prev_close, chg_pct, last_date, the 52-week extremes and the PnF
    # column would all silently describe a half-finished session. The scheduled
    # run is post-close and never sees one; a manual or early run does. Same
    # trap that had the live scan scoring a bar with ~1% of its normal volume.
    if U.is_market_hours():
        today = U.now_et().date().isoformat()
        dropped = 0
        for sym, df in list(frames.items()):
            if df is not None and len(df) and str(df.index[-1])[:10] == today:
                frames[sym] = df.iloc[:-1]
                dropped += 1
        if dropped:
            print(f"  session still open — dropped the forming {today} bar "
                  f"from {dropped} symbol(s)")

    rows, missing, truncated = [], [], []
    for sym in symbols:
        df = frames.get(sym)
        df, note = truncate_at_spinoff(sym, df, cuts)
        if note:
            truncated.append((sym, note))
        r = build_row(sym, meta[sym], df)
        if r is None:
            missing.append(sym)
        else:
            if note:
                r["history_from"] = note["date"]
            rows.append(r)

    if truncated:
        print(f"history restarted at a large spin-off ({len(truncated)}):")
        for sym, n in sorted(truncated):
            print(f"   {sym:<7} from {n['date']}  {n['bars']} bars kept, "
                  f"{n['dropped']} dropped")

    out = {
        "generated_at": datetime.now(U.EXCHANGE_TZ).isoformat(),
        "history": HISTORY,
        "ema_spans": EMA_SPANS,
        "pnf": {"available": True, "box_pct": PNF_BOX_PCT,
                "reversal": PNF_REVERSAL,
                "medium_box_pct": PNF_MED_BOX_PCT,
                "medium_reversal": PNF_MED_REVERSAL,
                "legend": {"X": "demand", "O": "supply"}},
        "counts": {"universe": len(symbols), "built": len(rows),
                   "missing": len(missing),
                   "stocks": sum(1 for r in rows if r["kind"] == "stock"),
                   "etfs": sum(1 for r in rows if r["kind"] == "etf")},
        "groups": {"sectors": list(U.SPX_SECTORS), "etf": list(U.ETF_GROUPS)},
        "spinoffs": [{"symbol": s, **n} for s, n in truncated],
        "missing": sorted(missing),
        "rows": rows,
    }

    charts = {}
    for r in rows:
        c = r.pop("_cols", None)
        if c and (c.get("pnf") or c.get("pnfm")):
            charts[r["symbol"]] = {k: v for k, v in c.items() if v}

    DATA.mkdir(exist_ok=True)
    (DATA / "levels.json").write_text(json.dumps(out, separators=(",", ":")))
    (DATA / "pnf.json").write_text(json.dumps({
        "generated_at": out["generated_at"],
        "base": pnf_column.BASE,
        "max_columns": PNF_CHART_COLUMNS,
        "presets": {"pnf": {"box_pct": PNF_BOX_PCT, "reversal": PNF_REVERSAL},
                    "pnfm": {"box_pct": PNF_MED_BOX_PCT,
                             "reversal": PNF_MED_REVERSAL}},
        "format": "[dir_of_first(1=X,0=O), base_box, (bottom-base, height) per column]",
        "cols": charts,
    }, separators=(",", ":")))

    print(f"OK  built {len(rows)}/{len(symbols)}  missing={len(missing)}")
    if missing:
        print("    missing:", ", ".join(sorted(missing)[:20]),
              "..." if len(missing) > 20 else "")
    return 0 if rows else 4


if __name__ == "__main__":
    raise SystemExit(main())
