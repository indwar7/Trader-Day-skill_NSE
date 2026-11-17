"""nse-exit tests. All offline: Yahoo is never called (_open_url is faked in the CLI tests).
Bars for the ATR stop and the entry -> plan chain come from the shared fixtures in
tests/fixtures/nse/ (SBIN's real 5-minute bars of the morning of 2026-11-17, see
test_entry.py) and nse-entry's cut of the full 2026-11-16 session.

tests/fixtures/nse-exit/SBIN-3mo-1d.json  SBIN's daily closes 2026-08-17..2026-11-17, cut
from the shared spark-3mo-1d.json into Yahoo's v8 chart shape (range=3mo&interval=1d).
Month-end closes: 2026-08-31 1,060.00 (tick 0.10), 2026-09-30 959.50 and 2026-10-30
961.85 (tick 0.05), so SBIN trades on 0.05 ticks above 1,000 in November.
"""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

from _load import load, FIX, ROOT

exitp = load("nse-exit", "exit")
entry = load("nse-entry", "entry")

SBIN = os.path.join(FIX, "nse", "SBIN-1d-5m.json")
SBIN_1116 = os.path.join(FIX, "nse-entry", "SBIN-2026-11-16-5m.json")
SBIN_DAILY = os.path.join(FIX, "nse-exit", "SBIN-3mo-1d.json")
with open(SBIN_DAILY, encoding="utf-8") as _fh:
    SBIN_DAILY_TEXT = _fh.read()
MONTHLY = {"rule": "NSE monthly review", "close": 961.85, "date": "2026-10-30"}

# trader/plan.json for the reel, exactly as the contract writes it
REEL_PLAN = {"created_by": "nse-exit", "symbol": "SBIN", "side": "buy", "date": "2026-11-17", "setup": "orb",
             "entry": 1105.7, "entry_time": "9:45 AM", "entry_bar": 5, "interval": "5m",
             "opening_range": {"high": 1101.4, "low": 1092.0, "minutes": 15},
             "stop": 1096.7, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
             "target": 1114.7, "target_r": 1.0, "target_label": "1R (one times the risk)",
             "risk_per_share": 9.0, "reward_risk": 1.0, "flat_by": "15:00"}
PLAN_KEYS = sorted(list(REEL_PLAN) + ["stop_detail", "created_at", "note", "tick", "tick_basis"])
REEL_ENTRY = {"created_by": "nse-entry", "symbol": "SBIN", "setup": "orb", "side": "buy", "date": "2026-11-17",
              "status": "triggered", "time": "9:45 AM", "bar_start": "09:40", "bar_index": 5, "price": 1105.7,
              "level": 1101.4, "interval": "5m", "opening_range": {"high": 1101.4, "low": 1092.0, "minutes": 15},
              "source": "yahoo", "checked_at": "2026-11-17T04:15:00Z"}
DEC = Decimal


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def c(entry_px, stop, target="2R", oh=1101.4, ol=1092.0, atr=None, n=None, tick=None):
    sr, sp = exitp.parse_stop(stop)
    tr, tp = exitp.parse_target(target)
    return exitp.compute(entry_px, sr, sp, tr, tp, oh, ol, atr, n, tick)


class FakeYahoo(object):
    """Stands in for exit.py's _open_url: SBIN's daily chart from the fixture, nothing else."""

    def __init__(self):
        self.urls = []

    def __call__(self, url, timeout=20):
        self.urls.append(url)
        if "/chart/SBIN.NS?" in url and "interval=1d" in url:
            return SBIN_DAILY_TEXT
        raise urllib.error.URLError("no network in tests")


