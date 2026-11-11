"""Offline tests for crypto.py. Run: python3 -m unittest discover -s .claude/skills/crypto-trader/tests"""

import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import random
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("crypto", os.path.join(HERE, "..", "crypto.py"))
crypto = importlib.util.module_from_spec(spec)
spec.loader.exec_module(crypto)

DAY = dt.date(2026, 11, 11)
BAR = crypto.BAR_MS


def kline(t, o, h, l, c, v=10.0):
    return [t, f"{o:.2f}", f"{h:.2f}", f"{l:.2f}", f"{c:.2f}", f"{v:.4f}", t + BAR - 1, f"{v * c:.2f}", 1, "0", "0", "0"]


def build_offline(d):
    """35 days of a seeded random walk in 5-minute bars, plus a clean breakout on DAY."""
    rnd = random.Random(7)
    start = crypto.to_ms(crypto.midnight(DAY - dt.timedelta(days=35)))
    end = crypto.to_ms(crypto.midnight(DAY + dt.timedelta(days=1)))
    rows, price, t = [], 2000.0, start
    s_range = crypto.to_ms(crypto.midnight(DAY)) + (14 * 60 + 30) * 60000
    while t < end:
        o = price
        if s_range <= t < s_range + 3600000:          # the range: 2000 to 2010
            o, c = 2005.0, 2005.0 + rnd.uniform(-3, 3)
            h, l = 2010.0, 2000.0
        elif s_range + 3600000 <= t < s_range + 3600000 + BAR:   # first bar after: closes 2015
            o, h, l, c = 2006.0, 2016.0, 2005.0, 2015.0
        else:
            c = o * (1 + rnd.gauss(0, 0.002))
            h, l = max(o, c) * (1 + abs(rnd.gauss(0, 0.001))), min(o, c) * (1 - abs(rnd.gauss(0, 0.001)))
        rows.append(kline(t, o, h, l, c))
        price, t = c, t + BAR
    with open(os.path.join(d, "klines-ETHUSDT-5m.json"), "w") as f:
        json.dump(rows, f)
    daily, p = [], 1500.0
    for i in range(60):
        t = crypto.to_ms(crypto.midnight(DAY - dt.timedelta(days=59 - i)))
        c = p * (1.01 if i < 59 else 1.05)
        daily.append(kline(t, p, c * 1.01, p * 0.99, c, 1e5))
        p = c
    with open(os.path.join(d, "klines-ETHUSDT-1d.json"), "w") as f:
        json.dump(daily, f)
    info = {"symbols": [{"symbol": "ETHUSDT", "status": "TRADING", "baseAsset": "ETH", "quoteAsset": "USDT",
                         "isSpotTradingAllowed": True, "otoAllowed": True, "ocoAllowed": True,
                         "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
                                     {"filterType": "LOT_SIZE", "minQty": "0.00010000", "maxQty": "9000",
                                      "stepSize": "0.00010000"},
                                     {"filterType": "NOTIONAL", "minNotional": "5.00000000"}]},
                        {"symbol": "USDCUSDT", "status": "TRADING", "baseAsset": "USDC", "quoteAsset": "USDT",
                         "isSpotTradingAllowed": True, "filters": []}]}
    with open(os.path.join(d, "exchangeInfo.json"), "w") as f:
        json.dump(info, f)
    with open(os.path.join(d, "ticker24hr.json"), "w") as f:
        json.dump([{"symbol": "ETHUSDT", "quoteVolume": "900000000", "priceChangePercent": "5.0"},
                   {"symbol": "USDCUSDT", "quoteVolume": "900000000", "priceChangePercent": "0"}], f)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.getcwd()
        os.chdir(self.tmp.name)
        self.off = os.path.join(self.tmp.name, "offline")
        os.makedirs(self.off)
        build_offline(self.off)
        os.makedirs("trader/crypto")
        cfg = dict(crypto.DEFAULTS, max_daily_loss_usd=45)
        with open("trader/crypto/config.json", "w") as f:
            json.dump(cfg, f)

    def tearDown(self):
        os.chdir(self.old)
        self.tmp.cleanup()

    def run_cli(self, *argv, sender=None):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = crypto.main(["--offline", self.off, *argv], sender=sender)
        return code, buf.getvalue()

    def pipeline(self, now="2026-11-11 15:40"):
        c, out = self.run_cli("--now", now, "entry", "check", "ETHUSDT", "--write")
        self.assertEqual(c, 0, out)
        c, out = self.run_cli("plan", "--target", "1R", "--write")
        self.assertEqual(c, 0, out)
        c, out = self.run_cli("--now", now, "size", "--no-quote", "--write")
        self.assertEqual(c, 0, out)
        return out


