#!/usr/bin/env python3
"""crypto-trader: day-trading tools for Binance spot (Binance.com).

Standard library only. Market data comes from Binance's public market-data
host (data-api.binance.vision, no key). Orders go to Binance Spot Testnet by
default (testnet.binance.vision) and to api.binance.com only when the config
says "paper": false AND the place command gets --live.

Every file lives under trader/crypto/ in the current directory. All times are
UTC: crypto trades 24/7, so the "session" is a window the user picks.

Subcommands:
  init         write trader/crypto/config.json (copies the stock pack's money rules)
  scan         daily-close setups over liquid USDT pairs -> watchlist.json
  price SYM    last price
  entry check|watch SYM   range breakout (or --level) on closed 5-minute bars
  plan         stop and target from entry.json or flags -> plan.json
  timemachine  10,000 replays of the rest of the window + the rule on past days
  size         units that fit your max loss and budget -> orders.json (OTOCO bracket)
  risk         PASS / BLOCK every order in orders.json -> state/risk.json
  ticket show|approve|place|flatten|log
  balance      signed account read -> portfolio.json (needs keys)

Nothing is ever placed except by `ticket place`, and only when the risk check
passed that exact order today, the user's explicit yes is recorded for it, and
the approval is fresh.
"""

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal

DATA_HOST = "https://data-api.binance.vision"
TESTNET_HOST = "https://testnet.binance.vision"
LIVE_HOST = "https://api.binance.com"
UA = "crypto-trader/1.0 (stdlib urllib)"
BAR_MS = 5 * 60 * 1000
DAY_MS = 24 * 60 * 60 * 1000
SLOTS = 288  # 5-minute bars in a UTC day
DIR = os.path.join("trader", "crypto")
APPROVAL_TTL_S = 15 * 60

DEFAULTS = {
    "budget": 1000,
    "quote": "USDT",
    "cash_buffer_pct": 10,
    "max_position_pct": 75,
    "max_order_usd": 750,
    "min_order_usd": 10,
    "max_loss_per_trade_usd": 15,
    "max_daily_loss_usd": 45,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 1.0,
    "require_stop": True,
    "session_start": "14:30",
    "range_minutes": 60,
    "no_entries_after": "20:30",
    "flat_by": "20:55",
    "stop_limit_offset_pct": 0.2,
    "min_quote_volume_usd": 20000000,
    "paper": True,
    "blocked": [],
}
COPY_FROM_STOCK = ("budget", "cash_buffer_pct", "max_position_pct", "max_loss_per_trade_usd",
                   "max_daily_loss_usd", "max_trades_per_day", "max_orders_per_day",
                   "min_reward_risk", "require_stop", "paper")

STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "PYUSD", "AEUR", "EUR",
           "EURI", "XUSD", "USD1", "BFUSD", "RLUSD", "UST", "USTC", "GBP", "TRY", "BRL",
           "JPY", "ZAR", "UAH", "PLN", "RON", "ARS", "MXN", "COP", "IDRT", "BIDR", "BKRW", "BVND"}
LEVERAGED = re.compile(r"^[A-Z0-9]+(UP|DOWN|BULL|BEAR)$")
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}$")


class Fail(Exception):
    """A plain one-line reason; printed without a traceback."""


# ----------------------------------------------------------------- files

def p(name):
    return os.path.join(DIR, name)


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        raise Fail(f"{path} is not valid JSON ({e})")


def save_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def load_config(path=None):
    cfg = dict(DEFAULTS)
    user = load_json(path or p("config.json"), {})
    if not isinstance(user, dict):
        raise Fail("trader/crypto/config.json must be a JSON object")
    cfg.update(user)
    for k in ("session_start", "no_entries_after", "flat_by"):
        hhmm(cfg[k], k)
    if int(cfg["range_minutes"]) % 5 or int(cfg["range_minutes"]) <= 0:
        raise Fail("range_minutes must be a positive multiple of 5")
    return cfg


# ----------------------------------------------------------------- time

def hhmm(s, what="time"):
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(s).strip())
    if not m:
        raise Fail(f"{what} must be HH:MM (24-hour, UTC), got {s!r}")
    return int(m.group(1)) * 60 + int(m.group(2))


def utcnow(args=None):
    s = getattr(args, "now", None) if args is not None else None
    if s:
        return parse_utc(s)
    return dt.datetime.now(dt.timezone.utc)


def parse_utc(s):
    s = str(s).strip().replace("T", " ").rstrip("Z").replace(" UTC", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s[:19], fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            pass
    raise Fail(f"cannot read the time {s!r}: use YYYY-MM-DD HH:MM (UTC)")


def to_ms(t):
    return int(t.timestamp() * 1000)


def from_ms(x):
    return dt.datetime.fromtimestamp(x / 1000, dt.timezone.utc)


def midnight(d):
    return dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc)


def ft(t):
    return t.strftime("%Y-%m-%d %H:%M UTC")


def fhm(t):
    return t.strftime("%H:%M")


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class Session:
    """The user's trading window on one UTC day."""

    def __init__(self, cfg, day):
        base = midnight(day)
        self.day = day
        self.start = base + dt.timedelta(minutes=hhmm(cfg["session_start"]))
        self.range_end = self.start + dt.timedelta(minutes=int(cfg["range_minutes"]))
        self.last_entry = base + dt.timedelta(minutes=hhmm(cfg["no_entries_after"]))
        self.flat = base + dt.timedelta(minutes=hhmm(cfg["flat_by"]))
        if not (self.start < self.range_end <= self.last_entry < self.flat):
            raise Fail("the window must run session_start < session_start + range_minutes"
                       " <= no_entries_after < flat_by, all on one UTC day")


# ----------------------------------------------------------------- numbers

def D(x):
    return x if isinstance(x, Decimal) else Decimal(str(x))


def floor_to(x, step):
    step = D(step)
    return (D(x) / step).to_integral_value(ROUND_DOWN) * step


def ceil_to(x, step):
    step = D(step)
    return (D(x) / step).to_integral_value(ROUND_UP) * step


def round_to(x, step):
    step = D(step)
    return (D(x) / step).to_integral_value(ROUND_HALF_UP) * step


def ds(x):
    """Decimal to a plain string, no exponent, no trailing zeros."""
    s = format(D(x).normalize(), "f")
    return "0" if s in ("-0", "") else s


def money(x):
    return f"${float(x):,.2f}"


def px(x):
    x = float(x)
    if x >= 1000:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:.4f}".rstrip("0").rstrip(".") if x < 10 else f"{x:.2f}"
    return f"{x:.8f}".rstrip("0")


def is_multiple(x, step):
    return D(x) % D(step) == 0


# ----------------------------------------------------------------- market data

def bar_of(r):
    return {"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
            "c": float(r[4]), "v": float(r[5]), "qv": float(r[7])}


IV_MS = {"1m": 60000, "5m": BAR_MS, "15m": 900000, "1h": 3600000, "1d": DAY_MS}


