import datetime
import gzip
import hashlib
import io
import json
import os
import random
import shutil
import ssl
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

tm = load("nse-timemachine", "timemachine")

NSE = os.path.join(FIX, "nse")
RELIANCE = os.path.join(NSE, "RELIANCE-60d-5m.json.gz")   # Yahoo v8 chart, range=60d interval=5m, 2026-11-17 09:48 IST
SBIN = os.path.join(NSE, "SBIN-60d-5m.json.gz")
with gzip.open(RELIANCE, "rt", encoding="utf-8") as _fh:
    TEXT = _fh.read()
with gzip.open(SBIN, "rt", encoding="utf-8") as _fh:
    SBIN_TEXT = _fh.read()
NOW = "2026-11-17 09:50"
NOW_UTC = tm.parse_now(NOW)
# the SKILL.md example: a live run on 2026-11-17 at 11:04 IST, the chart saved for repeat runs
LIVE = os.path.join(FIX, "nse-timemachine", "SBIN-60d-5m-2026-11-17.json.gz")
LIVE_PLAN = os.path.join(FIX, "nse-timemachine", "plan-sbin-2026-11-17.json")

# A long on Monday 2026-11-16 right after the 15-minute opening range (09:15-09:30: high 1347.10, low 1340.30):
# in at the 09:30 bar's close (9:35 AM, bar 3), stop at the range low, 1R target, flat by 15:00 (the pack default).
PLAN = {"created_by": "nse-exit", "symbol": "RELIANCE", "side": "buy", "date": "2026-11-16", "setup": "orb",
        "entry": 1343.8, "entry_time": "9:35 AM", "entry_bar": 3, "interval": "5m",
        "opening_range": {"high": 1347.1, "low": 1340.3, "minutes": 15},
        "stop": 1340.3, "stop_rule": "or_low", "stop_label": "opening-range low",
        "target": 1347.3, "target_r": 1.0, "target_label": "1R (one times the risk)",
        "risk_per_share": 3.5, "reward_risk": 1.0, "flat_by": "15:00"}
# SBIN the same morning (range 1099.50 / 1083.90): in at 1095.00, stop at the midpoint 1091.70, 1R target
SBIN_PLAN = dict(PLAN, symbol="SBIN", entry=1095.0, opening_range={"high": 1099.5, "low": 1083.9, "minutes": 15},
                 stop=1091.7, stop_rule="or_mid", stop_label="opening-range midpoint", target=1098.3,
                 risk_per_share=3.3)

# fingerprints of the seed-19 run (regression: same data, same seed, same numbers on any machine)
BANDS = "901dfd4519a6d10f"
PATHS = "a1aa0d48ebbada6b"
TRADES = "65a35bfcccf5d307"
HIST = {"lo": 1313.14, "hi": 1373.63,
        "bins": [93, 99, 142, 209, 262, 388, 456, 537, 634, 723, 810, 824, 777, 776, 649, 606, 470, 397, 304, 207,
                 182, 139, 88, 61]}


def sha(x):
    return hashlib.sha256(json.dumps(x).encode("utf-8")).hexdigest()[:16]


def plan(**kw):
    raw = dict(PLAN)
    raw.update(kw)
    return tm.normalize_plan(raw)


def machine(p=None, text=TEXT, now=NOW_UTC, **kw):
    return tm.time_machine(text, p or plan(), now, **kw)


def edited(fn, text=TEXT):
    """The fixture's chart JSON, changed by fn(result), back as text."""
    doc = json.loads(text)
    fn(doc["chart"]["result"][0])
    return json.dumps(doc)


def keep_bars(result, keep):
    """Keep only the bars where keep(ts) is true."""
    q = result["indicators"]["quote"][0]
    idx = [i for i, ts in enumerate(result["timestamp"]) if keep(ts)]
    result["timestamp"] = [result["timestamp"][i] for i in idx]
    for k in q:
        q[k] = [q[k][i] for i in idx]


def dict_plan_without_tick(p):
    raw = dict(PLAN)
    raw.update({k: v for k, v in p.items() if k not in ("tick", "tick_basis", "from") and v is not None})
    raw["stop_rule"] = None
    return tm.normalize_plan(raw)


def ist(ts):
    return tm.to_ist(tm.utc_of(ts))


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tm.main(argv)
    return code, out.getvalue(), err.getvalue()


