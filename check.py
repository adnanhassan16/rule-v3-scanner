"""
Manual check: list every Rule v3 setup on ALL Binance spot coins in the last N hours.
Use it to confirm the bot did not miss anything.
Run on the server:   cd /opt/rulev3 && venv/bin/python check.py 48
"""
import sys, time, datetime as dt
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import scanner as sc

HOURS = int(sys.argv[1]) if len(sys.argv) > 1 else 48
NOW = time.time() * 1000
FROM = NOW - HOURS * 3600 * 1000


def one(sym):
    k = sc.api("/api/v3/klines", {"symbol": sym + "USDT", "interval": "1h", "limit": 1000})
    if not k or len(k) < 800:
        return []
    closed = [x for x in k if x[6] < NOW]
    O = np.array([float(x[1]) for x in closed]); H = np.array([float(x[2]) for x in closed])
    L = np.array([float(x[3]) for x in closed]); C = np.array([float(x[4]) for x in closed])
    setups, taken, info = sc.find_setups(O, H, L, C)
    out = []
    for i in setups:
        if closed[i][0] < FROM:
            continue
        if i == len(C) - 1:
            status = "waiting (next candle forming now)"
        else:
            status = "TRIGGERED (buy signal)" if i in taken else "no trigger"
        out.append((int(closed[i][0]) + 3600 * 1000, sym, info[i]["rsi"], status))
    if out:
        d = sc.api("/api/v3/klines", {"symbol": sym + "USDT", "interval": "1d", "limit": sc.MIN_AGE_DAYS})
        if not d or len(d) < sc.MIN_AGE_DAYS:
            return []
    return out


syms = sc.symbols()
with ThreadPoolExecutor(10) as ex:
    rows = sorted(r for lst in ex.map(one, syms) for r in lst)
print(f"Checked {len(syms)} coins, last {HOURS} hours. Setups found: {len(rows)}")
for t, sym, rsi, status in rows:
    print(f"{dt.datetime.fromtimestamp(t / 1000, sc.JEDDAH):%d %b %H:%M} (Jeddah)  {sym:<10} RSI {rsi:4.1f}  {status}")
if not rows:
    print("No setups in this period (normal in quiet markets).")