class Market:
    def __init__(self, offline=None, host=DATA_HOST):
        self.offline = offline
        self.host = host
        self.requests = 0

    def get(self, path, params=None):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if self.offline:
            return self._offline(path, params)
        url = self.host + path + ("?" + urllib.parse.urlencode(params) if params else "")
        last = None
        for attempt in range(4):
            self.requests += 1
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=25) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                body = e.read()[:300].decode("utf-8", "replace")
                if e.code in (418, 429) or e.code >= 500:
                    wait = min(int(e.headers.get("Retry-After") or 5), 60)
                    last = f"HTTP {e.code}"
                    time.sleep(wait)
                    continue
                if e.code == 451:
                    raise Fail(f"{self.host} refused this location (HTTP 451): Binance blocks some regions")
                raise Fail(f"Binance {path} returned HTTP {e.code}: {body}")
            except urllib.error.URLError as e:
                last = str(e.reason)
                if "403" in last:
                    raise Fail(f"could not reach {self.host}: {last} (is the host allowed by the network policy?)")
                time.sleep(2 * (attempt + 1))
        raise Fail(f"could not reach {self.host}{path}: {last}")

    def _offline(self, path, params):
        d = self.offline
        if path == "/api/v3/klines":
            f = os.path.join(d, f"klines-{params['symbol']}-{params['interval']}.json")
            rows = load_json(f)
            if rows is None:
                raise Fail(f"no offline klines for {params['symbol']} {params['interval']}")
            if "startTime" in params:
                rows = [r for r in rows if r[0] >= int(params["startTime"])]
            if "endTime" in params:
                rows = [r for r in rows if r[0] <= int(params["endTime"])]
            lim = int(params.get("limit", 500))
            return rows[:lim] if "startTime" in params else rows[-lim:]
        if path == "/api/v3/exchangeInfo":
            data = load_json(os.path.join(d, "exchangeInfo.json"))
            if "symbol" in params:
                syms = [s for s in data["symbols"] if s["symbol"] == params["symbol"]]
                if not syms:
                    raise Fail(f"Binance has no symbol {params['symbol']}")
                return {"symbols": syms}
            return data
        if path == "/api/v3/ticker/24hr":
            data = load_json(os.path.join(d, "ticker24hr.json"))
            if "symbol" in params:
                return next(t for t in data if t["symbol"] == params["symbol"])
            return data
        if path == "/api/v3/ticker/price":
            prices = load_json(os.path.join(d, "prices.json"), {})
            if params["symbol"] in prices:
                return {"symbol": params["symbol"], "price": str(prices[params["symbol"]])}
            raise Fail(f"no offline price for {params['symbol']}")
        raise Fail(f"offline mode has no data for {path}")

    def klines(self, symbol, interval, start=None, end=None, limit=1000):
        if start is None:
            return [bar_of(r) for r in self.get("/api/v3/klines",
                                                 {"symbol": symbol, "interval": interval, "limit": limit, "endTime": end})]
        out, cur = [], int(start)
        while True:
            rows = self.get("/api/v3/klines", {"symbol": symbol, "interval": interval,
                                               "startTime": cur, "endTime": end, "limit": 1000})
            if not rows:
                break
            out.extend(rows)
            if len(rows) < 1000:
                break
            cur = int(rows[-1][0]) + 1
        return [bar_of(r) for r in out]

    def price(self, symbol):
        return float(self.get("/api/v3/ticker/price", {"symbol": symbol})["price"])

    def symbol_info(self, symbol):
        try:
            data = self.get("/api/v3/exchangeInfo", {"symbol": symbol})
        except Fail as e:
            if "HTTP 400" in str(e):
                raise Fail(f"Binance has no spot symbol {symbol}")
            raise
        return parse_info(data["symbols"][0])


def parse_info(s):
    f = {x["filterType"]: x for x in s.get("filters", [])}
    notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
    return {
        "symbol": s["symbol"], "status": s.get("status"), "base": s.get("baseAsset"),
        "quote": s.get("quoteAsset"),
        "tick": ds(f.get("PRICE_FILTER", {}).get("tickSize", "0.00000001")),
        "step": ds(f.get("LOT_SIZE", {}).get("stepSize", "0.00000001")),
        "min_qty": ds(f.get("LOT_SIZE", {}).get("minQty", "0")),
        "max_qty": ds(f.get("LOT_SIZE", {}).get("maxQty", "0")),
        "min_notional": ds(notional.get("minNotional", "0")),
        "spot": bool(s.get("isSpotTradingAllowed", True)),
        "oto": bool(s.get("otoAllowed", True)),
        "oco": bool(s.get("ocoAllowed", True)),
    }


def norm_symbol(s, quote="USDT"):
    s = str(s).upper().replace("/", "").replace("-", "").strip()
    if not SYMBOL_RE.fullmatch(s):
        raise Fail(f"{s!r} is not a Binance symbol like BTCUSDT")
    if not s.endswith(quote) and len(s) <= 5:
        s += quote
    return s


def closed(bars, interval_ms, now):
    n = to_ms(now)
    return [b for b in bars if b["t"] + interval_ms <= n]


# ----------------------------------------------------------------- indicators

def rsi_series(closes, n=14):
    if len(closes) <= n:
        return []
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    out = [100.0 if al == 0 else 100 - 100 / (1 + ag / al)]
    for g, l in zip(gains[n:], losses[n:]):
        ag, al = (ag * (n - 1) + g) / n, (al * (n - 1) + l) / n
        out.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
    return out


def daily_setups(closes):
    last, prev = closes[-1], closes[-2]
    out = []
    if last > max(closes[-21:-1]):
        out.append("breakout_20d")
    if closes[-6] / closes[-16] - 1 > 0.10 and max(closes[-5:]) / min(closes[-5:]) - 1 <= 0.05:
        out.append("bull_flag")
    if prev < statistics.fmean(closes[-21:-1]) and last > statistics.fmean(closes[-20:]):
        out.append("reclaim_ma20")
    r = rsi_series(closes)
    rsi_now = r[-1] if r else None
    if rsi_now is not None and rsi_now < 30:
        out.append("oversold")
    if len(r) >= 2 and r[-2] < 30 and last > prev:
        out.append("oversold_bounce")
    return out, rsi_now


def atr(bars, n=14):
    if len(bars) < n + 1:
        return None
    bars = bars[-(n + 1):]
    trs = [max(b["h"] - b["l"], abs(b["h"] - a["c"]), abs(b["l"] - a["c"])) for a, b in zip(bars, bars[1:])]
    return sum(trs) / n


SETUP_NAMES = {"breakout_20d": "20-day closing-high breakout", "bull_flag": "bull flag",
               "reclaim_ma20": "reclaimed the 20-day average", "oversold": "oversold (RSI under 30)",
               "oversold_bounce": "oversold bounce"}


# ----------------------------------------------------------------- scan

def cmd_scan(args):
    cfg = load_config(args.config)
    mk = Market(args.offline)
    now = utcnow(args)
    quote = cfg["quote"]
    ex = mk.get("/api/v3/exchangeInfo")
    pairs = {}
    for s in ex["symbols"]:
        if s.get("status") != "TRADING" or s.get("quoteAsset") != quote:
            continue
        if not s.get("isSpotTradingAllowed", True):
            continue
        base = s.get("baseAsset", "")
        if base in STABLES or LEVERAGED.fullmatch(base):
            continue
        pairs[s["symbol"]] = s
    if args.symbols:
        want = [norm_symbol(x, quote) for x in args.symbols.split(",") if x.strip()]
        missing = [w for w in want if w not in pairs]
        if missing:
            print(f"Not a trading {quote} spot pair (skipped): {', '.join(missing)}")
        pairs = {w: pairs[w] for w in want if w in pairs}
    tick = {t["symbol"]: t for t in mk.get("/api/v3/ticker/24hr")}
    min_vol = float(args.min_volume if args.min_volume is not None else cfg["min_quote_volume_usd"])
    liquid = sorted((s for s in pairs if float(tick.get(s, {}).get("quoteVolume", 0)) >= min_vol),
                    key=lambda s: -float(tick[s]["quoteVolume"]))
    if args.limit:
        liquid = liquid[: args.limit]
    blocked = {norm_symbol(b, quote) for b in cfg.get("blocked", [])}
    liquid = [s for s in liquid if s not in blocked]
    print(f"Daily scan, {ft(now)}, Binance spot {quote} pairs (closed UTC daily candles).")
    print(f"{len(pairs):,} trading pairs after dropping stablecoins and leveraged tokens; "
          f"{len(liquid):,} with 24h volume of ${min_vol / 1e6:,.0f}M or more.")
    hits, failed, short = [], 0, 0
    for i, sym in enumerate(liquid, 1):
        try:
            bars = closed(mk.klines(sym, "1d", limit=60), DAY_MS, now)
        except Fail:
            failed += 1
            continue
        closes = [b["c"] for b in bars]
        if len(closes) < 30:
            short += 1
            continue
        setups, r = daily_setups(closes)
        if setups:
            hits.append({"symbol": sym, "setups": setups, "price": closes[-1],
                         "close_date": from_ms(bars[-1]["t"]).date().isoformat(),
                         "rsi": round(r, 1) if r is not None else None,
                         "quote_volume_24h": float(tick[sym]["quoteVolume"]),
                         "change_24h_pct": float(tick[sym].get("priceChangePercent", 0))})
        if i % 25 == 0 and not args.json:
            print(f"  {i} of {len(liquid)} charts", file=sys.stderr)
    scanned = len(liquid) - failed - short
    counts = {k: sum(k in h["setups"] for h in hits) for k in SETUP_NAMES}
    out = {"mode": "daily", "as_of": iso(now), "venue": "binance", "quote": quote,
           "universe": len(pairs), "liquid": len(liquid), "scanned": scanned, "failed": failed,
           "short_history": short, "hits": hits}
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"Fetched {len(liquid) - failed} of {len(liquid)} charts"
              + (f" ({failed} failed)" if failed else "") + (f", {short} under 30 days" if short else "")
              + f"; {scanned} scanned.")
        print(f"{len(hits)} charts matched a setup: "
              + ", ".join(f"{counts[k]} {SETUP_NAMES[k]}" for k in SETUP_NAMES) + ".")
        if hits:
            print("Most traded first (24h quote volume):")
        for h in hits[: args.show]:
            print(f"  {h['symbol']:<12} {px(h['price']):>14}  RSI {h['rsi'] if h['rsi'] is not None else '-':>5}"
                  f"  24h {h['change_24h_pct']:+6.2f}%  {', '.join(SETUP_NAMES[s] for s in h['setups'])}")
        if len(hits) > args.show:
            print(f"  ... and {len(hits) - args.show} more in the watchlist (--show N for more).")
    if not args.no_write:
        save_json(p("watchlist.json"), out)
        if not args.json:
            print("Wrote trader/crypto/watchlist.json.")
    if not args.json:
        print("A scan is a list of charts that match a rule, not a list of coins to buy.")
    return 0 if hits else 1