class Reel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = machine()

    def test_the_odds(self):
        m = self.doc["mc"]
        self.assertEqual((m["replays"], m["seed"], m["window"], m["until"], m["until_from"]),
                         (10000, 19, 2, "15:00", "flat_by"))
        self.assertEqual(m["pool_sessions"], 57)
        self.assertEqual(m["bars_left"], 65)
        self.assertEqual((m["pool_from"], m["pool_to"]), ("2026-08-20", "2026-11-13"))
        self.assertEqual(m["p_target"], 0.5275)
        self.assertEqual(m["p_stop"], 0.4725)
        self.assertEqual(m["p_neither"], 0.0)
        self.assertEqual(m["p_green"], 0.5275)
        self.assertEqual(m["median_close"], 1342.63)
        self.assertEqual(m["p5_close"], 1322.0)
        self.assertEqual(m["p95_close"], 1364.5)
        self.assertEqual(m["avg_R"], 0.055)
        self.assertEqual(m["r_at_target"], 1.0)

    def test_bit_for_bit_with_a_fixed_seed(self):
        m = self.doc["mc"]
        self.assertEqual(sha(m["bands"]), BANDS)
        self.assertEqual(len(m["sample_paths"]), 160)
        self.assertEqual(sha(m["sample_paths"]), PATHS)
        self.assertEqual(m["hist"], HIST)
        self.assertEqual(m["bands"][-1], [1321.71, 1333.8, 1342.38, 1350.52, 1364.03])

    def test_the_history_replay(self):
        h = self.doc["history"]
        self.assertEqual((h["sessions"], h["triggered"]), (57, 32))
        self.assertEqual((h["target"], h["stop"], h["close"], h["green"]), (9, 10, 13, 16))
        self.assertEqual(h["avg_R"], -0.066)
        self.assertEqual(h["rule"], {"setup": "orb", "or_minutes": 15, "stop_rule": "or_low", "target_r": 1.0,
                                     "tick": 0.1, "tick_from": "band"})
        slim = [{"date": t["date"], "entry": t["entry"], "outcome": t["outcome"], "R": t["R"]} for t in h["trades"]]
        self.assertEqual(sha(slim), TRADES)
        self.assertEqual(h["trades"][0], {"date": "2026-08-20", "time": "12:55 PM", "entry": 1315.0, "stop": 1307.5,
                                          "target": 1322.5, "outcome": "close", "R": -0.16})
        for t in h["trades"]:   # on the NSE tick grid: 0.10 between ₹1,000 and ₹5,000
            for k in ("entry", "stop", "target"):
                self.assertAlmostEqual(t[k] * 10, round(t[k] * 10), places=6, msg=t)

    def test_the_verdict_says_coin_flip(self):
        self.assertEqual(self.doc["verdict"], "Close to a coin flip: target first 52.8 % vs stop first 47.2 %.")
        gap, share, even = tm.lean(self.doc["mc"])
        self.assertEqual((round(share, 3), even), (0.527, 0.5))
        self.assertEqual(self.doc["verdict_notes"], [])

    def test_bands_start_at_the_entry(self):
        m = self.doc["mc"]
        self.assertEqual(m["bands"][0], [1343.8] * 5)
        self.assertEqual(len(m["bands"]), 66)
        self.assertEqual((m["band_times"][0], m["band_times"][-1], len(m["band_times"])), ("09:35", "15:00", 66))
        self.assertTrue(all(p[0] == 1343.8 and len(p) == 66 for p in m["sample_paths"]))

    def test_the_plan_is_carried_through(self):
        p = self.doc["plan"]
        self.assertEqual((p["symbol"], p["entry"], p["stop"], p["target"], p["entry_bar"], p["entry_time"]),
                         ("RELIANCE", 1343.8, 1340.3, 1347.3, 3, "9:35 AM"))
        self.assertEqual((p["risk_per_share"], p["target_r"], p["flat_by"]), (3.5, 1.0, "15:00"))
        self.assertEqual(self.doc["warnings"], [])
        self.assertEqual(self.doc["data"]["symbol"], "RELIANCE.NS")
        self.assertEqual(self.doc["data"]["last_bar"], "2026-11-17 09:40 IST")
        self.assertEqual(self.doc["created_by"], "nse-timemachine")

    def test_the_text_report(self):
        text = "\n".join(tm.report(self.doc))
        for line in ("RELIANCE buy, Monday 2026-11-16: entry ₹1,343.80 at 9:35 AM (bar 3), stop ₹1,340.30, "
                     "target ₹1,347.30 (1R, risk ₹3.50 a share).",
                     "10,000 replays of the rest of the session, 9:35 AM to 3:00 PM IST (the plan's flat-by; "
                     "65 five-minute bars), seed 19.",
                     "57 complete sessions (2026-08-20 to 2026-11-13)",
                     "target first   52.8 %", "stop first     47.2 %", "neither        0.0 %    still open at 3:00 PM",
                     "green          52.8 %", "median ₹1,342.63, 90 % of replays between ₹1,322.00 and ₹1,364.50",
                     "Average R: +0.055 (R = the ₹3.50 risk", "1R target, prices on the ₹0.10 tick of the entry's "
                     "price band) on each of those 57 real sessions fired 32 times: 9 target, 10 stop, 13 neither. "
                     "Average R -0.066", "Close to a coin flip: target first 52.8 % vs stop first 47.2 %.",
                     "Left out 1 session(s) with gaps in Yahoo's bars before 3:00 PM: 2026-10-15.",
                     "not a prediction", "Charges (brokerage, STT, GST) are not in these numbers", "--seed 19"):
            self.assertIn(line, text)
        for word in ("profit", "P&L", "guarantee", "real time", "tick by tick", "$", "New York", " ET"):
            self.assertNotIn(word, text)


class Sbin(unittest.TestCase):
    def test_the_sbin_plan(self):
        doc = machine(tm.normalize_plan(SBIN_PLAN), text=SBIN_TEXT)
        m, h = doc["mc"], doc["history"]
        self.assertEqual((m["p_target"], m["p_stop"], m["p_neither"], m["p_green"]), (0.5178, 0.4822, 0.0, 0.5178))
        self.assertEqual((m["median_close"], m["p5_close"], m["p95_close"], m["avg_R"]), (1093.31, 1076.97, 1109.88, 0.036))
        self.assertEqual((sha(m["bands"]), sha(m["sample_paths"])), ("c2f780a12ae46147", "876d2cad207f9b38"))
        self.assertEqual(h["rule"]["stop_rule"], "or_mid")
        self.assertEqual((h["triggered"], h["target"], h["stop"], h["close"], h["avg_R"]), (32, 12, 15, 5, -0.046))
        self.assertEqual(h["trades"][0], {"date": "2026-08-21", "time": "9:45 AM", "entry": 1052.0, "stop": 1049.7,
                                          "target": 1054.3, "outcome": "target", "R": 1.0})
        self.assertEqual(doc["verdict"], "Close to a coin flip: target first 51.8 % vs stop first 48.2 %.")

    def test_auto_finds_the_midpoint_stop(self):
        doc = machine(tm.normalize_plan(dict(SBIN_PLAN, stop_rule=None)), text=SBIN_TEXT, replays=100)
        self.assertEqual(doc["history"]["rule"]["stop_rule"], "or_mid")


class LiveExample(unittest.TestCase):
    def test_the_skill_example(self):
        code, text, err = run(["run", "--plan", LIVE_PLAN, "--offline", LIVE, "--now", "2026-11-17 11:04"])
        self.assertEqual(code, 0, err)
        for line in ("SBIN buy, Tuesday 2026-11-17: entry ₹1,105.70 at 9:45 AM (bar 5), stop ₹1,096.70, "
                     "target ₹1,114.70 (1R, risk ₹9.00 a share).",
                     "10,000 replays of the rest of the session, 9:45 AM to 3:00 PM IST (the plan's flat-by; "
                     "63 five-minute bars), seed 19.",
                     "target first   26.4 %", "stop first     43.8 %", "neither        29.8 %   still open at 3:00 PM",
                     "green          40.6 %   target first, or above the entry at 3:00 PM",
                     "Price at 3:00 PM: median ₹1,103.40, 90 % of replays between ₹1,087.78 and ₹1,119.50.",
                     "Average R: -0.179 (R = the ₹9.00 risk: +1R at the target, -1R at the stop).",
                     "History: the same rule (15-min opening range, stop at the opening-range midpoint, 1R target, "
                     "prices on the ₹0.05 tick from the plan) on each of those 58 real sessions fired 32 times: "
                     "12 target, 15 stop, 5 neither. Average R -0.045.",
                     "Leans toward the stop: target first 26.4 % vs stop first 43.8 %."):
            self.assertIn(line, text)

    def test_nse_exits_2r_plan_for_the_same_trade(self):
        # the plan nse-exit writes for this trade with its 2R target: ₹1,123.70
        with open(LIVE_PLAN, encoding="utf-8") as fh:
            raw = json.load(fh)
        raw.update(target=1123.7, target_r=2.0, reward_risk=2.0, target_label="2R (two times the risk)")
        with gzip.open(LIVE, "rt", encoding="utf-8") as fh:
            text = fh.read()
        now = tm.parse_now("2026-11-17 11:04")
        doc = tm.time_machine(text, tm.normalize_plan(raw), now)
        m = doc["mc"]
        self.assertEqual((m["r_at_target"], m["p_target"], m["p_stop"], m["p_neither"], m["avg_R"]),
                         (2.0, 0.0477, 0.4475, 0.5048, -0.199))
        self.assertEqual(doc["verdict"], "Strongly toward the stop: at 2R the target has to come first in 33 % of the "
                                         "replays that hit one side; it did in 10 % (target first 4.8 % vs stop "
                                         "first 44.8 %).")
        self.assertEqual(doc["history"]["rule"], {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid",
                                                  "target_r": 2.0, "tick": 0.05, "tick_from": "plan"})
        self.assertEqual((doc["history"]["triggered"], doc["history"]["avg_R"]), (32, -0.171))
        auto = tm.time_machine(text, tm.normalize_plan(dict(raw, stop_rule=None)), now, replays=100)
        self.assertEqual(auto["history"]["rule"]["stop_rule"], "or_mid")

    def test_the_example_plan_is_a_real_breakout(self):
        with gzip.open(LIVE, "rt", encoding="utf-8") as fh:
            text = fh.read()
        bars = tm.parse_bars(tm.chart_result(text, "SBIN"), tm.parse_now("2026-11-17 11:04"))[1]
        day = tm.split_sessions(bars)["2026-11-17"]
        oh, ol = tm.opening_range(day, 3)
        self.assertEqual((round(oh, 2), round(ol, 2)), (1101.4, 1092.0))
        self.assertEqual(tm.paise(day[4]["c"]), 1101.4)                  # the 09:35 bar closes AT the high, not above
        first = next(i for i in range(3, 75) if tm.paise(day[i]["c"]) > oh)
        self.assertEqual((first, tm.tick_round(tm.paise(day[first]["c"]))), (5, 1105.7))
        self.assertEqual(tm.tick_away((oh + ol) / 2, True, 1105.7), 1096.7)
        with open(LIVE_PLAN, encoding="utf-8") as fh:
            p = json.load(fh)
        self.assertEqual((p["entry"], p["stop"], p["target"], p["entry_time"], p["flat_by"]),
                         (1105.7, 1096.7, 1114.7, "9:45 AM", "15:00"))


