"""nse-entry tests. All offline: Yahoo is never called.

Shared fixtures (tests/fixtures/nse/, captured 2026-11-17 09:48 IST, read-only):
  SBIN-1d-5m.json        SBIN's 5-minute bars 09:15-09:45 of 2026-11-17, ending with the
                         in-progress 09:45 bar and Yahoo's live tick at 09:48:32. The reel:
                         range 1,092.00-1,101.40, the 9:35 bar closes AT the high, the 9:40
                         bar closes above it at 1,105.70.
  SBIN-1d-1m.json        the same morning in 1-minute bars (a null 09:48 bar, a live tick).
  M&M-1d-5m.json, BAJAJ-AUTO-1d-5m.json   symbols with & and - (M&M is M%26M.NS on Yahoo).
  SBIN-60d-5m.json.gz    60 full sessions plus that partial morning.
Own fixtures (tests/fixtures/nse-entry/):
  SBIN-2026-11-16-5m.json, RELIANCE-2026-11-16-5m.json   the reference full session of
                         Monday 2026-11-16 (75 bars, nulls kept), cut from the 60d files. meta
                         trimmed like a range=1d answer: regularMarketTime set to the 15:30
                         close, previousClose set to 2026-11-13's last close in the 60d file.
"""

import copy
import datetime
import gzip
import io
import json
import os
import shutil
import ssl
import tempfile
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

entry = load("nse-entry", "entry")

NSE = os.path.join(FIX, "nse")
EDIR = os.path.join(FIX, "nse-entry")
SBIN = os.path.join(NSE, "SBIN-1d-5m.json")            # 2026-11-17 morning, the reel
SBIN_1M = os.path.join(NSE, "SBIN-1d-1m.json")
MM = os.path.join(NSE, "M&M-1d-5m.json")
BAJAJ = os.path.join(NSE, "BAJAJ-AUTO-1d-5m.json")
SBIN_60D = os.path.join(NSE, "SBIN-60d-5m.json.gz")
SBIN_1116 = os.path.join(EDIR, "SBIN-2026-11-16-5m.json")
REL_1116 = os.path.join(EDIR, "RELIANCE-2026-11-16-5m.json")
OPEN_TS = 1794887100       # 2026-11-17 09:15 IST = 03:45 UTC
DT = datetime.datetime


def ist(text):
    return entry.parse_now(text)


def doc(path=SBIN):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def chart(d=None, sym="SBIN"):
    return entry.parse_chart(json.dumps(d if d is not None else doc()), sym)


def at(text, d=None, **kw):
    return entry.evaluate(chart(d), ist(text), **kw)


def day_of(path):
    return chart(doc(path))


def shift_to(d, day, open_ts=OPEN_TS):
    """The same session moved to another date: 9:15 IST is 03:45 UTC every day of the year."""
    d = copy.deepcopy(d)
    r = d["chart"]["result"][0]
    new_open = int((DT.combine(day, datetime.time(3, 45)) - DT(1970, 1, 1)).total_seconds())
    delta = new_open - open_ts
    r["timestamp"] = [t + delta for t in r["timestamp"]]
    r["meta"]["regularMarketTime"] += delta
    return d


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = entry.main(argv)
    return code, out.getvalue(), err.getvalue()


class TheReel(unittest.TestCase):
    """SBIN, Tuesday 2026-11-17, 5-minute bars."""

    def test_triggers_at_945_on_the_940_bar(self):
        r = at("2026-11-17 09:45")
        self.assertEqual(r["status"], "triggered")
        self.assertEqual(r["opening_range"]["high"], 1101.4)
        self.assertEqual(r["opening_range"]["low"], 1092.0)
        self.assertEqual(r["opening_range"]["bars"], 3)
        t = r["trigger"]
        self.assertEqual(t["time"], "9:45 AM")
        self.assertEqual(t["bar_start"], "09:40")
        self.assertEqual(t["bar_index"], 5)
        self.assertEqual(t["price"], 1105.7)
        self.assertEqual(t["close"], 1105.7)                        # Yahoo's float32 1105.699951171875, snapped
        self.assertEqual(t["pct_over"], 0.39)
        self.assertEqual(r["level"], 1101.4)
        self.assertEqual(r["prev_close"], 1090.5)
        text = "\n".join(entry.describe(r))
        self.assertIn("SBIN  Tue 2026-11-17 09:45 IST", text)
        self.assertIn("TRIGGERED 9:45 AM at ₹1,105.70: the 9:40 bar (bar 5) closed above the range high ₹1,101.40",
                      text)
        self.assertIn("/nse-exit", text)

    def test_a_close_at_the_high_is_not_above_it(self):
        r = at("2026-11-17 09:44")
        self.assertEqual(r["status"], "waiting")
        self.assertIsNone(r["trigger"])
        self.assertEqual(r["last"]["bar_start"], "09:35")
        self.assertEqual(r["last"]["close"], 1101.4)
        self.assertIn("is AT the range high ₹1,101.40, not above it", r["reason"])
        self.assertEqual(r["forming"]["bar_start"], "09:40")
        self.assertEqual(at("2026-11-17 09:44:59")["status"], "waiting")
        r = at("2026-11-17 09:39")
        self.assertIn("last close ₹1,098.50 (9:30 bar), ₹2.90 under the range high ₹1,101.40 (-0.26%)", r["reason"])

    def test_before_the_open_and_while_the_range_forms(self):
        r = at("2026-11-17 09:10")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "pre-open"))
        self.assertIn("opens at 9:15 IST", r["reason"])
        self.assertIn("set at 9:30", r["reason"])
        r = at("2026-11-17 09:29")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "forming"))
        self.assertEqual(r["opening_range"]["bars"], 2)
        self.assertFalse(r["opening_range"]["complete"])
        r = at("2026-11-17 09:31")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "open"))
        self.assertEqual(r["opening_range"]["bars"], 3)

    def test_the_live_tick_and_the_in_progress_bar_are_dropped(self):
        c = chart()
        self.assertEqual(c["bars"][-1]["t"], OPEN_TS + 33 * 60 + 32)   # 09:48:32 is in the file
        r = entry.evaluate(c, entry.utc_of(c["data_time"]))            # read as of the file's own time
        self.assertEqual(r["now"], "2026-11-17 09:48:32")
        self.assertEqual(r["_bars"][-1]["slot"], 5)                     # the 9:40 bar
        self.assertEqual(r["forming"]["bar_start"], "09:45")
        self.assertEqual(r["bars_closed"], 6)
        # 09:50: closed by the clock, but Yahoo's last trade (09:48:32) is before its close
        r = at("2026-11-17 09:50")
        self.assertEqual(r["_bars"][-1]["slot"], 5)
        self.assertIn("Yahoo's last trade is 09:48:32", r["lag_note"])
        # a minute later with no newer trade it counts; the 09:48:32 tick never becomes a bar
        r = at("2026-11-17 09:51")
        self.assertEqual(r["_bars"][-1]["slot"], 6)
        self.assertEqual(r["bars_closed"], 7)
        self.assertTrue(all(b["t"] % 300 == 0 for b in r["_bars"]))

    def test_after_the_close_it_stays_triggered_and_says_it_is_late(self):
        r = at("2026-11-17 15:35")
        self.assertEqual((r["status"], r["phase"]), ("triggered", "after-close"))
        self.assertEqual(r["trigger"]["time"], "9:45 AM")
        text = "\n".join(entry.describe(r))
        self.assertIn("minutes ago", text)
        self.assertNotIn("This is the moment", text)