def cmd_price(args):
    cfg = load_config(args.config)
    mk = Market(args.offline)
    sym = norm_symbol(args.symbol, cfg["quote"])
    print(f"{sym} {px(mk.price(sym))} (Binance spot last price, {ft(utcnow(args))})")
    return 0


# ----------------------------------------------------------------- entry

def entry_state(cfg, mk, sym, now, level=None, min_volume_x=None):
    sess = Session(cfg, now.date())
    res = {"symbol": sym, "date": sess.day.isoformat(), "setup": "level" if level else "range",
           "session_start": iso(sess.start), "range_end": iso(sess.range_end),
           "last_entry": iso(sess.last_entry), "flat_by": iso(sess.flat)}
    if now < sess.start:
        res.update(status="not_started", line=f"{sym}: the window opens at {fhm(sess.start)} UTC; nothing to check yet.")
        return res
    if now < sess.range_end:
        res.update(status="forming", line=f"{sym}: the range is still forming until {fhm(sess.range_end)} UTC.")
        return res
    bars = closed(mk.klines(sym, "5m", to_ms(sess.start), to_ms(sess.flat) - 1), BAR_MS, now)
    rng = [b for b in bars if b["t"] < to_ms(sess.range_end)]
    need = int(cfg["range_minutes"]) // 5
    if len(rng) < need:
        res.update(status="no_data", line=f"{sym}: only {len(rng)} of {need} range bars came back; no signal.")
        return res
    hi, lo = max(b["h"] for b in rng), min(b["l"] for b in rng)
    avg_v = statistics.fmean(b["v"] for b in rng)
    lvl = float(level) if level else hi
    res.update(range_high=hi, range_low=lo, level=lvl)
    after = [b for b in bars if b["t"] >= to_ms(sess.range_end) and b["t"] + BAR_MS <= to_ms(sess.last_entry)]
    for b in after:
        if b["c"] > lvl and (not min_volume_x or b["v"] >= float(min_volume_x) * avg_v):
            close_t = from_ms(b["t"] + BAR_MS)
            res.update(status="triggered", entry=b["c"], entry_time=iso(close_t), bar_open=iso(from_ms(b["t"])),
                       line=f"{sym}: TRIGGERED at {fhm(close_t)} UTC, the bar closed at {px(b['c'])}, "
                            f"above {px(lvl)} ({(b['c'] / lvl - 1) * 100:+.2f}%).")
            late = (to_ms(now) - (b["t"] + BAR_MS)) // BAR_MS
            if now > sess.last_entry:
                res["line"] += f" New entries closed at {fhm(sess.last_entry)} UTC: no entry today."
            elif late >= 1:
                res["line"] += f" That bar closed {late * 5} minutes ago: a late entry is a different trade."
            return res
    if now >= sess.last_entry:
        res.update(status="expired", line=f"{sym}: no close above {px(lvl)} by {fhm(sess.last_entry)} UTC; the setup expired.")
        return res
    last = bars[-1] if bars else None
    gap = (lvl / last["c"] - 1) * 100 if last else None
    res.update(status="waiting", last_close=last["c"] if last else None,
               line=f"{sym}: waiting. Last close {px(last['c'])} at {fhm(from_ms(last['t'] + BAR_MS))} UTC, "
                    f"{gap:.2f}% under {px(lvl)}." if last else f"{sym}: waiting for bars.")
    return res


