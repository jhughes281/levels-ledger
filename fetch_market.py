"""Pull daily candles for the Levels & Ledger page and write market.json.

Usage: python fetch_market.py [out_path]
The JSON goes into the artifact's db as market/today (ArtifactData set with file_path).
Levels, expected move and setups are computed in the page from these candles.
"""
import json, math, sys, urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TICKERS = ["QQQ", "SPY", "GLD"]
KEEP = 90
ET = ZoneInfo("America/New_York")


def fetch(sym):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=6mo&interval=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        res = json.load(r)["chart"]["result"][0]
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
        "candles": candles[-KEEP:],
        "last": round(meta.get("regularMarketPrice", candles[-1][4]), 2),
        "marketTime": datetime.fromtimestamp(meta["regularMarketTime"], ET).isoformat(),
    }


def fetch_5m(sym, day):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=5m"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        res = json.load(r)["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    bars = []
    for i, ts in enumerate(res["timestamp"]):
        t = datetime.fromtimestamp(ts, ET)
        row = [q[k][i] for k in ("open", "high", "low", "close")]
        if None in row or t.strftime("%Y-%m-%d") != day or not (9, 30) <= (t.hour, t.minute) < (16, 0):
            continue
        bars.append((t, *row))
    return bars


# ---- replay: the page's setups for one finished session, walked through its 5-minute bars ----
R = 0.04


def _N(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _bs(S, K, T, v, typ):
    if T <= 1e-6:
        return (max(0.0, S - K), 1.0 if S > K else 0.0) if typ == "C" else (max(0.0, K - S), -1.0 if S < K else 0.0)
    d1 = (math.log(S / K) + (R + v * v / 2) * T) / (v * math.sqrt(T)); d2 = d1 - v * math.sqrt(T)
    if typ == "C":
        return S * _N(d1) - K * math.exp(-R * T) * _N(d2), _N(d1)
    return K * math.exp(-R * T) * _N(-d2) - S * _N(-d1), _N(d1) - 1


def _T(t_end):
    """Years left until the 4:00 PM ET close, for a same-day expiry."""
    return max(0.0, 16 * 60 - (t_end.hour * 60 + t_end.minute)) / 60 / 6.5 / 252


def replay(sym, complete, bars):
    """Setups for the session complete[-1], built from complete[-2] the way the page builds them (0DTE, trigger after 8:45 CT)."""
    prior = complete[:-1]
    _, _, H, L, C = prior[-1]
    P = (H + L + C) / 3; R1 = 2 * P - L; S1 = 2 * P - H; R2 = P + (H - L); S2 = P - (H - L)
    cl = [c[4] for c in prior]
    rets = [math.log(cl[i] / cl[i - 1]) for i in range(len(cl) - 20, len(cl))]
    m = sum(rets) / len(rets)
    vol = math.sqrt(sum((x - m) ** 2 for x in rets) / (len(rets) - 1) * 252)
    em = C * vol * math.sqrt(1 / 252); sma20 = sum(cl[-20:]) / 20
    ups = sorted([P, R1, R2, C + em, H]); dns = sorted([P, S1, S2, C - em, L], reverse=True)
    rows = []
    for tag, typ, trig in [("PDH breakout", "C", H), ("PDL breakdown", "P", L), ("Pivot reclaim", "C", P), ("Pivot reject", "P", P)]:
        call = typ == "C"
        K = math.ceil(trig) if call else math.floor(trig)
        tgt = next((u for u in ups if u > trig + em * 0.15), trig + em * 0.5) if call else next((u for u in dns if u < trig - em * 0.15), trig - em * 0.5)
        stp = trig - em * 0.25 if call else trig + em * 0.25
        Te, Tl = (6.25 / 6.5) / 252, (5.25 / 6.5) / 252
        e, dlt = _bs(trig, K, Te, vol, typ)
        gain, loss = _bs(tgt, K, Tl, vol, typ)[0] / e - 1, _bs(stp, K, Tl, vol, typ)[0] / e - 1
        rr = gain / abs(loss) if loss < 0 else 0
        score = sum([rr >= 2, (C >= sma20) if call else (C < sma20), abs(trig - C) <= em, 0.35 <= abs(dlt) <= 0.65])
        row = {"sym": sym, "setup": tag, "type": typ, "grade": "DDCBA"[score], "strike": K,
               "trigger": round(trig, 2), "target": round(tgt, 2), "stop": round(stp, 2), "result": "untriggered"}
        entry = None
        for i, (t, o, h, l, c) in enumerate(bars):
            if (t.hour, t.minute) < (9, 40):  # first bar that closes at 8:45 CT
                continue
            if tag == "PDH breakout": hit = c > trig
            elif tag == "PDL breakdown": hit = c < trig
            elif tag == "Pivot reclaim": hit = l <= trig and c > trig
            else: hit = h >= trig and c < trig
            if hit:
                entry = (i, t + timedelta(minutes=5), c); break
        if entry:
            i0, t0, px0 = entry
            out = None
            for t, o, h, l, c in bars[i0 + 1:]:
                stopped = l <= stp if call else h >= stp
                reached = h >= tgt if call else l <= tgt
                if stopped:  # if both happen in one bar, count the stop
                    out = ("stop", t, stp); break
                if reached:
                    out = ("target", t, tgt); break
            if not out:
                out = ("close", bars[-1][0], bars[-1][4])
            why, t1, px1 = out
            t1 += timedelta(minutes=5)
            o0, o1 = _bs(px0, K, _T(t0), vol, typ)[0], _bs(px1, K, _T(t1), vol, typ)[0]
            pl = (o1 - o0) * 100
            ct = lambda t: (t - timedelta(hours=1)).strftime("%H:%M")  # ET to CT
            row.update(result="win" if pl > 0 else "loss", exit=why, entryCT=ct(t0), entryPx=round(px0, 2),
                       exitCT=ct(t1), exitPx=round(px1, 2), optIn=round(o0, 2), optOut=round(o1, 2), pl=round(pl))
        rows.append(row)
    return rows


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "market.json"
    now = datetime.now(ET)
    doc = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "updatedAtET": now.strftime("%a %b %d, %I:%M %p ET"),
        "source": "Yahoo Finance daily chart",
        "tickers": {s: fetch(s) for s in TICKERS},
    }
    # A candle dated today while the session is still open is partial; levels use the last complete one.
    today = now.strftime("%Y-%m-%d")
    open_now = now.weekday() < 5 and (9, 30) <= (now.hour, now.minute) < (16, 0)
    for t in doc["tickers"].values():
        t["lastIsPartial"] = t["candles"][-1][0] == today and open_now
    # Replay the last finished session. A session that ended today counts once the close is in.
    try:
        rows, day = [], None
        for s, t in doc["tickers"].items():
            complete = t["candles"][:-1] if t["lastIsPartial"] or (t["candles"][-1][0] == today and now.hour < 16) else t["candles"]
            day = complete[-1][0]
            rows += replay(s, complete, fetch_5m(s, day))
        doc["replay"] = {"session": day, "levelsFrom": complete[-2][0], "rows": rows}
    except Exception as err:  # the replay is extra; never block the price refresh on it
        print("replay skipped:", err)
    with open(out, "w") as f:
        json.dump(doc, f)
    print(out, {s: t["candles"][-1] for s, t in doc["tickers"].items()})


if __name__ == "__main__":
    main()