class FullSession(unittest.TestCase):
    """The reference full session, Monday 2026-11-16: no breakout of the 15-minute range."""

    def c(self, path=SBIN_1116):
        return chart(doc(path))

    def ev(self, text, path=SBIN_1116, **kw):
        return entry.evaluate(self.c(path), ist(text), **kw)

    def test_75_bars_nulls_dropped(self):
        r = self.ev("2026-11-16 15:31")
        self.assertEqual(r["bars_closed"], 73)                      # 15:15 and 15:20 came as nulls
        slots = [b["slot"] for b in r["_bars"]]
        self.assertEqual((slots[0], slots[-1]), (0, 74))
        self.assertNotIn(72, slots)
        r = self.ev("2026-11-16 15:31", REL_1116)
        self.assertEqual(r["bars_closed"], 74)

    def test_no_breakout_waits_then_expires_at_1430(self):
        r = self.ev("2026-11-16 12:00")
        self.assertEqual(r["status"], "waiting")
        self.assertEqual((r["opening_range"]["high"], r["opening_range"]["low"]), (1099.5, 1083.9))
        self.assertEqual(self.ev("2026-11-16 14:29")["status"], "waiting")
        r = self.ev("2026-11-16 14:30")
        self.assertEqual((r["status"], r["phase"]), ("expired", "after-cutoff"))
        self.assertIn("no close above the range high ₹1,099.50 by 14:30", r["reason"])
        self.assertEqual(self.ev("2026-11-16 15:31")["phase"], "after-close")
        self.assertEqual((r["no_entries_before"], r["no_entries_after"]), ("09:30", "14:30"))

    def test_the_bar_closing_at_the_cutoff_still_counts_when_yahoo_lags(self):
        d = doc(SBIN_1116)
        r0 = d["chart"]["result"][0]
        open_ts = r0["timestamp"][0]
        keep = [i for i, t in enumerate(r0["timestamp"]) if t <= open_ts + 62 * 300]   # live: up to the 14:25 bar
        r0["timestamp"] = [r0["timestamp"][i] for i in keep]
        q = r0["indicators"]["quote"][0]
        for k in q:
            q[k] = [q[k][i] for i in keep]
        r0["meta"]["regularMarketTime"] = open_ts + 314 * 60 + 40                    # last trade 14:29:40
        r = entry.evaluate(chart(d), ist("2026-11-16 14:30:10"))           # the 14:25 bar closed by the clock only
        self.assertEqual(r["status"], "waiting")
        self.assertIn("Yahoo's last trade is 14:29:40", r["lag_note"])
        self.assertEqual(entry.evaluate(chart(d), ist("2026-11-16 14:31"))["status"], "expired")

    def test_level_setup(self):
        r = self.ev("2026-11-16 10:30", setup="level", level=1096.5)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"], r["trigger"]["price"]),
                         ("9:55 AM", 7, 1096.75))
        # a level under the 9:25 close: the first eligible close is at 9:30 (no_entries_before)
        r = self.ev("2026-11-16 10:30", setup="level", level=1096.0)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("9:30 AM", 2))
        r = self.ev("2026-11-16 10:30", REL_1116, setup="level", level=1343.5)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"], r["trigger"]["price"]),
                         ("9:30 AM", 2, 1343.6))
        r = self.ev("2026-11-16 10:30", REL_1116, setup="level", level=1343.7)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"], r["trigger"]["price"]),
                         ("9:35 AM", 3, 1343.8))
        with self.assertRaises(entry.EntryError):
            self.ev("2026-11-16 10:30", setup="level")

    def test_a_close_at_the_level_and_a_close_after_the_cutoff_do_not_trigger(self):
        r = self.ev("2026-11-16 09:46", REL_1116, setup="level", level=1344.0)
        self.assertEqual(r["status"], "waiting")
        self.assertIn("last close ₹1,344.00 (9:40 bar) is AT the level ₹1,344.00, not above it", r["reason"])
        # the 15:25 bar prints 1,344.80, above the level, but after the 14:30 cutoff
        r = self.ev("2026-11-16 15:31", REL_1116, setup="level", level=1344.0)
        self.assertEqual(r["status"], "expired")
        # Yahoo's float noise: 1343.800048828125 is a close AT 1343.80, not above it
        r = self.ev("2026-11-16 09:36", REL_1116, setup="level", level=1343.8)
        self.assertEqual(r["status"], "waiting")

    def test_no_entries_before_holds_an_early_close_above(self):
        r = self.ev("2026-11-16 09:26", or_minutes=5)
        self.assertEqual(r["opening_range"]["high"], 1096.45)
        self.assertIn("the 9:20 bar closed above at ₹1,098.30, but no entries before 9:30 (config)", r["reason"])
        r = self.ev("2026-11-16 10:00", or_minutes=5)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"], r["trigger"]["price"]),
                         ("9:55 AM", 7, 1096.75))

    def test_volume_leaves_out_the_zero_volume_915_bar(self):
        r = self.ev("2026-11-16 09:56", setup="level", level=1096.5, min_volume_x=1)
        self.assertEqual(r["opening_range"]["avg_volume"], 594868)        # (572559 + 617177) / 2
        self.assertEqual(r["opening_range"]["volume_bars"], 2)
        self.assertEqual(r["status"], "waiting")
        self.assertIn("0.96x the range's volume, under your 1.00x", r["reason"])
        r = self.ev("2026-11-16 10:30", setup="level", level=1096.5, min_volume_x=0.9)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["volume_x"]), ("9:55 AM", 0.96))
        self.assertEqual(self.ev("2026-11-16 14:50", setup="level", level=1096.5, min_volume_x=1)["status"],
                         "expired")
        d = doc(SBIN_1116)
        q = d["chart"]["result"][0]["indicators"]["quote"][0]
        q["volume"] = [0] * len(q["volume"])
        r = entry.evaluate(chart(d), ist("2026-11-16 09:56"), setup="level", level=1096.5, min_volume_x=1)
        self.assertIsNone(r["opening_range"]["avg_volume"])
        self.assertIn("no volume for the range bars", r["reason"])

    def test_config_cutoff(self):
        cfg = {"no_entries_before": "09:30", "no_entries_after": "09:50"}
        r = self.ev("2026-11-16 09:55", setup="level", level=1096.5, cfg=cfg)
        self.assertEqual(r["status"], "expired")
        cfg = {"no_entries_before": "09:55", "no_entries_after": "14:30"}
        r = self.ev("2026-11-16 10:30", setup="level", level=1096.0, cfg=cfg)
        self.assertEqual(r["trigger"]["time"], "9:55 AM")             # the 9:50 bar, first to close at or after 9:55

    def test_or_minutes_30(self):
        r = self.ev("2026-11-16 10:49", REL_1116, or_minutes=30)
        self.assertEqual(r["opening_range"]["bars"], 6)
        self.assertEqual(r["opening_range"]["high"], 1347.1)
        self.assertEqual(r["status"], "waiting")

    def test_bad_inputs(self):
        with self.assertRaises(entry.EntryError):
            at("2026-11-17 09:45", or_minutes=7)
        with self.assertRaises(entry.EntryError):
            at("2026-11-17 09:45", interval="15m")
        with self.assertRaises(entry.EntryError):
            entry.parse_now("tomorrow")
        with self.assertRaises(entry.EntryError):
            entry.parse_hhmm("25:00", "x")