def cmd_entry(args):
    cfg = load_config(args.config)
    mk = Market(args.offline)
    sym = norm_symbol(args.symbol, cfg["quote"])
    if args.action == "check":
        st = entry_state(cfg, mk, sym, utcnow(args), args.level, args.min_volume_x)
        print(st["line"])
        if st["status"] == "triggered":
            if args.write:
                save_json(p("entry.json"), st)
                print("Wrote trader/crypto/entry.json. Nothing was placed. Next: plan the stop and target.")
            return 0
        return 1
    # watch: one line per closed bar until the trigger or the expiry
    seen = None
    deadline = time.time() + args.max_hours * 3600
    while time.time() < deadline:
        now = utcnow()
        st = entry_state(cfg, mk, sym, now, args.level, args.min_volume_x)
        key = (st["status"], st.get("last_close"), st.get("entry_time"))
        if key != seen:
            print(f"[{fhm(now)} UTC] {st['line']}", flush=True)
            seen = key
        if st["status"] == "triggered":
            save_json(p("entry.json"), st)
            print("Wrote trader/crypto/entry.json. Nothing was placed.", flush=True)
            return 0
        if st["status"] in ("expired",):
            return 1
        nxt = (to_ms(now) // BAR_MS + 1) * BAR_MS + 4000
        time.sleep(max(5, (nxt - to_ms(utcnow())) / 1000))
    print("watch stopped: --max-hours reached")
    return 1


# ----------------------------------------------------------------- plan

def cmd_plan(args):
    cfg = load_config(args.config)
    mk = Market(args.offline)
    if args.symbol:
        if args.entry is None or not args.time:
            raise Fail("with --symbol, give --entry PRICE and --time 'YYYY-MM-DD HH:MM' (UTC, the entry bar's close)")
        ent = {"symbol": norm_symbol(args.symbol, cfg["quote"]), "entry": float(args.entry),
               "entry_time": iso(parse_utc(args.time)), "setup": args.setup or "level",
               "range_high": args.range_high, "range_low": args.range_low}
    else:
        ent = load_json(args.from_ or p("entry.json"))
        if not ent or ent.get("status") not in (None, "triggered"):
            raise Fail("no triggered entry in trader/crypto/entry.json: run entry check, or pass --symbol/--entry/--time")
        if args.entry is not None:
            ent = dict(ent, entry=float(args.entry))
    sym = ent["symbol"]
    info = mk.symbol_info(sym)
    tick = info["tick"]
    E = D(ent["entry"])
    t_entry = parse_utc(ent["entry_time"])
    rule = args.stop or ("range-mid" if ent.get("range_high") is not None else "atr:1.5")
    hi, lo = ent.get("range_high"), ent.get("range_low")
    if rule in ("range-mid", "range-low") and (hi is None or lo is None):
        raise Fail(f"stop {rule} needs the range high and low (--range-high/--range-low)")
    if rule == "range-mid":
        stop = (D(hi) + D(lo)) / 2
    elif rule == "range-low":
        stop = D(lo)
    elif rule.startswith("pct:"):
        stop = E * (1 - D(rule[4:]) / 100)
    elif rule.startswith("price:"):
        stop = D(rule[6:])
    elif rule.startswith("atr:"):
        bars = mk.klines(sym, "5m", to_ms(t_entry) - 16 * BAR_MS, to_ms(t_entry) - 1)
        bars = [b for b in bars if b["t"] + BAR_MS <= to_ms(t_entry)]
        a = atr(bars)
        if a is None:
            raise Fail("not enough 5-minute bars before the entry for an ATR stop")
        stop = E - D(rule[4:]) * D(a)
    else:
        raise Fail("--stop must be range-mid, range-low, pct:X, atr:K or price:X")
    stop = floor_to(stop, tick)
    if stop >= E:
        raise Fail(f"the stop {ds(stop)} is not below the entry {ds(E)}: no plan")
    risk = E - stop
    if risk < 3 * D(tick) or risk / E < D("0.0005"):
        raise Fail(f"the stop {ds(stop)} is only {ds(risk)} under the entry: too tight to survive the spread")
    tr = args.target or "2R"
    m = re.fullmatch(r"(\d+(?:\.\d+)?)R", tr, re.I)
    if m:
        target = ceil_to(E + D(m.group(1)) * risk, tick)
    elif tr.startswith("price:"):
        target = round_to(D(tr[6:]), tick)
    else:
        raise Fail("--target must be 1R, 1.5R, 2R ... or price:X")
    if target <= E:
        raise Fail(f"the target {ds(target)} is not above the entry {ds(E)}: no plan")
    rr = (target - E) / risk
    sess = Session(cfg, t_entry.date())
    plan = {"symbol": sym, "side": "buy", "date": t_entry.date().isoformat(), "setup": ent.get("setup", "level"),
            "entry": float(E), "entry_time": iso(t_entry), "level": ent.get("level"),
            "range_high": hi, "range_low": lo, "stop": float(stop), "stop_rule": rule,
            "target": float(target), "target_rule": tr, "risk_per_unit": float(risk),
            "reward_risk": round(float(rr), 2), "flat_by": iso(sess.flat), "tick": tick, "step": info["step"]}
    print(f"{sym} long plan, entry {px(E)} at {fhm(t_entry)} UTC on {plan['date']}")
    print(f"  Stop         {px(stop)}  ({rule}, {float(risk / E) * 100:.2f}% under the entry)")
    print(f"  Target       {px(target)}  ({tr})")
    print(f"  Risk a unit  {px(risk)}   Reward:risk {float(rr):.2f}")
    print(f"  Flat by      {fhm(sess.flat)} UTC, win or lose: no overnight hold")
    print("A stop is a plan, not a guarantee: a fast market can fill past it.")
    if args.write is not None:
        out = args.write or p("plan.json")
        save_json(out, plan)
        print(f"Wrote {out}. Nothing was placed.")
    if args.json:
        print(json.dumps(plan, indent=2))
    return 0


# ----------------------------------------------------------------- time machine

def day_bars(bars):
    days = {}
    for b in bars:
        days.setdefault(from_ms(b["t"]).date(), []).append(b)
    full = {}
    for d, bs in days.items():
        if len(bs) == SLOTS and all(bs[i + 1]["t"] - bs[i]["t"] == BAR_MS for i in range(SLOTS - 1)):
            full[d] = bs
    return full


def history_run(cfg, plan, full_days, r_mult):
    """The plan's own range-breakout rule on every past full day."""
    res = {"days": 0, "triggered": 0, "target": 0, "stop": 0, "neither": 0, "r": []}
    rule = plan["stop_rule"]
    for d in sorted(full_days):
        bs = full_days[d]
        sess = Session(cfg, d)
        res["days"] += 1
        rng = [b for b in bs if to_ms(sess.start) <= b["t"] < to_ms(sess.range_end)]
        hi, lo = max(b["h"] for b in rng), min(b["l"] for b in rng)
        trig = next((i for i, b in enumerate(bs) if b["t"] >= to_ms(sess.range_end)
                     and b["t"] + BAR_MS <= to_ms(sess.last_entry) and b["c"] > hi), None)
        if trig is None:
            continue
        e = bs[trig]["c"]
        if rule == "range-mid":
            stop = (hi + lo) / 2
        elif rule == "range-low":
            stop = lo
        elif rule.startswith("pct:"):
            stop = e * (1 - float(rule[4:]) / 100)
        else:
            return None
        if stop >= e:
            continue
        risk = e - stop
        target = e + r_mult * risk
        res["triggered"] += 1
        out, last = None, e
        for b in bs[trig + 1:]:
            if b["t"] >= to_ms(sess.flat):
                break
            if b["l"] <= stop:
                out = "stop"
                break
            if b["h"] >= target:
                out = "target"
                break
            last = b["c"]
        if out == "stop":
            res["stop"] += 1
            res["r"].append(-1.0)
        elif out == "target":
            res["target"] += 1
            res["r"].append(r_mult)
        else:
            res["neither"] += 1
            res["r"].append((last - e) / risk)
    return res


def verdict_line(tp, sp, r_mult):
    hit = tp + sp
    if hit == 0:
        return "No replay reached the stop or the target: the price at the end decides everything."
    share = tp / hit * 100
    be = 100 / (1 + r_mult)
    d = share - be
    if abs(d) < 5:
        return (f"Close to a coin flip: target first {tp:.1f} % vs stop first {sp:.1f} %." if abs(r_mult - 1) < 1e-9
                else f"Close to break-even: at {r_mult:g}R the target has to come first in {be:.0f} % of the replays "
                     f"that hit one side; it did in {share:.0f} %.")
    side = "target" if d > 0 else "stop"
    strength = "Leans toward the" if abs(d) < 15 else "Strongly toward the"
    if abs(r_mult - 1) < 1e-9:
        return f"{strength} {side}: target first {tp:.1f} % vs stop first {sp:.1f} %."
    return (f"{strength} {side}: at {r_mult:g}R the target has to come first in {be:.0f} % of the replays "
            f"that hit one side; it did in {share:.0f} % (target first {tp:.1f} % vs stop first {sp:.1f} %).")


def cmd_timemachine(args):
    cfg = load_config(args.config)
    mk = Market(args.offline)
    if args.symbol:
        need = [args.entry, args.stop, args.target, args.time]
        if any(v is None for v in need):
            raise Fail("with --symbol give --entry, --stop, --target and --time (UTC, the entry bar's close)")
        plan = {"symbol": norm_symbol(args.symbol, cfg["quote"]), "entry": args.entry, "stop": args.stop,
                "target": args.target, "entry_time": iso(parse_utc(args.time)), "setup": "level",
                "stop_rule": "price"}
    else:
        plan = load_json(args.plan or p("plan.json"))
        if not plan:
            raise Fail("no trader/crypto/plan.json: run plan first, or pass the plan as flags")
    sym = plan["symbol"]
    E, S, T = float(plan["entry"]), float(plan["stop"]), float(plan["target"])
    if not S < E < T:
        raise Fail("a long plan needs stop < entry < target")
    risk = E - S
    r_mult = round((T - E) / risk, 4)
    t_entry = parse_utc(plan["entry_time"])
    if (t_entry.minute % 5) or t_entry.second:
        raise Fail("the entry time must be a 5-minute bar close (like 15:35)")
    sess = Session(cfg, t_entry.date())
    until = sess.flat if not args.until else midnight(t_entry.date()) + dt.timedelta(minutes=hhmm(args.until))
    if until <= t_entry:
        raise Fail(f"the entry at {fhm(t_entry)} UTC is not before the end time {fhm(until)} UTC")
    start = midnight(t_entry.date()) - dt.timedelta(days=args.days)
    bars = mk.klines(sym, "5m", to_ms(start) - BAR_MS, to_ms(midnight(t_entry.date())) - 1)
    full = day_bars(bars)
    if len(full) < 20:
        raise Fail(f"only {len(full)} full days of 5-minute bars came back; need at least 20")
    prev_close = {}
    for a, b in zip(bars, bars[1:]):
        if b["t"] - a["t"] == BAR_MS:
            prev_close[b["t"]] = a["c"]
    pool = [[] for _ in range(SLOTS)]
    for d, bs in full.items():
        for i, b in enumerate(bs):
            pc = prev_close.get(b["t"])
            if pc:
                pool[i].append((b["o"] / pc, b["h"] / pc, b["l"] / pc, b["c"] / pc))
    w = args.window
    cand = [sum((pool[s] for s in range(max(0, i - w), min(SLOTS, i + w + 1))), []) for i in range(SLOTS)]
    first = (t_entry.hour * 60 + t_entry.minute) // 5
    last = (until.hour * 60 + until.minute) // 5 if until.date() == t_entry.date() else SLOTS
    rnd = random.Random(args.seed)
    tp = sp = ne = green = 0
    rs, ends = [], []
    for _ in range(args.replays):
        price, out = E, None
        for slot in range(first, last):
            o, h, l, c = rnd.choice(cand[slot])
            if price * l <= S or price * o <= S:
                out = "stop"
                break
            if price * h >= T:
                out = "target"
                break
            price *= c
        if out == "stop":
            sp += 1
            rs.append(-1.0)
            ends.append(S)
        elif out == "target":
            tp += 1
            green += 1
            rs.append(r_mult)
            ends.append(T)
        else:
            ne += 1
            rs.append((price - E) / risk)
            ends.append(price)
            if price > E:
                green += 1
    n = args.replays
    q = statistics.quantiles(ends, n=20)
    res = {"symbol": sym, "entry": E, "stop": S, "target": T, "r_multiple": r_mult, "replays": n, "seed": args.seed,
           "days_in_pool": len(full), "pool_from": min(full).isoformat(), "pool_to": max(full).isoformat(),
           "until": iso(until), "target_first_pct": round(tp / n * 100, 1), "stop_first_pct": round(sp / n * 100, 1),
           "neither_pct": round(ne / n * 100, 1), "green_pct": round(green / n * 100, 1),
           "median_end": statistics.median(ends), "p5_end": q[0], "p95_end": q[-1],
           "avg_r": round(statistics.fmean(rs), 3)}
    res["verdict"] = verdict_line(res["target_first_pct"], res["stop_first_pct"], r_mult)
    hist = None
    rule = str(plan.get("stop_rule", ""))
    if plan.get("setup") == "range" and (rule in ("range-mid", "range-low") or rule.startswith("pct:")):
        hist = history_run(cfg, plan, full, r_mult)
    res["history"] = hist
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"{sym} long: entry {px(E)} at {fhm(t_entry)} UTC, stop {px(S)}, target {px(T)} "
              f"({r_mult:g}R, risk {px(risk)} a unit).")
        print(f"{n:,} replays of {fhm(t_entry)} to {fhm(until)} UTC ({last - first} five-minute bars), seed {args.seed}.")
        print(f"Each bar is drawn from {sym}'s own 5-minute bars at the same time of day (+-{w} bars) over its last "
              f"{len(full)} full UTC days ({res['pool_from']} to {res['pool_to']}).")
        print(f"  target first  {res['target_first_pct']:5.1f} %")
        print(f"  stop first    {res['stop_first_pct']:5.1f} %")
        print(f"  neither       {res['neither_pct']:5.1f} %   still open at {fhm(until)} UTC")
        print(f"  green         {res['green_pct']:5.1f} %   target first, or above the entry at {fhm(until)} UTC")
        print(f"  Price at {fhm(until)} UTC: median {px(res['median_end'])}, 90 % of replays between "
              f"{px(res['p5_end'])} and {px(res['p95_end'])}.")
        print(f"  Average R: {res['avg_r']:+.3f}")
        if hist and hist["triggered"]:
            avg = statistics.fmean(hist["r"])
            print(f"History: the range breakout fired on {hist['triggered']} of {hist['days']} past days: target first "
                  f"{hist['target']}, stop first {hist['stop']}, neither {hist['neither']}, average R {avg:+.2f}.")
        elif hist:
            print(f"History: the range breakout never fired in {hist['days']} past days.")
        else:
            print("History: skipped (it re-runs range-breakout plans with a range-mid, range-low or pct stop only).")
        print(res["verdict"])
        if res["neither_pct"] > 40:
            print(f"{res['neither_pct']:.1f} % of the replays hit neither; the price at {fhm(until)} UTC decides those.")
        print("A simulation from the last few weeks of this coin's own bars. Not a prediction: news, listings, "
              f"hacks and liquidations can do things those days never did. Repeat it with --seed {args.seed}.")
    if args.write is not None:
        save_json(args.write or p("timemachine.json"), res)
    return 0


