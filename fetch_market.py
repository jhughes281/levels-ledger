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

TICKERS = ["QQQ", "SPY", "GLD"]  # shown on the dashboard
# Research tickers are simulated and graded every run but not shown as cards, so the grade gets tested on more
# trades. Promote one to TICKERS once its record holds up. A ticker that fails to download is skipped.
RESEARCH = ["IWM", "TLT", "XLF"]
KEEP = 90
# Bump when the simulation rules change. The page warns when the data was written by an older copy.
SCRIPT_VERSION = "2026-11-18"
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
        vol = (q.get("volume") or [None] * len(res["timestamp"]))[i]
        days[t.strftime("%Y-%m-%d")].append((t, *row, vol))
    return add_indicators(days)


# ---- confirmation indicators on the 5-minute bars (volume, VWAP, RSI, 20/50 EMA) and daily ADX ----
CONFIRMS = ["volume", "vwap", "rsi", "ema", "adx"]
REL_VOL = 1.5   # entry bar volume vs. the same 5-minute slot's average over the prior 10 sessions
ADX_MIN = 25    # daily ADX(14) at or above this = trending


def add_indicators(days):
    """Append an indicator dict to every bar: session VWAP, RSI(14), EMA(20), EMA(50) on a continuous 5-minute
    series, and relative volume vs. the same time slot in earlier sessions. Values are as of that bar's close."""
    out, prev = {}, None
    e20 = e50 = None; gain = loss = None; seed = []
    slot_hist = defaultdict(list)
    for d in sorted(days):
        pv = vv = 0.0; rows = []
        for k, (t, o, h, l, c, v) in enumerate(days[d]):
            a20, a50 = 2 / 21, 2 / 51
            e20 = c if e20 is None else e20 + a20 * (c - e20)
            e50 = c if e50 is None else e50 + a50 * (c - e50)
            rsi = None
            if prev is not None:
                ch = c - prev
                if gain is None:
                    seed.append(ch)
                    if len(seed) == 14:
                        gain = sum(x for x in seed if x > 0) / 14; loss = sum(-x for x in seed if x < 0) / 14
                else:
                    gain = (gain * 13 + max(ch, 0)) / 14; loss = (loss * 13 + max(-ch, 0)) / 14
                if gain is not None:
                    rsi = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
            prev = c
            if v:
                pv += (h + l + c) / 3 * v; vv += v
            past = [x for x in slot_hist[k][-10:] if x]
            rv = (v / (sum(past) / len(past))) if v and past else None
            rows.append((t, o, h, l, c, v, {"vwap": pv / vv if vv else None, "rsi": rsi, "e20": e20, "e50": e50, "rv": rv}))
        for k, r in enumerate(days[d]):
            slot_hist[k].append(r[5])
        out[d] = rows
    return out


def adx(prior, n=14):
    """Wilder's ADX(14) on daily candles [date, o, h, l, c]."""
    if len(prior) < 2 * n + 2:
        return None
    tr, pdm, mdm = [], [], []
    for (_, _, h0, l0, c0), (_, _, h, l, c) in zip(prior[:-1], prior[1:]):
        up, dn = h - h0, l0 - l
        tr.append(max(h - l, abs(h - c0), abs(l - c0))); pdm.append(up if up > dn and up > 0 else 0); mdm.append(dn if dn > up and dn > 0 else 0)
    atr, ap, am = sum(tr[:n]), sum(pdm[:n]), sum(mdm[:n]); dx = []
    for i in range(n, len(tr) + 1):
        if i > n:
            atr = atr - atr / n + tr[i - 1]; ap = ap - ap / n + pdm[i - 1]; am = am - am / n + mdm[i - 1]
        pdi, mdi = 100 * ap / atr if atr else 0, 100 * am / atr if atr else 0
        dx.append(100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0)
    a = sum(dx[:n]) / n
    for x in dx[n:]:
        a = (a * (n - 1) + x) / n
    return a


def confirmations(lv, call, px, ind):
    """The five confirmation checks at entry (True/False)."""
    sg = 1 if call else -1
    return {
        "volume": ind["rv"] is not None and ind["rv"] >= REL_VOL,
        "vwap": ind["vwap"] is not None and (px - ind["vwap"]) * sg > 0,
        "rsi": ind["rsi"] is not None and (ind["rsi"] - 50) * sg > 0,
        "ema": (ind["e20"] - ind["e50"]) * sg > 0 and (px - ind["e20"]) * sg > 0,
        "adx": lv["adx"] is not None and lv["adx"] >= ADX_MIN,
    }


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


