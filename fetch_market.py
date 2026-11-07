"""Pull market data for the Levels & Ledger page and write market.json.

Usage: python fetch_market.py [out_path]
The JSON goes into the artifact's db as market/today (ArtifactData set with file_path).
Levels, expected move and setups are computed in the page from the daily candles.
The script also replays the setups on Yahoo's 5-minute bars:
  replay   - the last finished session, setup by setup
  backtest - every session Yahoo has 5-minute bars for (about 60), with rule variants
"""
import json, math, sys, urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TICKERS = ["QQQ", "SPY", "GLD"]
KEEP = 90
ET = ZoneInfo("America/New_York")
R = 0.04


def _get(sym, rng, interval):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval={interval}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["chart"]["result"][0]


def fetch(sym):
    res = _get(sym, "1y", "1d")
    q = res["indicators"]["quote"][0]
    meta = res["meta"]
    market_day = datetime.fromtimestamp(meta["regularMarketTime"], ET).strftime("%Y-%m-%d")
    candles = []
    for i, ts in enumerate(res["timestamp"]):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        d = datetime.fromtimestamp(ts, ET).strftime("%Y-%m-%d")
        if None in (h, l, c) and d == market_day and candles:
            # Yahoo often leaves the latest daily bar empty; rebuild it from the quote summary.
            o = o if o is not None else candles[-1][4]
            h, l, c = meta.get("regularMarketDayHigh"), meta.get("regularMarketDayLow"), meta.get("regularMarketPrice")
        if None in (o, h, l, c):
            continue
        if candles and not (l <= o <= h):
            # Early in the session Yahoo sometimes repeats the prior day's open; fall back to the prior close.
            o = min(max(candles[-1][4], l), h)
        candles.append([d, round(o, 2), round(h, 2), round(l, 2), round(c, 2)])
    return {
        "candles": candles,  # trimmed to KEEP before writing
        "last": round(meta.get("regularMarketPrice", candles[-1][4]), 2),
        "marketTime": datetime.fromtimestamp(meta["regularMarketTime"], ET).isoformat(),
    }


def fetch_5m(sym):
    """Regular-session 5-minute bars for every day Yahoo keeps (about 60), keyed by date."""
    res = _get(sym, "60d", "5m")
    q = res["indicators"]["quote"][0]
    days = defaultdict(list)
    for i, ts in enumerate(res["timestamp"]):
        t = datetime.fromtimestamp(ts, ET)
        row = [q[k][i] for k in ("open", "high", "low", "close")]
        if None in row or not (9, 30) <= (t.hour, t.minute) < (16, 0):
            continue
        days[t.strftime("%Y-%m-%d")].append((t, *row))
    return days


# ---- simulation: the page's setups walked through one session's 5-minute bars ----