class OneMinute(unittest.TestCase):
    def c(self):
        return chart(doc(SBIN_1M))

    def test_a_wick_and_a_close_at_the_high_do_not_trigger(self):
        c = self.c()
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m")
        self.assertEqual(r["now"], "2026-11-17 09:48:38")
        orr = r["opening_range"]
        self.assertEqual((orr["high"], orr["low"], orr["bars"], orr["of"]), (1101.4, 1092.0, 15, 15))
        self.assertEqual(orr["volume_bars"], 14)                    # the 9:15 minute shows no volume
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_start"], t["bar_index"], t["price"]), ("9:41 AM", "09:40", 25, 1102.4))
        r = entry.evaluate(c, ist("2026-11-17 09:40:59"), interval="1m")
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["forming"]["bar_start"], "09:40")
        self.assertEqual(r["last"]["close"], 1101.4)                # the 9:39 bar wicked to 1,101.90, closed AT the high

    def test_null_bar_and_tick(self):
        c = self.c()
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m")
        slots = [b["slot"] for b in r["_bars"]]
        self.assertEqual(slots[-1], 32)                             # 9:47; 9:48 came as nulls, 09:48:38 is a tick
        self.assertNotIn(33, slots)

    def test_the_file_and_the_interval_must_agree(self):
        code, _, err = run(["check", "SBIN", "--offline", SBIN_1M])
        self.assertEqual(code, 1)
        self.assertIn("1m bars, not 5m", err)