# Trading costs per contract, applied to every simulated trade: half the bid/ask spread on the way in
# and on the way out (wider for GLD, whose options trade thinner), plus a commission on each side.
HALF_SPREAD = {"QQQ": 0.03, "SPY": 0.02, "GLD": 0.05, "IWM": 0.03, "TLT": 0.03, "XLF": 0.02}  # $ per share, estimates
FEE = 0.65  # $ per contract per side


def _T(t_end, dte):
    """Years left to expiry from t_end (ET), counting trading time only."""
    return (max(0.0, 16 * 60 - (t_end.hour * 60 + t_end.minute)) / 60 / 6.5 + dte) / 252


BASE = {"dte": 0, "tgt": 1.0, "stop": 0.25, "entry": (9, 45), "timeStop": None, "minTgt": 0.15, "skipNear": None,
        "anchor": "entry", "skipPast": False, "trend": False, "skipSetups": (), "minScore": 0, "minConfirm": 0,
        "skipOpenPivot": False, "noEntry": None}
# skipSetups: (ticker or "*", setup name) pairs that are not traded
# skipOpenPivot: a pivot setup whose trigger is the opening bar (closes 8:45 CT) is not traded
# noEntry: (from, to) ET bar-close times; a setup whose trigger falls inside the window is not traded
# anchor "entry": target and stop are measured from the actual fill, so an entry that gaps or runs past
# the trigger cannot land beyond its own target. "trigger" was the original rule.
# Each variant changes one rule from BASE. Times are ET bar-close times (CT + 1 hour).
VARIANTS = [
    ("base", "Current rules (0DTE, next-level target and ¼-move stop measured from the entry, enter from 8:45 CT, hold to close)", {}),
    ("trig", "Old rule: target and stop measured from the trigger", {"anchor": "trigger"}),
    ("skippast", "Old rule, but skip entries already past the target", {"anchor": "trigger", "skipPast": True}),
    ("trend", "Only trade with the 20-day trend (calls above the 20-day average, puts below)", {"trend": True}),
    ("trend1", "With the trend and next-day expiry", {"trend": True, "dte": 1}),
    ("focus", "Trend + next-day expiry, no pivot setups on QQQ or GLD", {"trend": True, "dte": 1,
     "skipSetups": (("QQQ", "Pivot reclaim"), ("QQQ", "Pivot reject"), ("GLD", "Pivot reclaim"), ("GLD", "Pivot reject"))}),
    ("focusall", "Trend + next-day expiry, no pivot setups at all", {"trend": True, "dte": 1,
     "skipSetups": (("*", "Pivot reclaim"), ("*", "Pivot reject"))}),
    ("gradeA", "Only grade A or A+ (4–5 of the 5 checks)", {"minScore": 4}),
    ("gradeAp", "Only grade A+ (all 5 checks)", {"minScore": 5}),
    ("gradeA1", "Grade A or A+ with next-day expiry", {"minScore": 4, "dte": 1}),
    ("conf3", "At least 3 of the 5 confirmations (volume, VWAP, RSI, EMA, ADX) at entry", {"minConfirm": 3}),
    ("conf4", "At least 4 of the 5 confirmations at entry", {"minConfirm": 4}),
    ("gradeAconf3", "Grade A or A+ and at least 3 confirmations", {"minScore": 4, "minConfirm": 3}),
    ("dte1", "Next-day expiry instead of same day", {"dte": 1}),
    ("dte2", "2-day expiry instead of same day", {"dte": 2}),
    ("half", "Target halfway to the next level", {"tgt": 0.5}),
    ("wide", "Wider stop: ½ expected move", {"stop": 0.5}),
    ("late", "Enter only from 9:30 CT", {"entry": (10, 30)}),
    ("openpivot", "Skip pivot setups that trigger on the opening bar (8:45 CT)", {"skipOpenPivot": True}),
    ("midmorning", "Skip setups that trigger between 10:00 and noon CT", {"noEntry": ((11, 0), (13, 0))}),
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
    return dict(H=H, L=L, C=C, P=P, R1=2 * P - L, S1=2 * P - H, R2=P + (H - L), S2=P - (H - L), vol=vol, em=em,
                sma20=sum(cl[-20:]) / 20, C5=cl[-6], adx=adx(prior))


# Grade = how many of five checks a setup meets. Each check improved results in both the earlier and later
# backtest sessions (Nov 2026 study); the old four rules didn't separate winners (R:R >= 2 picked worse ones).
GRADES = {5: "A+", 4: "A", 3: "B", 2: "C", 1: "D", 0: "D"}
CALM_VOL = 0.25   # 20-day realized vol at or below this
PUSH_EM = 0.25    # entry close at least this many expected moves past the trigger


def checks(lv, tag, call, open_px=None, entry_px=None):
    """The five grade checks; None where the value isn't known yet (gap before the open, push before the trigger)."""
    sg = 1 if call else -1
    return {
        "trend5": (lv["C"] - lv["C5"]) * sg > 0,
        "calm": lv["vol"] <= CALM_VOL,
        "breakout": not tag.startswith("Pivot"),
        "gap": None if open_px is None else (open_px - lv["C"]) * sg > 0,
        "push": None if entry_px is None else (entry_px - lv[{"PDH breakout": "H", "PDL breakdown": "L"}.get(tag, "P")]) * sg >= PUSH_EM * lv["em"],
    }


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
        # grade before entry: the push check counts only once the trade triggers
        score = sum(bool(v) for v in checks(lv, tag, call, open_px=bars[0][1]).values())
        # this variant's rules
        far = max(0.5, rules["minTgt"])

        def exits(ref):
            level_tgt = next((u for u in ups if u > ref + em * rules["minTgt"]), ref + em * far) if call else next((u for u in dns if u < ref - em * rules["minTgt"]), ref - em * far)
            return ref + (level_tgt - ref) * rules["tgt"], (ref - em * rules["stop"] if call else ref + em * rules["stop"])
        tgt, stp = exits(trig)
        row = {"sym": sym, "setup": tag, "type": typ, "grade": GRADES[score], "score": score, "strike": K,
               "trigger": round(trig, 2), "target": round(tgt, 2), "stop": round(stp, 2), "result": "untriggered"}
        if (sym, tag) in rules["skipSetups"] or ("*", tag) in rules["skipSetups"]:
            row["result"] = "skipped"; rows.append(row); continue
        if rules["trend"] and ((C < lv["sma20"]) if call else (C >= lv["sma20"])):
            row["result"] = "skipped"; rows.append(row); continue
        if rules["skipNear"] and abs(full_tgt - trig) < em * rules["skipNear"]:
            row["result"] = "skipped"; rows.append(row); continue
        entry = None
        for i, (t, o, h, l, c, *_) in enumerate(bars):
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
            score = sum(bool(v) for v in checks(lv, tag, call, open_px=bars[0][1], entry_px=px0).values())
            cf = confirmations(lv, call, px0, bars[i0][6])
            row.update(grade=GRADES[score], score=score, conf=sum(1 << k for k, name in enumerate(CONFIRMS) if cf[name]))
            if score < rules["minScore"] or sum(cf.values()) < rules["minConfirm"]:
                row["result"] = "skipped"; rows.append(row); continue
            hm = (t0.hour, t0.minute)
            if (rules["skipOpenPivot"] and tag.startswith("Pivot") and hm == (9, 45)) or \
                    (rules["noEntry"] and rules["noEntry"][0] <= hm < rules["noEntry"][1]):
                row["result"] = "skipped"; rows.append(row); continue
            if rules["skipPast"] and ((px0 >= tgt) if call else (px0 <= tgt)):
                row["result"] = "skipped"; rows.append(row); continue
            if rules["anchor"] == "entry":
                tgt, stp = exits(px0)
                row.update(target=round(tgt, 2), stop=round(stp, 2))
            out = None
            for t, o, h, l, c, *_ in bars[i0 + 1:]:
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
            hs = HALF_SPREAD.get(sym, 0.03)
            pl = (max(0.0, o1 - hs) - (o0 + hs)) * 100 - 2 * FEE
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


SETUP_NAMES = [tag for tag, _, _ in SETUPS]
HIST_FIELDS = ["sym", "setup", "type", "grade", "score", "conf", "strike", "trigger", "target", "stop", "result",
               "exit", "entryCT", "entryPx", "exitCT", "exitPx", "optIn", "optOut", "pl"]


def history(rows, sessions):
    """Every session's replay under the current rules, packed as arrays (field order in HIST_FIELDS) to keep the doc small."""
    by = defaultdict(list)
    for r in rows:
        by[r["date"]].append([SETUP_NAMES.index(r[k]) if k == "setup" else r.get(k) for k in HIST_FIELDS])
    return {"fields": HIST_FIELDS, "setups": SETUP_NAMES, "days": {d: by[d] for d in sessions}}


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
    order = {"grade": ["A+", "A", "B", "C", "D"]}
    out["by"] = {name: {v: stats([r for r in base_rows if r[field] == v]) for v in order.get(name, sorted({r[field] for r in base_rows})) if any(r[field] == v for r in base_rows)} for name, field in groups.items()}
    out["gradeRules"] = {"calmVol": CALM_VOL, "pushEm": PUSH_EM}
    out["confirmations"] = confirm_stats(base_rows, split)
    out["base"] = stats(base_rows)
    out["bySetupTicker"] = {s: {tag: stats([r for r in base_rows if r["sym"] == s and r["setup"] == tag]) for tag, _, _ in SETUPS} for s in TICKERS}
    out["costs"] = {"halfSpread": HALF_SPREAD, "fee": FEE}
    out["history"] = history(base_rows, sessions)
    out["daily"] = [[d, sum(r.get("pl", 0) for r in base_rows if r["date"] == d)] for d in sessions]
    return out


def confirm_stats(rows, split):
    """For each confirmation indicator: trades where it agreed vs. didn't, plus results by how many agreed."""
    t = [r for r in rows if r["result"] in ("win", "loss") and "conf" in r]
    half = lambda rs: {"all": stats(rs), "earlier": stats([r for r in rs if r["date"] < split]), "later": stats([r for r in rs if r["date"] >= split])}
    each = {name: {"with": half([r for r in t if r["conf"] >> k & 1]), "without": half([r for r in t if not r["conf"] >> k & 1])}
            for k, name in enumerate(CONFIRMS)}
    count = lambda r: bin(r["conf"]).count("1")
    return {"names": CONFIRMS, "rules": {"relVol": REL_VOL, "adxMin": ADX_MIN}, "each": each,
            "byCount": {n: half([r for r in t if count(r) == n]) for n in range(6) if any(count(r) == n for r in t)}}


def research(daily, bars5, split, last_day):
    """Current rules on the research tickers: overall, by grade and for grade A/A+ only, earlier vs. later sessions."""
    out = {}
    for s in daily:
        rows = []
        for d in sorted(bars5[s]):
            if d > last_day:
                continue
            idx = next((i for i, c in enumerate(daily[s]) if c[0] == d), None)
            if idx is None or idx < 21:
                continue
            for r in simulate(s, levels(daily[s][:idx]), bars5[s][d], BASE):
                r["date"] = d; rows.append(r)
        if not rows:
            continue
        half = lambda rs: {"earlier": stats([r for r in rs if r["date"] < split]), "later": stats([r for r in rs if r["date"] >= split])}
        top = [r for r in rows if r.get("grade") in ("A+", "A")]
        out[s] = {"sessions": len({r["date"] for r in rows}), "all": stats(rows), **half(rows),
                  "byGrade": {g: stats([r for r in rows if r.get("grade") == g]) for g in ["A+", "A", "B", "C", "D"] if any(r.get("grade") == g for r in rows)},
                  "top": {"all": stats(top), **half(top)},
                  "bySetup": {tag: stats([r for r in rows if r["setup"] == tag]) for tag, _, _ in SETUPS}}
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
        "scriptVersion": SCRIPT_VERSION,
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
        rdaily, rbars = {}, {}
        for s in RESEARCH:
            try:
                rdaily[s] = [c for c in fetch(s)["candles"] if c[0] < today or now.hour >= 16]
                rbars[s] = fetch_5m(s)
            except Exception as err:
                print("research ticker skipped:", s, repr(err))
        doc["backtest"]["research"] = research(rdaily, rbars, doc["backtest"]["splitAt"], day)
    except Exception as err:  # replay and backtest are extras; never block the price refresh on them
        print("replay/backtest skipped:", repr(err))
    with open(out, "w") as f:
        json.dump(doc, f)
    print(out, {s: t["candles"][-1] for s, t in doc["tickers"].items()})


if __name__ == "__main__":
    main()