class TickStops(unittest.TestCase):
    """nse-exit sets a stop from the range in paise, onto the entry's tick grid (down for a long).
    Plans written that way must be read the same way, not with upstream's cents."""
    RANGE_1111 = {"high": 1340.9, "low": 1330.6, "minutes": 15}   # 2026-11-11, midpoint 1335.75: off the 0.10 grid

    def test_paise_absorbs_yahoos_float32_prices(self):
        self.assertEqual(tm.paise(1231.5999755859375), 1231.6)
        self.assertEqual(tm.paise(1101.4000244140625), 1101.4)
        self.assertEqual(tm.tick_away(1214.5999755859375, True, 1234.5), 1214.5)   # the raw float floors a tick low
        self.assertEqual(tm.tick_away(tm.paise(1214.5999755859375), True, 1234.5), 1214.6)
        self.assertEqual(tm.tick_away(1340.65, True, 1345.0), 1340.6)
        self.assertEqual(tm.opening_range(tm.split_sessions(tm.parse_bars(tm.chart_result(TEXT, "RELIANCE"),
                                                                          NOW_UTC)[1])["2026-11-02"], 3),
                         (1231.6, 1214.6))

    def test_a_tick_rounded_midpoint_stop_is_recognised(self):
        p = plan(date="2026-11-11", entry=1342.8, opening_range=self.RANGE_1111, stop=1335.7, stop_rule=None,
                 target=1349.9)
        doc = machine(p, replays=100)
        self.assertEqual(doc["history"]["rule"]["stop_rule"], "or_mid")
        self.assertEqual(doc["plan"]["stop_rule"], "or_mid")
        cents = plan(date="2026-11-11", entry=1342.8, opening_range=self.RANGE_1111, stop=1335.75, stop_rule=None,
                     target=1349.85)
        rule = machine(cents, replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["stop_pct"]), ("pct", 0.525))   # not a stop nse-exit would set

    def test_float32_range_ends_still_match(self):
        # 2026-11-02: Yahoo sends the range as 1231.5999755859375 / 1214.5999755859375
        base = dict(date="2026-11-02", entry=1234.5, opening_range={"high": 1231.6, "low": 1214.6, "minutes": 15},
                    stop_rule=None)
        self.assertEqual(machine(plan(stop=1214.6, target=1254.4, **base), replays=100)["history"]["rule"]["stop_rule"],
                         "or_low")
        self.assertEqual(machine(plan(stop=1223.1, target=1245.9, **base), replays=100)["history"]["rule"]["stop_rule"],
                         "or_mid")

    def test_the_plans_own_range_when_the_days_bars_are_missing(self):
        def no_open(res):
            keep_bars(res, lambda ts: ist(ts).strftime("%Y-%m-%d %H:%M") != "2026-11-11 09:15")
        text = edited(no_open)
        p = plan(date="2026-11-11", entry=1342.8, opening_range=self.RANGE_1111, stop=1335.7, stop_rule=None,
                 target=1349.9)
        self.assertEqual(machine(p, text=text, replays=100)["history"]["rule"]["stop_rule"], "or_mid")
        bare = plan(date="2026-11-11", entry=1342.8, opening_range={"minutes": 15}, stop=1335.7, stop_rule=None,
                    target=1349.9)
        self.assertEqual(machine(bare, text=text, replays=100)["history"]["rule"]["stop_rule"], "pct")

    def test_the_plans_tick_wins_over_the_price_band(self):
        # nse-exit writes the month's tick: 0.05 for a stock that closed last month under Rs 1,000, even at 1,342.80
        p = plan(date="2026-11-11", entry=1342.8, opening_range=self.RANGE_1111, stop=1335.75, stop_rule=None,
                 target=1349.85, tick=0.05, tick_basis={"reference_close": 961.85, "reference_date": "2026-10-30"})
        self.assertEqual((p["tick"], p["tick_basis"]["reference_close"]), (0.05, 961.85))
        self.assertEqual(tm.plan_tick(p), (0.05, "plan"))
        self.assertEqual(tm.plan_tick(plan()), (0.1, "band"))
        doc = machine(p, replays=100)
        rule = doc["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["tick"], rule["tick_from"]), ("or_mid", 0.05, "plan"))
        self.assertEqual(doc["plan"]["tick"], 0.05)
        for t in doc["history"]["trades"]:   # every past stop, entry and target on the 0.05 grid
            for k in ("entry", "stop", "target"):
                self.assertAlmostEqual(t[k] * 20, round(t[k] * 20), places=6, msg=t)
        self.assertTrue(any(round(t["stop"] * 10, 6) % 1 for t in doc["history"]["trades"]))   # some x.x5 stops
        self.assertIn("1R target, prices on the ₹0.05 tick from the plan) on each of those 54 real sessions fired "
                      "29 times: 12 target, 11 stop, 6 neither. Average R +0.011.", "\n".join(tm.report(doc)))
        # the same stop without the plan's tick is off the 0.10 band grid: not nse-exit's or_mid
        self.assertEqual(machine(dict_plan_without_tick(p), replays=100)["history"]["rule"]["stop_rule"], "pct")
        for bad in ("abc", 0, -0.05, 6):
            with self.assertRaises(tm.TMError, msg=bad):
                plan(tick=bad)

    def test_nse_exits_own_rule_is_used_as_written(self):
        p = plan(date="2026-11-11", entry=1342.8, opening_range=self.RANGE_1111, stop=1335.7, stop_rule="or_mid",
                 target=1349.9)
        self.assertEqual(machine(p, replays=100)["history"]["rule"]["stop_rule"], "or_mid")
        for other in ("price", "pct", "atr"):
            rule = machine(plan(date="2026-11-11", entry=1342.8, opening_range=self.RANGE_1111, stop=1335.7,
                                stop_rule=other, target=1349.9), replays=100)["history"]["rule"]
            self.assertEqual(rule["stop_rule"], "pct", other)


class Seeds(unittest.TestCase):
    def test_same_seed_same_numbers(self):
        a = machine(replays=2000, seed=5)
        b = machine(replays=2000, seed=5)
        self.assertEqual(a["mc"], b["mc"])
        self.assertEqual(a["history"], b["history"])
        c = machine(tm.normalize_plan(SBIN_PLAN), text=SBIN_TEXT, replays=2000, seed=5)["mc"]
        self.assertEqual(c, machine(tm.normalize_plan(SBIN_PLAN), text=SBIN_TEXT, replays=2000, seed=5)["mc"])

    def test_another_seed_gives_other_numbers(self):
        m = machine(seed=7)["mc"]
        self.assertNotEqual((m["p_target"], m["p_stop"], m["median_close"]), (0.5184, 0.4816, 1342.69))
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertNotEqual(sha(m["bands"]), BANDS)

    def test_the_draws_are_stable_across_pythons(self):
        # random.Random(seed), randint and choice are stable since Python 3.2; the numbers above are the
        # real proof, this pins the first draws so a failure points at the RNG
        r = random.Random(19)
        self.assertEqual([r.randint(-2, 2) for _ in range(8)], [-2, 2, -2, 2, -1, 1, 0, 2])
        self.assertEqual(r.choice(range(59)), 18)

    def test_window_and_replays(self):
        a = machine(replays=500, window=0)["mc"]
        self.assertEqual((a["replays"], a["window"]), (500, 0))
        self.assertNotEqual(a["bands"], machine(replays=500)["mc"]["bands"])
        with self.assertRaises(tm.TMError):
            machine(replays=0)
        with self.assertRaises(tm.TMError):
            machine(window=11)

    def test_a_bar_that_touches_both_counts_as_the_stop(self):
        # one synthetic slot: every bar dips 0.5 % and pokes 1 % above its open, closing flat
        pool = [[(1.0, 1.01, 0.995, 1.0)]] * 5
        both = tm.simulate(pool, 100.0, 99.6, 100.9, 0, 4, replays=50, seed=1, window=0)
        self.assertEqual((both["p_stop"], both["p_target"]), (1.0, 0.0))
        target = tm.simulate(pool, 100.0, 99.0, 100.9, 0, 4, replays=50, seed=1, window=0)
        self.assertEqual((target["p_target"], target["avg_R"]), (1.0, 0.9))
        flat = tm.simulate(pool, 100.0, 99.0, 102.0, 0, 4, replays=50, seed=1, window=0)
        self.assertEqual((flat["p_neither"], flat["p_green"], flat["avg_R"]), (1.0, 0.0, 0.0))


class Bars(unittest.TestCase):
    def setUp(self):
        self.result = tm.chart_result(TEXT, "RELIANCE")

    def test_the_in_progress_tick_is_dropped(self):
        meta, bars, dropped = tm.parse_bars(self.result, NOW_UTC)
        # 102 empty bars (Yahoo's 15:20 and often 15:25 bar), the live 09:48:30 tick, the 09:45 bar still open
        self.assertEqual(dropped, {"no_price": 102, "off_grid": 1, "not_complete": 1, "outside_hours": 0})
        last = bars[-1]
        self.assertEqual((last["date"], last["start"], last["slot"]), ("2026-11-17", "09:40", 5))
        # with the real clock (any time later) the data's own time still marks 09:45 as in progress
        self.assertEqual(tm.parse_bars(self.result, tm.parse_now(None))[1][-1]["start"], "09:40")
        entry = [b for b in bars if b["date"] == "2026-11-16" and b["slot"] == 3][0]
        self.assertEqual((entry["start"], entry["c"]), ("09:30", 1343.800048828125))

    def test_ist_conversion(self):
        self.assertEqual(tm.to_ist(datetime.datetime(2026, 11, 17, 3, 45)), datetime.datetime(2026, 11, 17, 9, 15))
        self.assertEqual(tm.ist_to_utc(datetime.datetime(2026, 11, 17, 15, 30)), datetime.datetime(2026, 11, 17, 10, 0))
        self.assertEqual(tm.parse_now("2026-11-17 09:15"), datetime.datetime(2026, 11, 17, 3, 45))
        self.assertEqual(tm.parse_now("2026-11-17T03:45:00Z"), datetime.datetime(2026, 11, 17, 3, 45))
        self.assertEqual(tm.parse_now("2026-11-17T09:15:00+05:30"), datetime.datetime(2026, 11, 17, 3, 45))
        self.assertEqual(tm.parse_now("2026-03-10 09:15"), datetime.datetime(2026, 3, 10, 3, 45))   # no DST in India
        self.assertEqual(ist(1794887100), datetime.datetime(2026, 11, 17, 9, 15))   # Yahoo's NSE session start
        with self.assertRaises(tm.TMError):
            tm.parse_now("tomorrow")
        # every bar of 60 real sessions lands on the 09:15 to 15:25 grid in IST
        bars = tm.parse_bars(self.result, NOW_UTC)[1]
        self.assertEqual(min(b["start"] for b in bars), "09:15")
        self.assertEqual(max(b["start"] for b in bars), "15:25")
        self.assertTrue(all(0 <= b["slot"] < tm.SESSION_BARS for b in bars))

    def test_the_fixture_skips_the_nse_holidays_and_weekends(self):
        days = sorted(tm.split_sessions(tm.parse_bars(self.result, NOW_UTC)[1]))
        d, want = datetime.date(2026, 8, 20), []
        while d <= datetime.date(2026, 11, 17):
            if tm.trading_day(d)[0]:
                want.append(d.isoformat())
            d += datetime.timedelta(days=1)
        self.assertEqual(days, want)
        for h in ("2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10"):
            self.assertNotIn(h, days)

    def test_now_pins_the_clock(self):
        bars = tm.parse_bars(self.result, tm.parse_now("2026-11-16 09:34"))[1]
        self.assertEqual(len([b for b in bars if b["date"] == "2026-11-16"]), 3)
        with self.assertRaises(tm.TMError) as cm:
            machine(now=tm.parse_now("2026-11-16 09:34"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("09:25 bar", str(cm.exception))
        self.assertEqual(machine(now=tm.parse_now("2026-11-16 09:35"), replays=200)["mc"]["pool_sessions"], 57)

    def test_yahoos_empty_closing_bars(self):
        # the 15:15 bar is missing on 6 of these sessions, so a replay to 15:20 has fewer of them
        doc = machine(until="15:20", replays=300)
        self.assertEqual(doc["mc"]["pool_sessions"], 51)
        self.assertEqual(doc["warnings"], ["the replays run past 15:05, when Groww stops taking new intraday orders "
                                           "on F&O stocks"])
        self.assertEqual(len(doc["data"]["short_sessions_left_out"]), 7)
        # the 15:20 bar is empty in every session: a replay to the close is refused, with the way out
        for until in ("15:25", "15:30"):
            with self.assertRaises(tm.TMError) as cm:
                machine(until=until, replays=10)
            self.assertIn("only 0 session(s)", str(cm.exception))
            self.assertIn("after 15:15", str(cm.exception))

    def test_a_replay_past_groww_square_off_says_so(self):
        def fill(res):   # pretend Yahoo had the closing bars: flat bars at the previous close
            q = res["indicators"]["quote"][0]
            for i, ts in enumerate(res["timestamp"]):
                if q["close"][i] is None and (ist(ts).hour, ist(ts).minute) >= (15, 15):
                    for k in ("open", "high", "low", "close"):
                        q[k][i] = q["close"][i - 1]
        doc = machine(text=edited(fill), until="15:30", replays=300)
        self.assertEqual((doc["mc"]["pool_sessions"], doc["mc"]["bars_left"]), (57, 71))
        self.assertEqual(doc["warnings"], ["the replays run past 15:20, when Groww squares off open intraday (MIS) "
                                           "positions for ₹50 + GST each"])
        self.assertIn("Note: the replays run past 15:20", "\n".join(tm.report(doc)))

    def test_a_gap_leaves_the_session_out(self):
        def hole(res):
            for i, ts in enumerate(res["timestamp"]):
                if ist(ts).strftime("%Y-%m-%d %H:%M") == "2026-08-20 10:00":
                    res["indicators"]["quote"][0]["close"][i] = None
        doc = machine(text=edited(hole), replays=200)
        self.assertEqual(doc["data"]["dropped"]["no_price"], 103)
        self.assertEqual(doc["mc"]["pool_sessions"], 56)
        self.assertEqual(doc["mc"]["pool_from"], "2026-08-21")
        self.assertEqual(doc["data"]["short_sessions_left_out"][0], {"date": "2026-08-20", "bars": 68, "missing": ["10:00"]})
        self.assertNotIn("2026-08-20", [t["date"] for t in doc["history"]["trades"]])

    def test_a_gap_after_the_replay_ends_does_not_matter(self):
        # 2026-10-15 lost its 11:45 and 11:50 bars; a replay that ends at 11:00 keeps that session
        self.assertEqual(machine(until="11:00", replays=200)["mc"]["pool_sessions"], 58)

    def test_today_is_not_in_the_pool(self):
        doc = machine(replays=200)
        self.assertEqual(doc["mc"]["pool_to"], "2026-11-13")
        self.assertNotIn("2026-11-16", [t["date"] for t in doc["history"]["trades"]])
        self.assertNotIn("2026-11-17", [t["date"] for t in doc["history"]["trades"]])

    def test_empty_and_broken_data(self):
        empty = json.dumps({"chart": {"result": [{"meta": {"symbol": "RELIANCE.NS"}, "timestamp": [],
                                                  "indicators": {"quote": [{}]}}], "error": None}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=empty)
        self.assertIn("no complete 5-minute bars", str(cm.exception))
        gone = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                               "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=gone)
        self.assertIn("No data found", str(cm.exception))
        self.assertIn("RELIANCE.NS", str(cm.exception))
        with self.assertRaises(tm.TMError):
            machine(text="<html>rate limited</html>")

    def test_fewer_than_20_sessions_is_refused(self):
        def trim(res):
            keep_bars(res, lambda ts: ist(ts).date().isoformat() >= "2026-10-16")
        with self.assertRaises(tm.TMError) as cm:
            machine(text=edited(trim))
        self.assertIn("only 19 session(s)", str(cm.exception))
        self.assertIn("at least 20", str(cm.exception))

        def twenty(res):
            keep_bars(res, lambda ts: ist(ts).date().isoformat() >= "2026-10-14")
        self.assertEqual(machine(text=edited(twenty), replays=200)["mc"]["pool_sessions"], 20)

    def test_the_data_must_be_the_plans_symbol_and_nse(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(symbol="SBIN"))
        self.assertIn("data is for RELIANCE.NS", str(cm.exception))

        def us(res):
            res["meta"]["exchangeTimezoneName"] = "America/New_York"
        with self.assertRaises(tm.TMError) as cm:
            machine(text=edited(us))
        self.assertIn("not NSE data", str(cm.exception))

    def test_a_holiday_or_weekend_says_why_there_are_no_bars(self):
        for date, words in (("2026-11-10", "no bars for that day: NSE holiday: Diwali Balipratipada"),
                            ("2026-11-15", "no bars for that day: weekend"),
                            ("2026-11-18", "no bars for that day)")):
            with self.assertRaises(tm.TMError) as cm:
                machine(plan(date=date), replays=10)
            self.assertIn(words, str(cm.exception), date)

    def test_a_2027_date_runs_with_a_calendar_warning(self):
        def a_year_on(res):   # the same bars 52 weeks later: same weekdays, same IST times
            res["timestamp"] = [t + 364 * 86400 for t in res["timestamp"]]
            res["meta"]["regularMarketTime"] += 364 * 86400
        doc = machine(plan(date="2027-11-15"), text=edited(a_year_on), now=tm.parse_now("2027-11-16 09:50"))
        self.assertEqual(doc["warnings"], ["NSE holiday list for 2027 not loaded: check nseindia.com"])
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["avg_R"], doc["mc"]["pool_to"]), (0.5275, 0.055, "2027-11-12"))
        self.assertIn("Note: NSE holiday list for 2027 not loaded: check nseindia.com.", "\n".join(tm.report(doc)))
        self.assertEqual(tm.trading_day(datetime.date(2027, 1, 26)), (True, ""))   # unknown: a weekday trades
        self.assertIsNone(tm.calendar_warning(datetime.date(2026, 12, 31)))


class Money(unittest.TestCase):
    def test_inr(self):
        self.assertEqual(tm.inr(123456.5), "₹1,23,456.50")
        self.assertEqual(tm.inr(-1250), "-₹1,250.00")
        self.assertEqual(tm.inr(0), "₹0.00")
        self.assertEqual(tm.inr(999.999), "₹1,000.00")
        self.assertEqual(tm.inr(10000000), "₹1,00,00,000.00")
        self.assertEqual(tm.inr(1343.800048828125), "₹1,343.80")

    def test_tick_size_at_the_band_edges(self):
        for price, tick in ((249.99, "0.01"), (250, "0.05"), (999.95, "0.05"), (1000, "0.10"), (4999.9, "0.10"),
                            (5000, "0.50"), (9999.5, "0.50"), (10000, "1.00"), (19999, "1.00"), (20000, "5.00")):
            self.assertEqual(str(tm.tick_size(price)), tick, price)

    def test_stops_round_away_from_the_entry_onto_the_grid(self):
        self.assertEqual(tm.tick_away(1091.75, True, 1095.0), 1091.7)
        self.assertEqual(tm.tick_away(1091.75, False, 1095.0), 1091.8)
        self.assertEqual(tm.tick_away(1340.300048828125, True, 1343.8), 1340.3)
        self.assertEqual(tm.tick_away(1340.2999999999998, True, 1343.8), 1340.3)   # float noise is not a tick
        self.assertEqual(tm.tick_away(249.987, True, 249.99), 249.98)
        self.assertEqual(tm.tick_away(252.37, True, 255.0), 252.35)
        self.assertEqual(tm.tick_away(10794.4, True, 10800.0), 10794.0)
        self.assertEqual(tm.tick_away(10794.4, False, 10780.0), 10795.0)
        self.assertEqual(tm.tick_round(1343.800048828125), 1343.8)
        self.assertEqual(tm.tick_round(1347.35, 1343.8), 1347.4)
        self.assertEqual(tm.tick_round(812.47), 812.45)

    def test_symbols(self):
        self.assertEqual(tm.norm_symbol("reliance.ns"), "RELIANCE")
        self.assertEqual(tm.norm_symbol(" m&m "), "M&M")
        self.assertEqual(tm.norm_symbol("BAJAJ-AUTO"), "BAJAJ-AUTO")
        self.assertEqual(tm.norm_symbol("20MICRONS"), "20MICRONS")
        for bad in ("", "RELIANCE INDUSTRIES", "^NSEI", "-ABC", "A" * 21, None):
            self.assertIsNone(tm.norm_symbol(bad), bad)
        self.assertEqual(plan(symbol="reliance.ns")["symbol"], "RELIANCE")
        self.assertEqual(tm.chart_url("M&M"), "https://query2.finance.yahoo.com/v8/finance/chart/M%26M.NS"
                                             "?range=60d&interval=5m")
        self.assertIn("/chart/BAJAJ-AUTO.NS?", tm.chart_url("BAJAJ-AUTO", "query1"))

    def test_m_and_m_data_matches_its_plan(self):
        # Yahoo answers the M%26M.NS request with meta.symbol M&M.NS: the symbol check passes, and one
        # morning of bars is then too few sessions
        with open(os.path.join(NSE, "M&M-1d-5m.json"), encoding="utf-8") as fh:
            text = fh.read()
        p = plan(symbol="M&M", date="2026-11-17", entry=3397.0, stop=3380.0, target=3414.0, stop_rule="pct")
        with self.assertRaises(tm.TMError) as cm:
            machine(p, text=text)
        self.assertIn("only 0 session(s) of M&M", str(cm.exception))


class Plans(unittest.TestCase):
    def test_an_entry_not_in_the_data_is_refused(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(date="2026-11-18"))
        self.assertIn("not in the data", str(cm.exception))
        with self.assertRaises(tm.TMError) as cm:      # 09:50 has not happened in this data yet
            machine(plan(date="2026-11-17", entry_time="9:50 AM", entry_bar=None))
        self.assertIn("the data stops at the 09:40 bar", str(cm.exception))

    def test_plan_checks(self):
        cases = [
            (dict(entry_time="9:37 AM", entry_bar=None), "5-minute grid"),
            (dict(entry_time="3:30 PM", entry_bar=None), "between 9:20 AM and 3:25 PM"),
            (dict(entry_bar=4), "disagrees with itself"),
            (dict(entry_time=None, entry_bar=None), "entry_time"),
            (dict(interval="1m"), "7 days of 1-min bars"),
            (dict(stop=1345.0), "stop < entry < target"),
            (dict(side="short"), "buy or sell"),
            (dict(symbol=""), "NSE symbol"),
            (dict(symbol="RELIANCE INDUSTRIES"), "NSE symbol"),
            (dict(entry="abc"), "number for entry"),
            (dict(date="16 Nov"), "date like"),
            (dict(opening_range={"minutes": 7}), "multiple of 5"),
            (dict(flat_by="3pm"), "IST time"),
        ]
        for change, words in cases:
            with self.assertRaises(tm.TMError, msg=change) as cm:
                plan(**change)
            self.assertIn(words, str(cm.exception), change)

    def test_entry_bar_or_entry_time_alone(self):
        self.assertEqual(plan(entry_time=None)["entry_time"], "9:35 AM")
        self.assertEqual(plan(entry_bar=None)["entry_bar"], 3)
        self.assertEqual(plan(entry_time="09:35", entry_bar=None)["entry_bar"], 3)
        self.assertEqual(plan(entry_time="9:35am", entry_bar=None)["entry_bar"], 3)
        self.assertEqual(plan(entry_time="2:30 PM", entry_bar=None)["entry_bar"], 62)

    def test_the_replay_ends_at_the_flat_by_time(self):
        m = machine(plan(flat_by="14:30"))["mc"]
        self.assertEqual((m["until"], m["until_from"], m["bars_left"], m["band_times"][-1]), ("14:30", "flat_by", 59, "14:30"))
        self.assertEqual((m["p_target"], m["p_stop"], m["avg_R"]), (0.5202, 0.4798, 0.04))
        over = machine(until="14:30")["mc"]                       # --until wins over the plan's flat_by (15:00)
        self.assertEqual(over["until_from"], "--until")
        self.assertEqual(dict(over, until_from="flat_by"), m)
        self.assertEqual(machine(until="2:30 PM", replays=500)["mc"], machine(until="14:30", replays=500)["mc"])
        doc = machine(plan(flat_by=None), replays=500)            # no flat_by: the pack default, 15:00
        self.assertEqual((doc["mc"]["until"], doc["mc"]["until_from"]), ("15:00", "default"))
        self.assertIn("to 3:00 PM IST (the default flat-by, before Groww's 15:05 order cut-off and 15:20 square-off;",
                      "\n".join(tm.report(doc)))

    def test_bad_until(self):
        for bad, words in (("9:35 AM", "nothing to replay"), ("15:35", "no later than 3:30 PM"),
                           ("15:07", "5-minute grid"), ("noon", "IST time")):
            with self.assertRaises(tm.TMError) as cm:
                machine(until=bad, replays=10)
            self.assertIn(words, str(cm.exception), bad)
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(flat_by="9:30"), replays=10)
        self.assertIn("the plan's flat_by", str(cm.exception))

    def test_an_entry_outside_its_bar_is_flagged(self):
        doc = machine(plan(entry=1350.0, stop=1346.5, target=1353.5), replays=200)
        self.assertEqual(doc["warnings"], ["the plan's entry 1350.00 is outside that bar's range 1342.10 to 1344.20"])

    def test_the_stop_rule_is_worked_out_when_not_given(self):
        rule = machine(plan(stop_rule=None), replays=100)["history"]["rule"]
        self.assertEqual(rule["stop_rule"], "or_low")
        rule = machine(plan(stop_rule="atr", stop=1338.0, target=1355.4), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["stop_pct"], rule["target_r"]), ("pct", 0.4316, 2.0))

    def test_a_setup_other_than_orb_skips_the_history(self):
        doc = machine(plan(setup="flag"), replays=200)
        self.assertIsNone(doc["history"]["rule"])
        self.assertIn("orb", doc["history"]["note"])
        self.assertEqual(doc["mc"]["pool_sessions"], 57)
        self.assertIn("History: skipped", "\n".join(tm.report(doc)))

    def test_a_sell_plan_mirrors_the_rules(self):
        doc = machine(plan(side="sell", stop=1347.3, target=1340.3, stop_rule="or_high"), replays=2000)
        m = doc["mc"]
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertEqual((m["r_at_target"], m["p_target"], m["p_stop"]), (1.0, 0.463, 0.537))
        self.assertEqual(doc["history"]["rule"]["stop_rule"], "or_high")
        for t in doc["history"]["trades"]:
            self.assertTrue(t["target"] < t["entry"] < t["stop"], t)
        self.assertIn("below the entry", "\n".join(tm.report(doc)))

    def test_the_verdict_thresholds_at_1r(self):
        # 1R: the target's share of the replays that hit one side against 50 %
        self.assertTrue(tm.verdict({"p_target": 0.30, "p_stop": 0.26}).startswith("Close to a coin flip"))
        self.assertTrue(tm.verdict({"p_target": 0.40, "p_stop": 0.30}).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict({"p_target": 0.20, "p_stop": 0.30}).startswith("Leans toward the stop"))
        self.assertTrue(tm.verdict({"p_target": 0.60, "p_stop": 0.30}).startswith("Strongly toward the target"))
        self.assertEqual(tm.verdict({"p_target": 0.10, "p_stop": 0.60, "r_at_target": 1.0}),
                         "Strongly toward the stop: target first 10.0 % vs stop first 60.0 %.")
        self.assertEqual(tm.verdict({"p_target": 0.0, "p_stop": 0.0, "until": "15:00"}),
                         "No replay reached the target or the stop by 3:00 PM: the price at 3:00 PM decides every one.")

    def test_a_2r_target_is_judged_against_its_break_even(self):
        # made-up odds: it needs 33 %, got 32 %
        mc = {"p_target": 0.241, "p_stop": 0.505, "p_neither": 0.254, "r_at_target": 2.0, "avg_R": 0.095}
        self.assertEqual(tm.verdict(mc), "Close to break-even: at 2R the target has to come first in 33 % of the "
                                         "replays that hit one side; it did in 32 % (target first 24.1 % vs stop "
                                         "first 50.5 %).")
        self.assertEqual(tm.verdict_notes(mc), [])
        lean = dict(mc, p_target=0.15, p_stop=0.55)
        self.assertTrue(tm.verdict(lean).startswith("Leans toward the stop: at 2R the target has to come first in "
                                                    "33 % of the replays that hit one side; it did in 21 %"))
        self.assertEqual(tm.verdict_notes(lean), ["Average R is +0.095 all the same: the replays that hit neither "
                                                  "pull it the other way."])
        self.assertTrue(tm.verdict(dict(mc, p_target=0.30, p_stop=0.40)).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict(dict(mc, p_target=0.40, p_stop=0.40)).startswith("Strongly toward the target"))
        self.assertIn("at 1.5R the target has to come first in 40 %", tm.verdict(dict(mc, r_at_target=1.5)))

    def test_2r_on_the_fixture_near_break_even(self):
        # the same entry and stop with a 2R target 1350.80: 38.0 % vs 61.8 % looks lopsided, but 2R only needs a third
        doc = machine(plan(target=1350.8))
        m = doc["mc"]
        self.assertEqual((m["r_at_target"], m["p_target"], m["p_stop"], m["avg_R"]), (2.0, 0.3805, 0.6179, 0.144))
        self.assertEqual(doc["verdict"], "Close to break-even: at 2R the target has to come first in 33 % of the "
                                         "replays that hit one side; it did in 38 % (target first 38.0 % vs stop "
                                         "first 61.8 %).")
        self.assertEqual(doc["verdict_notes"], [])

    def test_2r_on_the_fixture_a_lean_with_both_notes(self):
        # stop 1336.80, 2R target 1357.80, out by 11:00: the target first 12.8 % vs stop first 35.4 %
        doc = machine(plan(stop=1336.8, target=1357.8, stop_rule="pct"), until="11:00")
        m = doc["mc"]
        self.assertEqual((m["r_at_target"], m["p_target"], m["p_stop"], m["p_neither"], m["avg_R"]),
                         (2.0, 0.1284, 0.3539, 0.5177, 0.097))
        self.assertEqual(doc["verdict"], "Leans toward the stop: at 2R the target has to come first in 33 % of the "
                                         "replays that hit one side; it did in 27 % (target first 12.8 % vs stop "
                                         "first 35.4 %).")
        self.assertEqual(doc["verdict_notes"], [
            "51.8 % of the replays hit neither by 11:00 AM; the price at 11:00 AM decides those.",
            "Average R is +0.097 all the same: the replays that hit neither pull it the other way."])
        text = "\n".join(tm.report(doc))
        self.assertIn(doc["verdict"] + "\n" + doc["verdict_notes"][0] + "\n" + doc["verdict_notes"][1], text)


