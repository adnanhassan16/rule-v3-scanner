"""
ICT Rule v3 LIVE BOT — runs 24/7 on a small cloud server (VPS).
  • Every hour, 3 seconds after the 1H candle closes: checks ALL Binance spot USDT coins.
  • Setup found  -> Telegram: coin, trigger price, target, sell-by time.
  • Price passes trigger -> Telegram "BUY NOW" + opens a PAPER trade (one at a time, $500).
  • Watches the paper trade: +2% target (uses candle highs, so wicks are not missed) or 12h exit.
  • Logs every paper trade to paper_trades.csv and sends a daily summary.
  • Pings healthchecks.io every hour (optional) so you get warned if the bot stops.
Same rule logic as the backtest (find_setups in scanner.py, checked 104/104 against the backtest).
"""
import os, json, csv, time, datetime as dt, traceback
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import scanner as sc   # same folder: find_setups, api, tg, symbols, settings

HC_URL = os.environ.get("HC_URL", "")          # optional healthchecks.io ping URL
TRADE_USD = 500
STATE_FILE = "state.json"
LOG_FILE = "paper_trades.csv"
HOUR = 3600
JEDDAH = sc.JEDDAH


def now():
    return time.time()


def jd(ts):
    return dt.datetime.fromtimestamp(ts, JEDDAH)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"pending": {}, "trade": None, "last_scan_hour": 0, "last_daily": ""}


def save_state(s):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, STATE_FILE)


def klines(sym, interval="1h", limit=1000):
    return sc.api("/api/v3/klines", {"symbol": sym + "USDT", "interval": interval, "limit": limit})


# ------------------------------ hourly scan ------------------------------
def scan_one(sym, scan_ts):
    k = klines(sym)
    if not k or len(k) < 800:
        return None
    closed = [x for x in k if x[6] < scan_ts * 1000]
    if not closed or closed[-1][0] < (scan_ts - 2 * HOUR) * 1000:      # stale / halted coin
        return None
    O = np.array([float(x[1]) for x in closed]); H = np.array([float(x[2]) for x in closed])
    L = np.array([float(x[3]) for x in closed]); C = np.array([float(x[4]) for x in closed])
    setups, _, info = sc.find_setups(O, H, L, C)
    if len(C) - 1 not in setups:
        return None
    d = klines(sym, "1d", sc.MIN_AGE_DAYS)
    if not d or len(d) < sc.MIN_AGE_DAYS:
        return None
    return dict(sym=sym, candle_open_ms=int(closed[-1][0]), **{k2: float(v) for k2, v in info[len(C) - 1].items()})


def hourly_scan(state):
    scan_ts = now()
    syms = sc.symbols()
    with ThreadPoolExecutor(10) as ex:
        hits = [r for r in ex.map(lambda s: scan_one(s, scan_ts), syms) if r]
    state["pending"] = {}
    for r in hits:
        entry_hour_ms = r["candle_open_ms"] + HOUR * 1000          # the candle we may buy in
        state["pending"][r["sym"]] = dict(r, entry_hour_ms=entry_hour_ms, open=None, trig=None, tp=None)
        busy = " (you already have a trade open — paper bot will SKIP this one)" if state["trade"] else ""
        sc.tg(f"🟡 SETUP — {r['sym']}USDT{busy}\n"
              f"Setup candle closed {jd(entry_hour_ms / 1000):%d %b %H:%M} (Jeddah)\n"
              f"RSI {r['rsi']:.1f} | above EMA100 ✅ | crash candles {int(r['crash'])} | G1 +{r['p1']:.2f}% G2 +{r['p2']:.2f}%\n"
              f"Wait for my BUY NOW message (price must rise 0.05% above this hour's open).\n"
              f"Chart: https://www.tradingview.com/chart/?symbol=BINANCE:{r['sym']}USDT")
    state["last_scan_hour"] = int(scan_ts // HOUR)
    save_state(state)
    print(f"{jd(scan_ts):%Y-%m-%d %H:%M:%S} scanned {len(syms)} coins, setups {len(hits)}", flush=True)


# --------------------------- trigger + trade watch ---------------------------
def watch_pending(state):
    """During the hour after a setup: did price go 0.05% above the candle open?"""
    t = now()
    for sym, p in list(state["pending"].items()):
        if t * 1000 >= p["entry_hour_ms"] + HOUR * 1000:            # hour over, no trigger
            sc.tg(f"⚪ {sym}USDT: no trigger this hour — setup cancelled.")
            del state["pending"][sym]
            continue
        k = sc.api("/api/v3/klines", {"symbol": sym + "USDT", "interval": "1h",
                                      "startTime": p["entry_hour_ms"], "limit": 1})
        if not k:
            continue
        o, h, c = float(k[0][1]), float(k[0][2]), float(k[0][4])
        trig = o * (1 + sc.TRIG_PCT / 100); tp = o * (1 + sc.TP_PCT / 100)
        if h < trig:
            continue
        sell_by_ms = p["entry_hour_ms"] + sc.MAX_HOLD * HOUR * 1000
        msg = (f"🟢 BUY NOW — {sym}USDT\n"
               f"Hour open {sc.fmt(o)} | trigger {sc.fmt(trig)} hit | price now {sc.fmt(c)}\n"
               f"🎯 Put LIMIT SELL at {sc.fmt(tp)} (+2% from open)\n"
               f"⏰ If not sold, sell at market at {jd(sell_by_ms / 1000):%d %b %H:%M} (Jeddah)")
        if state["trade"] is None:
            state["trade"] = dict(sym=sym, open=o, entry=trig, tp=tp, entry_hour_ms=p["entry_hour_ms"],
                                  sell_by_ms=sell_by_ms, worst=0.0, setup=p)
            msg += "\n📝 Paper trade OPENED ($500)."
        else:
            msg += f"\n⚠️ Paper bot skips it: {state['trade']['sym']} trade still open (one trade at a time)."
        sc.tg(msg)
        del state["pending"][sym]
    save_state(state)


def close_trade(state, price, why, t_ms):
    tr = state["trade"]
    ret_entry = price / tr["entry"] - 1          # realistic (bought at trigger)
    ret_bt = price / tr["open"] - 1              # backtest way (bought at hour open)
    row = dict(date_jeddah=jd(tr["entry_hour_ms"] / 1000).strftime("%Y-%m-%d %H:%M"), coin=tr["sym"],
               entry=round(tr["entry"], 8), exit=round(price, 8), exit_reason=why,
               result_pct=round(ret_entry * 100, 3), result_usd=round(ret_entry * TRADE_USD, 2),
               result_pct_backtest_way=round(ret_bt * 100, 3), worst_open_loss_pct=round(tr["worst"] * 100, 2),
               rsi=round(tr["setup"]["rsi"], 1), crash_candles=int(tr["setup"]["crash"]),
               exit_time_jeddah=jd(t_ms / 1000).strftime("%Y-%m-%d %H:%M"))
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)
    icon = "✅" if ret_entry > 0 else "🔴"
    sc.tg(f"{icon} PAPER TRADE CLOSED — {tr['sym']}USDT ({why})\n"
          f"Entry {sc.fmt(tr['entry'])} → exit {sc.fmt(price)} = {ret_entry * 100:+.2f}% (${ret_entry * TRADE_USD:+.2f} on $500)\n"
          f"Worst open loss {tr['worst'] * 100:.2f}%")
    state["trade"] = None


