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

from _load import load, FIX

scan = load("nse-scanner", "scan")

NSE = os.path.join(FIX, "nse")                  # shared live fixtures, 2026-11-17 09:48 IST (read-only)
SDIR = os.path.join(FIX, "nse-scanner")         # this skill's own live captures, 2026-11-17
N500 = os.path.join(NSE, "nifty500list.csv")    # niftyindices.com, 501 rows
EQL = os.path.join(NSE, "EQUITY_L.csv")         # nsearchives.nseindia.com, 2,633 rows
SPARK5 = os.path.join(NSE, "spark-1d-5m.json")  # real 5-minute spark, 8 symbols, fetched 9:48:38 AM IST
SPARK3MO = os.path.join(NSE, "spark-3mo-1d.json")             # real 3-month daily spark, same 8 symbols
WALL = os.path.join(SDIR, "wall5m-2026-11-16.json.gz")        # 40 real v8 charts of the reference session
DAILY = os.path.join(SDIR, "daily-2026-11-17")                # two real 3-month spark answers, 40 symbols
LIVE = os.path.join(SDIR, "orb-live-2026-11-17.json.gz")      # a live orb run's answers for those 40
REF = datetime.date(2026, 11, 16)                             # the reference full session (Monday)
UA = "Mozilla/5.0 (compatible; nse-day-trader/1.0)"
DT = datetime.datetime
D = datetime.date


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


def ts(ist_text):
    """IST wall-clock text -> unix seconds."""
    utc = scan.ist_to_utc(DT.strptime(ist_text, "%Y-%m-%d %H:%M:%S" if ist_text.count(":") == 2
                                      else "%Y-%m-%d %H:%M"))
    return int((utc - DT(1970, 1, 1)).total_seconds())