class Math(unittest.TestCase):
    def test_the_reel_or_mid_1r(self):
        p = c(1105.70, "or-mid", "1R")
        self.assertEqual(p["tick"], DEC("0.10"))
        self.assertEqual(p["stop"], DEC("1096.70"))
        self.assertEqual(p["risk"], DEC("9.00"))
        self.assertEqual(p["target"], DEC("1114.70"))
        self.assertEqual(p["reward_risk"], DEC("1.00"))
        self.assertEqual(p["target_label"], "1R (one times the risk)")
        self.assertEqual(p["stop_detail"], "(₹1,101.40 + ₹1,092.00) / 2 = ₹1,096.70")

    def test_the_default_2r(self):
        p = c(1105.70, "or-mid")
        self.assertEqual(p["target"], DEC("1123.70"))
        self.assertEqual(p["target_label"], "2R (two times the risk)")

    def test_raw_yahoo_floats_give_the_same_prices(self):
        p = c(1105.699951171875, "or-mid", "1R", 1101.4000244140625, 1092.0)
        self.assertEqual((p["entry"], p["stop"], p["target"]), (DEC("1105.70"), DEC("1096.70"), DEC("1114.70")))
        self.assertEqual(p["entry_detail"], "")

    def test_a_midpoint_off_the_tick_rounds_down(self):
        # RELIANCE, 2026-11-17: range 1,337.70-1,343.60, planned one tick over the high
        p = c(1343.70, "or-mid", "2R", 1343.60, 1337.70)
        self.assertEqual(p["stop"], DEC("1340.60"))
        self.assertEqual(p["stop_detail"], "(₹1,343.60 + ₹1,337.70) / 2 = ₹1,340.65, rounded down to the ₹0.10 tick")
        self.assertEqual((p["risk"], p["target"]), (DEC("3.10"), DEC("1349.90")))

    def test_other_stops(self):
        p = c(1105.70, "or-low")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (DEC("1092.00"), DEC("13.70"), DEC("1133.10")))
        p = c(1105.70, "price:1096.73")
        self.assertEqual(p["stop"], DEC("1096.70"))                 # rounds away from the entry, to the tick
        self.assertEqual(p["stop_detail"], "₹1,096.73 rounded down to the ₹0.10 tick")
        p = c(1105.70, "price:₹1,096.70")
        self.assertEqual((p["stop"], p["stop_detail"]), (DEC("1096.70"), ""))
        p = c(1105.70, "pct:1")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (DEC("1094.60"), DEC("11.10"), DEC("1127.90")))
        self.assertIn("₹1,105.70 x (1 - 1%) = ₹1,094.643, rounded down", p["stop_detail"])
        p = c(1105.70, "atr:1.5", atr=4.791687, n=6)
        self.assertEqual(p["stop"], DEC("1098.50"))                 # 1105.70 - 7.1875305 = 1098.5124695
        self.assertEqual(p["stop_label"], "1.5 x ATR(6) under the entry")

    def test_tick_bands(self):
        for price, tick in ((249.99, "0.01"), (250, "0.05"), (999.95, "0.05"), (1000, "0.10"), (4999.9, "0.10"),
                            (5000, "0.50"), (9999.5, "0.50"), (10000, "1.00"), (19999, "1.00"), (20000, "5.00"),
                            (64500, "5.00")):
            self.assertEqual(exitp.tick_for(price), DEC(tick), price)

    def test_rounding_at_the_band_edges(self):
        # (entry, stop, target) -> (tick, entry, stop, target)
        cases = [
            ((249.99, "pct:1", "2R"), ("0.01", "249.99", "247.49", "254.99")),
            ((250.00, "pct:1", "2R"), ("0.05", "250.00", "247.50", "255.00")),
            ((249.99, "price:247.53", "2R"), ("0.01", "249.99", "247.53", "254.91")),
            ((250.00, "price:247.53", "2R"), ("0.05", "250.00", "247.50", "255.00")),
            ((999.95, "price:990.03", "2R"), ("0.05", "999.95", "990.00", "1019.85")),
            ((1000.00, "price:990.03", "2R"), ("0.10", "1000.00", "990.00", "1020.00")),
            ((999.95, "price:990.00", "1.5R"), ("0.05", "999.95", "990.00", "1014.90")),   # 1014.875 half up
            ((4999.90, "price:4950.07", "2R"), ("0.10", "4999.90", "4950.00", "5099.70")),
            ((5000.00, "price:4950.07", "2R"), ("0.50", "5000.00", "4950.00", "5100.00")),
            ((5000.00, "price:4986.6", "1.5R"), ("0.50", "5000.00", "4986.50", "5020.50")),  # 5020.25 half up
            ((10799.00, "pct:1", "2R"), ("1.00", "10799.00", "10691.00", "11015.00")),
            ((20000.00, "pct:0.37", "1R"), ("5.00", "20000.00", "19925.00", "20075.00")),
        ]
        for (e, stop, target), want in cases:
            p = c(e, stop, target, None, None)
            got = (p["tick"], p["entry"], p["stop"], p["target"])
            self.assertEqual(got, tuple(DEC(x) for x in want), (e, stop, target))
            for k in ("entry", "stop", "target"):
                self.assertEqual(p[k] % p["tick"], 0, (e, k))

    def test_an_entry_off_the_grid_rounds_half_up_and_says_so(self):
        p = c(250.02, "pct:1", "2R", None, None)
        self.assertEqual((p["entry"], p["tick"]), (DEC("250.00"), DEC("0.05")))
        self.assertEqual(p["entry_detail"], "the entry ₹250.02 is off the ₹0.05 tick: planned at ₹250.00")
        self.assertEqual(c(250.03, "pct:1", "2R", None, None)["entry"], DEC("250.05"))
        p = c(249.996, "pct:1", "2R", None, None)                  # rounds onto the 250 edge: the 0.05 band
        self.assertEqual((p["entry"], p["tick"]), (DEC("250.00"), DEC("0.05")))
        p = c(999.98, "pct:1", "2R", None, None)
        self.assertEqual((p["entry"], p["tick"]), (DEC("1000.00"), DEC("0.10")))
        self.assertEqual(c(1105.73, "or-mid")["entry"], DEC("1105.70"))

    def test_target_rounds_half_up_and_price_targets(self):
        p = c(100, "price:99.01", "1.5R", None, None)
        self.assertEqual(p["target"], DEC("101.49"))               # 100 + 1.485
        self.assertEqual(p["target_label"], "1.5R (1.5 times the risk)")
        p = c(1105.70, "or-mid", "price:1120")
        self.assertEqual(p["reward_risk"], DEC("1.59"))
        self.assertEqual(p["target_label"], "your price (1.59R)")
        self.assertEqual(p["target_r"], DEC("1.59"))
        self.assertEqual(c(1105.70, "or-mid", "price:1120.04")["target"], DEC("1120.00"))

    def test_refusals_say_why(self):
        cases = [("price:1105.70", "2R", "at or above the entry"),
                 ("price:1105.75", "2R", "at or above the entry"),     # rounds down onto the entry
                 ("price:1106", "2R", "at or above the entry"),
                 ("or-mid", "price:1105.70", "at or below the entry"),
                 ("or-mid", "price:1105.74", "at or below the entry"),  # rounds half up onto the entry
                 ("price:1105.20", "2R", "too tight to survive the spread"),
                 ("pct:0.04", "2R", "too tight to survive the spread")]
        for stop, target, why in cases:
            with self.assertRaises(exitp.PlanError) as cm:
                c(1105.70, stop, target)
            self.assertIn(why, str(cm.exception), stop)
        # 0.05% of 1,105.70 is 0.55: a ₹0.60 risk is allowed, ₹0.50 is not
        self.assertEqual(c(1105.70, "price:1105.10")["risk"], DEC("0.60"))
        with self.assertRaises(exitp.PlanError) as cm:
            c(1105.70, "price:1105.20")
        self.assertIn("(₹0.55 on ₹1,105.70)", str(cm.exception))
        with self.assertRaises(exitp.PlanError):
            c(1096.00, "or-mid")                                    # the midpoint is above this entry
        for bad in ("mid-ish", "pct:100", "price:0", "atr:-1", "price:$5"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_stop(bad)
        for bad in ("0R", "R", "lots", "price:x"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_target(bad)
        with self.assertRaises(exitp.PlanError):
            c(1105.70, "or-mid", oh=None, ol=None)
        with self.assertRaises(exitp.PlanError):
            c(1105.70, "or-mid", oh=1092.0, ol=1101.4)
        with self.assertRaises(exitp.PlanError):
            c(0, "pct:1")

    def test_inr(self):
        self.assertEqual(exitp.inr(123456.5), "₹1,23,456.50")
        self.assertEqual(exitp.inr(-1250), "-₹1,250.00")
        self.assertEqual(exitp.inr(DEC("-0.004")), "₹0.00")
        self.assertEqual(exitp.inr(10799), "₹10,799.00")
        self.assertEqual(exitp.inr(12345678.9), "₹1,23,45,678.90")
        self.assertEqual(exitp.inr_exact(DEC("1094.643")), "₹1,094.643")
        self.assertEqual(exitp.inr_exact(DEC("100000")), "₹1,00,000.00")
        self.assertEqual(exitp.inr_exact(DEC("1340.65")), "₹1,340.65")

    def test_atr_on_the_session_bars(self):
        ch = exitp.load_chart("SBIN", "5m", None, SBIN)
        day = datetime.date(2026, 11, 17)
        bars = exitp.session_bars(ch, day, "5m", upto_slot=5)
        self.assertEqual(len(bars), 6)                              # 9:15 ... the 9:40 entry bar
        # by hand: the true range of each bar, the first one without the overnight gap
        trs, prev = [], None
        for b in bars:
            r = b["h"] - b["l"]
            if prev is not None:
                r = max(r, abs(b["h"] - prev), abs(b["l"] - prev))
            trs.append(r)
            prev = b["c"]
        self.assertAlmostEqual(trs[0], 1101.40 - 1092.00, places=4)
        atr, n = exitp.atr_of(bars)
        self.assertEqual(n, 6)
        self.assertAlmostEqual(atr, sum(trs) / 6, places=9)
        self.assertAlmostEqual(atr, 4.7917, places=4)
        # the file's in-progress 9:45 bar and its 09:48:32 tick never count
        self.assertEqual(len(exitp.session_bars(ch, day, "5m")), 6)
        # a full session uses the last 14 bars; the 15:15 and 15:20 nulls are dropped
        full = exitp.session_bars(exitp.load_chart("SBIN", "5m", None, SBIN_1116), datetime.date(2026, 11, 16), "5m")
        self.assertEqual(len(full), 73)
        self.assertEqual(exitp.atr_of(full)[1], 14)

    def test_chart_symbols_and_files(self):
        with self.assertRaises(exitp.PlanError) as cm:
            exitp.load_chart("RELIANCE", "5m", None, SBIN)
        self.assertIn("holds SBIN.NS, not RELIANCE.NS", str(cm.exception))
        with self.assertRaises(exitp.PlanError) as cm:
            exitp.load_chart("SBIN", "1m", None, SBIN)
        self.assertIn("5m bars, not 1m", str(cm.exception))
        mm = exitp.load_chart("m&m.ns", "5m", None, os.path.join(FIX, "nse", "M&M-1d-5m.json"))
        self.assertEqual(len(mm["bars"]), 8)
        for bad in ("^NSEI", "BRK.B"):
            with self.assertRaises(exitp.PlanError):
                exitp.nse_symbol(bad)


class Tick(unittest.TestCase):
    """NSE reviews each stock's tick monthly, from its close on the previous month's last trading day."""

    def setUp(self):
        self._open = exitp._open_url
        self.fake = exitp._open_url = FakeYahoo()

    def tearDown(self):
        exitp._open_url = self._open

    def test_month_end_close(self):
        for day, close, date, tick in ((datetime.date(2026, 11, 17), "961.85", datetime.date(2026, 10, 30), "0.05"),
                                       (datetime.date(2026, 11, 2), "961.85", datetime.date(2026, 10, 30), "0.05"),
                                       (datetime.date(2026, 10, 5), "959.50", datetime.date(2026, 9, 30), "0.05"),
                                       (datetime.date(2026, 9, 10), "1060.00", datetime.date(2026, 8, 31), "0.10")):
            got = exitp.month_end_close(SBIN_DAILY_TEXT, "SBIN", day)
            self.assertEqual(got, (DEC(close), date), day)
            self.assertEqual(exitp.tick_for(got[0]), DEC(tick), day)
        with self.assertRaises(exitp.PlanError) as cm:
            exitp.month_end_close(SBIN_DAILY_TEXT, "SBIN", datetime.date(2026, 8, 20))
        self.assertIn("no close in July 2026", str(cm.exception))
        with self.assertRaises(exitp.PlanError) as cm:
            exitp.month_end_close(SBIN_DAILY_TEXT, "RELIANCE", datetime.date(2026, 11, 17))
        self.assertIn("holds SBIN.NS, not RELIANCE.NS", str(cm.exception))

    def test_float32_prices_snap_to_the_paisa(self):
        d = json.loads(SBIN_DAILY_TEXT)
        r = d["chart"]["result"][0]
        oct30 = int((datetime.datetime(2026, 10, 30, 3, 45) - datetime.datetime(1970, 1, 1)).total_seconds())
        r["indicators"]["quote"][0]["close"][r["timestamp"].index(oct30)] = 1214.5999755859375   # the 2026-10-30 close
        self.assertEqual(exitp.month_end_close(json.dumps(d), "SBIN", datetime.date(2026, 11, 17))[0], DEC("1214.60"))
        # a float32 range low floors onto itself, not one tick lower
        p = c(1220.00, "or-low", "2R", 1225.0, 1214.5999755859375, tick=DEC("0.05"))
        self.assertEqual(p["stop"], DEC("1214.60"))
        p = c(1105.699951171875, "or-mid", "1R", 1101.4000244140625, 1092.0, tick=DEC("0.05"))
        self.assertEqual((p["entry"], p["stop"], p["target"]), (DEC("1105.70"), DEC("1096.70"), DEC("1114.70")))
        self.assertEqual(exitp.snap(1214.5999755859375), 1214.6)

    def test_the_monthly_tick_drives_the_rounding(self):
        # SBIN at 1,105 sits in the 0.10 band, but its October-end close (961.85) set 0.05 for November
        p = c(1105.70, "pct:1", tick=DEC("0.05"))
        self.assertEqual((p["tick"], p["stop"]), (DEC("0.05"), DEC("1094.60")))     # 1094.643 floors to 1094.60
        p = c(1105.70, "price:1096.73", tick=DEC("0.05"))
        self.assertEqual(p["stop"], DEC("1096.70"))
        p = c(1105.70, "price:1096.78", tick=DEC("0.05"))
        self.assertEqual(p["stop"], DEC("1096.75"))                 # on the 0.05 grid; the band would say 1096.70
        self.assertEqual(c(1105.70, "price:1096.78")["stop"], DEC("1096.70"))
        p = c(1105.73, "or-mid", tick=DEC("0.05"))
        self.assertEqual(p["entry"], DEC("1105.75"))
        # CONCORDBIO-like: a ~575 stock on 0.10 because its last month-end close was over 1,000
        p = c(575.05, "pct:1", "2R", None, None, tick=DEC("0.10"))
        self.assertEqual((p["entry"], p["stop"], p["target"]), (DEC("575.10"), DEC("569.30"), DEC("586.70")))

    def test_resolve_tick(self):
        day = datetime.date(2026, 11, 17)
        self.assertEqual(exitp.resolve_tick("SBIN", day), (DEC("0.05"), MONTHLY, ""))
        self.assertIn("/v8/finance/chart/SBIN.NS?range=", self.fake.urls[-1])
        self.assertTrue(self.fake.urls[-1].endswith("&interval=1d"))
        tick, basis, why = exitp.resolve_tick("M&M", day)            # the fake has no M&M chart: fall back
        self.assertIsNone(tick)
        self.assertEqual(basis, {"rule": "entry-price band (fallback)"})
        self.assertIn("could not connect", why)
        self.assertIn("/chart/M%26M.NS?", self.fake.urls[-1])
        n = len(self.fake.urls)
        self.assertEqual(exitp.resolve_tick("SBIN", day, offline="x.json")[1], {"rule": "entry-price band (fallback)"})
        self.assertEqual(exitp.resolve_tick("SBIN", day, flag="0.10"), (DEC("0.10"), {"rule": "flag"}, ""))
        self.assertEqual(len(self.fake.urls), n)                     # neither --offline nor --tick reads Yahoo
        for bad in ("0.07", "0", "x"):
            with self.assertRaises(exitp.PlanError):
                exitp.resolve_tick("SBIN", day, flag=bad)
        self.assertEqual(exitp.daily_range(day, day), "3mo")
        self.assertEqual(exitp.daily_range(datetime.date(2026, 6, 1), day), "1y")


class Calendar(unittest.TestCase):
    def test_ist(self):
        self.assertEqual(exitp.parse_now("2026-11-17 09:46"), datetime.datetime(2026, 11, 17, 4, 16))
        self.assertEqual(exitp.parse_now("2026-11-17 09:46 IST"), datetime.datetime(2026, 11, 17, 4, 16))
        self.assertEqual(exitp.to_ist(datetime.datetime(2026, 11, 17, 4, 16)), datetime.datetime(2026, 11, 17, 9, 46))
        self.assertEqual(exitp.trading_day(datetime.date(2026, 11, 24)),
                         (False, "NSE holiday: Prakash Gurpurb Sri Guru Nanak Dev"))
        self.assertEqual(exitp.trading_day(datetime.date(2027, 1, 26)), (True, ""))
        self.assertEqual(exitp.calendar_note(datetime.date(2027, 1, 26)),
                         "NSE holiday list for 2027 not loaded: check nseindia.com")
        self.assertEqual(exitp.NSE_HOLIDAYS, entry.NSE_HOLIDAYS)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("trader")
        self._open = exitp._open_url
        self.fake = exitp._open_url = FakeYahoo()

    def tearDown(self):
        exitp._open_url = self._open
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def write(self, path, doc):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    def plan(self, *args):
        return run(exitp, ["plan", "--now", "2026-11-17 09:46"] + list(args))

    def read_plan(self):
        with open(os.path.join("trader", "plan.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_entry_to_plan_end_to_end(self):
        code, _, _ = run(entry, ["check", "SBIN", "--offline", SBIN, "--now", "2026-11-17 09:45", "--write"])
        self.assertEqual(code, 0)
        code, text, err = self.plan("--stop", "or-mid", "--target", "1R", "--write")
        self.assertEqual(code, 0, err)
        p = self.read_plan()
        for k, v in REEL_PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertEqual(sorted(p), PLAN_KEYS)                     # the schema the other tools read
        self.assertEqual((p["tick"], p["tick_basis"]), (0.05, MONTHLY))
        self.assertIn("15:20", p["note"])
        self.assertIn("Exit plan: SBIN long, intraday (MIS), Tue 2026-11-17", text)
        self.assertIn("Stop          ₹1,096.70  opening-range midpoint", text)
        self.assertIn("Target        ₹1,114.70  1R (one times the risk)", text)
        self.assertIn("Risk a share  ₹9.00 (0.81% of the entry)", text)
        self.assertIn("Reward:risk   1.0", text)
        self.assertIn("Tick          ₹0.05, from the ₹961.85 close on 30 Oct (NSE reviews ticks monthly)", text)
        self.assertIn("Flat by       15:00 IST", text)
        self.assertIn("not a guarantee", text)
        self.assertIn("Groww stops new intraday orders in F&O stocks at 15:05 and squares off intraday (MIS) "
                      "positions still open at 15:20, for ₹50 + GST per position", text)
        self.assertIn("/nse-timemachine", text)
        self.assertNotIn("Note:", text)
        self.assertNotIn("pro" + "fit", text.lower())   # never on screen
        code, text, _ = self.plan("--write")                       # the defaults: or-mid, 2R
        self.assertEqual(self.read_plan()["target"], 1123.7)
        self.assertEqual(self.read_plan()["target_r"], 2.0)
        code, text, _ = run(exitp, ["show", "--now", "2026-11-17 09:50"])
        self.assertEqual(code, 0)
        self.assertIn("₹1,123.70", text)
        self.assertIn("₹0.05, from the ₹961.85 close on 30 Oct", text)
        self.assertNotIn("not today", text)
        code, text, _ = run(exitp, ["show", "--now", "2026-11-18 09:50"])
        self.assertIn("not today", text)

    def test_manual_flags_and_json(self):
        code, text, _ = self.plan("--symbol", "sbin.ns", "--entry", "1105.70", "--or-high", "1101.40", "--or-low",
                                  "1092.00", "--time", "9:45", "--entry-bar", "5", "--date", "2026-11-17",
                                  "--target", "1R", "--json")
        self.assertEqual(code, 0)
        p = json.loads(text)
        for k, v in REEL_PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertEqual(p["tick_basis"], MONTHLY)
        self.assertEqual(p["warnings"], [])

    def test_m_and_m_and_bajaj_auto(self):
        code, text, err = self.plan("--symbol", "M&M", "--entry", "3392", "--or-high", "3389.10", "--or-low", "3355.30",
                                    "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["symbol"], p["stop"], p["risk_per_share"], p["target"]), ("M&M", 3372.2, 19.8, 3431.6))
        self.assertEqual((p["tick"], p["tick_basis"]), (0.1, {"rule": "entry-price band (fallback)"}))
        self.assertIn("Tick ₹0.10 from the entry price band, a fallback", p["warnings"][0])
        code, text, _ = self.plan("--symbol", "BAJAJ-AUTO", "--entry", "10799", "--or-high", "10795", "--or-low",
                                  "10709")
        self.assertEqual(code, 0)
        self.assertIn("Stop          ₹10,752.00", text)
        self.assertIn("Target        ₹10,893.00", text)
        self.assertIn("Tick          ₹1.00, from the entry price band (fallback", text)
        code, text, _ = self.plan("--symbol", "SBIN", "--entry", "1105.73", "--or-high", "1101.40", "--or-low",
                                  "1092.00")
        self.assertIn("Note: The entry ₹1,105.73 is off the ₹0.05 tick: planned at ₹1,105.75.", text)
        code, _, err = self.plan("--symbol", "^NSEI", "--entry", "25000", "--stop", "pct:1")
        self.assertIn("index", err)

    def test_atr_stop_reads_bars(self):
        self.write(os.path.join("trader", "entry.json"), REEL_ENTRY)
        code, text, err = self.plan("--stop", "atr:1.5", "--offline", SBIN, "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["stop"], p["risk_per_share"], p["target"]), (1098.5, 7.2, 1120.1))
        self.assertEqual((p["stop_rule"], p["atr_bars"]), ("atr", 6))
        self.assertAlmostEqual(p["atr"], 4.7917, places=4)
        code, _, err = self.plan("--stop", "atr:1.5", "--offline", SBIN, "--entry-bar", "1")
        self.assertEqual(code, 1)
        self.assertIn("at least 3 closed", err)

    def test_refusals_from_the_cli(self):
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, status="waiting", time=None, price=None,
                                                              bar_index=None, bar_start=None))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("SBIN is waiting, not triggered", err)
        code, text, _ = self.plan("--entry", "1101.50", "--json")    # plan ahead one tick over the high
        self.assertEqual(code, 0)
        p = json.loads(text)
        self.assertEqual((p["entry"], p["stop"], p["entry_time"]), (1101.5, 1096.7, None))

        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, setup="level", level=1101.0))
        code, _, err = self.plan()
        self.assertIn("level setup has no opening-range stop", err)
        self.assertEqual(self.plan("--stop", "pct:1")[0], 0)

        code, _, err = self.plan("--side", "sell")
        self.assertIn("longs only", err)
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, side="sell"))
        code, _, err = self.plan()
        self.assertIn("longs only", err)
        os.remove(os.path.join("trader", "entry.json"))
        code, _, err = self.plan()
        self.assertIn("no entry", err)
        self.assertIn("/nse-entry", err)
        code, _, err = self.plan("--symbol", "SBIN")
        self.assertIn("needs --entry", err)
        code, _, err = self.plan("--symbol", "SBIN", "--from", "x.json", "--entry", "1")
        self.assertIn("not both", err)
        code, _, err = self.plan("--symbol", "SBIN", "--entry", "1105.70", "--or-high", "1101.40")
        self.assertIn("both --or-high and --or-low", err)
        self.assertNotIn("Traceback", err)

    def test_flat_by_square_off_and_late_entries(self):
        self.write(os.path.join("trader", "entry.json"), REEL_ENTRY)
        self.write(os.path.join("trader", "config.json"), {"flat_by": "14:50", "min_reward_risk": 1.5})
        code, text, _ = self.plan("--target", "1R", "--json")
        p = json.loads(text)
        self.assertEqual(p["flat_by"], "14:50")
        self.assertIn("under your min_reward_risk 1.5", p["warnings"][0])
        self.assertIn("nse-risk", p["warnings"][0])
        os.remove(os.path.join("trader", "config.json"))

        code, text, _ = self.plan("--flat-by", "15:25")
        self.assertEqual(code, 0)
        self.assertIn("Note: Flat-by 15:25: at or after Groww's 15:20 auto square-off", text)
        self.assertIn("₹50 + GST", text)
        code, _, err = self.plan("--flat-by", "15:30")
        self.assertEqual(code, 1)
        self.assertIn("at or after the 3:30 PM NSE close", err)
        code, _, err = self.plan("--flat-by", "09:00")
        self.assertIn("at or before the 9:15 AM NSE open", err)
        self.write(os.path.join("trader", "config.json"), {"flat_by": "16:00"})
        code, _, err = self.plan()
        self.assertIn("flat_by in the config 16:00 is at or after", err)
        os.remove(os.path.join("trader", "config.json"))
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, time="3:00 PM"))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("the entry at 3:00 PM is at or after your flat-by 15:00: there is no time left", err)
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, time="2:55 PM"))
        self.assertEqual(self.plan()[0], 0)

    def test_calendar_warnings(self):
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, date="2026-11-24"))
        code, text, _ = self.plan("--json")
        self.assertEqual(code, 0)
        w = json.loads(text)["warnings"]
        self.assertIn("2026-11-24 is not an NSE trading day (NSE holiday: Prakash Gurpurb Sri Guru Nanak Dev).", w)
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, date="2027-01-26"))
        code, text, _ = run(exitp, ["plan", "--now", "2027-01-26 09:46", "--tick", "0.05", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["warnings"], ["NSE holiday list for 2027 not loaded: check nseindia.com."])
        code, text, _ = run(exitp, ["plan", "--now", "2027-01-26 09:46", "--json"])     # no December close in the fixture
        self.assertIn("no close in December 2026", json.loads(text)["warnings"][0])
        self.write(os.path.join("trader", "entry.json"), REEL_ENTRY)
        self.write(os.path.join("trader", "config.json"), {"holidays": ["2026-11-17"]})
        code, text, _ = self.plan("--json")
        self.assertIn("2026-11-17 is not an NSE trading day (a holiday in your config).", json.loads(text)["warnings"])

    def test_tick_override_and_offline_fallback(self):
        self.write(os.path.join("trader", "entry.json"), REEL_ENTRY)
        code, text, err = self.plan("--tick", "0.10", "--stop", "price:1096.78", "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["tick"], p["tick_basis"], p["stop"]), (0.1, {"rule": "flag"}, 1096.7))
        self.assertEqual(p["warnings"], [])
        code, text, _ = self.plan("--tick", "0.10")
        self.assertIn("Tick          ₹0.10, from --tick", text)
        code, _, err = self.plan("--tick", "0.07")
        self.assertIn("--tick is one of NSE's ticks", err)
        n = len(self.fake.urls)
        code, text, _ = self.plan("--offline", SBIN, "--json")
        p = json.loads(text)
        self.assertEqual(len(self.fake.urls), n)                    # --offline never reads Yahoo
        self.assertEqual((p["tick"], p["tick_basis"]), (0.1, {"rule": "entry-price band (fallback)"}))
        self.assertIn("(--offline reads no daily chart)", p["warnings"][0])
        code, text, _ = self.plan("--offline", SBIN)
        self.assertIn("Tick          ₹0.10, from the entry price band (fallback", text)
        self.assertIn("Note: Tick ₹0.10 from the entry price band, a fallback", text)
        code, text, _ = self.plan("--stop", "price:1096.78", "--json")     # the monthly tick: 0.05
        self.assertEqual(json.loads(text)["stop"], 1096.75)

    def test_show_without_a_plan(self):
        code, text, _ = run(exitp, ["show"])
        self.assertEqual(code, 1)
        self.assertIn("No plan", text)


def section(text, title):
    start = text.index(title)
    nxt = text.find("\n## ", start + 1)
    return text[start:] if nxt < 0 else text[start:nxt]


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "nse-exit", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "nse-entry", "SKILL.md"), encoding="utf-8") as fh:
            other = fh.read()
        self.assertTrue(text.startswith("---\nname: nse-exit\n"))
        self.assertGreater(len(text.splitlines()), 60)
        self.assertEqual(section(text, "## Safety"), section(other, "## Safety"))
        low = " ".join(text.lower().split())
        for must in ("before the entry", "a stop is a plan, not a guarantee", "not financial advice", "71%", "80%",
                     "sebi", "/nse-timemachine", "/nse-budget", "longs only", "₹1,096.70", "₹1,114.70", "₹9.00",
                     "₹1,123.70", "too tight to survive the spread", "15:00", "15:05", "15:20", "₹50 + gst",
                     "tick", "monthly", "--tick", "tick_basis", "last trading day of the previous month", "ist",
                     "groww", "intraday"):
            self.assertIn(must, low)
        for never in (chr(0x2014), "tick " + "by tick", "real " + "time", "pro" + "fit", "nyse", " et ",
                      "new york", "trader-"):
            self.assertNotIn(never, low)
        self.assertNotRegex(text, r"\$(?!\{CLAUDE_SKILL_DIR\})")         # rupees only; the skill-dir variable stays
        self.assertNotRegex(low, r"\bcents?\b")


if __name__ == "__main__":
    unittest.main()