class Network(unittest.TestCase):
    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = tm._get, tm._sleep
        tm._sleep = self.sleeps.append

    def tearDown(self):
        tm._get, tm._sleep = self._get, self._sleep

    def answer(self, codes):
        codes = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url.split(".finance")[0].split("//")[1])
            code = codes.pop(0)
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return TEXT
        tm._get = fake

    def test_url_and_user_agent(self):
        self.assertEqual(tm.chart_url("RELIANCE"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=60d&interval=5m")
        self.assertEqual(tm.UA, "Mozilla/5.0 (compatible; nse-day-trader/1.0)")

    def test_every_request_verifies_tls(self):
        seen = []
        real = tm.urllib.request.urlopen

        def fake(req, timeout=None, context=None):
            seen.append((req.full_url, req.get_header("User-agent"), context))
            return io.BytesIO(b"{}")
        tm._get = self._get
        tm.urllib.request.urlopen = fake
        try:
            self.assertEqual(tm._get(tm.chart_url("M&M")), "{}")
        finally:
            tm.urllib.request.urlopen = real
        url, ua, ctx = seen[0]
        self.assertIn("/chart/M%26M.NS?", url)
        self.assertEqual(ua, tm.UA)
        self.assertIsInstance(ctx, ssl.SSLContext)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_429_waits_retries_then_falls_back_to_query1(self):
        self.answer([429, 429, 200])
        self.assertEqual(tm.fetch_chart("RELIANCE"), TEXT)
        self.assertEqual(self.calls, ["query2", "query2", "query1"])
        self.assertEqual(self.sleeps, [tm.BACKOFF, tm.PAUSE])

    def test_one_429_then_ok(self):
        self.answer([429, 200])
        self.assertEqual(tm.fetch_chart("RELIANCE"), TEXT)
        self.assertEqual(self.calls, ["query2", "query2"])

    def test_5xx_falls_back(self):
        self.answer([503, 200])
        self.assertEqual(tm.fetch_chart("RELIANCE"), TEXT)
        self.assertEqual(self.calls, ["query2", "query1"])

    def test_rate_limited_everywhere_is_one_plain_line(self):
        self.answer([429, 429, 429, 429])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("RELIANCE")
        self.assertIn("rate limiting", str(cm.exception))

    def test_404(self):
        self.answer([404])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("ZZZZ")
        self.assertIn("ZZZZ.NS", str(cm.exception))
        self.assertIn("check the NSE symbol", str(cm.exception))

    def test_run_over_the_network_path(self):
        self.answer([200])
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "plan.json")
            with open(p, "w") as fh:
                json.dump(PLAN, fh)
            code, text, _ = run(["run", "--plan", p, "--now", NOW, "--replays", "300"])
            self.assertEqual(code, 0)
            self.assertEqual(self.calls, ["query2"])
        finally:
            shutil.rmtree(d)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.plan = os.path.join(self.d, "trader", "plan.json")
        os.makedirs(os.path.dirname(self.plan))
        with open(self.plan, "w") as fh:
            json.dump(PLAN, fh, indent=2)
        self.out = os.path.join(self.d, "trader", "timemachine.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_run_with_the_plan_end_to_end(self):
        code, text, err = run(["run", "--plan", self.plan, "--offline", RELIANCE, "--now", NOW, "--write", self.out])
        self.assertEqual(code, 0, err)
        self.assertIn("target first   52.8 %", text)
        self.assertIn("Close to a coin flip: target first 52.8 % vs stop first 47.2 %.", text)
        self.assertIn("Wrote %s." % self.out, text)
        self.assertIn("/nse-budget", text)
        with open(self.out, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(k for k in ("plan", "mc", "history") if k in doc), ["history", "mc", "plan"])
        self.assertEqual(doc["created_by"], "nse-timemachine")
        self.assertEqual(doc["source"], "offline")
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["avg_R"]), (0.5275, 0.055))
        self.assertEqual(sha(doc["mc"]["sample_paths"]), PATHS)
        self.assertEqual((doc["history"]["triggered"], doc["history"]["avg_R"]), (32, -0.066))
        self.assertEqual(doc["plan"]["from"], self.plan)

    def test_json_output(self):
        code, text, _ = run(["run", "--plan", self.plan, "--offline", RELIANCE, "--now", NOW, "--json"])
        self.assertEqual(code, 0)
        doc = json.loads(text)
        self.assertEqual(doc["mc"]["p_stop"], 0.4725)
        self.assertNotIn("sample_paths", doc["mc"])
        self.assertEqual(doc["verdict"][:20], "Close to a coin flip")
        self.assertFalse(os.path.exists(self.out))

    def test_the_plan_as_flags_matches_the_file(self):
        code, text, err = run(["run", "--symbol", "reliance.ns", "--date", "2026-11-16", "--entry", "1343.8",
                               "--stop", "1340.3", "--target", "1347.3", "--entry-time", "9:35 AM",
                               "--offline", RELIANCE, "--now", NOW, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["p_green"]), (0.5275, 0.5275))
        self.assertEqual((doc["mc"]["until"], doc["mc"]["until_from"]), ("15:00", "default"))
        self.assertEqual(doc["plan"]["stop_rule"], "or_low")
        self.assertEqual((doc["history"]["target"], doc["history"]["stop"], doc["history"]["avg_R"]), (9, 10, -0.066))
        code, _, err = run(["run", "--symbol", "RELIANCE", "--entry", "1343.8", "--offline", RELIANCE])
        self.assertEqual(code, 1)
        self.assertIn("--stop", err)

    def test_offline_folder_and_default_paths(self):
        shutil.copy(RELIANCE, self.d)
        cwd = os.getcwd()
        os.chdir(self.d)
        try:
            code, text, err = run(["run", "--offline", ".", "--now", NOW, "--replays", "500", "--write"])
            self.assertEqual(code, 0, err)
            self.assertTrue(os.path.exists(os.path.join("trader", "timemachine.json")))
            code, text, err = run(["chart", "--from"])
            self.assertEqual(code, 0, err)
            self.assertIn("* median", text)
            with open(os.path.join("trader", "timemachine.json"), encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["mc"]["replays"], 500)
        finally:
            os.chdir(cwd)

    def test_errors_are_one_plain_line(self):
        for argv in (["run", "--plan", os.path.join(self.d, "nope.json")],
                     ["run", "--plan", self.plan, "--offline", os.path.join(self.d, "missing.json.gz")],
                     ["run", "--plan", self.plan, "--offline", RELIANCE, "--until", "9:00"],
                     ["run", "--plan", self.plan, "--offline", RELIANCE, "--now", NOW, "--until", "15:30"],
                     ["chart", "--from", os.path.join(self.d, "nope.json")]):
            code, text, err = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertTrue(err.startswith("timemachine.py: "), err)
            self.assertNotIn("Traceback", err)
        self.assertEqual(run([])[0], 1)

    def test_chart(self):
        run(["run", "--plan", self.plan, "--offline", RELIANCE, "--now", NOW, "--write", self.out])
        code, text, _ = run(["chart", "--from", self.out])
        self.assertEqual(code, 0)
        lines = text.splitlines()
        self.assertIn("RELIANCE 2026-11-16, 2,000 replay paths from the 9:35 AM IST entry (seed 19)", lines[0])
        self.assertTrue(any(l.startswith("  1347.30 |") and l.endswith("  target") for l in lines))
        self.assertTrue(any(l.startswith("  1340.30 |") and l.endswith("  stop") for l in lines))
        self.assertTrue(any(l.startswith("  1343.80 |*") and l.endswith("  entry") for l in lines))
        self.assertIn("9:35", text)
        self.assertIn("3:00", text)
        self.assertEqual(lines[-1], "Close to a coin flip: target first 52.8 % vs stop first 47.2 %.")
        code, again, _ = run(["chart", "--plan", self.plan, "--offline", RELIANCE, "--now", NOW])
        self.assertEqual(again, text)


if __name__ == "__main__":
    unittest.main()