def bars_series(day, closes, highs=None, lows=None, start="09:15", extra=None):
    """A v8-like series of 5-min bars from `start` on `day` (YYYY-MM-DD)."""
    t0 = DT.strptime(day + " " + start, "%Y-%m-%d %H:%M")
    t = [ts((t0 + datetime.timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M")) for i in range(len(closes))]
    s = {"t": t, "c": list(closes), "h": list(highs or [c + 0.1 if c is not None else None for c in closes]),
         "l": list(lows or [c - 0.1 if c is not None else None for c in closes]), "o": None,
         "v": [1000] * len(closes), "meta": {}}
    for when, c in (extra or []):
        s["t"].append(ts(when))
        for k in ("c", "h", "l"):
            s[k].append(c)
        s["v"].append(10)
    return s


def read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def v8_file(sym):
    return scan.parse_chart(read_text(os.path.join(NSE, sym + "-1d-5m.json")), sym)


class Universe(unittest.TestCase):
    def setUp(self):
        self.n500, self.n500_counts = scan.build_universe(read_text(N500))
        self.all, self.all_counts = scan.build_universe(read_text(EQL))

    def test_nifty_500(self):
        syms = [s for s, _ in self.n500]
        self.assertEqual(len(syms), 495)
        self.assertEqual(self.n500_counts, {"rows": 501, "keep": 495, "series RR": 3, "series BE": 2,
                                            "placeholder": 1})
        names = dict(self.n500)
        self.assertEqual(names["M&M"], "Mahindra & Mahindra Ltd.")
        self.assertEqual(names["BAJAJ-AUTO"], "Bajaj Auto Ltd.")
        for sym in ("RELIANCE", "SBIN", "360ONE", "J&KBANK", "ARE&M", "NAM-INDIA", "M&MFIN"):
            self.assertIn(sym, names)
        for sym in ("EMBASSY", "BIRET", "BAGMANE",      # REITs, series RR
                    "HFCL", "STLTECH",                  # series BE: trade-for-trade, no intraday
                    "DUMMYHEG"):                        # a placeholder row with a DUM... ISIN
            self.assertNotIn(sym, names)
        self.assertEqual(syms, sorted(syms))

    def test_every_eq_stock(self):
        self.assertEqual(len(self.all), 2404)
        self.assertEqual(self.all_counts, {"rows": 2633, "keep": 2404, "series BE": 195, "series BZ": 34})
        names = dict(self.all)
        self.assertEqual(names["M&M"], "Mahindra & Mahindra Limited")
        self.assertIn("20MICRONS", names)
        self.assertNotIn("21STCENMGM", names)           # series BE
        self.assertTrue(set(s for s, _ in self.n500) <= set(names))   # every kept Nifty 500 stock is EQ there

    def test_one_case_per_rule(self):
        def row(symbol="ABC", series="EQ", name="Abc Ltd.", isin="INE000A01010"):
            return {"symbol": symbol, "series": series, "name": name, "isin": isin}
        self.assertEqual(scan.classify(row()), "keep")
        self.assertEqual(scan.classify(row(symbol="M&M")), "keep")
        self.assertEqual(scan.classify(row(symbol="BAJAJ-AUTO")), "keep")
        self.assertEqual(scan.classify(row(series="BE")), "series BE")
        self.assertEqual(scan.classify(row(series="BZ")), "series BZ")
        self.assertEqual(scan.classify(row(series="RR")), "series RR")
        self.assertEqual(scan.classify(row(series="")), "series blank")
        self.assertEqual(scan.classify(row(isin="DUM545A01024")), "placeholder")
        self.assertEqual(scan.classify(row(name="Dummy Something Ltd.")), "placeholder")
        self.assertEqual(scan.classify(row(symbol="^NSEI")), "not an NSE symbol")
        self.assertEqual(scan.classify(row(symbol="ABC.NS")), "not an NSE symbol")
        self.assertEqual(scan.classify(row(symbol="-ABC")), "not an NSE symbol")

    def test_header_spaces_bom_and_quotes(self):
        text = ("﻿SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING\n"
                "M&M,\"Mahindra & Mahindra, Limited\",EQ,03-JAN-1996\n\n"
                "XYZ,Xyz Limited,BE,01-JAN-2000\n")
        kept, counts = scan.build_universe(text)
        self.assertEqual(kept, [("M&M", "Mahindra & Mahindra, Limited")])
        self.assertEqual(counts, {"rows": 2, "keep": 1, "series BE": 1})

    def test_not_a_symbol_list(self):
        with self.assertRaises(scan.ScanError):
            scan.parse_list("<html>Access Denied</html>")
        with self.assertRaises(scan.ScanError):
            scan.parse_list("")


class Time(unittest.TestCase):
    def test_ist_conversion(self):
        # HDFCBANK's meta on 2026-11-17: the regular session starts at 1794887100 and ends at 1794909600
        self.assertEqual(scan.ts_to_ist(1794887100), DT(2026, 11, 17, 9, 15))
        self.assertEqual(scan.ts_to_ist(1794909600), DT(2026, 11, 17, 15, 30))
        self.assertEqual(scan.to_ist(DT(2026, 11, 17, 3, 45)), DT(2026, 11, 17, 9, 15))
        self.assertEqual(scan.ist_to_utc(DT(2026, 11, 17, 9, 15)), DT(2026, 11, 17, 3, 45))
        self.assertEqual(scan.to_ist(DT(2026, 11, 16, 20, 0)), DT(2026, 11, 17, 1, 30))   # crosses midnight
        for month in (1, 3, 7, 11):                      # no daylight saving, ever
            self.assertEqual(scan.to_ist(DT(2026, month, 10, 0, 0)), DT(2026, month, 10, 5, 30))
        self.assertEqual(scan.parse_now("2026-11-17 09:15"), DT(2026, 11, 17, 3, 45))
        self.assertEqual(scan.parse_now("2026-11-17T04:15:00Z"), DT(2026, 11, 17, 4, 15))
        self.assertEqual(scan.parse_now("2026-11-17T09:45:00+05:30"), DT(2026, 11, 17, 4, 15))
        self.assertEqual(scan.clock_label(DT(2026, 11, 17, 9, 35)), "9:35 AM")
        self.assertEqual(scan.clock_label(datetime.time(12, 5)), "12:05 PM")
        self.assertEqual(scan.clock_label(datetime.time(15, 30)), "3:30 PM")
        with self.assertRaises(scan.ScanError):
            scan.parse_now("tomorrow")
        with self.assertRaises(scan.ScanError):
            scan.parse_hhmm("25:00")
        self.assertEqual(scan.SESSION_BARS, 75)

    def test_holidays(self):
        self.assertEqual(scan.CALENDAR_YEARS, (2026,))
        want = ["2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03", "2026-04-14",
                "2026-05-01", "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10",
                "2026-11-24", "2026-12-25"]
        self.assertEqual(sorted(scan.NSE_HOLIDAYS), want)
        for iso in want:
            self.assertLess(D.fromisoformat(iso).weekday(), 5, iso)
        self.assertEqual(scan.trading_day(D(2026, 11, 24)), (False, "NSE holiday: Guru Nanak Jayanti"))
        self.assertEqual(scan.trading_day(D(2026, 11, 21)), (False, "weekend"))
        self.assertEqual(scan.trading_day(D(2026, 11, 17)), (True, ""))
        self.assertEqual(scan.trading_day(D(2026, 11, 18), {"2026-11-18"}),
                         (False, "a holiday in trader/config.json"))
        self.assertEqual(scan.config_holidays({"holidays": ["2026-11-18", " 2026-12-31 "]}),
                         {"2026-11-18", "2026-12-31"})
        with self.assertRaises(scan.ScanError):
            scan.config_holidays({"holidays": ["18/11/2026"]})

    def test_another_year_warns_and_trades(self):
        self.assertEqual(scan.trading_day(D(2027, 1, 26)), (True, ""))       # not loaded: a weekday trades
        self.assertEqual(scan.trading_day(D(2027, 1, 23)), (False, "weekend"))
        self.assertEqual(scan.calendar_warning(D(2027, 1, 26)),
                         "NSE holiday list for 2027 not loaded: check nseindia.com")
        self.assertIsNone(scan.calendar_warning(D(2026, 11, 17)))
        day, cut = scan.orb_window(scan.parse_now("2027-01-26 10:30"), None, 15, datetime.time(14, 45))
        self.assertEqual((day, cut), (D(2027, 1, 26), DT(2027, 1, 26, 10, 30)))


class Symbols(unittest.TestCase):
    def test_yahoo_encoding(self):
        self.assertEqual(scan.to_yahoo("M&M"), "M%26M.NS")
        self.assertEqual(scan.to_yahoo("bajaj-auto.ns"), "BAJAJ-AUTO.NS")
        self.assertEqual(scan.to_yahoo("J&KBANK"), "J%26KBANK.NS")
        self.assertEqual(scan.yahoo_symbol("M&M"), "M&M.NS")
        self.assertEqual(scan.from_yahoo("M&M.NS"), "M&M")
        self.assertEqual(scan.from_yahoo("M%26M.NS"), "M&M")
        self.assertEqual(scan.from_yahoo("BAJAJ-AUTO.NS"), "BAJAJ-AUTO")   # the dash stays a dash
        self.assertEqual(scan.spark_path(["M&M", "bajaj-auto", "RELIANCE.NS"], "1d", "5m"),
                         "/v7/finance/spark?symbols=M%26M.NS,BAJAJ-AUTO.NS,RELIANCE.NS&range=1d&interval=5m")
        self.assertEqual(scan.chart_path("M&M", "1d", "5m"), "/v8/finance/chart/M%26M.NS?range=1d&interval=5m")

    def test_user_input(self):
        class A:
            symbols, watchlist, universe, limit = " m&m, bajaj-auto.ns,RELIANCE  sbin,M&M", None, None, None
        self.assertEqual(scan.read_symbols(A()), ["M&M", "BAJAJ-AUTO", "RELIANCE", "SBIN"])
        A.symbols = "RELIANCE,^NSEI"
        with self.assertRaises(scan.ScanError) as cm:
            scan.read_symbols(A())
        self.assertIn("^NSEI", str(cm.exception))


class Money(unittest.TestCase):
    def test_inr(self):
        self.assertEqual(scan.inr(123456.5), "₹1,23,456.50")
        self.assertEqual(scan.inr(-1250), "-₹1,250.00")
        self.assertEqual(scan.inr(0), "₹0.00")
        self.assertEqual(scan.inr(999.995), "₹1,000.00")
        self.assertEqual(scan.inr(0.005), "₹0.01")
        self.assertEqual(scan.inr(12.57), "₹12.57")
        self.assertEqual(scan.inr(1e7), "₹1,00,00,000.00")
        self.assertEqual(scan.inr(-0.001), "₹0.00")
        self.assertEqual(scan.count(2404), "2,404")
        self.assertEqual(scan.count(150000), "1,50,000")
        self.assertEqual(scan.count(495), "495")
        self.assertEqual(scan.rupees_short(1e8), "₹10 crore")
        self.assertEqual(scan.rupees_short(2.5e7), "₹2.50 crore")
        self.assertEqual(scan.rupees_short(5e5), "₹5 lakh")
        self.assertEqual(scan.rupees_short(10), "₹10")
        self.assertEqual(scan._p(10799.0), "10,799.00")

    def test_printed_prices_are_yahoos_closes(self):
        # Yahoo's closes are float32 noise around a traded price in paise; round(c, 2) only strips the noise.
        # The scanner plans no order, so it rounds nothing to a tick.
        data = scan.parse_spark(read_text(SPARK5))
        n = 0
        for sym, s in data.items():
            for c in s["c"]:
                if c is not None:
                    self.assertLess(abs(round(c, 2) - c), 0.001, (sym, c))
                    n += 1
        self.assertEqual(n, 64)
        self.assertEqual(round(data["SBIN"]["c"][1], 2), 1097.75)   # a real traded price, 9:20 AM bar


class Spark(unittest.TestCase):
    def test_a_real_spark_answer(self):
        syms = ["RELIANCE", "HDFCBANK", "SBIN", "INFY", "ICICIBANK", "TCS", "M&M", "BAJAJ-AUTO", "ZZQXNOPE"]
        data = scan.parse_spark(read_text(SPARK3MO), {scan.yahoo_symbol(s): s for s in syms})
        self.assertEqual(sorted(data), sorted(syms[:-1]))  # Yahoo answers M&M.NS and BAJAJ-AUTO.NS: ours again
        self.assertEqual(data["M&M"]["meta"]["symbol"], "M&M.NS")
        self.assertEqual(sorted(scan.parse_spark(read_text(SPARK3MO))), sorted(syms[:-1]))  # without the map too
        r = data["RELIANCE"]
        self.assertEqual(len(r["t"]), 63)
        self.assertIsNone(r["h"])                  # spark is closes only
        self.assertEqual(r["c"][-1], 1342.2)
        self.assertEqual(r["meta"]["regularMarketVolume"], 835450)
        self.assertEqual(r["meta"]["exchangeTimezoneName"], "Asia/Kolkata")
        self.assertEqual(scan.daily_closes(r, scan.parse_now("2026-11-17 09:14"))[-1], (D(2026, 11, 16), 1344.8))
        # read during Tuesday's session: Tuesday's bar is still in progress
        self.assertEqual(scan.daily_closes(r, scan.parse_now("2026-11-17 12:00"))[-1][0], D(2026, 11, 16))
        self.assertEqual(scan.daily_closes(r, scan.parse_now("2026-11-17 15:30"))[-1], (D(2026, 11, 17), 1342.2))

    def test_holidays_and_skipped_sessions_in_real_data(self):
        data = scan.parse_spark(read_text(SPARK3MO))
        rel = [d for d, _ in scan.daily_closes(data["RELIANCE"], scan.parse_now("2026-11-17 09:00"))]
        mm = [d for d, _ in scan.daily_closes(data["M&M"], scan.parse_now("2026-11-17 09:00"))]
        for holiday in (D(2026, 10, 2), D(2026, 10, 20), D(2026, 11, 10)):   # no bar on an NSE holiday
            self.assertNotIn(holiday, rel)
        self.assertTrue(all(scan.trading_day(d)[0] for d in rel))
        self.assertIn(D(2026, 11, 12), rel)
        self.assertNotIn(D(2026, 11, 12), mm)      # Yahoo skipped a session of M&M: the rules use what comes back
        self.assertEqual((len(rel), len(mm)), (62, 61))

    def test_spark_errors(self):
        too_many = '{"spark":{"result":null,"error":{"code":"Bad Request",' \
                   '"description":"Number of symbols needs to be less than or equal to 20"}}}'  # 21 .NS, live
        with self.assertRaises(scan.ScanError) as cm:
            scan.parse_spark(too_many)
        self.assertIn("less than or equal to 20", str(cm.exception))
        self.assertEqual(scan.SPARK_MAX, 20)
        self.assertEqual(scan.parse_spark('{"spark":{"result":[],"error":null}}'), {})
        with self.assertRaises(scan.ScanError):
            scan.parse_spark('{"chart":{}}')
        with self.assertRaises(scan.ScanError) as cm:
            scan.parse_chart('{"chart":{"result":null,"error":{"code":"Not Found",'
                             '"description":"No data found, symbol may be delisted"}}}', "NOPE")
        self.assertIn("no data for NOPE", str(cm.exception))


class Network(unittest.TestCase):
    """429s and 5xx without the network."""

    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = scan.http_get, scan.SLEEP
        scan.SLEEP = self.sleeps.append
        self.body = read_text(SPARK5)

    def tearDown(self):
        scan.http_get, scan.SLEEP = self._get, self._sleep

    def answers(self, codes):
        codes = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url.split("/v")[0])
            code = codes.pop(0) if codes else 200
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return self.body
        scan.http_get = fake

    def test_429_falls_back_then_waits_once(self):
        self.answers([429, 429, 200])
        text = scan.yahoo_get("/v7/finance/spark?symbols=SBIN.NS&range=3mo&interval=1d")
        self.assertEqual(text, self.body)
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com",
                                      "https://query2.finance.yahoo.com"])
        self.assertEqual(self.sleeps, [scan.BACKOFF])

    def test_5xx_goes_to_query1_without_waiting(self):
        self.answers([503, 200])
        scan.yahoo_get("/v8/finance/chart/SBIN.NS?range=1d&interval=5m")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.sleeps, [])

    def test_429_for_good_is_reported_as_partial(self):
        self.answers([200] + [429] * 10)
        real = ["HDFCBANK", "INFY", "ICICIBANK", "RELIANCE", "TCS", "SBIN", "BAJAJ-AUTO", "M&M"]
        syms = real + ["ZZQA%02d" % i for i in range(12)] + ["ZZQB%02d" % i for i in range(20)]
        data, failed = scan.fetch_spark(syms, "3mo", "1d", 0.3)
        self.assertEqual(sorted(data), sorted(real))
        self.assertEqual(failed["ZZQA00"], "no data from Yahoo")
        self.assertEqual(len(failed), 32)
        self.assertIn("HTTP 429", failed["ZZQB00"])
        self.assertEqual(self.sleeps, [0.3, scan.BACKOFF])  # a pause between batches, one backoff
        hits, counts, _ = scan.scan_daily(data, syms, scan.parse_now("2026-11-17 09:00"), 10, 0)
        self.assertEqual((counts["requested"], counts["fetched"]), (40, 8))

    def test_a_404_is_final(self):
        self.answers([404])
        with self.assertRaises(scan.ScanError):
            scan.yahoo_get("/v8/finance/chart/NOPE.NS?range=1d&interval=5m")
        self.assertEqual(len(self.calls), 1)