class Calendar(unittest.TestCase):
    def test_ist_conversion(self):
        self.assertEqual(entry.to_ist(DT(2026, 11, 17, 4, 15)), DT(2026, 11, 17, 9, 45))
        self.assertEqual(entry.ist_to_utc(DT(2026, 11, 17, 9, 15)), DT(2026, 11, 17, 3, 45))
        self.assertEqual(entry.to_ist(DT(2026, 11, 16, 20, 0)), DT(2026, 11, 17, 1, 30))
        for text in ("2026-11-17 09:45", "2026-11-17T09:45", "2026-11-17T04:15:00Z", "2026-11-17 09:45 IST",
                     "2026-11-17T09:45:00+05:30"):
            self.assertEqual(entry.parse_now(text), DT(2026, 11, 17, 4, 15), text)
        # no daylight saving: June and December have the same offset
        self.assertEqual(entry.parse_now("2026-06-15 09:45"), DT(2026, 6, 15, 4, 15))
        self.assertEqual(entry.parse_now("2026-12-15 09:45"), DT(2026, 12, 15, 4, 15))

    def test_the_same_session_on_other_dates(self):
        for day in (datetime.date(2026, 6, 15), datetime.date(2026, 12, 7), datetime.date(2026, 3, 9)):
            d = shift_to(doc(), day)
            r = entry.evaluate(chart(d), ist("%s 09:45" % day))
            self.assertEqual((r["status"], r["trigger"]["time"], r["trigger"]["bar_index"]),
                             ("triggered", "9:45 AM", 5), day)
            self.assertEqual(entry.evaluate(chart(d), ist("%s 09:44" % day))["status"], "waiting", day)

    def test_weekend_and_holidays(self):
        r = at("2026-11-15 10:30")                                  # Sunday
        self.assertEqual((r["status"], r["phase"]), ("no-data", "closed"))
        self.assertEqual(r["next_open"], "2026-11-16 09:15")
        r = at("2026-11-24 10:30")
        self.assertIn("NSE holiday: Prakash Gurpurb Sri Guru Nanak Dev", r["reason"])
        self.assertEqual(r["next_open"], "2026-11-25 09:15")
        self.assertIn("Diwali", at("2026-11-10 10:30")["reason"])
        self.assertIn("Maharashtra municipal elections", at("2026-01-15 10:30")["reason"])
        self.assertEqual(at("2026-12-25 10:30")["next_open"], "2026-12-28 09:15")
        self.assertEqual(len(entry.NSE_HOLIDAYS), 16)
        self.assertTrue(all(datetime.date.fromisoformat(k).weekday() < 5 for k in entry.NSE_HOLIDAYS))
        text = "\n".join(entry.describe(at("2026-11-24 10:30")))
        self.assertIn("MARKET CLOSED", text)
        self.assertIn("Next open: 2026-11-25 09:15 IST.", text)

    def test_config_holidays(self):
        cfg = {"no_entries_before": "09:30", "no_entries_after": "14:30", "holidays": ["2026-11-17"]}
        r = at("2026-11-17 09:45", cfg=cfg)
        self.assertEqual(r["phase"], "closed")
        self.assertIn("a holiday in your config", r["reason"])
        self.assertEqual(r["next_open"], "2026-11-18 09:15")

    def test_a_2027_date_warns_and_does_not_raise(self):
        d = shift_to(doc(), datetime.date(2027, 1, 26))             # a weekday; 2027's list is not loaded
        r = entry.evaluate(chart(d), ist("2027-01-26 09:45"))
        self.assertEqual((r["status"], r["trigger"]["time"]), ("triggered", "9:45 AM"))
        self.assertFalse(r["calendar_known"])
        self.assertIn("NSE holiday list for 2027 not loaded: check nseindia.com.", entry.describe(r))
        r = at("2027-01-30 10:00")                                  # a Saturday is still a weekend
        self.assertEqual(r["phase"], "closed")
        self.assertNotIn("NSE holiday list", "\n".join(entry.describe(at("2026-11-17 09:45"))))

    def test_empty_data_and_yesterdays_bars(self):
        empty = copy.deepcopy(doc())
        res = empty["chart"]["result"][0]
        del res["timestamp"]
        res["indicators"] = {"quote": [{}]}
        r = at("2026-11-17 09:45", empty)
        self.assertEqual((r["status"], r["phase"]), ("no-data", "open"))
        r = entry.evaluate(chart(doc(SBIN_1116)), ist("2026-11-17 10:15"))   # Yahoo sent yesterday's bars only
        self.assertEqual(r["status"], "no-data")

    def test_yahoo_errors(self):
        bad = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                              "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(entry.EntryError) as cm:
            entry.parse_chart(bad, "ZZZZ")
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(entry.EntryError):
            entry.parse_chart("<html>", "SBIN")