# ----------------------------------------------------------------- sizing

def otoco_from(sym, qty, price, stop, stop_limit, target):
    return {"symbol": sym, "workingType": "LIMIT", "workingSide": "BUY", "workingPrice": ds(price),
            "workingQuantity": ds(qty), "workingTimeInForce": "GTC", "pendingSide": "SELL",
            "pendingQuantity": ds(qty), "pendingAboveType": "LIMIT_MAKER", "pendingAbovePrice": ds(target),
            "pendingBelowType": "STOP_LOSS_LIMIT", "pendingBelowStopPrice": ds(stop),
            "pendingBelowPrice": ds(stop_limit), "pendingBelowTimeInForce": "GTC"}


ENTRY_FIELDS = ("symbol", "workingType", "workingSide", "workingPrice", "workingQuantity", "workingTimeInForce",
                "pendingSide", "pendingQuantity", "pendingAboveType", "pendingAbovePrice", "pendingBelowType",
                "pendingBelowStopPrice", "pendingBelowPrice", "pendingBelowTimeInForce")
EXIT_FIELDS = ("symbol", "side", "type", "quantity")


def core(order):
    fields = EXIT_FIELDS if order.get("kind") == "exit" else ENTRY_FIELDS
    return {k: order[k] for k in fields if k in order}


def fingerprint(order):
    return hashlib.sha256(json.dumps(core(order), sort_keys=True).encode()).hexdigest()[:16]


def cmd_size(args):
    cfg = load_config(args.config)
    mk = Market(args.offline)
    now = utcnow(args)
    plan = load_json(args.plan or p("plan.json"))
    if not plan:
        raise Fail("no trader/crypto/plan.json: run plan first")
    if plan["date"] != now.date().isoformat():
        raise Fail(f"the plan is for {plan['date']}, today is {now.date().isoformat()} UTC: make a new plan")
    sym = plan["symbol"]
    if sym in {norm_symbol(b, cfg["quote"]) for b in cfg.get("blocked", [])}:
        raise Fail(f"{sym} is on your blocked list")
    info = mk.symbol_info(sym)
    tick, step = info["tick"], info["step"]
    entry = D(plan["entry"])
    note = None
    if args.limit is not None:
        price = round_to(D(args.limit), tick)
    else:
        price = entry
        if not args.no_quote:
            try:
                last = D(mk.price(sym))
                if last > entry:
                    price = ceil_to(last, tick)
                    note = f"the price is {px(last)} now, above the plan's {px(entry)}: sized at the higher price"
            except Fail:
                note = "no fresh price: sized at the plan's entry"
    stop, target = D(plan["stop"]), D(plan["target"])
    if price <= stop:
        raise Fail(f"the price {px(price)} is at or below the stop {px(stop)}: no order")
    if target <= price:
        raise Fail(f"the price {px(price)} is already at the target {px(target)}: the move happened, no order")
    stop_limit = floor_to(stop * (1 - D(cfg["stop_limit_offset_pct"]) / 100), tick)
    risk_unit = price - stop_limit
    rr = (target - price) / (price - stop)
    if rr < D(str(cfg["min_reward_risk"])):
        raise Fail(f"reward:risk is {float(rr):.2f} at {px(price)}, under your minimum {cfg['min_reward_risk']}: no order")
    budget = D(str(cfg["budget"]))
    spendable = budget * (1 - D(str(cfg["cash_buffer_pct"])) / 100)
    port = load_json(p("portfolio.json"))
    if port and port.get("free", {}).get(cfg["quote"]) is not None:
        spendable = min(spendable, D(str(port["free"][cfg["quote"]])))
    limits = [
        ("max loss", D(str(cfg["max_loss_per_trade_usd"])) / risk_unit,
         f"{money(cfg['max_loss_per_trade_usd'])} / {px(risk_unit)} a unit (to the stop-limit {px(stop_limit)})"),
        ("position cap", budget * D(str(cfg["max_position_pct"])) / 100 / price,
         f"{cfg['max_position_pct']}% of {money(budget)} / {px(price)}"),
        ("max order", D(str(cfg["max_order_usd"])) / price, f"{money(cfg['max_order_usd'])} / {px(price)}"),
        ("cash", spendable / price, f"{money(spendable)} after the {cfg['cash_buffer_pct']}% buffer / {px(price)}"),
    ]
    limits = [(n, floor_to(q, step), why) for n, q, why in limits]
    name, qty, _ = min(limits, key=lambda x: x[1])
    if qty <= 0 or qty < D(info["min_qty"]):
        raise Fail(f"zero units fit ({name} allows {ds(qty)}, Binance's minimum is {info['min_qty']}): no order")
    notional = qty * price
    floor_usd = max(D(info["min_notional"]), D(str(cfg["min_order_usd"])))
    if notional < floor_usd:
        raise Fail(f"the order is {money(notional)}, under the minimum {money(floor_usd)}: no order")
    max_loss = qty * risk_unit
    order = otoco_from(sym, qty, price, stop, stop_limit, target)
    order["kind"] = "entry"
    order["why"] = (f"{plan.get('setup', 'level')} breakout on {sym}: {ds(qty)} units, bound by your {name} "
                    f"({ds(qty)} x {px(risk_unit)} = {money(max_loss)} at the stop-limit)")
    order["plan"] = {"entry": float(price), "stop": float(stop), "stop_limit": float(stop_limit),
                     "target": float(target), "risk_per_unit": float(risk_unit), "max_loss": round(float(max_loss), 2),
                     "notional": round(float(notional), 2), "reward_risk": round(float(rr), 2),
                     "setup": plan.get("setup"), "entry_time": plan.get("entry_time"), "flat_by": plan.get("flat_by"),
                     "date": plan["date"]}
    print(f"{sym}: buy limit {px(price)}, stop {px(stop)} (stop-limit {px(stop_limit)}), target {px(target)}")
    if note:
        print(f"  Note: {note}.")
    for n, q, why in limits:
        mark = "  <- decides it" if n == name else ""
        print(f"  {n:<13} {why:<58} {ds(q):>14}{mark}")
    print(f"Size: {ds(qty)} units = {money(notional)}. Max loss at the stop-limit: {money(max_loss)}. "
          f"Reward:risk {float(rr):.2f}.")
    print("A stop is an order, not a promise: in a fast market it can fill below the stop-limit, or not at all.")
    if args.write:
        save_json(p("orders.json"), [order])
        print("Wrote trader/crypto/orders.json (one OTOCO bracket). Nothing was placed. Next: risk.")
    return 0