class Https(unittest.TestCase):
    def test_honest_user_agent_and_verified_tls(self):
        seen = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"ok"

        def fake_urlopen(req, timeout=None, context=None):
            seen.update(req=req, timeout=timeout, context=context)
            return Resp()
        real = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            self.assertEqual(scan.http_get("https://query2.finance.yahoo.com/v8/finance/chart/M%26M.NS"), "ok")
        finally:
            urllib.request.urlopen = real
        self.assertEqual(seen["req"].get_header("User-agent"), UA)
        self.assertEqual(seen["req"].full_url, "https://query2.finance.yahoo.com/v8/finance/chart/M%26M.NS")
        ctx = seen["context"]
        self.assertIsInstance(ctx, ssl.SSLContext)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)  # never unverified
        self.assertTrue(ctx.check_hostname)

    def test_system_ca_file_when_python_ships_none(self):
        ctx = scan._ssl_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        if any(os.path.exists(p) for p in ("/etc/ssl/cert.pem", "/private/etc/ssl/cert.pem")):
            self.assertTrue(ctx.get_ca_certs() or ctx.cert_store_stats().get("x509_ca"))


class Daily(unittest.TestCase):
    # Hand-checked from the closes (shared fixture, closes through Mon 2026-11-16):
    EXPECT = {
        "HDFCBANK": ["breakout_20d", "bull_flag"],  # 816.50 > 812.30, the best close of the 20 before; up 13.8 %
                                                    # over the 10 sessions before the last 5, then a 2.25 % range
        "SBIN": ["breakout_20d"],                   # 1,090.50 > 1,086.50; up only 6.2 %, so no flag
        "ICICIBANK": ["bull_flag"],                 # 1,585.00 is under 1,590.00: no breakout; up 12.7 %, 1.16 % range
        "RELIANCE": ["breakout_20d", "bull_flag"],  # 1,344.80 > 1,339.70; up 10.9 %, 1.52 % range
    }

    def setUp(self):
        self.data, self.errors = scan.load_offline(SPARK3MO)
        self.syms = sorted(self.data)

    def test_setups_before_the_open(self):
        self.assertEqual(len(self.syms), 8)
        hits, counts, last = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-17 09:00"), 0, 0)
        self.assertEqual({h["symbol"]: h["setups"] for h in hits}, self.EXPECT)
        self.assertEqual([h["symbol"] for h in hits], ["HDFCBANK", "SBIN", "ICICIBANK", "RELIANCE"])  # most traded
        self.assertEqual(last, D(2026, 11, 16))
        self.assertEqual(counts, {"requested": 8, "fetched": 8, "illiquid": 0, "short": 0, "scanned": 8, "stale": 0,
                                  "hits": 4})
        rel = [h for h in hits if h["symbol"] == "RELIANCE"][0]
        self.assertEqual(rel, {"symbol": "RELIANCE", "setups": ["breakout_20d", "bull_flag"], "price": 1344.8,
                               "close_date": "2026-11-16", "rsi": 71.6, "volume": 835450})

    def test_the_in_progress_bar_is_dropped(self):
        before = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-17 09:00"), 0, 0)
        during = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-17 12:00"), 0, 0)
        self.assertEqual(before, during)
        bars = scan.daily_closes(self.data["SBIN"], scan.parse_now("2026-11-17 15:29"))
        self.assertEqual(bars[-1], (D(2026, 11, 16), 1090.5))
        bars = scan.daily_closes(self.data["SBIN"], scan.parse_now("2026-11-17 15:30"))
        self.assertEqual(bars[-1], (D(2026, 11, 17), 1106.35))

    def test_liquidity_filter(self):
        # meta is Yahoo's as of 9:48 AM Tuesday, so the volume is the day's so far
        now = scan.parse_now("2026-11-17 09:48")
        hits, counts, _ = scan.scan_daily(self.data, self.syms, now, 10, 0, 1e8)
        self.assertEqual((counts["illiquid"], counts["scanned"]), (0, 8))       # all over ₹10 crore by 9:48
        hits, counts, _ = scan.scan_daily(self.data, self.syms, now, 10, 0, 1e9)
        self.assertEqual(counts["illiquid"], 3)     # BAJAJ-AUTO ₹26.7 crore, INFY ₹86.8 crore, TCS ₹91.0 crore
        self.assertEqual(sorted(h["symbol"] for h in hits), ["HDFCBANK", "ICICIBANK", "RELIANCE", "SBIN"])
        hits, counts, _ = scan.scan_daily(self.data, self.syms, now, 10, 1000000)
        self.assertEqual(counts["scanned"], 3)      # shares: only HDFCBANK, ICICIBANK and SBIN have 10 lakh
        hits, counts, _ = scan.scan_daily(self.data, self.syms, now, 5000, 0)
        self.assertEqual(counts["scanned"], 1)      # BAJAJ-AUTO at ₹10,796 is the one over ₹5,000

    def test_short_and_empty_charts(self):
        s = {"t": [ts("2026-10-%02d 09:15" % d) for d in range(1, 11)], "c": [100.0] * 10,
             "meta": {"regularMarketPrice": 100, "regularMarketVolume": 5000000}}
        hits, counts, last = scan.scan_daily({"X": s, "E": {"t": [], "c": [], "meta": {}}}, ["X", "E", "GONE"],
                                             scan.parse_now("2026-11-17 09:00"), 10, 0, 1e8)
        self.assertEqual(hits, [])
        self.assertEqual((counts["fetched"], counts["short"], counts["illiquid"], counts["scanned"]), (2, 1, 1, 0))
        self.assertIsNone(last)

    def test_rsi_matches_wilder(self):
        self.assertIsNone(scan.rsi([1.0] * 10))
        self.assertEqual(scan.rsi([float(x) for x in range(1, 40)]), 100.0)
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61,
                  46.28, 46.28]
        self.assertAlmostEqual(scan.rsi(closes), 70.46, places=1)  # Wilder's textbook example