class SymbolsAndMoney(unittest.TestCase):
    def test_nse_symbols(self):
        self.assertEqual(entry.nse_symbol("m&m"), "M&M")
        self.assertEqual(entry.nse_symbol(" sbin.ns "), "SBIN")
        self.assertEqual(entry.nse_symbol("BAJAJ-AUTO.NS"), "BAJAJ-AUTO")
        self.assertEqual(entry.nse_symbol("20MICRONS"), "20MICRONS")
        for bad in ("^NSEI", "^nsebank", "BRK.B", "RELIANCE.BO", "", "-ABC", "A" * 21):
            with self.assertRaises(entry.EntryError):
                entry.nse_symbol(bad)

    def test_yahoo_encoding(self):
        self.assertEqual(entry.yahoo_symbol("M&M"), "M&M.NS")
        self.assertEqual(entry.chart_path("m&m", "1d", "5m"), "/v8/finance/chart/M%26M.NS?range=1d&interval=5m")
        self.assertEqual(entry.chart_path("BAJAJ-AUTO", "5d", "1m"),
                         "/v8/finance/chart/BAJAJ-AUTO.NS?range=5d&interval=1m")
        self.assertEqual(entry.chart_path("RELIANCE.NS", "1d", "5m"),
                         "/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m")

    def test_m_and_m_and_bajaj_auto_charts(self):
        c = chart(doc(MM), "M&M")
        self.assertEqual((c["symbol"], c["yahoo_symbol"]), ("M&M", "M&M.NS"))
        r = entry.evaluate(c, ist("2026-11-17 09:45"))
        self.assertEqual((r["trigger"]["time"], r["trigger"]["price"], r["level"]), ("9:35 AM", 3392.0, 3389.1))
        c = chart(doc(BAJAJ), "BAJAJ-AUTO")
        self.assertEqual(c["symbol"], "BAJAJ-AUTO")
        r = entry.evaluate(c, ist("2026-11-17 09:44"))
        self.assertEqual(r["status"], "waiting")
        self.assertIn("₹10,795.00", r["reason"])

    def test_inr(self):
        self.assertEqual(entry.inr(123456.5), "₹1,23,456.50")
        self.assertEqual(entry.inr(-1250), "-₹1,250.00")
        self.assertEqual(entry.inr(0), "₹0.00")
        self.assertEqual(entry.inr(999.95), "₹999.95")
        self.assertEqual(entry.inr(1105.699951171875), "₹1,105.70")
        self.assertEqual(entry.inr(12345678.9), "₹1,23,45,678.90")
        self.assertEqual(entry.inr(100000), "₹1,00,000.00")


class Network(unittest.TestCase):
    """Yahoo is never called: _open_url and _sleep are swapped for fakes."""

    def setUp(self):
        self.calls, self.sleeps = [], []
        self._open, self._sleep = entry._open_url, entry._sleep
        entry._sleep = lambda s: self.sleeps.append(s)
        self.body = {}
        for path, sym in ((SBIN, "SBIN"), (MM, "M&M"), (BAJAJ, "BAJAJ-AUTO")):
            with open(path, encoding="utf-8") as fh:
                self.body[sym] = fh.read()

    def tearDown(self):
        entry._open_url, entry._sleep = self._open, self._sleep

    def answers(self, *codes, sym="SBIN"):
        seq = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url)
            code = seq.pop(0)
            if code == 200:
                return self.body[sym]
            raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b'{"chart":{"result":null,'
                                                                           b'"error":{"description":"No data found"}}}'))
        entry._open_url = fake

    def get(self, sym="SBIN"):
        return entry.get_chart(sym, "5m", datetime.date(2026, 11, 17), today=datetime.date(2026, 11, 17))

    def test_url_and_429_retry_once_after_a_pause(self):
        self.answers(429, 200)
        c = self.get()
        self.assertEqual(len(c["bars"]), 8)
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com/v8/finance/chart/SBIN.NS?range=1d&interval=5m"] * 2)
        self.assertEqual(self.sleeps, [entry.BACKOFF])

    def test_m_and_m_is_encoded_in_the_url(self):
        self.answers(200, sym="M&M")
        c = self.get("M&M")
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com/v8/finance/chart/M%26M.NS?range=1d&interval=5m"])
        self.assertEqual(c["symbol"], "M&M")
        self.answers(200, sym="BAJAJ-AUTO")
        self.get("bajaj-auto.ns")
        self.assertIn("/chart/BAJAJ-AUTO.NS?", self.calls[-1])

    def test_429_twice_falls_back_to_query1(self):
        self.answers(429, 429, 200)
        self.get()
        self.assertEqual([u.split("/")[2] for u in self.calls],
                         ["query2.finance.yahoo.com", "query2.finance.yahoo.com", "query1.finance.yahoo.com"])

    def test_5xx_falls_back_without_waiting(self):
        self.answers(503, 200)
        self.get()
        self.assertEqual(self.sleeps, [])
        self.assertIn("query1", self.calls[1])

    def test_still_busy_is_one_plain_error(self):
        self.answers(429, 429, 429, 429)
        code, out, err = run(["check", "SBIN", "--now", "2026-11-17 09:45"])
        self.assertEqual(code, 1)
        self.assertIn("HTTP 429", err)
        self.assertNotIn("Traceback", err)

    def test_unknown_symbol_404(self):
        self.answers(404)
        with self.assertRaises(entry.EntryError) as cm:
            self.get("ZZZZ")
        self.assertIn("No data found", str(cm.exception))

    def test_ranges_for_past_sessions(self):
        today = datetime.date(2026, 11, 17)
        self.assertEqual(entry.chart_range(today, today, "5m"), "1d")
        self.assertEqual(entry.chart_range(datetime.date(2026, 11, 16), today, "5m"), "5d")
        self.assertEqual(entry.chart_range(datetime.date(2026, 10, 27), today, "5m"), "1mo")
        self.assertEqual(entry.chart_range(datetime.date(2026, 9, 25), today, "5m"), "60d")
        with self.assertRaises(entry.EntryError):
            entry.chart_range(datetime.date(2026, 11, 2), today, "1m")