# ----------------------------------------------------------------- risk

def broker_log():
    return load_json(p("state/broker.json"), {"events": []})


def today_events(log, day, kinds):
    return [e for e in log["events"] if e.get("date") == day and e.get("kind") in kinds]


def check_order(cfg, order, info, now, log, batch_seen):
    reasons = []
    kind = order.get("kind", "entry")
    sym = order.get("symbol")
    day = now.date().isoformat()
    if not sym:
        return ["format: no symbol"]
    if info is None:
        return [f"asset: Binance has no spot symbol {sym}"]
    if info["status"] != "TRADING" or not info["spot"]:
        reasons.append(f"asset: {sym} is not trading on spot right now ({info['status']})")
    if info["quote"] != cfg["quote"]:
        reasons.append(f"asset: {sym} is quoted in {info['quote']}, your config trades {cfg['quote']} pairs only")
    if kind == "exit":
        if order.get("side") != "SELL" or order.get("type") != "MARKET":
            reasons.append("format: an exit is a MARKET SELL")
        try:
            q = D(order["quantity"])
            if q <= 0 or not is_multiple(q, info["step"]):
                reasons.append(f"format: quantity {order['quantity']} is not a multiple of the step {info['step']}")
        except Exception:
            reasons.append("format: bad quantity")
        return reasons  # getting out is never blocked by a day limit
    missing = [k for k in ENTRY_FIELDS if k not in order]
    if missing:
        return reasons + [f"format: missing {', '.join(missing)}"]
    if order["workingType"] != "LIMIT" or order["workingSide"] != "BUY" or order["pendingSide"] != "SELL":
        reasons.append("format: entries are a LIMIT BUY with a SELL bracket (long only)")
    if order["pendingBelowType"] != "STOP_LOSS_LIMIT" or order["pendingAboveType"] != "LIMIT_MAKER":
        reasons.append("format: the bracket must be a LIMIT_MAKER target and a STOP_LOSS_LIMIT stop")
    try:
        price, qty = D(order["workingPrice"]), D(order["workingQuantity"])
        stop, slim = D(order["pendingBelowStopPrice"]), D(order["pendingBelowPrice"])
        target = D(order["pendingAbovePrice"])
    except Exception:
        return reasons + ["format: a price or quantity is not a number"]
    if D(order["pendingQuantity"]) != qty:
        reasons.append("format: the bracket quantity differs from the entry quantity")
    for name, v in (("price", price), ("stop", stop), ("stop-limit", slim), ("target", target)):
        if v <= 0 or not is_multiple(v, info["tick"]):
            reasons.append(f"format: {name} {ds(v)} is not a multiple of the tick {info['tick']}")
    if qty <= 0 or not is_multiple(qty, info["step"]) or qty < D(info["min_qty"]):
        reasons.append(f"format: quantity {ds(qty)} breaks the step {info['step']} / minimum {info['min_qty']}")
    if not info["oto"] or not info["oco"]:
        reasons.append(f"asset: {sym} does not allow OTO/OCO orders")
    if sym in {norm_symbol(b, cfg["quote"]) for b in cfg.get("blocked", [])}:
        reasons.append(f"blocked: {sym} is on your blocked list")
    if cfg.get("require_stop", True) and not stop:
        reasons.append("stop: no stop")
    if not slim <= stop < price < target:
        reasons.append("stop: needs stop-limit <= stop < entry < target")
        return reasons
    max_loss = qty * (price - slim)
    if max_loss > D(str(cfg["max_loss_per_trade_usd"])) + D("0.005"):
        reasons.append(f"max loss: {money(max_loss)} at the stop-limit is over your {money(cfg['max_loss_per_trade_usd'])}")
    rr = (target - price) / (price - stop)
    if rr < D(str(cfg["min_reward_risk"])):
        reasons.append(f"reward:risk {float(rr):.2f} is under your minimum {cfg['min_reward_risk']}")
    notional = qty * price
    budget = D(str(cfg["budget"]))
    if notional > D(str(cfg["max_order_usd"])):
        reasons.append(f"size: {money(notional)} is over your max order {money(cfg['max_order_usd'])}")
    if notional < max(D(str(cfg["min_order_usd"])), D(info["min_notional"])):
        reasons.append(f"size: {money(notional)} is under the minimum order")
    if notional > budget * D(str(cfg["max_position_pct"])) / 100:
        reasons.append(f"position: {money(notional)} is over {cfg['max_position_pct']}% of your {money(budget)} budget")
    spendable = budget * (1 - D(str(cfg["cash_buffer_pct"])) / 100)
    port = load_json(p("portfolio.json"))
    if port and port.get("free", {}).get(cfg["quote"]) is not None:
        spendable = min(spendable, D(str(port["free"][cfg["quote"]])))
    if notional > spendable:
        reasons.append(f"cash: {money(notional)} would dip into your cash buffer (spendable {money(spendable)})")
    placed = today_events(log, day, ("placed",))
    entries = [e for e in placed if e.get("order_kind") == "entry"]
    used = sum(D(str(e.get("max_loss", 0))) for e in entries)
    if used + max_loss > D(str(cfg["max_daily_loss_usd"])) + D("0.005"):
        reasons.append(f"daily loss: {money(used)} already at risk today + {money(max_loss)} is over your "
                       f"{money(cfg['max_daily_loss_usd'])}")
    if len(entries) >= int(cfg["max_trades_per_day"]):
        reasons.append(f"trades: {len(entries)} entries today already (your limit {cfg['max_trades_per_day']})")
    if len(placed) >= int(cfg["max_orders_per_day"]):
        reasons.append(f"daily limit: {len(placed)} orders placed today (your limit {cfg['max_orders_per_day']})")
    if any(e.get("symbol") == sym for e in entries) or sym in batch_seen:
        reasons.append(f"duplicate: a {sym} entry was already placed today or is in this batch")
    sess = Session(cfg, now.date())
    if not sess.range_end <= now <= sess.last_entry:
        reasons.append(f"time: new entries only {fhm(sess.range_end)} to {fhm(sess.last_entry)} UTC "
                       f"(it is {fhm(now)} UTC)")
    pd = (order.get("plan") or {}).get("date")
    if pd and pd != day:
        reasons.append(f"plan: the order's plan is for {pd}, not today ({day})")
    return reasons