class LiveFiveMinute(unittest.TestCase):
    """The shared real 5-minute spark answer and six v8 charts, fetched at 9:48 AM IST on Tue 2026-11-17."""

    def setUp(self):
        self.spark = scan.parse_spark(read_text(SPARK5))
        self.v8 = {s: v8_file(s) for s in ("RELIANCE", "HDFCBANK", "SBIN", "INFY", "M&M", "BAJAJ-AUTO")}
        self.now = scan.parse_now("2026-11-17T04:18:40Z")       # 9:48:40 AM IST

    def test_in_progress_bar_and_live_tick_are_dropped(self):
        self.assertEqual(len(self.spark), 8)
        t = self.spark["M&M"]
        self.assertEqual(len(t["t"]), 8)                   # 9:15 ... 9:45 (in progress) + the live tick
        self.assertEqual(scan.ts_to_ist(t["t"][-1]).replace(second=0), DT(2026, 11, 17, 9, 48))
        self.assertTrue(scan.ts_to_ist(t["t"][-1]).second)  # off the grid
        day, cut = scan.orb_window(self.now, None, 15, datetime.time(14, 45))
        self.assertEqual((day, cut), (D(2026, 11, 17), DT(2026, 11, 17, 9, 48)))
        bars = scan.session_bars(t, day, cut)
        self.assertEqual([b["start"].strftime("%H:%M") for b in bars],
                         ["09:15", "09:20", "09:25", "09:30", "09:35", "09:40"])
        self.assertEqual(len(scan.session_bars(self.v8["M&M"], day, cut)), 6)

    def test_breakouts_by_948(self):
        day, cut = scan.orb_window(self.now, None, 15, datetime.time(14, 45))

        def confirm(sym):
            if sym not in self.v8:
                raise scan.ScanError("Yahoo answered HTTP 429 for /v8/finance/chart/%s" % scan.to_yahoo(sym))
            return self.v8[sym]
        hits, counts, failed = scan.scan_orb(self.spark, sorted(self.spark), day, cut, 15, 10, 0, confirm)
        # M&M: range 9:15 to 9:30, high 3,389.10 (the 9:25 bar), low 3,355.30; the 9:30 bar closed 3,392.00.
        # SBIN: high 1,101.40 (9:15); the 9:35 bar closed AT 1,101.40 (not above), the 9:40 bar at 1,105.70.
        # BAJAJ-AUTO: high 10,795 (9:15); the 9:40 bar closed 10,799.
        self.assertEqual(hits, [
            {"symbol": "M&M", "setups": ["orb"], "price": 3392.0, "at": "9:35 AM", "bar_start": "09:30",
             "bar_index": 3, "or_high": 3389.1, "or_low": 3355.3, "pct_over": 0.09},
            {"symbol": "BAJAJ-AUTO", "setups": ["orb"], "price": 10799.0, "at": "9:45 AM", "bar_start": "09:40",
             "bar_index": 5, "or_high": 10795.0, "or_low": 10709.0, "pct_over": 0.04},
            {"symbol": "SBIN", "setups": ["orb"], "price": 1105.7, "at": "9:45 AM", "bar_start": "09:40",
             "bar_index": 5, "or_high": 1101.4, "or_low": 1092.0, "pct_over": 0.39}])
        # INFY passed stage 1 on closes (9:40 closed 1,139.00 >= the best range close 1,138.70) but its real
        # range high is 1,143.90: stage 2 throws it out. HDFCBANK and RELIANCE never got that far.
        self.assertEqual(counts, {"requested": 8, "fetched": 8, "unusable": 0, "illiquid": 0, "scanned": 8,
                                  "gaps": 0, "candidates": 6, "confirmed": 4, "unconfirmed": 0, "hits": 3})
        self.assertEqual(sorted(failed), ["ICICIBANK", "TCS"])
        self.assertIn("429", failed["TCS"])
        hits, counts, _ = scan.scan_orb(self.spark, sorted(self.spark), day, cut, 15, 5000, 0, confirm)
        self.assertEqual(([h["symbol"] for h in hits], counts["illiquid"]), (["BAJAJ-AUTO"], 7))


class DailyLive(unittest.TestCase):
    """Three real 3-month spark answers (60 Nifty 500 symbols) fetched at 11:00 AM IST on 2026-11-17."""

    def setUp(self):
        self.data, self.errors = scan.load_offline(DAILY)
        self.syms = sorted(self.data)
        self.now = scan.parse_now("2026-11-17 11:00")

    def test_setups_and_stale_closes(self):
        self.assertEqual((len(self.syms), self.errors), (60, {}))
        self.assertIn("M&M", self.data)
        self.assertIn("ARE&M", self.data)
        hits, counts, last = scan.scan_daily(self.data, self.syms, self.now, 10, 0, 1e8)
        self.assertEqual(last, D(2026, 11, 16))
        self.assertEqual((counts["illiquid"], counts["scanned"], counts["hits"]), (17, 43, 21))
        by = {h["symbol"]: h for h in hits}
        # LT: up 12.9 % over the 10 sessions before the last 5, the last 5 closes in a 1.74 % range;
        # 4,140.00 is under the 4,212.00 high of the 20 before, so a flag and no breakout
        self.assertEqual(by["LT"], {"symbol": "LT", "setups": ["bull_flag"], "price": 4140.0,
                                    "close_date": "2026-11-16", "rsi": 69.6, "volume": 512147})
        # LENSKART: 734.70 > 727.45 and up 15.7 % then a 2.16 % range
        self.assertEqual(by["LENSKART"]["setups"], ["breakout_20d", "bull_flag"])
        self.assertEqual(by["RRKABEL"]["setups"], ["oversold"])           # RSI 29.0 after a fall to 1,883.90
        # SAILIFE has no 2026-11-16 close in Yahoo yet (null): its setups use closes through Friday
        self.assertEqual((by["SAILIFE"]["close_date"], by["SAILIFE"]["setups"]), ("2026-11-13", ["breakout_20d"]))
        self.assertGreater(counts["stale"], 0)
        # KSB, the same: RSI 29.4 on 11-12, then up to 653.10 on 11-13. Under ₹10 crore traded by 11:00.
        hits, counts, _ = scan.scan_daily(self.data, self.syms, self.now, 0, 0, 0)
        ksb = [h for h in hits if h["symbol"] == "KSB"][0]
        self.assertEqual((ksb["setups"], ksb["close_date"], ksb["price"]), (["oversold_bounce"], "2026-11-13", 653.1))
        self.assertEqual(counts["hits"], 24)

    def test_stale_closes_are_said(self):
        code, text, _ = run(["daily", "--offline", DAILY, "--now", "2026-11-17 11:00", "--no-write", "--show", "60"])
        self.assertEqual(code, 0)
        self.assertIn("closes through 2026-11-16", text)
        self.assertRegex(text, r"\d+ of the 43 end before 2026-11-16: Yahoo has no close for that session yet")
        self.assertIn("SAILIFE", text)
        self.assertRegex(text, r"SAILIFE .*20-day closing-high breakout  \(closes through 2026-11-13\)")
        self.assertIn(", on today's volume so far", text)


