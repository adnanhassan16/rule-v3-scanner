"""
ICT Rule v3 live scanner — checks ALL Binance spot USDT coins every hour
and sends a Telegram message when a setup appears.
Same logic as the Colab backtest (ict_rule_v3_colab.py) and the TradingView script.
Runs free on GitHub Actions (see .github/workflows/scan.yml).
"""
import os, sys, time, datetime as dt
from concurrent.futures import ThreadPoolExecutor
import numpy as np, requests

# ---------------- Rule v3 settings (tested values — do not change) ----------------
RSI_LO, RSI_HI = 30, 40
PUMP_PCT = 0.7
MAX_LEG = 6
CRASH_PCT, CRASH_MAX, CRASH_BARS = 5, 1, 720
TP_PCT, MAX_HOLD, TRIG_PCT = 2.0, 12, 0.05
MIN_AGE_DAYS = 240
EXCLUDE = set("""
DOGE SHIB PEPE FLOKI BONK WIF MEME BOME PEOPLE TURBO NEIRO 1000SATS SATS DOGS NOT PNUT
ACT TRUMP PENGU BABYDOGE 1000CHEEMS CHEEMS MUBARAK BROCCOLI714 TST JELLYJELLY BAN USELESS
FARTCOIN SPX GOAT POPCAT MEW BRETT DEGEN CAT 1MBABYDOGE LUNC PUMP WLFI MOODENG 1000CAT
USDC FDUSD TUSD USDP DAI EUR AEUR USDE XUSD BFUSD RLUSD USD1 PAXG XAUT EURI BUSD
""".split())

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")
HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]
S = requests.Session()
JEDDAH = dt.timezone(dt.timedelta(hours=3))


def api(path, params=None):
    for h in HOSTS:
        for _ in range(3):
            try:
                r = S.get(h + path, params=params, timeout=20)
                if r.status_code == 200:
                    return r.json()
                if r.status_code in (418, 429):
                    time.sleep(5)
                    continue
                break
            except requests.RequestException:
                time.sleep(2)
    return None


def tg(text):
    print(text, "\n")
    if not TG_TOKEN or not TG_CHAT:
        return
    for _ in range(3):
        try:
            r = S.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                       data={"chat_id": TG_CHAT, "text": text, "disable_web_page_preview": True}, timeout=20)
            if r.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(3)


def symbols():
    info = api("/api/v3/exchangeInfo", {"permissions": "SPOT"})
    if not info:
        raise RuntimeError("Binance not reachable")
    out = []
    for s in info["symbols"]:
        b = s["baseAsset"]
        if s["quoteAsset"] != "USDT" or s["status"] != "TRADING":
            continue
        if b in EXCLUDE or b.endswith(("UP", "DOWN", "BULL", "BEAR")) or b.startswith("1000"):
            continue
        out.append(b)
    return sorted(set(out))


def rsi_wilder(c, n=14):
    r = np.full(len(c), np.nan); d = np.diff(c)
    if len(c) <= n + 1:
        return r
    g = np.where(d > 0, d, 0.0); l = np.where(d < 0, -d, 0.0)
    ag = g[:n].mean(); al = l[:n].mean()
    r[n] = 100 - 100 / (1 + ag / al) if al > 0 else 100
    for i in range(n + 1, len(c)):
        ag = (ag * (n - 1) + g[i - 1]) / n; al = (al * (n - 1) + l[i - 1]) / n
        r[i] = 100 - 100 / (1 + ag / al) if al > 0 else 100
    return r


def ema(x, n):
    e = np.zeros(len(x)); a = 2 / (n + 1); e[0] = x[0]
    for i in range(1, len(x)):
        e[i] = a * x[i] + (1 - a) * e[i - 1]
    return e