class HonestAgentAndTls(unittest.TestCase):
    def setUp(self):
        self._urlopen = urllib.request.urlopen

    def tearDown(self):
        urllib.request.urlopen = self._urlopen

    def test_user_agent_and_a_verifying_context(self):
        seen = {}

        class Resp(object):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"{}"

        def fake(req, timeout=None, context=None):
            seen.update(ua=req.get_header("User-agent"), ctx=context, url=req.full_url)
            return Resp()
        urllib.request.urlopen = fake
        entry._open_url("https://query2.finance.yahoo.com/v8/finance/chart/SBIN.NS?range=1d&interval=5m")
        self.assertEqual(seen["ua"], "Mozilla/5.0 (compatible; nse-day-trader/1.0)")
        ctx = seen["ctx"]
        self.assertIsInstance(ctx, ssl.SSLContext)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)
        if os.path.exists("/etc/ssl/cert.pem"):
            self.assertTrue(ctx.get_ca_certs() or ctx.cert_store_stats().get("x509_ca"))


def live_provider(d, lag_until=None, calls=None, spike=1102.0):
    """Bars the way Yahoo serves them at `now`: closed bars final, the in-progress bar
    showing a spike above the range high (it closes lower), plus a live tick.
    lag_until: before this UTC time Yahoo's last trade lags 30 seconds behind."""
    base = chart(d)

    def provider(now):
        if calls is not None:
            calls.append(now)
        nts = entry.ts_of(now)
        lagging = lag_until is not None and now < lag_until
        data_ts = nts - 30 if lagging else nts
        bars = []
        for b in base["bars"]:
            if b["t"] % 300 or b["t"] > data_ts:
                continue
            b = dict(b)
            if b["t"] + 300 > data_ts:
                b["c"], b["h"] = spike, max(b["h"], spike)
            bars.append(b)
        if data_ts % 300:
            bars.append({"t": data_ts, "o": spike, "h": spike, "l": spike, "c": spike, "v": 0})
        return {"symbol": base["symbol"], "meta": {}, "bars": bars, "prev_close": base["prev_close"],
                "data_time": data_ts}
    return provider