class Orb(unittest.TestCase):
    """40 real v8 charts of the reference session, Mon 2026-11-16 (two spark batches' worth of the Nifty 500)."""

    @classmethod
    def setUpClass(cls):
        cls.data, cls.errors = scan.load_offline(WALL)
        cls.syms = sorted(set(cls.data) | set(cls.errors))

    def scan_at(self, now, min_price=10, syms=None, or_minutes=15, until=datetime.time(14, 45)):
        day, cutoff = scan.orb_window(scan.parse_now(now), None, or_minutes, until)
        return scan.scan_orb(self.data, syms or self.syms, day, cutoff, or_minutes, min_price, 0)

    def test_the_reference_scan_at_1030(self):
        hits, counts, failed = self.scan_at("2026-11-16 10:30")
        self.assertEqual((len(self.syms), self.errors), (40, {}))
        self.assertEqual(counts["scanned"], 40)
        self.assertEqual(counts["candidates"], 25)
        self.assertEqual(len(hits), 13)
        # BAJAJ-AUTO: range 9:15 to 9:30 = the 9:15, 9:20 and 9:25 bars, high 10,844 (9:20), low 10,772 (9:15);
        # the 9:30 bar closed 10,850, 0.06 % over, at 9:35 AM
        bajaj = [h for h in hits if h["symbol"] == "BAJAJ-AUTO"][0]
        self.assertEqual(bajaj, {"symbol": "BAJAJ-AUTO", "setups": ["orb"], "price": 10850.0, "at": "9:35 AM",
                                 "bar_start": "09:30", "bar_index": 3, "or_high": 10844.0, "or_low": 10772.0,
                                 "pct_over": 0.06})
        self.assertNotIn("M&M", [h["symbol"] for h in hits])   # M&M did not break its range that morning
        self.assertIn("M&MFIN", [h["symbol"] for h in hits])
        self.assertEqual([h["symbol"] for h in hits[:4]], ["ASTERDM", "BAJAJ-AUTO", "LICHSGFIN", "LTF"])  # 9:35
        self.assertEqual(hits[-1]["symbol"], "BAJAJHFL")        # sorted by time, then symbol

    def test_shared_reference_charts(self):
        # RELIANCE and SBIN from the shared 60-day files: full sessions, no breakout on 2026-11-16
        for sym in ("RELIANCE", "SBIN"):
            with gzip.open(os.path.join(NSE, sym + "-60d-5m.json.gz"), "rt", encoding="utf-8") as fh:
                s = scan.parse_chart(fh.read(), sym)
            days = sorted(set(scan.ts_to_ist(t).date() for t in s["t"]))
            self.assertEqual((days[0], days[-2], days[-1]), (D(2026, 8, 20), REF, D(2026, 11, 17)))
            self.assertTrue(all(scan.trading_day(d)[0] for d in days))
            ref = [t for t in s["t"] if scan.ts_to_ist(t).date() == REF]
            self.assertEqual(len(ref), 75)                     # 9:15 to the bar that starts 3:25 PM
            cut = DT(2026, 11, 16, 14, 45)
            bars = scan.session_bars(s, REF, cut)
            self.assertEqual(len(bars), 66)
            self.assertTrue(scan.usable(bars, REF, cut, 3))
            self.assertIsNone(scan.orb_hit(bars, 3))

    def test_price_floor(self):
        hits, counts, _ = self.scan_at("2026-11-16 10:30", min_price=100)
        self.assertEqual(counts["illiquid"], 2)             # BAJAJHFL (₹81) and MAHABANK (₹92)
        self.assertEqual(len(hits), 12)
        self.assertNotIn("BAJAJHFL", [h["symbol"] for h in hits])

    def test_in_progress_bar_and_no_breakout_yet(self):
        hits, _, _ = self.scan_at("2026-11-16 10:29", syms=["BAJAJHFL"])
        self.assertEqual(hits, [])                         # the 10:25 bar is not done until 10:30
        hits, _, _ = self.scan_at("2026-11-16 10:30", syms=["BAJAJHFL"])
        # range high 81.25 (the 9:15 bar); the 9:50 and 9:55 bars touched it but closed 81.14
        self.assertEqual((hits[0]["at"], hits[0]["price"], hits[0]["or_high"]), ("10:30 AM", 81.51, 81.25))

    def test_the_live_tick_is_dropped(self):
        s = v8_file("RELIANCE")                             # shared, fetched 9:48:30 AM IST on 2026-11-17
        last = scan.ts_to_ist(s["t"][-1])
        self.assertEqual(last.replace(second=0), DT(2026, 11, 17, 9, 48))
        self.assertTrue(last.second)                        # off the grid
        bars = scan.session_bars(s, D(2026, 11, 17), DT(2026, 11, 17, 9, 49))
        self.assertEqual(len(bars), 6)                      # 9:15 to 9:40; 9:45 in progress, the tick dropped
        self.assertTrue(scan.usable(bars, D(2026, 11, 17), DT(2026, 11, 17, 9, 49), 3))

    def test_stage_one_is_a_necessary_condition(self):
        for t in ("09:35", "10:00", "10:30", "12:00", "14:30"):
            cut = DT.combine(REF, scan.parse_hhmm(t))
            for sym, s in self.data.items():
                bars = scan.session_bars(s, REF, cut)
                if scan.usable(bars, REF, cut, 3) and scan.orb_hit(bars, 3):
                    self.assertTrue(scan.orb_candidate(bars, 3), (sym, t))

    def test_confirm_uses_the_v8_highs(self):
        spark_like = {k: dict(v, h=None, l=None) for k, v in self.data.items()}  # closes only, like spark
        asked = []

        def confirm(sym):
            asked.append(sym)
            return self.data[sym]
        cut = DT(2026, 11, 16, 10, 30)
        hits, counts, failed = scan.scan_orb(spark_like, self.syms, REF, cut, 15, 10, 0, confirm)
        self.assertEqual(len(hits), 13)
        self.assertEqual(counts["candidates"], len(asked))
        self.assertLess(len(asked), 40)                     # stage 1 skipped 15 charts

        def broken(sym):
            raise scan.ScanError("Yahoo answered HTTP 429 for /v8/finance/chart/%s" % scan.to_yahoo(sym))
        hits, counts, failed = scan.scan_orb(spark_like, ["BAJAJ-AUTO", "M&MFIN"], REF, cut, 15, 10, 0, broken)
        self.assertEqual(hits, [])
        self.assertIn("M%26MFIN.NS", failed["M&MFIN"])

    def test_a_spark_gap_goes_to_stage_two(self):
        # Yahoo's spark skips 5-minute bars of NSE stocks that the v8 chart has: the v8 chart decides
        cut = DT(2026, 11, 16, 10, 30)
        spark_like = {k: dict(v, h=None, l=None) for k, v in self.data.items()}
        s = dict(spark_like["BAJAJHFL"])
        drop = [i for i, t in enumerate(s["t"]) if scan.ts_to_ist(t) == DT(2026, 11, 16, 10, 0)][0]
        s["t"] = s["t"][:drop] + s["t"][drop + 1:]
        s["c"] = s["c"][:drop] + s["c"][drop + 1:]
        s["v"] = s["v"][:drop] + s["v"][drop + 1:]
        spark_like["BAJAJHFL"] = s
        self.assertFalse(scan.usable(scan.session_bars(s, REF, cut), REF, cut, 3))
        hits, counts, _ = scan.scan_orb(spark_like, ["BAJAJHFL"], REF, cut, 15, 10, 0, lambda sym: self.data[sym])
        self.assertEqual((counts["gaps"], counts["candidates"], counts["scanned"], counts["unusable"]), (1, 1, 1, 0))
        self.assertEqual(hits[0]["at"], "10:30 AM")
        # with no second source (offline v8 files) a missing bar is a missing bar
        hits, counts, _ = scan.scan_orb({"BAJAJHFL": s}, ["BAJAJHFL"], REF, cut, 15, 10, 0)
        self.assertEqual((hits, counts["unusable"], counts["gaps"]), ([], 1, 0))
        # missing from the v8 chart too: a halt or no trades, out
        hits, counts, failed = scan.scan_orb({"BAJAJHFL": s}, ["BAJAJHFL"], REF, cut, 15, 10, 0, lambda sym: s)
        self.assertEqual((hits, counts["unusable"], counts["scanned"], failed), ([], 1, 0, {}))

    def test_stage_two_cap_takes_the_most_traded(self):
        spark_like = {k: dict(v, h=None, l=None) for k, v in self.data.items()}
        asked = []

        def confirm(sym):
            asked.append(sym)
            return self.data[sym]
        cut = DT(2026, 11, 16, 10, 30)
        hits, counts, _ = scan.scan_orb(spark_like, self.syms, REF, cut, 15, 10, 0, confirm, max_confirm=5)
        self.assertEqual((counts["confirmed"], counts["unconfirmed"]), (5, 20))
        self.assertEqual(asked, ["ATHERENERG", "BAJFINANCE", "AXISBANK", "ASIANPAINT", "LICHSGFIN"])  # most ₹ traded
        hits_all, _, _ = scan.scan_orb(spark_like, self.syms, REF, cut, 15, 10, 0, confirm, max_confirm=0)
        self.assertEqual(len(hits_all), 13)
        for h in hits:
            self.assertIn(h, hits_all)

    def test_a_missing_bar_makes_a_chart_unusable(self):
        s = bars_series("2026-11-18", [100, 100.2, 100.1, None, 100.5, 100.6, 100.7, 100.8, 100.9])
        bars = scan.session_bars(s, D(2026, 11, 18), DT(2026, 11, 18, 10, 0))
        self.assertFalse(scan.usable(bars, D(2026, 11, 18), DT(2026, 11, 18, 10, 0), 3))

    def test_a_30_minute_range(self):
        closes = [100, 101, 100.5, 100.8, 101.2, 100.9, 101.5] + [101.0] * 60
        s = bars_series("2026-11-18", closes, highs=[c + 0.2 for c in closes])
        hits, counts, _ = scan.scan_orb({"X": s}, ["X"], D(2026, 11, 18), DT(2026, 11, 18, 12, 0), 30, 10, 0)
        self.assertEqual(counts["scanned"], 1)
        self.assertEqual(hits[0]["at"], "9:50 AM")          # range 9:15 to 9:45 (high 101.4), 9:45 bar 101.5
        self.assertEqual(hits[0]["or_high"], 101.4)
        bars = scan.session_bars(s, D(2026, 11, 18), DT(2026, 11, 18, 15, 30))
        self.assertEqual(len(bars), 67)                     # every bar given; the session has room for 75
        s = bars_series("2026-11-18", [100.0] * 80)         # bars past 3:25 PM are not in the session
        self.assertEqual(len(scan.session_bars(s, D(2026, 11, 18), DT(2026, 11, 18, 23, 0))), 75)

    def test_yahoo_history_leaves_late_bars_null(self):
        # in Yahoo's stored 5-minute history the 3:15 and 3:20 PM bars are often null: a replay past 3:15 PM
        # loses those charts. no_entries_after (14:30) stops long before.
        _, counts, _ = scan.scan_orb(self.data, self.syms, REF, DT(2026, 11, 16, 15, 15), 15, 10, 0)
        self.assertEqual(counts["unusable"], 0)
        _, counts, _ = scan.scan_orb(self.data, self.syms, REF, DT(2026, 11, 16, 15, 30), 15, 10, 0)
        self.assertEqual(counts["unusable"], 20)

    def test_window_messages(self):
        until = datetime.time(14, 45)

        def msg(now, at=None, until=until, holidays=()):
            with self.assertRaises(scan.ScanError) as cm:
                scan.orb_window(scan.parse_now(now), at, 15, until, None, holidays)
            return str(cm.exception)
        self.assertIn("weekend", msg("2026-11-21 10:30"))
        self.assertIn("NSE holiday: Guru Nanak Jayanti", msg("2026-11-24 10:30"))
        self.assertIn("NSE holiday: Diwali Balipratipada", msg("2026-11-10 10:30"))
        self.assertIn("a holiday in trader/config.json", msg("2026-11-18 10:30", holidays={"2026-11-18"}))
        self.assertIn("Run orb again after 9:35 AM", msg("2026-11-16 09:20"))
        self.assertIn("Run orb again after 9:35 AM", msg("2026-11-16 09:34"))
        self.assertIn("runs 9:15 to 9:30 AM", msg("2026-11-16 09:34"))
        self.assertIn("expired", msg("2026-11-16 14:46"))
        self.assertIn("expired", msg("2026-11-16 11:30", until=datetime.time(11, 0)))
        self.assertIn("closed at 3:30 PM IST", msg("2026-11-16 15:45"))
        self.assertIn("later than now", msg("2026-11-16 10:00", at=datetime.time(10, 30)))
        self.assertIn("after the 3:30 PM close", msg("2026-11-16 17:00", at=datetime.time(15, 45), until=None))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-11-16 16:30"), datetime.time(10, 30), 15, until),
                         (REF, DT(2026, 11, 16, 10, 30)))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-11-16 09:35"), None, 15, until)[1],
                         DT(2026, 11, 16, 9, 35))
        with self.assertRaises(scan.ScanError):
            scan.orb_window(scan.parse_now("2026-11-16 10:30"), None, 7, until)