def cmd_risk(args):
    cfg = load_config(args.config)
    mk = Market(args.offline)
    now = utcnow(args)
    orders = load_json(p("orders.json"))
    if not orders:
        raise Fail("no trader/crypto/orders.json: run size --write (or ticket flatten) first")
    log = broker_log()
    state = load_json(p("state/risk.json"), {"verdicts": []})
    seen, results, ok_all = set(), [], True
    for i, o in enumerate(orders, 1):
        try:
            info = mk.symbol_info(o.get("symbol", ""))
        except Fail:
            info = None
        reasons = check_order(cfg, o, info, now, log, seen)
        if o.get("kind", "entry") == "entry":
            seen.add(o.get("symbol"))
        verdict = "PASS" if not reasons else "BLOCK"
        ok_all &= not reasons
        fp = fingerprint(o)
        results.append({"index": i, "symbol": o.get("symbol"), "kind": o.get("kind", "entry"), "fingerprint": fp,
                        "verdict": verdict, "reasons": reasons, "time": iso(now), "date": now.date().isoformat()})
        print(f"#{i} {o.get('symbol')} {o.get('kind', 'entry')}: {verdict}")
        for r in reasons:
            print(f"   - {r}")
        if not reasons and o.get("kind", "entry") == "entry":
            pl = o.get("plan", {})
            print(f"   max loss {money(pl.get('max_loss', 0))}, {money(pl.get('notional', 0))} order, "
                  f"reward:risk {pl.get('reward_risk')}")
    if not args.no_write:
        state["verdicts"].extend(results)
        save_json(p("state/risk.json"), state)
    print("A PASS is a rules check, not a recommendation." + (" Next: ticket show." if ok_all else ""))
    return 0 if ok_all else 1


# ----------------------------------------------------------------- ticket / broker

def latest_verdict(fp, day):
    st = load_json(p("state/risk.json"), {"verdicts": []})
    vs = [v for v in st["verdicts"] if v["fingerprint"] == fp and v["date"] == day]
    return vs[-1] if vs else None


YES = re.compile(r"^\s*(yes|y)\b", re.I)
HEDGE = re.compile(r"\b(no|not|don'?t|wait|later|cancel|stop|hold|maybe)\b", re.I)


def ticket_text(i, o, cfg):
    live = not cfg.get("paper", True)
    where = "LIVE Binance.com (real money)" if live else "Binance Spot Testnet (paper)"
    if o.get("kind") == "exit":
        return (f"Ticket #{i}  EXIT  {where}\n  Cancel every open {o['symbol']} order, then MARKET SELL "
                f"{o['quantity']} {o['symbol']}.")
    pl = o.get("plan", {})
    return (f"Ticket #{i}  {o['symbol']}  {where}\n"
            f"  BUY {o['workingQuantity']} at limit {o['workingPrice']} (GTC)\n"
            f"  then one-cancels-the-other: take profit {o['pendingAbovePrice']} (LIMIT_MAKER) /\n"
            f"  stop {o['pendingBelowStopPrice']} -> sell limit {o['pendingBelowPrice']} (STOP_LOSS_LIMIT)\n"
            f"  Order {money(pl.get('notional', 0))}. Max loss at the stop-limit {money(pl.get('max_loss', 0))}. "
            f"Reward:risk {pl.get('reward_risk')}. Flat by {str(pl.get('flat_by', ''))[11:16]} UTC.")


def cmd_ticket(args):
    cfg = load_config(args.config)
    now = utcnow(args)
    day = now.date().isoformat()
    log = broker_log()
    if args.action == "log":
        for e in log["events"][-args.last:]:
            print(json.dumps(e))
        return 0
    if args.action == "flatten":
        sym = norm_symbol(args.symbol, cfg["quote"])
        mk = Market(args.offline)
        info = mk.symbol_info(sym)
        qty = floor_to(D(args.qty), info["step"])
        if qty <= 0:
            raise Fail("the quantity rounds to zero")
        save_json(p("orders.json"), [{"symbol": sym, "side": "SELL", "type": "MARKET", "quantity": ds(qty),
                                      "kind": "exit", "why": f"flatten {ds(qty)} {sym}"}])
        print(f"Wrote an exit ticket to trader/crypto/orders.json: cancel open {sym} orders, MARKET SELL {ds(qty)}.")
        print("Next: risk, then ticket show / approve / place. Nothing was placed.")
        return 0
    orders = load_json(p("orders.json")) or []
    if not orders:
        raise Fail("no orders in trader/crypto/orders.json")
    if args.action == "show":
        for i, o in enumerate(orders, 1):
            fp = fingerprint(o)
            v = latest_verdict(fp, day)
            placed = any(e.get("fingerprint") == fp for e in today_events(log, day, ("placed",)))
            state = "PLACED" if placed else ("READY: waiting for your yes" if v and v["verdict"] == "PASS"
                                             else "NOT READY: " + ("risk BLOCK" if v else "no risk check today"))
            print(ticket_text(i, o, cfg))
            print(f"  [{state}]  fingerprint {fp}")
            print("  Exact request: POST " + ("/api/v3/order" if o.get("kind") == "exit" else "/api/v3/orderList/otoco"))
            print("  " + json.dumps(core(o)))
        return 0
    i = args.index
    if not 1 <= i <= len(orders):
        raise Fail(f"there is no ticket #{i}")
    o = orders[i - 1]
    fp = fingerprint(o)
    if args.action == "approve":
        reply = args.reply or ""
        if not YES.search(reply) or HEDGE.search(reply):
            log["events"].append({"time": iso(now), "date": day, "kind": "declined", "index": i, "fingerprint": fp,
                                  "symbol": o["symbol"], "reply": reply})
            save_json(p("state/broker.json"), log)
            print(f"Not a clear yes ({reply!r}): ticket #{i} stays unplaced.")
            return 1
        v = latest_verdict(fp, day)
        if not v or v["verdict"] != "PASS":
            raise Fail(f"ticket #{i} has no risk PASS today: run risk first")
        log["events"].append({"time": iso(now), "date": day, "kind": "approved", "index": i, "fingerprint": fp,
                              "symbol": o["symbol"], "reply": reply})
        save_json(p("state/broker.json"), log)
        print(f"Recorded your yes for ticket #{i} word for word. It is good for 15 minutes.")
        return 0
    if args.action == "place":
        v = latest_verdict(fp, day)
        if not v or v["verdict"] != "PASS":
            raise Fail(f"ticket #{i} has no risk PASS today: nothing placed")
        if any(e.get("fingerprint") == fp for e in today_events(log, day, ("placed",))):
            raise Fail(f"ticket #{i} was already placed today")
        appr = [e for e in today_events(log, day, ("approved",)) if e["fingerprint"] == fp]
        if not appr:
            raise Fail(f"no recorded yes for ticket #{i}: nothing placed")
        age = (now - parse_utc(appr[-1]["time"])).total_seconds()
        if age > APPROVAL_TTL_S or parse_utc(appr[-1]["time"]) < parse_utc(v["time"]):
            raise Fail("the yes is older than 15 minutes or older than the risk check: ask again")
        live = not cfg.get("paper", True)
        if live and not args.live:
            raise Fail('the config says "paper": false; add --live to send a real order')
        if args.live and not live:
            raise Fail('--live given but the config says "paper": true: nothing placed')
        host = LIVE_HOST if live else TESTNET_HOST
        client = Signed(host, *load_keys(live), offline_sender=getattr(args, "_sender", None))
        if not live and o.get("kind") != "exit" and not getattr(args, "_sender", None):
            try:
                tp = Market(host=TESTNET_HOST).price(o["symbol"])
                gap = abs(tp / float(o["workingPrice"]) - 1) * 100
                if gap > 1:
                    print(f"Warning: testnet's own {o['symbol']} price is {px(tp)}, {gap:.1f}% from the order's "
                          f"{o['workingPrice']}. Testnet has its own order book, so paper fills will not match "
                          "the real market.")
            except Fail:
                pass
        try:
            if o.get("kind") == "exit":
                try:
                    client.request("DELETE", "/api/v3/openOrders", {"symbol": o["symbol"]})
                except Fail as e:
                    if "-2011" not in str(e):
                        raise
                resp = client.request("POST", "/api/v3/order", dict(core(o), newOrderRespType="FULL"))
            else:
                resp = client.request("POST", "/api/v3/orderList/otoco", core(o))
        except Fail as e:
            log["events"].append({"time": iso(now), "date": day, "kind": "failed", "index": i, "fingerprint": fp,
                                  "symbol": o["symbol"], "error": str(e)})
            save_json(p("state/broker.json"), log)
            raise
        ev = {"time": iso(now), "date": day, "kind": "placed", "order_kind": o.get("kind", "entry"), "index": i,
              "fingerprint": fp, "symbol": o["symbol"], "venue": "live" if live else "testnet",
              "max_loss": (o.get("plan") or {}).get("max_loss", 0),
              "broker_ids": [x.get("orderId") for x in (resp.get("orderReports") or resp.get("orders") or [resp])
                             if isinstance(x, dict)]}
        log["events"].append(ev)
        save_json(p("state/broker.json"), log)
        print(f"Placed ticket #{i} on {'LIVE Binance.com' if live else 'Binance Spot Testnet'}: "
              f"order ids {ev['broker_ids']}.")
        return 0
    raise Fail("unknown ticket action")