def find_setups(O, H, L, C):
    """Return (setup bars, taken bars). setup = all Rule v3 checks pass on a closed red candle
    (before the next-candle trigger). taken = setups whose next candle traded 0.05% above its open.
    One trade per red leg: once a setup in a leg is taken, later bars of that leg are skipped."""
    N = len(C)
    RSI = rsi_wilder(C); E100 = ema(C, 100)
    col = np.sign(C - O)
    for i in range(1, N):
        if col[i] == 0:
            col[i] = col[i - 1]
    leg = np.zeros(N, int)
    for i in range(1, N):
        leg[i] = leg[i - 1] + (col[i] != col[i - 1])
    st, en = {}, {}
    for i in range(N):
        st.setdefault(leg[i], i); en[leg[i]] = i
    setups, taken, seen, info = [], [], set(), {}
    for i in range(30, N):
        if col[i] != -1:
            continue
        l1 = leg[i]
        if l1 - 4 < 0 or l1 in seen:
            continue
        legs = [l1 - 4, l1 - 3, l1 - 2, l1 - 1, l1]
        s = [st[x] for x in legs]; e = [en[x] if x != l1 else i for x in legs]
        if col[s[0]] != -1 or col[s[1]] != 1 or col[s[2]] != -1 or col[s[3]] != 1:
            continue
        if any(e[k] - s[k] + 1 > MAX_LEG for k in range(5)):
            continue
        p1 = (C[e[1]] / O[s[1]] - 1) * 100; p2 = (C[e[3]] / O[s[3]] - 1) * 100
        if p1 <= PUMP_PCT or p2 <= PUMP_PCT:
            continue
        if not (RSI_LO <= RSI[i] <= RSI_HI):
            continue
        if i < 100 or C[i] <= E100[i]:
            continue
        lo = max(1, i - CRASH_BARS)
        crash = int((C[lo:i + 1] / C[lo - 1:i] - 1 < -CRASH_PCT / 100).sum())
        if crash > CRASH_MAX:
            continue
        setups.append(i)
        info[i] = dict(rsi=RSI[i], ema=E100[i], crash=crash, p1=p1, p2=p2)
        if i + 1 < N and H[i + 1] >= O[i + 1] * (1 + TRIG_PCT / 100):
            taken.append(i); seen.add(l1)
    return setups, taken, info


def check(sym):
    k = api("/api/v3/klines", {"symbol": sym + "USDT", "interval": "1h", "limit": 1000})
    if not k or len(k) < 800:
        return None
    now_ms = time.time() * 1000
    closed = [x for x in k if x[6] < now_ms]          # x[6] = candle close time
    forming = [x for x in k if x[6] >= now_ms]
    O = np.array([float(x[1]) for x in closed]); H = np.array([float(x[2]) for x in closed])
    L = np.array([float(x[3]) for x in closed]); C = np.array([float(x[4]) for x in closed])
    setups, taken, info = find_setups(O, H, L, C)
    last = len(C) - 1
    if last not in setups:
        return None
    # age check only for candidates (saves API calls)
    d = api("/api/v3/klines", {"symbol": sym + "USDT", "interval": "1d", "limit": MIN_AGE_DAYS})
    if not d or len(d) < MIN_AGE_DAYS:
        return None
    res = dict(sym=sym, t=int(closed[-1][0]), **info[last])
    if forming:
        f = forming[0]
        res.update(open=float(f[1]), high=float(f[2]), price=float(f[4]))
    return res


def fmt(x):
    return f"{x:.6g}"


def main():
    try:
        syms = symbols()
    except RuntimeError:
        tg("⚠️ Rule v3 scanner: Binance not reachable this hour.")
        return
    with ThreadPoolExecutor(8) as ex:
        hits = [r for r in ex.map(check, syms) if r]
    now = dt.datetime.now(JEDDAH)
    for r in hits:
        candle = dt.datetime.fromtimestamp(r["t"] / 1000, JEDDAH)
        sell_by = candle + dt.timedelta(hours=1 + MAX_HOLD)
        lines = [f"🟡 RULE v3 SETUP — {r['sym']}USDT",
                 f"Setup candle: {candle:%d %b %H:%M} (Jeddah)",
                 f"RSI {r['rsi']:.1f} | above EMA100 ✅ | crash candles {r['crash']} | G1 +{r['p1']:.2f}% G2 +{r['p2']:.2f}%"]
        if "open" in r:
            o = r["open"]; trig = o * (1 + TRIG_PCT / 100); tp = o * (1 + TP_PCT / 100)
            status = "✅ TRIGGERED — price already went above trigger" if r["high"] >= trig else "⏳ waiting — buy only if price goes above trigger this hour"
            lines += [f"Open now: {fmt(o)} | Trigger: {fmt(trig)} | Price: {fmt(r['price'])}",
                      status,
                      f"🎯 Target +2%: {fmt(tp)} (limit sell)",
                      f"⏰ Sell at market by {sell_by:%d %b %H:%M} if target not hit"]
        lines += ["One trade at a time. Paper size $500.",
                  f"Chart: https://www.tradingview.com/chart/?symbol=BINANCE:{r['sym']}USDT"]
        tg("\n".join(lines))
    print(f"{now:%Y-%m-%d %H:%M} scanned {len(syms)} coins, setups: {len(hits)}")
    if now.hour == 9 and not hits:                       # daily 'I am alive' message
        tg(f"✅ Rule v3 scanner running — scanned {len(syms)} coins at {now:%H:%M}, no setup this hour.")


if __name__ == "__main__":
    main()