class LiveScan(unittest.TestCase):
    """The networked path, replayed: every Yahoo answer a live Nifty 500 orb run got for 60 of its symbols
    (spark batches 3, 15 and 21, then each candidate's v8 chart), 11:00:55 to 11:04:30 AM IST, 2026-11-17."""

    def setUp(self):
        with gzip.open(LIVE, "rt", encoding="utf-8") as fh:
            self.live = json.load(fh)
        self.calls = []
        self.old = scan.yahoo_get, scan.SLEEP, scan.parse_now
        real_now = scan.parse_now

        def get(path):
            self.calls.append(path)
            return self.live["responses"][path]
        scan.yahoo_get, scan.SLEEP = get, (lambda s: None)
        scan.parse_now = lambda text: real_now(text or "2026-11-17 11:01")  # the machine clock, pinned
        self.d = tempfile.mkdtemp()
        self.uni = os.path.join(self.d, "universe.txt")
        with open(self.uni, "w", encoding="utf-8") as fh:
            fh.write("\n".join(self.live["symbols"]) + "\n")

    def tearDown(self):
        scan.yahoo_get, scan.SLEEP, scan.parse_now = self.old
        shutil.rmtree(self.d)

    def test_the_live_run_reproduced(self):
        self.assertEqual(len(self.live["symbols"]), 60)
        self.assertEqual(self.live["first_fetch_utc"], "2026-11-17T05:30:55Z")
        code, text, _ = run(["orb", "--universe", self.uni, "--at", "11:00", "--json", "--no-write"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual(out["counts"], {"requested": 60, "fetched": 60, "unusable": 1, "illiquid": 0, "scanned": 59,
                                         "gaps": 12, "candidates": 46, "confirmed": 45, "unconfirmed": 0,
                                         "hits": 34})
        self.assertEqual((out["as_of"], out["failed"]), ("2026-11-17 11:00 IST", 0))
        self.assertEqual(sum(1 for c in self.calls if c.startswith("/v7/")), 3)   # 60 symbols, 20 a request
        self.assertEqual(sum(1 for c in self.calls if c.startswith("/v8/")), 46)
        self.assertIn("/v8/finance/chart/M%26M.NS?range=1d&interval=5m", self.calls)
        self.assertTrue(any("ARE%26M.NS" in c for c in self.calls if c.startswith("/v7/")))
        by = {h["symbol"]: h for h in out["hits"]}
        # the same two hits the shared 9:48 AM fixtures give (LiveFiveMinute): stable as the morning went on
        self.assertEqual(by["M&M"], {"symbol": "M&M", "setups": ["orb"], "price": 3392.0, "at": "9:35 AM",
                                     "bar_start": "09:30", "bar_index": 3, "or_high": 3389.1, "or_low": 3355.3,
                                     "pct_over": 0.09})
        self.assertEqual(by["BAJAJ-AUTO"]["at"], "9:45 AM")
        self.assertNotIn("RAMCOCEM", by)       # no 10:45 AM bar in spark or v8: no trades, unusable
        # 12 spark charts missed a bar (AUBANK's 10:20 AM, say) that the v8 chart has; 6 of them broke out.
        # Dropping a spark chart for a gap, as a missing bar meant upstream, would have lost these hits.
        day, cut = D(2026, 11, 17), DT(2026, 11, 17, 11, 0)
        spark = {}
        for path, body in self.live["responses"].items():
            if path.startswith("/v7/"):
                spark.update(scan.parse_spark(body))
        gaps = sorted(s for s, ser in spark.items() if not scan.usable(scan.session_bars(ser, day, cut), day, cut, 3))
        self.assertEqual(len(gaps), 12)
        self.assertEqual(sorted(set(gaps) & set(by)), ["ATGL", "AUBANK", "BAJAJHFL", "RECLTD", "RHIM", "SAGILITY"])
        self.assertEqual((by["AUBANK"]["at"], by["RHIM"]["at"]), ("10:10 AM", "10:00 AM"))

    def test_the_text(self):
        code, text, _ = run(["orb", "--universe", self.uni, "--at", "11:00", "--no-write"])
        self.assertIn("Opening-range breakout scan, Tue 2026-11-17 11:00 AM IST: 15-minute range (9:15 to 9:30 AM)",
                      text)
        self.assertIn("Fetched 60 of 60 charts; 59 usable; 46 candidates (12 of them for a missing bar in Yahoo's "
                      "spark closes), 45 confirmed with real highs and lows.", text)
        self.assertIn("34 of 59 charts closed above their opening-range high by 11:00 AM:", text)
        self.assertIn("   9:35 AM  M&M           ₹3,392.00  range 3,355.30 to 3,389.10  +0.09% over", text)
        self.assertIn("   9:45 AM  BAJAJ-AUTO   ₹10,799.00  range 10,709.00 to 10,795.00  +0.04% over", text)
        self.assertIn("/nse-news SYMBOL and /nse-entry SYMBOL", text)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.t = os.path.join(self.d, "trader")
        self.wl = os.path.join(self.t, "watchlist.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_universe_daily_orb_end_to_end(self):
        uni = os.path.join(self.t, "universe.txt")
        code, text, _ = run(["universe", "--offline", NSE, "--out", uni])   # a folder holding nifty500list.csv
        self.assertEqual(code, 0)
        self.assertIn("Nifty 500 constituents (niftyindices.com): 501 rows.", text)
        self.assertIn("Kept 495 EQ-series stocks", text)
        self.assertIn("Dropped: 3 series RR, 2 series BE, 1 placeholder.", text)
        with open(uni, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 495)
        self.assertIn("M&M|Mahindra & Mahindra Ltd.", lines)
        self.assertIn("BAJAJ-AUTO|Bajaj Auto Ltd.", lines)

        code, text, _ = run(["universe", "--list", "all", "--offline", EQL, "--no-write", "--json"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual((out["list"], out["count"], out["rows"], out["dropped"]),
                         ("all", 2404, 2633, {"series BE": 195, "series BZ": 34}))

        # daily over that universe with the shared charts: only 8 of the 495, so partial
        code, text, _ = run(["daily", "--universe", uni, "--offline", SPARK3MO, "--now", "2026-11-17 09:00",
                             "--out", self.wl])
        self.assertEqual(code, 0)
        self.assertIn("Daily scan, Tue 2026-11-17 09:00 IST, closes through 2026-11-16", text)
        self.assertIn("Fetched 8 of 495 charts (487 failed or had no data). 8 passed the liquidity filter "
                      "(₹10+ and ₹10 crore+ traded); 8 scanned.", text)
        self.assertIn("  HDFCBANK        ₹816.50  RSI  75.6  20-day closing-high breakout, bull flag", text)
        doc = self.read(self.wl)
        self.assertEqual((doc["created_by"], doc["mode"], doc["universe"], doc["scanned"], doc["failed"]),
                         ("nse-scanner", "daily", 495, 8, 487))
        self.assertEqual(doc["as_of"], "2026-11-16 close")
        self.assertEqual(doc["filters"], {"min_price": 10.0, "min_volume": 0, "min_value": 1e8})
        self.assertEqual([h["symbol"] for h in doc["hits"]], ["HDFCBANK", "SBIN", "ICICIBANK", "RELIANCE"])

        # the reference session's scan at 10:30 AM
        code, text, _ = run(["orb", "--offline", WALL, "--now", "2026-11-16 10:30", "--out", self.wl])
        self.assertEqual(code, 0)
        self.assertIn("Opening-range breakout scan, Mon 2026-11-16 10:30 AM IST: 15-minute range (9:15 to 9:30 AM)",
                      text)
        self.assertIn("Fetched 40 of 40 charts; 40 usable; 25 candidates, 25 confirmed with real highs and lows.",
                      text)
        self.assertIn("13 of 40 charts closed above their opening-range high by 10:30 AM:", text)
        self.assertIn("   9:35 AM  BAJAJ-AUTO   ₹10,850.00  range 10,772.00 to 10,844.00  +0.06% over", text)
        self.assertIn("  10:30 AM  BAJAJHFL         ₹81.51  range 80.86 to 81.25  +0.32% over", text)
        self.assertIn("not a list of stocks to buy", text)
        doc = self.read(self.wl)
        self.assertEqual({k: doc[k] for k in ("created_by", "created_at", "mode", "as_of", "universe", "scanned",
                                              "source", "or_minutes", "interval")},
                         {"created_by": "nse-scanner", "created_at": "2026-11-16T05:00:00Z", "mode": "orb",
                          "as_of": "2026-11-16 10:30 IST", "universe": 40, "scanned": 40, "source": "yahoo",
                          "or_minutes": 15, "interval": "5m"})
        self.assertEqual(len(doc["hits"]), 13)
        self.assertIn({"symbol": "BAJAJ-AUTO", "setups": ["orb"], "price": 10850.0, "at": "9:35 AM",
                       "bar_start": "09:30", "bar_index": 3, "or_high": 10844.0, "or_low": 10772.0,
                       "pct_over": 0.06}, doc["hits"])

        # the hits read back as a watchlist, with --at instead of --now
        code, text, _ = run(["orb", "--offline", WALL, "--at", "10:30", "--watchlist", self.wl, "--json",
                             "--no-write"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"], len(out["hits"])), (13, 13, 13))

    def test_partial_stage_two_is_said(self):
        """The networked path with a fake Yahoo that answers from the reference-session bundle."""
        with gzip.open(WALL, "rt", encoding="utf-8") as fh:
            bundle = json.load(fh)
        calls = []

        def get(path):
            calls.append(path)
            if path.startswith("/v7/finance/spark"):
                res = []
                for y in path.split("symbols=")[1].split("&")[0].split(","):
                    r = ((bundle.get(scan.from_yahoo(y)) or {}).get("chart") or {}).get("result")
                    if r:
                        closes = r[0]["indicators"]["quote"][0].get("close", [])
                        res.append({"symbol": scan.yahoo_symbol(scan.from_yahoo(y)),
                                    "response": [dict(r[0], indicators={"quote": [{"close": closes}]})]})
                return json.dumps({"spark": {"result": res, "error": None}})
            return json.dumps(bundle[scan.from_yahoo(path.split("/chart/")[1].split("?")[0])])
        old_get, old_sleep, real_now = scan.yahoo_get, scan.SLEEP, scan.parse_now
        scan.yahoo_get, scan.SLEEP = get, (lambda s: None)
        scan.parse_now = lambda text: real_now(text or "2026-11-16 10:35")  # the machine clock, pinned
        uni = os.path.join(self.d, "u.txt")
        with open(uni, "w", encoding="utf-8") as fh:
            fh.write("\n".join(sorted(bundle)) + "\n")
        try:
            code, text, _ = run(["orb", "--universe", uni, "--now", "2026-11-16 10:30", "--max-confirm", "5",
                                 "--no-write"])
        finally:
            scan.yahoo_get, scan.SLEEP, scan.parse_now = old_get, old_sleep, real_now
        self.assertEqual(code, 0)
        self.assertIn("40 usable; 25 candidates, 5 confirmed", text)
        self.assertIn("PARTIAL: 20 more candidates were not checked (only the 5 most traded are; --max-confirm 0 "
                      "checks all).", text)
        # stage 1 is spark closes only, so "most traded" comes from Yahoo's meta volume (here the bundle's)
        self.assertIn("1 of the 5 checked charts closed above their opening-range high by 10:30 AM", text)
        self.assertIn("   9:50 AM  ATHERENERG    ₹2,005.00  range 1,940.20 to 2,003.60  +0.07% over", text)
        self.assertEqual(sum(1 for c in calls if c.startswith("/v8/")), 5)
        self.assertEqual(sum(1 for c in calls if c.startswith("/v7/")), 2)   # 40 symbols, 20 a request
        self.assertTrue(all("range=1d&interval=5m" in c for c in calls))
        self.assertTrue(any("M%26M.NS" in c for c in calls))

    def test_plain_messages_and_exit_codes(self):
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-11-21 10:30", "--out", self.wl])
        self.assertEqual(code, 1)
        self.assertIn("No session on Sat 2026-11-21 (weekend)", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists(self.wl))
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-11-24 10:30", "--out", self.wl])
        self.assertIn("No session on Tue 2026-11-24 (NSE holiday: Guru Nanak Jayanti)", err)
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-11-16 09:30", "--out", self.wl])
        self.assertIn("after 9:35 AM", err)
        cfg = os.path.join(self.t, "config.json")
        os.makedirs(self.t)
        with open(cfg, "w") as fh:
            json.dump({"no_entries_after": "10:00"}, fh)
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-11-16 10:30", "--config", cfg])
        self.assertEqual(code, 1)
        self.assertIn("expired", err)
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-11-16 14:50", "--config", cfg + ".none"])
        self.assertIn("after 14:30", err)                    # the default no_entries_after
        with open(cfg, "w") as fh:
            json.dump({"holidays": ["2026-11-16"]}, fh)
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-11-16 10:30", "--config", cfg])
        self.assertIn("No session on Mon 2026-11-16 (a holiday in trader/config.json)", err)
        code, _, err = run(["orb", "--offline", WALL])
        self.assertIn("--at HH:MM", err)
        code, text, _ = run(["orb", "--offline", WALL, "--now", "2026-11-16 09:35", "--symbols", "M&M",
                             "--out", self.wl])
        self.assertEqual(code, 1)                          # no breakout yet, but the empty list is written
        self.assertIn("No chart closed above its opening-range high by 9:35 AM", text)
        self.assertEqual(self.read(self.wl)["hits"], [])
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-11-16 10:30", "--symbols", "^NSEI"])
        self.assertIn("not an NSE symbol: ^NSEI", err)
        code, _, err = run(["daily", "--universe", os.path.join(self.d, "nope.txt")])
        self.assertEqual(code, 1)
        self.assertIn("scan.py universe", err)
        code, _, err = run(["daily", "--offline", os.path.join(self.d, "nothing-here")])
        self.assertIn("no offline file", err)
        empty = os.path.join(self.d, "empty.json")
        with open(empty, "w") as fh:
            fh.write("{}")
        code, _, err = run(["daily", "--offline", empty])
        self.assertEqual(code, 1)
        self.assertIn("no symbols to scan", err)
        code, _, err = run(["universe", "--offline", self.d])
        self.assertIn("no ind_nifty500list.csv or nifty500list.csv in", err)

    def test_a_year_without_a_holiday_list(self):
        code, text, _ = run(["daily", "--offline", SPARK3MO, "--now", "2027-01-04 09:00", "--no-write"])
        self.assertTrue(text.startswith("NSE holiday list for 2027 not loaded: check nseindia.com.\n"), text[:80])
        code, text, _ = run(["daily", "--offline", SPARK3MO, "--now", "2027-01-04 09:00", "--no-write", "--json"])
        self.assertEqual(json.loads(text)["warning"], "NSE holiday list for 2027 not loaded: check nseindia.com")
        code, text, _ = run(["daily", "--offline", SPARK3MO, "--now", "2026-11-17 09:00", "--no-write", "--json"])
        self.assertNotIn("warning", json.loads(text))

    def test_offline_folder_of_v8_files(self):
        folder = os.path.join(self.d, "charts")
        os.makedirs(folder)
        for name in ("M&M-1d-5m.json", "BAJAJ-AUTO-1d-5m.json", "SBIN-1d-5m.json", "NSEI-1d-5m.json"):
            shutil.copy(os.path.join(NSE, name), folder)     # the chart's own symbol names it, not the file
        with open(os.path.join(folder, "NOPE.json"), "w") as fh:
            fh.write('{"chart":{"result":null,"error":{"code":"Not Found",'
                     '"description":"No data found, symbol may be delisted"}}}')
        code, text, _ = run(["orb", "--offline", folder, "--now", "2026-11-17 09:48", "--no-write", "--json"])
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"], out["failed"]), (4, 3, 1))   # ^NSEI is not tradable
        self.assertEqual([(h["symbol"], h["at"]) for h in out["hits"]],
                         [("M&M", "9:35 AM"), ("BAJAJ-AUTO", "9:45 AM"), ("SBIN", "9:45 AM")])
        self.assertEqual(code, 0)

    def test_symbol_lists(self):
        txt = os.path.join(self.d, "list.txt")
        with open(txt, "w", encoding="utf-8") as fh:
            fh.write("# my list\nM&M|Mahindra & Mahindra Ltd.\nsbin.ns\n\nM&M\n")
        self.assertEqual(scan.symbols_from_file(txt), ["M&M", "SBIN", "M&M"])
        js = os.path.join(self.d, "wl.json")
        with open(js, "w", encoding="utf-8") as fh:
            json.dump({"hits": [{"symbol": "m&m"}, {"symbol": "BAJAJ-AUTO.NS"}]}, fh)
        self.assertEqual(scan.symbols_from_file(js), ["M&M", "BAJAJ-AUTO"])
        code, text, _ = run(["daily", "--offline", SPARK3MO, "--watchlist", txt, "--now", "2026-11-17 09:00",
                             "--json", "--no-write"])
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"]), (2, 2))
        code, text, _ = run(["daily", "--offline", SPARK3MO, "--symbols", "m&m,BAJAJ-AUTO.NS", "--now",
                             "2026-11-17 09:00", "--json", "--no-write"])
        self.assertEqual((json.loads(text)["universe"], json.loads(text)["scanned"]), (2, 2))
        code, text, _ = run(["daily", "--offline", SPARK3MO, "--limit", "3", "--now", "2026-11-17 09:00", "--json",
                             "--no-write"])
        self.assertEqual(json.loads(text)["universe"], 3)

    def test_daily_json(self):
        code, text, _ = run(["daily", "--offline", SPARK3MO, "--now", "2026-11-17 09:00", "--json", "--no-write",
                             "--min-value", "0", "--min-price", "0"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual(len(out["hits"]), 4)
        self.assertEqual(out["note"], "A scan is a list of charts that match a rule, not a list of stocks to buy.")
        self.assertEqual(out["counts"]["stale"], 0)


if __name__ == "__main__":
    unittest.main()