def load_keys(live):
    pre = "BINANCE_API" if live else "BINANCE_TESTNET_API"
    key, secret = os.environ.get(pre + "_KEY"), os.environ.get(pre + "_SECRET")
    if not (key and secret):
        f = os.path.expanduser(f"~/.config/crypto-trader/{'live' if live else 'testnet'}.json")
        data = load_json(f)
        if data:
            if os.name == "posix" and os.stat(f).st_mode & 0o077:
                raise Fail(f"{f} is readable by other users: chmod 600 it")
            key, secret = data.get("api_key"), data.get("api_secret")
    if not (key and secret):
        raise Fail(f"no {'live' if live else 'testnet'} API key: set {pre}_KEY and {pre}_SECRET in your shell, "
                   f"or write ~/.config/crypto-trader/{'live' if live else 'testnet'}.json (chmod 600). "
                   "Never paste keys into the chat.")
    return key, secret


def sign(secret, query):
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


class Signed:
    def __init__(self, host, key, secret, offline_sender=None):
        self.host, self.key, self.secret, self.sender = host, key, secret, offline_sender

    def request(self, method, path, params):
        params = dict(params, recvWindow=5000, timestamp=int(time.time() * 1000))
        q = urllib.parse.urlencode(params)
        q += "&signature=" + sign(self.secret, q)
        if self.sender:
            return self.sender(method, path, q)
        url = self.host + path
        data = None
        if method in ("GET", "DELETE"):
            url += "?" + q
        else:
            data = q.encode()
        req = urllib.request.Request(url, data=data, method=method, headers={
            "X-MBX-APIKEY": self.key, "User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read()[:400].decode("utf-8", "replace")
            if e.code == 451:
                raise Fail(f"{self.host} refused this location (HTTP 451): place from a computer where Binance is available")
            raise Fail(f"Binance {path} HTTP {e.code}: {body}")
        except urllib.error.URLError as e:
            raise Fail(f"could not reach {self.host}: {e.reason}")


def cmd_balance(args):
    cfg = load_config(args.config)
    live = not cfg.get("paper", True)
    client = Signed(LIVE_HOST if live else TESTNET_HOST, *load_keys(live))
    acct = client.request("GET", "/api/v3/account", {"omitZeroBalances": "true"})
    free = {b["asset"]: float(b["free"]) for b in acct.get("balances", []) if float(b["free"]) or float(b["locked"])}
    locked = {b["asset"]: float(b["locked"]) for b in acct.get("balances", []) if float(b["locked"])}
    save_json(p("portfolio.json"), {"as_of": iso(utcnow()), "venue": "live" if live else "testnet",
                                    "free": free, "locked": locked})
    print(f"{'Live' if live else 'Testnet'} balances: {cfg['quote']} free {free.get(cfg['quote'], 0):,.2f}; "
          f"{len(free)} assets. Wrote trader/crypto/portfolio.json.")
    return 0


def cmd_init(args):
    path = p("config.json")
    if os.path.exists(path) and not args.force:
        print(f"{path} already exists (use --force to rewrite it).")
        return 0
    cfg = dict(DEFAULTS)
    stock = load_json(os.path.join("trader", "config.json"), {}) or {}
    for k in COPY_FROM_STOCK:
        if k in stock:
            cfg[k] = stock[k]
    if cfg["max_order_usd"] > cfg["budget"]:
        cfg["max_order_usd"] = cfg["budget"]
    save_json(path, cfg)
    print(f"Wrote {path}: budget {money(cfg['budget'])}, max loss {money(cfg['max_loss_per_trade_usd'])} a trade, "
          f"{money(cfg['max_daily_loss_usd'])} a day, {cfg['max_trades_per_day']} trades, window "
          f"{cfg['session_start']}-{cfg['flat_by']} UTC, paper={cfg['paper']}.")
    return 0


# ----------------------------------------------------------------- cli

def build_parser():
    ap = argparse.ArgumentParser(prog="crypto.py", description="Binance spot day-trading tools (stdlib only).")
    ap.add_argument("--config", help="config path (default trader/crypto/config.json)")
    ap.add_argument("--offline", help="directory of saved Binance JSON (no network)")
    ap.add_argument("--now", help='pretend it is this UTC time, "YYYY-MM-DD HH:MM"')
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init")
    s.add_argument("--force", action="store_true")
    s = sub.add_parser("scan")
    s.add_argument("--symbols")
    s.add_argument("--min-volume", type=float)
    s.add_argument("--limit", type=int)
    s.add_argument("--show", type=int, default=15)
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-write", action="store_true")
    s = sub.add_parser("price")
    s.add_argument("symbol")
    s = sub.add_parser("entry")
    s.add_argument("action", choices=["check", "watch"])
    s.add_argument("symbol")
    s.add_argument("--level", type=float)
    s.add_argument("--min-volume-x", type=float)
    s.add_argument("--write", action="store_true")
    s.add_argument("--max-hours", type=float, default=12)
    s = sub.add_parser("plan")
    s.add_argument("--from", dest="from_")
    s.add_argument("--symbol")
    s.add_argument("--entry", type=float)
    s.add_argument("--time")
    s.add_argument("--setup", choices=["range", "level"])
    s.add_argument("--range-high", type=float)
    s.add_argument("--range-low", type=float)
    s.add_argument("--stop")
    s.add_argument("--target")
    s.add_argument("--write", nargs="?", const="")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("timemachine")
    s.add_argument("--plan")
    s.add_argument("--symbol")
    s.add_argument("--entry", type=float)
    s.add_argument("--stop", type=float)
    s.add_argument("--target", type=float)
    s.add_argument("--time")
    s.add_argument("--replays", type=int, default=10000)
    s.add_argument("--seed", type=int, default=19)
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--window", type=int, default=2)
    s.add_argument("--until")
    s.add_argument("--json", action="store_true")
    s.add_argument("--write", nargs="?", const="")
    s = sub.add_parser("size")
    s.add_argument("--plan")
    s.add_argument("--limit", type=float)
    s.add_argument("--no-quote", action="store_true")
    s.add_argument("--write", action="store_true")
    s = sub.add_parser("risk")
    s.add_argument("--no-write", action="store_true")
    s = sub.add_parser("ticket")
    s.add_argument("action", choices=["show", "approve", "place", "flatten", "log"])
    s.add_argument("--index", type=int, default=1)
    s.add_argument("--reply")
    s.add_argument("--live", action="store_true")
    s.add_argument("--symbol")
    s.add_argument("--qty")
    s.add_argument("--last", type=int, default=20)
    sub.add_parser("balance")
    return ap


COMMANDS = {"init": cmd_init, "scan": cmd_scan, "price": cmd_price, "entry": cmd_entry, "plan": cmd_plan,
            "timemachine": cmd_timemachine, "size": cmd_size, "risk": cmd_risk, "ticket": cmd_ticket,
            "balance": cmd_balance}


def main(argv=None, sender=None):
    args = build_parser().parse_args(argv)
    if sender:
        args._sender = sender
    if args.cmd == "ticket" and args.action == "flatten" and not (args.symbol and args.qty):
        print("crypto.py: flatten needs --symbol and --qty", file=sys.stderr)
        return 2
    try:
        return COMMANDS[args.cmd](args)
    except Fail as e:
        print(f"crypto.py: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