def _N(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _bs(S, K, T, v, typ):
    if T <= 1e-6:
        return (max(0.0, S - K), 1.0 if S > K else 0.0) if typ == "C" else (max(0.0, K - S), -1.0 if S < K else 0.0)
    d1 = (math.log(S / K) + (R + v * v / 2) * T) / (v * math.sqrt(T)); d2 = d1 - v * math.sqrt(T)
    if typ == "C":
        return S * _N(d1) - K * math.exp(-R * T) * _N(d2), _N(d1)
    return K * math.exp(-R * T) * _N(-d2) - S * _N(-d1), _N(d1) - 1


def _T(t_end, dte):
    """Years left to expiry from t_end (ET), counting trading time only."""
    return (max(0.0, 16 * 60 - (t_end.hour * 60 + t_end.minute)) / 60 / 6.5 + dte) / 252


BASE = {"dte": 0, "tgt": 1.0, "stop": 0.25, "entry": (9, 45), "timeStop": None, "minTgt": 0.15, "skipNear": None,
        "anchor": "entry", "skipPast": False}
# anchor "entry": target and stop are measured from the actual fill, so an entry that gaps or runs past
# the trigger cannot land beyond its own target. "trigger" was the original rule.
# Each variant changes one rule from BASE. Times are ET bar-close times (CT + 1 hour).
VARIANTS = [
    ("base", "Current rules (0DTE, next-level target and ¼-move stop measured from the entry, enter from 8:45 CT, hold to close)", {}),
    ("trig", "Old rule: target and stop measured from the trigger", {"anchor": "trigger"}),
    ("skippast", "Old rule, but skip entries already past the target", {"anchor": "trigger", "skipPast": True}),
    ("dte1", "Next-day expiry instead of same day", {"dte": 1}),
    ("dte2", "2-day expiry instead of same day", {"dte": 2}),
    ("half", "Target halfway to the next level", {"tgt": 0.5}),
    ("wide", "Wider stop: ½ expected move", {"stop": 0.5}),
    ("late", "Enter only from 9:30 CT", {"entry": (10, 30)}),
    ("tstop", "Time stop: exit at 11:00 CT if nothing hit", {"timeStop": (12, 0)}),
    ("min35", "Target at least ⅓ of the expected move away", {"minTgt": 0.35}),
    ("min50", "Target at least ½ of the expected move away", {"minTgt": 0.5}),
    ("skip", "Skip setups whose next level is under ½ move away", {"skipNear": 0.5}),
]


def levels(prior):
    """Levels the page shows for the session after prior[-1]."""
    _, _, H, L, C = prior[-1]
    P = (H + L + C) / 3
    cl = [c[4] for c in prior]
    rets = [math.log(cl[i] / cl[i - 1]) for i in range(len(cl) - 20, len(cl))]
    m = sum(rets) / len(rets)
    vol = math.sqrt(sum((x - m) ** 2 for x in rets) / (len(rets) - 1) * 252)
    em = C * vol * math.sqrt(1 / 252)
    return dict(H=H, L=L, C=C, P=P, R1=2 * P - L, S1=2 * P - H, R2=P + (H - L), S2=P - (H - L), vol=vol, em=em, sma20=sum(cl[-20:]) / 20)


SETUPS = [("PDH breakout", "C", "H"), ("PDL breakdown", "P", "L"), ("Pivot reclaim", "C", "P"), ("Pivot reject", "P", "P")]


def simulate(sym, lv, bars, rules):
    """One row per setup for one session. Grades always use the page's default (0DTE, 8:45 CT entry)."""
    H, L, C, P, em, vol = lv["H"], lv["L"], lv["C"], lv["P"], lv["em"], lv["vol"]
    ups = sorted([P, lv["R1"], lv["R2"], C + em, H]); dns = sorted([P, lv["S1"], lv["S2"], C - em, L], reverse=True)
    rows = []
    for tag, typ, key in SETUPS:
        trig = lv[key]; call = typ == "C"
        K = math.ceil(trig) if call else math.floor(trig)
        full_tgt = next((u for u in ups if u > trig + em * 0.15), trig + em * 0.5) if call else next((u for u in dns if u < trig - em * 0.15), trig - em * 0.5)
        # grade, exactly as the page computes it
        Te, Tl = (6.25 / 6.5) / 252, (5.25 / 6.5) / 252
        e, dlt = _bs(trig, K, Te, vol, typ)
        g_stop = trig - em * 0.25 if call else trig + em * 0.25
        gain, loss = _bs(full_tgt, K, Tl, vol, typ)[0] / e - 1, _bs(g_stop, K, Tl, vol, typ)[0] / e - 1
        rr = gain / abs(loss) if loss < 0 else 0
        score = sum([rr >= 2, (C >= lv["sma20"]) if call else (C < lv["sma20"]), abs(trig - C) <= em, 0.35 <= abs(dlt) <= 0.65])
        # this variant's rules
        far = max(0.5, rules["minTgt"])

        def exits(ref):
            level_tgt = next((u for u in ups if u > ref + em * rules["minTgt"]), ref + em * far) if call else next((u for u in dns if u < ref - em * rules["minTgt"]), ref - em * far)
            return ref + (level_tgt - ref) * rules["tgt"], (ref - em * rules["stop"] if call else ref + em * rules["stop"])
        tgt, stp = exits(trig)
        row = {"sym": sym, "setup": tag, "type": typ, "grade": "DDCBA"[score], "strike": K,
               "trigger": round(trig, 2), "target": round(tgt, 2), "stop": round(stp, 2), "result": "untriggered"}
        if rules["skipNear"] and abs(full_tgt - trig) < em * rules["skipNear"]:
            row["result"] = "skipped"; rows.append(row); continue
        entry = None
        for i, (t, o, h, l, c) in enumerate(bars):
            end = t + timedelta(minutes=5)
            if (end.hour, end.minute) < rules["entry"]:
                continue
            if rules["timeStop"] and (end.hour, end.minute) >= rules["timeStop"]:
                break
            if tag == "PDH breakout": hit = c > trig
            elif tag == "PDL breakdown": hit = c < trig
            elif tag == "Pivot reclaim": hit = l <= trig and c > trig
            else: hit = h >= trig and c < trig
            if hit:
                entry = (i, end, c); break
        if entry:
            i0, t0, px0 = entry
            if rules["skipPast"] and ((px0 >= tgt) if call else (px0 <= tgt)):
                row["result"] = "skipped"; rows.append(row); continue
            if rules["anchor"] == "entry":
                tgt, stp = exits(px0)
                row.update(target=round(tgt, 2), stop=round(stp, 2))
            out = None
            for t, o, h, l, c in bars[i0 + 1:]:
                end = t + timedelta(minutes=5)
                if (l <= stp) if call else (h >= stp):  # both in one bar counts as the stop
                    out = ("stop", end, stp); break
                if (h >= tgt) if call else (l <= tgt):
                    out = ("target", end, tgt); break
                if rules["timeStop"] and (end.hour, end.minute) >= rules["timeStop"]:
                    out = ("time", end, c); break
            if not out:
                out = ("close", bars[-1][0] + timedelta(minutes=5), bars[-1][4])
            why, t1, px1 = out
            o0, o1 = _bs(px0, K, _T(t0, rules["dte"]), vol, typ)[0], _bs(px1, K, _T(t1, rules["dte"]), vol, typ)[0]
            pl = (o1 - o0) * 100
            ct = lambda t: (t - timedelta(hours=1)).strftime("%H:%M")  # ET to CT
            row.update(result="win" if pl > 0 else "loss", exit=why, entryCT=ct(t0), entryPx=round(px0, 2),
                       exitCT=ct(t1), exitPx=round(px1, 2), optIn=round(o0, 2), optOut=round(o1, 2), pl=round(pl))
        rows.append(row)
    return rows


def stats(rows):
    t = [r for r in rows if r["result"] in ("win", "loss")]
    w = [r["pl"] for r in t if r["result"] == "win"]; lo = [r["pl"] for r in t if r["result"] == "loss"]
    return {"setups": len(rows), "trades": len(t), "wins": len(w), "winRate": round(len(w) / len(t), 3) if t else None,
            "avgWin": round(sum(w) / len(w)) if w else None, "avgLoss": round(sum(lo) / len(lo)) if lo else None,
            "avg": round((sum(w) + sum(lo)) / len(t)) if t else None, "total": round(sum(w) + sum(lo))}


def backtest(daily, bars5):
    """Every session with 5-minute bars, under every rule variant. Earlier two-thirds vs. later third."""
    sessions = sorted(set.intersection(*(set(bars5[s]) for s in TICKERS)))
    sessions = [d for d in sessions if all(any(c[0] == d for c in daily[s]) for s in TICKERS)]
    split = sessions[int(len(sessions) * 2 / 3)]
    out = {"from": sessions[0], "to": sessions[-1], "sessions": len(sessions), "splitAt": split, "variants": []}
    base_rows = None
    for key, label, change in VARIANTS:
        rules = {**BASE, **change}
        rows = []
        for d in sessions:
            for s in TICKERS:
                idx = next(i for i, c in enumerate(daily[s]) if c[0] == d)
                prior = daily[s][:idx]
                if len(prior) < 21:
                    continue
                for r in simulate(s, levels(prior), bars5[s][d], rules):
                    r["date"] = d; rows.append(r)
        earlier = [r for r in rows if r["date"] < split]; later = [r for r in rows if r["date"] >= split]
        per_setup = {tag: {"earlier": stats([r for r in earlier if r["setup"] == tag]), "later": stats([r for r in later if r["setup"] == tag])} for tag, _, _ in SETUPS}
        per_side = {side: {"earlier": stats([r for r in earlier if r["type"] == side]), "later": stats([r for r in later if r["type"] == side])} for side in "CP"}
        out["variants"].append({"key": key, "label": label, "all": stats(rows), "earlier": stats(earlier), "later": stats(later),
                                "bySetup": per_setup, "bySide": per_side})
        if key == "base":
            base_rows = rows
    # breakdowns and the day-by-day line use the current rules
    groups = {"grade": "grade", "setup": "setup", "side": "type", "ticker": "sym"}
    out["by"] = {name: {v: stats([r for r in base_rows if r[field] == v]) for v in sorted({r[field] for r in base_rows})} for name, field in groups.items()}
    out["base"] = stats(base_rows)
    out["daily"] = [[d, sum(r.get("pl", 0) for r in base_rows if r["date"] == d)] for d in sessions]
    return out


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "market.json"
    now = datetime.now(ET)
    tickers = {s: fetch(s) for s in TICKERS}
    full = {s: t["candles"] for s, t in tickers.items()}
    # A candle dated today while the session is still open is partial; levels use the last complete one.
    today = now.strftime("%Y-%m-%d")
    open_now = now.weekday() < 5 and (9, 30) <= (now.hour, now.minute) < (16, 0)
    for t in tickers.values():
        t["lastIsPartial"] = t["candles"][-1][0] == today and open_now
    doc = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "updatedAtET": now.strftime("%a %b %d, %I:%M %p ET"),
        "source": "Yahoo Finance daily and 5-minute charts",
        "tickers": {s: {**t, "candles": t["candles"][-KEEP:]} for s, t in tickers.items()},
    }
    # Replay and backtest cover finished sessions only; today counts once the close is in.
    try:
        done = {s: [c for c in full[s] if c[0] < today or now.hour >= 16] for s in TICKERS}
        bars5 = {s: fetch_5m(s) for s in TICKERS}
        day = done["SPY"][-1][0]
        rows = []
        for s in TICKERS:
            rows += simulate(s, levels(done[s][:-1]), bars5[s][day], BASE)
        doc["replay"] = {"session": day, "levelsFrom": done["SPY"][-2][0], "rows": rows}
        bars5 = {s: {d: b for d, b in bars5[s].items() if d <= day} for s in TICKERS}
        doc["backtest"] = backtest(done, bars5)
    except Exception as err:  # replay and backtest are extras; never block the price refresh on them
        print("replay/backtest skipped:", repr(err))
    with open(out, "w") as f:
        json.dump(doc, f)
    print(out, {s: t["candles"][-1] for s, t in doc["tickers"].items()})


if __name__ == "__main__":
    main()