class TestMath(unittest.TestCase):
    def test_signature_matches_binance_docs_example(self):
        secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
        q = ("symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1"
             "&recvWindow=5000&timestamp=1499827319559")
        self.assertEqual(crypto.sign(secret, q), "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71")

    def test_rounding(self):
        self.assertEqual(crypto.ds(crypto.floor_to("0.36129", "0.0001")), "0.3612")
        self.assertEqual(crypto.ds(crypto.ceil_to("2096.751", "0.01")), "2096.76")
        self.assertEqual(crypto.ds(crypto.floor_to("12345", "10")), "12340")

    def test_symbols(self):
        self.assertEqual(crypto.norm_symbol("btc"), "BTCUSDT")
        self.assertEqual(crypto.norm_symbol("eth/usdt"), "ETHUSDT")
        self.assertEqual(crypto.norm_symbol("ETHBTC"), "ETHBTC")

    def test_setups(self):
        closes = [100 + i * 0.1 for i in range(40)] + [110]
        s, _ = crypto.daily_setups(closes)
        self.assertIn("breakout_20d", s)
        down = [100 * 0.98 ** i for i in range(40)]
        s, r = crypto.daily_setups(down)
        self.assertIn("oversold", s)
        self.assertLess(r, 30)

    def test_verdict(self):
        self.assertTrue(crypto.verdict_line(30, 32, 1).startswith("Close to a coin flip"))
        self.assertTrue(crypto.verdict_line(30, 50, 1).startswith("Leans toward the stop"))
        self.assertTrue(crypto.verdict_line(10, 60, 2).startswith("Strongly toward the stop"))


