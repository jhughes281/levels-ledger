"""Pull daily candles for the Levels & Ledger page and write market.json.

Usage: python fetch_market.py [out_path]
The JSON goes into the artifact's db as market/today (ArtifactData set with file_path).
Levels, expected move and setups are computed in the page from these candles.
"""
import json, sys, urllib.request
from datetime import datetime, timezone
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
        candles.append([d, round(o, 2), round(h, 2), round(l, 2), round(c, 2)])
    return {
        "candles": candles[-KEEP:],
        "last": round(meta.get("regularMarketPrice", candles[-1][4]), 2),
        "marketTime": datetime.fromtimestamp(meta["regularMarketTime"], ET).isoformat(),
    }


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
    with open(out, "w") as f:
        json.dump(doc, f)
    print(out, {s: t["candles"][-1] for s, t in doc["tickers"].items()})


if __name__ == "__main__":
    main()