def watch_trade(state):
    tr = state["trade"]
    if not tr:
        return
    k = sc.api("/api/v3/klines", {"symbol": tr["sym"] + "USDT", "interval": "1h",
                                  "startTime": tr["entry_hour_ms"], "limit": 14})
    if not k:
        return
    t_ms = now() * 1000
    for x in k:                                   # walk the candles since entry, in order
        st, o, h, l, c, close_ms = int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), int(x[6])
        if st >= tr["sell_by_ms"]:
            break
        tr["worst"] = min(tr["worst"], l / tr["entry"] - 1)
        if h >= tr["tp"]:
            close_trade(state, max(tr["tp"], o) if st > tr["entry_hour_ms"] else tr["tp"], "target", t_ms)
            save_state(state)
            return
        if st + HOUR * 1000 >= tr["sell_by_ms"] and close_ms < t_ms:   # 12th candle closed
            sc.tg(f"⏰ 12 HOURS UP — SELL {tr['sym']}USDT AT MARKET NOW (cancel the limit order first).")
            close_trade(state, c, "12h", t_ms)
            save_state(state)
            return
    save_state(state)


# ------------------------------ daily summary ------------------------------
def daily_summary(state):
    t = jd(now())
    key = t.strftime("%Y-%m-%d")
    if t.hour != 9 or state.get("last_daily") == key:
        return
    state["last_daily"] = key
    save_state(state)
    rows = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            rows = list(csv.DictReader(f))
    n = len(rows); wins = sum(float(r["result_pct"]) > 0 for r in rows)
    usd = sum(float(r["result_usd"]) for r in rows)
    open_tr = state["trade"]["sym"] if state["trade"] else "none"
    sc.tg(f"📊 Daily report {key}\nBot running ✅\nPaper trades: {n} | wins {wins} ({(wins / n * 100 if n else 0):.0f}%)\n"
          f"Total result: ${usd:+.2f} (at $500 per trade, before fees)\nOpen trade: {open_tr}\n"
          f"Backtest target: ~75% wins (ict_results.xlsx)")


def ping():
    if HC_URL:
        try:
            sc.S.get(HC_URL, timeout=10)
        except Exception:
            pass


# ------------------------------ main loop ------------------------------
def main():
    state = load_state()
    last_err = [0.0]
    sc.tg("🤖 Rule v3 bot started.")
    while True:
        try:
            t = now()
            hour_no = int(t // HOUR)
            if hour_no > state.get("last_scan_hour", 0) and t - hour_no * HOUR >= 3:
                hourly_scan(state)
                ping()
            if state["pending"]:
                watch_pending(state)
            if state["trade"]:
                watch_trade(state)
            daily_summary(state)
        except Exception:
            traceback.print_exc()
            if now() - last_err[0] > 600:                       # max one error message per 10 min
                last_err[0] = now()
                sc.tg("⚠️ Bot error (it keeps running and retries):\n" + traceback.format_exc()[-600:])
        time.sleep(15)


if __name__ == "__main__":
    main()