class Watch(unittest.TestCase):
    OPTS = {"setup": "orb", "or_minutes": 15, "interval": "5m", "level": None, "min_volume_x": None, "cfg": None}

    def loop(self, provider, start, opts=None, **kw):
        clock = entry.SimClock(ist(start))
        lines, fired = [], []
        code, res = entry.watch_loop(provider, clock.now, clock.sleep, lines.append, dict(opts or self.OPTS),
                                     on_trigger=lambda r, now: fired.append((r, now)), **kw)
        return code, res, lines, fired, clock

    def test_fires_on_the_first_closed_bar_not_the_spike_in_progress(self):
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(doc(), calls=calls), "2026-11-17 09:37", every=60)
        self.assertEqual(code, 0)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][0]["trigger"]["time"], "9:45 AM")
        self.assertEqual(fired[0][0]["trigger"]["price"], 1105.7)
        # it looked at 09:37 (9:35 bar spiking to 1,102 in progress) and at 09:40:02 (that bar closed AT the high)
        self.assertEqual([entry.to_ist(c).strftime("%H:%M:%S") for c in calls], ["09:37:00", "09:40:02", "09:45:02"])
        text = "\n".join(lines)
        self.assertIn(" 9:30 bar  closed ₹1,098.50  ₹2.90 under ₹1,101.40", text)
        self.assertIn(" 9:35 bar  closed ₹1,101.40  AT ₹1,101.40, not above it", text)
        self.assertIn(" 9:40 bar  closed ₹1,105.70  TRIGGERED", text)
        self.assertEqual(text.count("TRIGGERED"), 1)

    def test_waits_for_yahoo_to_catch_up(self):
        lag = ist("2026-11-17 09:45:30")
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(doc(), lag_until=lag, calls=calls),
                                                   "2026-11-17 09:42", every=30)
        self.assertEqual(code, 0)
        times = [entry.to_ist(c).strftime("%H:%M:%S") for c in calls]
        self.assertEqual(times, ["09:42:00", "09:45:02", "09:45:32"])   # 09:45:02: Yahoo still at 09:44:32
        self.assertEqual(fired[0][0]["trigger"]["time"], "9:45 AM")

    def test_from_before_the_open_prints_the_range_bar_by_bar(self):
        code, res, lines, fired, clock = self.loop(live_provider(doc()), "2026-11-17 09:00", every=30)
        self.assertEqual(code, 0)
        text = "\n".join(lines)
        self.assertIn("not open yet", lines[0])
        self.assertIn("IST", lines[0])
        self.assertIn(" 9:25 bar  closed ₹1,095.05  opening range set: high ₹1,101.40, low ₹1,092.00", text)
        self.assertEqual(sum(1 for x in lines if " bar  closed " in x), 6)    # 9:15 ... 9:40, one line each

    def test_a_computer_that_dozes_off_catches_up_when_it_wakes(self):
        class Dozing(entry.SimClock):
            naps = 0

            def sleep(self, seconds):
                entry.SimClock.sleep(self, seconds)
                self.naps += 1
                if self.naps == 3:                      # the laptop sleeps for 17 minutes mid-wait
                    self.t += datetime.timedelta(minutes=17)
        clock = Dozing(ist("2026-11-17 09:31"))
        calls, lines = [], []
        code, res = entry.watch_loop(live_provider(doc(), calls=calls), clock.now, clock.sleep, lines.append,
                                     dict(self.OPTS), every=30)
        self.assertEqual(code, 0)
        times = [entry.to_ist(c).strftime("%H:%M:%S") for c in calls]
        self.assertEqual(times[:2], ["09:31:00", "09:49:30"])   # straight after waking, not a long sleep later
        text = "\n".join(lines)
        for start in (" 9:30", " 9:35", " 9:40"):
            self.assertIn(start + " bar  closed", text)          # the bars it slept through, one line each
        self.assertEqual(res["trigger"]["time"], "9:45 AM")

    def test_expires_at_the_cutoff_and_until(self):
        d = doc(SBIN_1116)
        code, res, lines, fired, clock = self.loop(live_provider(d, spike=1099.0), "2026-11-16 14:00", every=300)
        self.assertEqual(code, 1)
        self.assertEqual(res["status"], "expired")
        self.assertEqual(fired, [])
        self.assertEqual(entry.to_ist(clock.now()).strftime("%H:%M"), "14:30")
        until = DT(2026, 11, 16, 11, 0)
        code, res, lines, fired, clock = self.loop(live_provider(d, spike=1099.0), "2026-11-16 10:30", every=300,
                                                   until=until)
        self.assertEqual(code, 1)
        self.assertIn("--until 11:00", lines[-1])

    def test_closed_day_2027_note_and_repeated_errors(self):
        code, _, lines, _, _ = self.loop(live_provider(doc()), "2026-11-15 10:00")
        self.assertEqual(code, 1)
        self.assertIn("weekend", lines[0])
        code, _, lines, _, _ = self.loop(live_provider(doc()), "2026-11-24 10:00")
        self.assertIn("NSE holiday: Prakash Gurpurb Sri Guru Nanak Dev", lines[0])
        d = shift_to(doc(), datetime.date(2027, 3, 2))
        code, res, lines, fired, _ = self.loop(live_provider(d), "2027-03-02 09:42", every=60)
        self.assertEqual(code, 0)
        self.assertEqual(lines[0], "NSE holiday list for 2027 not loaded: check nseindia.com.")
        self.assertEqual(sum(1 for x in lines if x.startswith("NSE holiday list")), 1)

        def broken(now):
            raise entry.EntryError("Yahoo did not answer")
        code, _, lines, _, _ = self.loop(broken, "2026-11-17 10:00", every=30)
        self.assertEqual(code, 1)
        self.assertIn("Stopping after 5 failed reads", lines[-1])


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_check_write_is_the_contract_shape(self):
        code, text, _ = run(["check", "SBIN", "--offline", SBIN, "--now", "2026-11-17 09:45", "--write"])
        self.assertEqual(code, 0)
        self.assertIn("TRIGGERED 9:45 AM at ₹1,105.70", text)
        self.assertIn("/nse-exit", text)
        with open(os.path.join("trader", "entry.json"), encoding="utf-8") as fh:
            e = json.load(fh)
        want = {"created_by": "nse-entry", "symbol": "SBIN", "setup": "orb", "side": "buy", "date": "2026-11-17",
                "status": "triggered", "time": "9:45 AM", "bar_start": "09:40", "bar_index": 5, "price": 1105.7,
                "level": 1101.4, "interval": "5m", "opening_range": {"high": 1101.4, "low": 1092.0, "minutes": 15},
                "source": "yahoo", "checked_at": "2026-11-17T04:15:00Z"}
        for k, v in want.items():
            self.assertEqual(e[k], v, k)
        self.assertEqual(sorted(e), sorted(list(want) + ["reason"]))

    def test_check_waiting_json_and_exit_codes(self):
        code, text, _ = run(["check", "SBIN", "--offline", SBIN, "--now", "2026-11-17 09:44", "--json"])
        self.assertEqual(code, 1)
        r = json.loads(text)
        self.assertEqual(r["status"], "waiting")
        self.assertNotIn("_bars", r)
        code, text, _ = run(["check", "SBIN", "--offline", SBIN, "--now", "2026-11-17 09:44", "--write", "e.json"])
        with open("e.json", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["status"], "waiting")
        self.assertIn("still forming", text)

    def test_offline_gz_folder_symbols_and_mismatch(self):
        os.mkdir("charts")
        with open(SBIN, "rb") as src, gzip.open(os.path.join("charts", "SBIN.json.gz"), "wb") as dst:
            dst.write(src.read())
        shutil.copy(MM, os.path.join("charts", "M&M.json"))
        code, text, _ = run(["check", "sbin.ns", "--offline", "charts", "--now", "2026-11-17 09:45"])
        self.assertEqual(code, 0)
        code, text, _ = run(["check", "m&m", "--offline", "charts", "--now", "2026-11-17 09:45"])
        self.assertEqual(code, 0)
        self.assertIn("M&M  Tue 2026-11-17 09:45 IST", text)
        self.assertIn("TRIGGERED 9:35 AM at ₹3,392.00", text)
        code, _, err = run(["check", "RELIANCE", "--offline", SBIN])
        self.assertEqual(code, 1)
        self.assertIn("holds SBIN.NS, not RELIANCE.NS", err)
        code, _, err = run(["check", "^NSEI", "--offline", SBIN])
        self.assertIn("index", err)

    def test_the_60_day_file_replays_the_reference_session(self):
        code, text, _ = run(["check", "SBIN", "--offline", SBIN_60D, "--now", "2026-11-16 14:50"])
        self.assertEqual(code, 1)
        self.assertIn("EXPIRED  no close above the range high ₹1,099.50 by 14:30", text)
        code, text, _ = run(["check", "SBIN", "--offline", SBIN_60D])          # as of the file's own time
        self.assertEqual(code, 0)
        self.assertIn("TRIGGERED 9:45 AM at ₹1,105.70", text)

    def test_longs_only_and_flag_errors(self):
        code, _, err = run(["check", "SBIN", "--offline", SBIN, "--side", "short"])
        self.assertEqual(code, 1)
        self.assertIn("longs only", err)
        code, _, err = run(["check", "SBIN", "--offline", SBIN, "--level", "300"])
        self.assertIn("--level is for --setup level", err)
        code, _, err = run(["check", "SBIN", "--offline", SBIN, "--setup", "level"])
        self.assertIn("needs --level", err)

    def test_config_is_read(self):
        os.mkdir("trader")
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump({"no_entries_after": "09:40"}, fh)
        code, text, _ = run(["check", "SBIN", "--offline", SBIN, "--now", "2026-11-17 09:45"])
        self.assertEqual(code, 1)
        self.assertIn("EXPIRED", text)
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump({"holidays": ["2026-11-17"]}, fh)
        code, text, _ = run(["check", "SBIN", "--offline", SBIN, "--now", "2026-11-17 09:45"])
        self.assertIn("a holiday in your config", text)
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump({"holidays": ["17-11-2026"]}, fh)
        code, _, err = run(["check", "SBIN", "--offline", SBIN])
        self.assertIn("YYYY-MM-DD", err)
        with open(os.path.join("trader", "config.json"), "w") as fh:
            fh.write("{nope")
        code, _, err = run(["check", "SBIN", "--offline", SBIN])
        self.assertIn("not valid JSON", err)

    def test_watch_replays_the_file_and_writes_the_entry(self):
        code, text, _ = run(["watch", "SBIN", "--offline", SBIN, "--now", "2026-11-17 09:31", "--every", "30"])
        self.assertEqual(code, 0)
        self.assertIn(" 9:40 bar  closed ₹1,105.70  TRIGGERED", text)
        with open(os.path.join("trader", "entry.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["bar_index"], 5)
        code, text, _ = run(["watch", "SBIN", "--offline", SBIN, "--now", "2026-11-17 09:31", "--until", "09:40",
                             "--out", "x.json"])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists("x.json"))

    def test_watchlist(self):
        os.mkdir("charts")
        shutil.copy(SBIN, os.path.join("charts", "SBIN.json"))
        shutil.copy(MM, os.path.join("charts", "M&M.json"))
        shutil.copy(BAJAJ, os.path.join("charts", "BAJAJ-AUTO.json"))
        os.mkdir("trader")
        with open(os.path.join("trader", "watchlist.json"), "w") as fh:
            json.dump({"created_by": "nse-scanner", "hits": [
                {"symbol": "SBIN", "setups": ["orb"]}, {"symbol": "ZZZZ", "setups": ["orb"]},
                {"symbol": "M&M", "setups": ["orb"]}, {"symbol": "BAJAJ-AUTO.NS", "setups": ["orb"]}]}, fh)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-11-17 09:44"])
        self.assertEqual(code, 0)
        rows = text.splitlines()
        self.assertTrue(rows[1].startswith("SBIN        waiting    -         -           ₹1,101.40   ₹1,101.40   +0.00%"), rows[1])
        self.assertIn("ZZZZ        error: no offline file", rows[2])
        self.assertTrue(rows[3].startswith("M&M         triggered  9:35 AM   ₹3,392.00   ₹3,389.10"), rows[3])
        self.assertTrue(rows[4].startswith("BAJAJ-AUTO  waiting"), rows[4])
        self.assertIn("1 of 4 triggered", text)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-11-17 09:45", "--json"])
        self.assertEqual([x["result"]["status"] if x["result"] else x["error"][:5] for x in json.loads(text)],
                         ["triggered", "no of", "triggered", "triggered"])
        code, _, err = run(["watchlist", "--watchlist", "missing.json"])
        self.assertIn("run /nse-scanner first", err)


def section(text, title):
    start = text.index(title)
    nxt = text.find("\n## ", start + 1)
    return text[start:] if nxt < 0 else text[start:nxt]


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "nse-entry", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "nse-exit", "SKILL.md"), encoding="utf-8") as fh:
            other = fh.read()
        self.assertTrue(text.startswith("---\nname: nse-entry\n"))
        self.assertGreater(len(text.splitlines()), 60)
        self.assertEqual(section(text, "## Safety"), section(other, "## Safety"))
        low = " ".join(text.lower().split())
        for must in ("bar by bar", "can lag", "not financial advice", "71%", "80%", "sebi", "/nse-exit", "longs only",
                     "in the background", "/loop", "nothing was placed", "9:45 am", "₹1,105.70", "bar 5", "ist",
                     "09:30", "14:30", "groww", "nifty 500", "intraday", "\"m&m\"", "nseindia.com"):
            self.assertIn(must, low)
        for never in (chr(0x2014), "tick " + "by tick", "real " + "time", "pro" + "fit", "nyse", " et ",
                      "new york", "trader-"):
            self.assertNotIn(never, low)
        self.assertNotRegex(text, r"\$(?!\{CLAUDE_SKILL_DIR\})")         # rupees only; the skill-dir variable stays


if __name__ == "__main__":
    unittest.main()