class TestPipeline(Base):
    def test_scan_skips_stablecoins(self):
        c, out = self.run_cli("--now", "2026-11-12 00:10", "scan")
        self.assertEqual(c, 0, out)
        wl = json.load(open("trader/crypto/watchlist.json"))
        self.assertEqual([h["symbol"] for h in wl["hits"]], ["ETHUSDT"])
        self.assertIn("breakout_20d", wl["hits"][0]["setups"])

    def test_entry_states(self):
        self.assertIn("not_started", crypto.entry_state(crypto.load_config(), crypto.Market(self.off), "ETHUSDT",
                                                        crypto.parse_utc("2026-11-11 10:00"))["status"])
        st = crypto.entry_state(crypto.load_config(), crypto.Market(self.off), "ETHUSDT",
                                crypto.parse_utc("2026-11-11 15:00"))
        self.assertEqual(st["status"], "forming")
        st = crypto.entry_state(crypto.load_config(), crypto.Market(self.off), "ETHUSDT",
                                crypto.parse_utc("2026-11-11 15:40"))
        self.assertEqual(st["status"], "triggered")
        self.assertEqual(st["entry"], 2015.0)
        self.assertEqual(st["entry_time"], "2026-11-11T15:35:00Z")
        self.assertEqual((st["range_high"], st["range_low"]), (2010.0, 2000.0))

    def test_plan_and_size_respect_max_loss(self):
        out = self.pipeline()
        plan = json.load(open("trader/crypto/plan.json"))
        self.assertEqual(plan["stop"], 2005.0)        # range midpoint
        self.assertEqual(plan["target"], 2025.0)      # 1R
        order = json.load(open("trader/crypto/orders.json"))[0]
        self.assertLessEqual(order["plan"]["max_loss"], 15.0)
        self.assertLessEqual(order["plan"]["notional"], 750.0)
        self.assertEqual(order["workingQuantity"], order["pendingQuantity"])
        self.assertIn("decides it", out)

    def test_plan_refuses_stop_above_entry(self):
        c, out = self.run_cli("plan", "--symbol", "ETHUSDT", "--entry", "2000", "--time", "2026-11-11 15:35",
                              "--stop", "price:2001")
        self.assertEqual(c, 1)
        self.assertIn("not below the entry", out)

    def test_timemachine_is_repeatable(self):
        self.pipeline()
        c1, o1 = self.run_cli("timemachine", "--replays", "2000", "--json")
        c2, o2 = self.run_cli("timemachine", "--replays", "2000", "--json")
        self.assertEqual(c1, 0, o1)
        r = json.loads(o1)
        self.assertEqual(o1, o2)
        self.assertAlmostEqual(r["target_first_pct"] + r["stop_first_pct"] + r["neither_pct"], 100, delta=0.2)
        self.assertGreaterEqual(r["days_in_pool"], 20)
        self.assertIsNotNone(r["history"])

    def test_risk_gates_and_ticket(self):
        self.pipeline()
        c, out = self.run_cli("--now", "2026-11-11 15:41", "risk")
        self.assertEqual(c, 0, out)
        sent = []

        def sender(method, path, q):
            sent.append((method, path, q))
            return {"orderReports": [{"orderId": 1}, {"orderId": 2}, {"orderId": 3}]}

        os.environ["BINANCE_TESTNET_API_KEY"] = "k"
        os.environ["BINANCE_TESTNET_API_SECRET"] = "s"
        try:
            c, out = self.run_cli("--now", "2026-11-11 15:42", "ticket", "place", sender=sender)
            self.assertEqual(c, 1)
            self.assertIn("no recorded yes", out)
            c, out = self.run_cli("--now", "2026-11-11 15:42", "ticket", "approve", "--reply", "yes, but not now")
            self.assertEqual(c, 1)
            c, out = self.run_cli("--now", "2026-11-11 15:42", "ticket", "approve", "--reply", "yes")
            self.assertEqual(c, 0, out)
            c, out = self.run_cli("--now", "2026-11-11 16:30", "ticket", "place", sender=sender)
            self.assertEqual(c, 1)                       # the yes went stale
            self.assertEqual(sent, [])
            c, out = self.run_cli("--now", "2026-11-11 15:43", "ticket", "place", sender=sender)
            self.assertEqual(c, 0, out)
            self.assertEqual(sent[0][1], "/api/v3/orderList/otoco")
            self.assertIn("signature=", sent[0][2])
            c, out = self.run_cli("--now", "2026-11-11 15:44", "ticket", "place", sender=sender)
            self.assertIn("already placed", out)
            # the same symbol again today is a duplicate
            c, out = self.run_cli("--now", "2026-11-11 15:45", "risk")
            self.assertEqual(c, 1)
            self.assertIn("duplicate", out)
            # but getting out is never blocked
            c, out = self.run_cli("--now", "2026-11-11 20:50", "ticket", "flatten", "--symbol", "ETHUSDT",
                                  "--qty", "0.3")
            self.assertEqual(c, 0, out)
            c, out = self.run_cli("--now", "2026-11-11 20:50", "risk")
            self.assertEqual(c, 0, out)
        finally:
            del os.environ["BINANCE_TESTNET_API_KEY"], os.environ["BINANCE_TESTNET_API_SECRET"]

    def test_risk_blocks_outside_window_and_live_without_flag(self):
        self.pipeline()
        c, out = self.run_cli("--now", "2026-11-11 21:00", "risk", "--no-write")
        self.assertEqual(c, 1)
        self.assertIn("time:", out)
        cfg = json.load(open("trader/crypto/config.json"))
        cfg["max_loss_per_trade_usd"] = 5
        json.dump(cfg, open("trader/crypto/config.json", "w"))
        c, out = self.run_cli("--now", "2026-11-11 15:41", "risk", "--no-write")
        self.assertIn("max loss", out)


if __name__ == "__main__":
    unittest.main()
