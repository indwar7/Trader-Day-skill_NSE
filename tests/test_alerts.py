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
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

alerts = load("nse-alerts", "alerts")

QDIR = os.path.join(FIX, "nse-alerts")    # Yahoo v8 chart, range=1d interval=1d, fetched 2026-11-17 10:49 IST
PLAN = os.path.join(QDIR, "plan-reliance.json")
DT = datetime.datetime
UA = "Mozilla/5.0 (compatible; nse-day-trader/1.0)"

# NSE capital-market holidays 2026 (NSE circulars CMTR/71775 and CMTR/72260)
NSE_2026 = ["2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03", "2026-04-14",
            "2026-05-01", "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10",
            "2026-11-24", "2026-12-25"]


def ist(text):
    return alerts.parse_now(text)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = alerts.main(argv)
    return code, out.getvalue(), err.getvalue()


def plan_doc():
    with open(PLAN) as fh:
        return json.load(fh)


def plan_rules():
    return alerts.rules_from_plan(plan_doc())


class Calendar(unittest.TestCase):
    def test_holidays_match_the_published_list(self):
        self.assertEqual(sorted(alerts.NSE_HOLIDAYS), NSE_2026)
        self.assertEqual(alerts.CALENDAR_YEARS, (2026,))
        for d in alerts.NSE_HOLIDAYS:
            self.assertLess(datetime.date.fromisoformat(d).weekday(), 5, d)
        self.assertFalse(hasattr(alerts, "NYSE_EARLY_CLOSE"))

    def test_ist_conversion(self):
        self.assertEqual(ist("2026-11-17T09:15"), DT(2026, 11, 17, 3, 45))
        self.assertEqual(ist("2026-11-17 15:30"), DT(2026, 11, 17, 10, 0))
        self.assertEqual(ist("2026-03-09 09:15"), DT(2026, 3, 9, 3, 45))       # no daylight saving in India
        self.assertEqual(ist("2026-11-17T05:19:04Z"), DT(2026, 11, 17, 5, 19, 4))
        self.assertEqual(ist("2026-11-17T10:49:04+05:30"), DT(2026, 11, 17, 5, 19, 4))
        self.assertEqual(alerts.to_ist(DT(2026, 11, 17, 18, 45)), DT(2026, 11, 18, 0, 15))   # past midnight in IST
        self.assertEqual(alerts.ist_to_utc(DT(2026, 11, 18, 0, 15)), DT(2026, 11, 17, 18, 45))
        with self.assertRaises(alerts.AlertError):
            ist("tomorrow")

    def test_market_status(self):
        st = alerts.market_status(ist("2026-11-17T10:00"))
        self.assertTrue(st["open"])
        self.assertEqual((st["reason"], st["opens"], st["closes"], st["square_off"]),
                         ("regular session", "09:15", "15:30", "15:20"))
        self.assertEqual((st["now_ist"], st["weekday"], st["warnings"]), ("2026-11-17 10:00", "Tuesday", []))
        early = alerts.market_status(ist("2026-11-17T09:14"))
        self.assertEqual((early["open"], early["reason"], early["next_open"]),
                         (False, "before the open", "2026-11-17 09:15"))
        self.assertTrue(alerts.market_status(ist("2026-11-17T09:15"))["open"])
        self.assertTrue(alerts.market_status(ist("2026-11-17T15:29"))["open"])
        after = alerts.market_status(ist("2026-11-17T15:30"))
        self.assertEqual((after["open"], after["reason"], after["next_open"]),
                         (False, "after the close", "2026-11-18 09:15"))
        diwali = alerts.market_status(ist("2026-11-10T11:00"))
        self.assertFalse(diwali["open"])
        self.assertEqual(diwali["reason"], "NSE holiday: Diwali Balipratipada")
        self.assertEqual(diwali["next_open"], "2026-11-11 09:15")
        self.assertNotIn("opens", diwali)
        self.assertEqual(alerts.market_status(ist("2026-11-23T20:00"))["next_open"], "2026-11-25 09:15")  # Gurpurb
        self.assertEqual(alerts.market_status(ist("2026-12-25T10:00"))["next_open"], "2026-12-28 09:15")
        sat = alerts.market_status(ist("2026-11-21T10:00"))
        self.assertEqual((sat["reason"], sat["next_open"]), ("weekend", "2026-11-23 09:15"))
        # Diwali Muhurat trading (Sunday 2026-11-08) is a special session: not supported, closed here
        self.assertEqual(alerts.market_status(ist("2026-11-08T18:15"))["reason"], "weekend")

    def test_a_date_outside_the_calendar_warns_and_trades_on_weekdays(self):
        nye = alerts.market_status(ist("2026-12-31T16:00"))
        self.assertEqual(nye["next_open"], "2027-01-01 09:15")
        self.assertEqual(nye["warnings"], ["NSE holiday list for 2027 not loaded: check nseindia.com"])
        st = alerts.market_status(ist("2027-01-26T10:00"))       # a weekday with no list: treated as open
        self.assertTrue(st["open"])
        self.assertFalse(st["calendar_known"])
        self.assertEqual(st["warnings"], ["NSE holiday list for 2027 not loaded: check nseindia.com"])

    def test_extra_holidays_from_the_config(self):
        d = tempfile.mkdtemp()
        try:
            cfg = os.path.join(d, "config.json")
            with open(cfg, "w") as fh:
                json.dump({"holidays": ["2026-12-24"], "flat_by": "15:00"}, fh)
            extra = alerts.load_holidays(cfg)
            self.assertEqual(extra, {"2026-12-24": "holiday in config.json"})
            st = alerts.market_status(ist("2026-12-24T10:00"), extra)
            self.assertEqual((st["open"], st["reason"], st["next_open"]),
                             (False, "holiday in config.json", "2026-12-28 09:15"))
            self.assertEqual(alerts.load_holidays(os.path.join(d, "none.json")), {})
            with open(cfg, "w") as fh:
                json.dump({"holidays": ["24 Dec"]}, fh)
            with self.assertRaises(alerts.AlertError):
                alerts.load_holidays(cfg)
        finally:
            shutil.rmtree(d)


class Quotes(unittest.TestCase):
    def read(self, sym):
        with open(os.path.join(QDIR, "yahoo-%s.json" % sym), encoding="utf-8") as fh:
            return fh.read()

    def test_yahoo_fixtures(self):
        q = alerts.parse_yahoo(self.read("RELIANCE"), "RELIANCE")
        self.assertEqual((q["symbol"], q["price"], q["prev_close"], q["open"]), ("RELIANCE", 1340.5, 1344.8, 1339.0))
        self.assertEqual((q["time"], q["currency"], q["source"]), ("2026-11-17T05:19:04Z", "INR", "yahoo"))
        self.assertEqual(alerts.parse_yahoo(self.read("M&M"), "M&M")["price"], 3385.8)
        self.assertEqual(alerts.parse_yahoo(self.read("BAJAJ-AUTO"), "BAJAJ-AUTO")["prev_close"], 10734.0)

    def test_the_answer_must_be_the_nse_listing_asked_for(self):
        with self.assertRaises(alerts.AlertError) as cm:
            alerts.parse_yahoo(self.read("RELIANCE"), "SBIN")
        self.assertIn("Yahoo answered for RELIANCE.NS, not SBIN.NS", str(cm.exception))
        usd = self.read("RELIANCE").replace('"currency":"INR"', '"currency":"USD"')
        with self.assertRaises(alerts.AlertError) as cm:
            alerts.parse_yahoo(usd, "RELIANCE")
        self.assertIn("not INR", str(cm.exception))
        with self.assertRaises(alerts.AlertError) as cm:
            alerts.parse_yahoo(self.read("ZZZZNOTREAL"), "ZZZZNOTREAL")
        self.assertIn("ZZZZNOTREAL.NS: No data found", str(cm.exception))

    def test_urls_and_user_agent(self):
        self.assertEqual(alerts.UA, UA)
        self.assertEqual(alerts.yahoo_url("RELIANCE"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=1d")
        self.assertEqual(alerts.yahoo_url("M&M"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/M%26M.NS?range=1d&interval=1d")
        self.assertIn("/chart/BAJAJ-AUTO.NS?", alerts.yahoo_url("BAJAJ-AUTO"))
        self.assertIn("//query1.finance.yahoo.com/", alerts.yahoo_url("SBIN", alerts.YAHOO_HOSTS[1]))
        self.assertFalse(hasattr(alerts, "stooq_url"))

    def test_symbols(self):
        self.assertEqual(alerts.norm_symbol("reliance.ns"), "RELIANCE")
        self.assertEqual(alerts.norm_symbol(" m&m "), "M&M")
        self.assertEqual(alerts.norm_symbol("bajaj-auto"), "BAJAJ-AUTO")
        self.assertEqual(alerts.norm_symbol("20MICRONS"), "20MICRONS")
        for bad in ("", "RELIANCE INDUSTRIES", "^NSEI", "-X", "A" * 21, None):
            self.assertIsNone(alerts.norm_symbol(bad), bad)

    def test_inr(self):
        self.assertEqual(alerts.inr(123456.5), "₹1,23,456.50")
        self.assertEqual(alerts.inr(-1250), "-₹1,250.00")
        self.assertEqual(alerts.inr(817.7), "₹817.70")
        self.assertEqual(alerts.inr(10775), "₹10,775.00")
        self.assertEqual(alerts.inr(12345678.9), "₹1,23,45,678.90")
        self.assertEqual(alerts.inr(0.005), "₹0.01")

    def test_offline_quotes_and_errors(self):
        q, errors = alerts.get_quotes(["RELIANCE", "sbin.ns", "ZZZZNOTREAL", "NOPE", "BAD SYMBOL"], QDIR)
        self.assertEqual(sorted(q), ["RELIANCE", "SBIN"])
        self.assertEqual(q["SBIN"]["price"], 1110.8)
        self.assertEqual(len(errors), 3)
        self.assertIn("No data found", errors[0])
        self.assertIn("no offline quote file", errors[1])
        self.assertIn("not an NSE symbol", errors[2])

    def test_offline_gz(self):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(QDIR, "yahoo-M&M.json"), "rb") as src:
                with gzip.open(os.path.join(d, "yahoo-M&M.json.gz"), "wb") as dst:
                    dst.write(src.read())
            q, errors = alerts.get_quotes(["M&M"], d)
            self.assertEqual((q["M&M"]["price"], errors), (3385.8, []))
        finally:
            shutil.rmtree(d)


class FakeNet(object):
    """Stands in for urllib: answers each request with the next status code."""

    def __init__(self, codes, body=None):
        self.codes, self.calls, self.sleeps, self.contexts = list(codes), [], [], []
        with open(os.path.join(QDIR, "yahoo-RELIANCE.json"), encoding="utf-8") as fh:
            self.body = body or fh.read()

    def urlopen(self, req, timeout=None, context=None):
        self.calls.append((req.full_url.split("/")[2], req.get_header("User-agent")))
        self.contexts.append(context)
        self.urls = getattr(self, "urls", []) + [req.full_url]
        code = self.codes.pop(0)
        if code != 200:
            fp = io.BytesIO(b'{"chart":{"result":null,"error":{"code":"Not Found",'
                            b'"description":"No data found, symbol may be delisted"}}}' if code == 404 else b"")
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, fp)
        return io.BytesIO(self.body.encode("utf-8"))


class Network(unittest.TestCase):
    def setUp(self):
        self.saved = (alerts._urlopen, alerts._sleep)

    def tearDown(self):
        alerts._urlopen, alerts._sleep = self.saved

    def net(self, codes, body=None):
        f = FakeNet(codes, body)
        alerts._urlopen = f.urlopen
        alerts._sleep = f.sleeps.append
        return f

    def hosts(self, f):
        return [h.split(".")[0] for h, _ in f.calls]

    def test_query2_first_with_an_honest_user_agent_and_verified_tls(self):
        f = self.net([200])
        q, errors = alerts.get_quotes(["RELIANCE"])
        self.assertEqual((q["RELIANCE"]["price"], errors), (1340.5, []))
        self.assertEqual(f.calls, [("query2.finance.yahoo.com", UA)])
        ctx = f.contexts[0]
        self.assertIsInstance(ctx, ssl.SSLContext)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_m_and_m_is_sent_encoded(self):
        with open(os.path.join(QDIR, "yahoo-M&M.json"), encoding="utf-8") as fh:
            f = self.net([200], fh.read())
        q, errors = alerts.get_quotes(["m&m"])
        self.assertEqual((q["M&M"]["price"], errors), (3385.8, []))
        self.assertIn("/v8/finance/chart/M%26M.NS?range=1d&interval=1d", f.urls[0])

    def test_one_retry_with_a_pause_on_429(self):
        f = self.net([429, 200])
        self.assertEqual(alerts.get_quotes(["RELIANCE"])[0]["RELIANCE"]["price"], 1340.5)
        self.assertEqual(self.hosts(f), ["query2", "query2"])
        self.assertEqual(f.sleeps, [alerts.BACKOFF])

    def test_falls_back_to_query1_on_429_and_5xx(self):
        f = self.net([429, 429, 200])
        self.assertIn("RELIANCE", alerts.get_quotes(["RELIANCE"])[0])
        self.assertEqual(self.hosts(f), ["query2", "query2", "query1"])
        f = self.net([503, 200])
        self.assertIn("RELIANCE", alerts.get_quotes(["RELIANCE"])[0])
        self.assertEqual(self.hosts(f), ["query2", "query1"])
        f = self.net([500, 502])
        q, errors = alerts.get_quotes(["RELIANCE"])
        self.assertEqual(q, {})
        self.assertIn("query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS answered HTTP 502", errors[0])

    def test_a_404_is_yahoos_answer_and_a_403_is_not_retried(self):
        f = self.net([404])
        q, errors = alerts.get_quotes(["NOPE"])
        self.assertIn("NOPE.NS: No data found", errors[0])
        self.assertEqual(self.hosts(f), ["query2"])
        f = self.net([403])
        q, errors = alerts.get_quotes(["RELIANCE"])
        self.assertIn("HTTP 403", errors[0])
        self.assertEqual(self.hosts(f), ["query2"])

    def test_a_pause_between_symbols(self):
        f = self.net([200, 200])
        alerts.get_quotes(["RELIANCE", "RELIANCE.NS"])
        self.assertEqual(f.sleeps, [alerts.PAUSE])


class Rules(unittest.TestCase):
    def setUp(self):
        self.q, _ = alerts.get_quotes(["RELIANCE", "SBIN", "M&M", "HDFCBANK"], QDIR)
        self.now = ist("2026-11-17T10:00")
        self.open = alerts.market_status(self.now)

    def at(self, text, rules, state=None, quotes=None):
        now = ist(text)
        return alerts.evaluate(rules, self.q if quotes is None else quotes, alerts.market_status(now),
                               state or {}, now)

    def test_a_target_rings_once(self):
        rules = [{"id": "m&m-3380", "symbol": "M&M", "type": "above", "price": 3380}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertEqual(len(rings), 1)
        self.assertEqual(rings[0]["message"], "M&M is ₹3,385.80: at or above your alert level ₹3,380.00.")
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(rings, [])

    def test_stop_and_rearm(self):
        rules = [{"id": "hdfc-stop", "symbol": "HDFCBANK", "type": "stop", "price": 820, "rearm": True}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertIn("HDFCBANK is ₹817.70: at or below your stop ₹820.00", rings[0]["message"])
        self.q["HDFCBANK"]["price"] = 821.0
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(rings, [])
        self.assertNotIn("hdfc-stop", st["rung"])
        self.q["HDFCBANK"]["price"] = 815.5
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(len(rings), 1)

    def test_pct_move_from_previous_close(self):
        up = [{"id": "sbin-1.5", "symbol": "SBIN", "type": "pct_move", "pct": 1.5}]
        rings, st = alerts.evaluate(up, self.q, self.open, {}, self.now)
        self.assertEqual(rings[0]["message"], "SBIN moved +1.86% to ₹1,110.80 (from ₹1,090.50).")
        self.assertEqual(alerts.evaluate(up, self.q, self.open, st, self.now)[0], [])
        big = [{"id": "sbin-3", "symbol": "SBIN", "type": "pct_move", "pct": 3}]
        self.assertEqual(alerts.evaluate(big, self.q, self.open, {}, self.now)[0], [])
        down = [{"id": "sbin-down", "symbol": "SBIN", "type": "pct_move", "pct": 1, "direction": "down"}]
        self.assertEqual(alerts.evaluate(down, self.q, self.open, {}, self.now)[0], [])
        rel = [{"id": "rel-down", "symbol": "RELIANCE", "type": "pct_move", "pct": 0.3, "direction": "down"}]
        rings, _ = alerts.evaluate(rel, self.q, self.open, {}, self.now)
        self.assertIn("RELIANCE moved -0.32% to ₹1,340.50 (from ₹1,344.80)", rings[0]["message"])

    def test_opening_bell_once_per_trading_day(self):
        rules = [{"id": "bell", "type": "opening_bell"}]
        self.assertEqual(self.at("2026-11-17T09:10", rules)[0], [])
        rings, st = self.at("2026-11-17T09:15", rules)
        self.assertEqual(rings[0]["message"], "Opening bell: NSE is open (2026-11-17 09:15 IST), regular session "
                                              "09:15 to 15:30 IST.")
        self.assertEqual(self.at("2026-11-17T11:00", rules, st)[0], [])
        self.assertEqual(self.at("2026-11-10T09:20", rules)[0], [])                 # Diwali Balipratipada
        self.assertEqual(len(self.at("2026-11-18T09:16", rules, st)[0]), 1)

    def test_the_plan_gives_a_stop_a_target_and_flat_by(self):
        rules = plan_rules()
        self.assertEqual([(r["id"], r["type"]) for r in rules],
                         [("reliance-plan-stop", "stop"), ("reliance-plan-target", "target"),
                          ("reliance-flat-by", "time")])
        self.assertEqual((rules[0]["price"], rules[1]["price"]), (1337.7, 1345.7))
        self.assertEqual((rules[2]["at"], rules[2]["label"]), ("15:00", "flat by"))
        self.assertNotIn("before_close", rules[2])
        self.assertTrue(all(r["date"] == "2026-11-17" for r in rules))
        rings, st = self.at("2026-11-17 10:49", rules)          # the real 10:49 quote, 1340.50
        self.assertEqual(rings, [])
        q = json.loads(json.dumps(self.q))
        q["RELIANCE"]["price"] = 1337.5
        rings, st = self.at("2026-11-17 11:30", rules, st, q)
        self.assertEqual([r["id"] for r in rings], ["reliance-plan-stop"])
        self.assertEqual(rings[0]["message"], "RELIANCE is ₹1,337.50: at or below your stop ₹1,337.70 "
                                              "(plan stop: opening-range low).")
        q["RELIANCE"]["price"] = 1345.7
        self.assertEqual([r["id"] for r in self.at("2026-11-17 12:00", rules, st, q)[0]], ["reliance-plan-target"])

    def test_flat_by_rings_once_and_names_growws_square_off(self):
        rules = plan_rules()[2:]
        self.assertEqual(self.at("2026-11-17 14:59", rules)[0], [])
        rings, st = self.at("2026-11-17 15:00", rules)
        self.assertEqual(len(rings), 1)
        msg = rings[0]["message"]
        self.assertTrue(msg.startswith("Flat by: it is 15:00 IST (set for 15:00), the market closes at 15:30. "
                                       "If RELIANCE is still open, close it now: Groww takes no new intraday orders "
                                       "on F&O stocks after 15:05 and squares off intraday (MIS) positions still "
                                       "open at 15:20 (₹50 + GST per position)."), msg)
        self.assertIn("nse-groww's ticket.py flatten --symbol RELIANCE", msg)
        self.assertEqual(self.at("2026-11-17 15:15", rules, st)[0], [])
        late = self.at("2026-11-17 15:25", rules)[0][0]["message"]          # a check that missed 15:00
        self.assertIn("It is past 15:20: Groww squares off intraday (MIS) positions still open then", late)
        rings, _ = self.at("2026-11-17 15:45", rules)
        self.assertIn("the market has closed", rings[0]["message"])
        self.assertEqual(self.at("2026-11-18 15:11", rules, st)[0], [])     # plan rules: their day only

    def test_flat_by_comes_from_the_plan_then_the_config_then_15_10(self):
        p = plan_doc()
        self.assertEqual(alerts.rules_from_plan(dict(p, flat_by=None))[2]["at"], "15:00")
        self.assertEqual(alerts.rules_from_plan(dict(p, flat_by=None), "14:45")[2]["at"], "14:45")
        self.assertEqual(alerts.rules_from_plan(dict(p, flat_by="14:55"), "14:45")[2]["at"], "14:55")
        self.assertEqual(alerts.rules_from_plan(dict(p, symbol="m&m.ns"))[0]["id"], "m&m-plan-stop")

    def test_a_time_alert_rings_on_trading_days_only(self):
        rule = [{"id": "flat", "type": "time", "at": "15:00", "label": "flat by"}]
        self.assertEqual(self.at("2026-11-10 15:11", rule)[0], [])          # NSE holiday
        self.assertEqual(self.at("2026-11-21 15:11", rule)[0], [])          # Saturday
        rings, st = self.at("2026-11-11 15:00", rule)
        self.assertEqual(len(rings), 1)
        self.assertEqual(st["rung"]["flat"], "2026-11-11T09:30:00Z")         # 15:00 IST in UTC
        self.assertEqual(self.at("2026-11-11 15:20", rule, st)[0], [])

    def test_the_example_rules_are_valid(self):
        with open(os.path.join(ROOT, "nse-alerts", "alerts.example.json"), encoding="utf-8") as fh:
            rules = json.load(fh)["rules"]
        for r in rules:
            alerts.validate(r)
        self.assertEqual([r for r in rules if r["id"].startswith("reliance-")], plan_rules())
        rings, _ = self.at("2026-11-17 10:50", rules)
        self.assertEqual(sorted(r["id"] for r in rings), ["opening-bell", "sbin-move-1.5"])   # M&M 3,385.80 < 3,400

    def test_bad_rules(self):
        for rule in ({"id": "x", "type": "moon"}, {"id": "x", "type": "above", "symbol": "SBIN"},
                     {"id": "x", "type": "time", "at": "3pm"},
                     {"id": "x", "type": "above", "symbol": "STATE BANK", "price": 1}):
            with self.assertRaises(alerts.AlertError, msg=rule):
                alerts.evaluate([rule], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan({"symbol": "RELIANCE", "stop": 1337.7})
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan(dict(plan_doc(), flat_by="3:00 pm"))


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.rules = os.path.join(self.d, "alerts.json")
        self.state = os.path.join(self.d, "state", "alerts.json")
        self.prices = os.path.join(self.d, "prices.json")
        self.cfg = os.path.join(self.d, "config.json")      # absent unless a test writes it

    def tearDown(self):
        shutil.rmtree(self.d)

    def check(self, now):
        return run(["check", "--rules", self.rules, "--state", self.state, "--prices-out", self.prices,
                    "--config", self.cfg, "--offline", QDIR, "--now", now])

    def test_the_trade_from_the_plan(self):
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state,
                             "--config", self.cfg])
        self.assertEqual(code, 0)
        self.assertIn("Added reliance-flat-by", text)
        self.assertNotIn("Note: flat-by", text)
        self.assertEqual(run(["add", "--rules", self.rules, "--opening-bell"])[0], 0)

        code, text, _ = self.check("2026-11-17 10:49")
        self.assertEqual(code, 0)                                      # the opening bell rings first
        code, text, _ = self.check("2026-11-17 10:50")
        self.assertEqual(code, 1)
        self.assertIn("NSE OPEN (2026-11-17 10:50 IST, regular session).", text)
        self.assertIn("Nothing hit. 4 rule(s) watched today, RELIANCE ₹1,340.50.", text)
        with open(self.prices, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["prices"], {"RELIANCE": 1340.5})
        self.assertEqual(doc["detail"]["RELIANCE"]["time"], "2026-11-17T05:19:04Z")
        self.assertEqual(doc["detail"]["RELIANCE"]["fetched"], "2026-11-17T05:20:00Z")

        code, text, _ = self.check("2026-11-17 15:00")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Flat by: it is 15:00 IST", text)
        self.assertIn("not an order", text)
        self.assertIn("place it in Groww", text)
        self.assertEqual(self.check("2026-11-17 15:12")[0], 1)
        code, text, _ = self.check("2026-11-18 09:20")
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-11-18 09:25")
        self.assertIn("Nothing hit. 1 rule(s) watched today.", text)

        # a new plan replaces the plan rules and re-arms them
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state,
                             "--config", self.cfg])
        with open(self.rules, encoding="utf-8") as fh:
            ids = [r["id"] for r in json.load(fh)["rules"]]
        self.assertEqual(sorted(ids), ["opening-bell", "reliance-flat-by", "reliance-plan-stop",
                                       "reliance-plan-target"])
        with open(self.state, encoding="utf-8") as fh:
            self.assertNotIn("reliance-flat-by", json.load(fh)["rung"])

    def test_the_skill_example(self):
        run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state, "--config", self.cfg])
        code, text, _ = self.check("2026-11-17 10:50")
        self.assertEqual(text.splitlines(), ["NSE OPEN (2026-11-17 10:50 IST, regular session).",
                                             "Nothing hit. 3 rule(s) watched today, RELIANCE ₹1,340.50."])
        code, text, _ = self.check("2026-11-17 15:00")
        self.assertIn("ALERT Flat by: it is 15:00 IST (set for 15:00), the market closes at 15:30. If RELIANCE is "
                      "still open, close it now: Groww takes no new intraday orders on F&O stocks after 15:05 and "
                      "squares off intraday (MIS) positions still open at 15:20 (₹50 + GST per position). nse-groww's "
                      "ticket.py flatten --symbol RELIANCE builds the sell ticket, then nse-risk, then you place it "
                      "in the Groww app.", text)

    def test_a_flat_by_at_or_after_square_off_gets_a_note(self):
        late = os.path.join(self.d, "plan.json")
        with open(late, "w") as fh:
            json.dump(dict(plan_doc(), flat_by="15:25"), fh)
        code, text, _ = run(["add", "--from-plan", "--plan", late, "--rules", self.rules, "--state", self.state,
                             "--config", self.cfg])
        self.assertEqual(code, 0)
        self.assertIn("Note: flat-by 15:25 is at or after 15:20, when Groww squares off", text)

    def test_a_portfolio_evening_and_morning(self):
        for args in (["--opening-bell"], ["--symbol", "SBIN", "--above", "1120"],
                     ["--symbol", "hdfcbank", "--stop", "800"]):
            code, _, _ = run(["add", "--rules", self.rules] + args)
            self.assertEqual(code, 0)
        code, _, err = run(["add", "--rules", self.rules, "--symbol", "SBIN", "--above", "1120"])
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)

        code, text, _ = self.check("2026-11-17T16:00")
        self.assertEqual(code, 1)
        self.assertIn("NSE closed (2026-11-17 16:00 IST, after the close).", text)
        self.assertIn("Nothing hit", text)
        with open(self.prices, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["prices"], {"SBIN": 1110.8, "HDFCBANK": 817.7})

        code, text, _ = self.check("2026-11-18T09:16")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-11-18T09:45")
        self.assertEqual(code, 1)

        code, text, _ = run(["list", "--rules", self.rules, "--state", self.state])
        self.assertIn("rang 2026-11-18T03:46:00Z, 09:16 IST", text)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 0)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 1)

    def test_add_time_and_target(self):
        code, text, _ = run(["add", "--rules", self.rules, "--at", "15:00", "--label", "flat by",
                             "--symbol", "reliance"])
        self.assertEqual(code, 0)
        code, text, _ = run(["add", "--rules", self.rules, "--symbol", "M&M", "--target", "3400",
                             "--date", "2026-11-17"])
        self.assertEqual(code, 0)
        with open(self.rules, encoding="utf-8") as fh:
            rules = json.load(fh)["rules"]
        self.assertEqual(rules[0], {"id": "time-1500", "type": "time", "at": "15:00", "label": "flat by",
                                    "symbol": "RELIANCE"})
        self.assertEqual(rules[1], {"id": "m&m-target-3400", "symbol": "M&M", "type": "target",
                                    "price": 3400.0, "date": "2026-11-17"})
        self.assertEqual(run(["add", "--rules", self.rules, "--at", "4pm"])[0], 1)
        code, _, err = run(["add", "--rules", self.rules, "--symbol", "STATE BANK", "--above", "1"])
        self.assertEqual(code, 1)
        self.assertIn("not an NSE symbol", err)
        code, _, err = run(["add", "--rules", self.rules, "--from-plan", "--plan", os.path.join(self.d, "none")])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)

    def test_quote_write_merges_into_the_prices_file(self):
        code, text, _ = run(["quote", "RELIANCE", "M&M", "--offline", QDIR, "--write", self.prices,
                             "--now", "2026-11-17T05:19:10Z"])
        self.assertEqual(code, 0)
        self.assertIn("RELIANCE   ₹1,340.50 (-0.32% vs prev close ₹1,344.80)  10:49 IST  [yahoo]", text)
        self.assertIn("M&M        ₹3,385.80 (+1.40% vs prev close ₹3,339.00)", text)
        self.assertIn("delayed", text)
        run(["quote", "sbin.ns", "--offline", QDIR, "--write", self.prices])
        with open(self.prices, encoding="utf-8") as fh:
            doc = json.load(fh)
        # the schema nse-budget and nse-risk read
        self.assertEqual(sorted(doc), ["as_of", "detail", "note", "prices", "source"])
        self.assertEqual(sorted(doc["prices"]), ["M&M", "RELIANCE", "SBIN"])
        self.assertEqual(sorted(doc["detail"]["SBIN"]), ["fetched", "prev_close", "source", "time"])
        self.assertEqual(doc["detail"]["M&M"]["prev_close"], 3339.0)
        self.assertEqual(doc["detail"]["M&M"]["fetched"], "2026-11-17T05:19:10Z")
        self.assertEqual(doc["source"], "yahoo")

    def test_quote_holdings_from_portfolio_and_plan(self):
        pf = os.path.join(self.d, "portfolio.json")
        with open(pf, "w") as fh:
            json.dump({"cash": 0, "positions": [{"ticker": "SBIN", "shares": 1}, {"symbol": "m&m.ns", "shares": 2}]}, fh)
        code, text, _ = run(["quote", "--holdings", "--portfolio", pf, "--plan", PLAN, "--offline", QDIR, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(json.loads(text)["quotes"]), ["M&M", "RELIANCE", "SBIN"])

    def test_clock(self):
        code, text, _ = run(["clock", "--now", "2026-11-17 10:15", "--config", self.cfg])
        self.assertEqual(code, 0)
        self.assertEqual(text.splitlines(), [
            "IST 2026-11-17 10:15 (Tuesday): NSE OPEN, regular session.",
            "Today's session: 09:15 to 15:30 IST.",
            "Groww takes no new intraday orders on F&O stocks after 15:05 and squares off intraday (MIS) positions "
            "still open at 15:20 (₹50 + GST per position); the pack's default flat-by is 15:00."])
        st = json.loads(run(["clock", "--now", "2026-11-17 10:15", "--config", self.cfg, "--json"])[1])
        self.assertEqual((st["no_new_orders"], st["square_off"]), ("15:05", "15:20"))
        code, text, _ = run(["clock", "--now", "2026-11-24T10:00", "--config", self.cfg])
        self.assertEqual(code, 1)
        self.assertIn("NSE closed, NSE holiday: Prakash Gurpurb Sri Guru Nanak Dev.", text)
        self.assertIn("Next open: 2026-11-25 09:15 IST.", text)
        code, text, _ = run(["clock", "--now", "2027-02-01 10:00", "--config", self.cfg])
        self.assertEqual(code, 0)
        self.assertIn("NSE holiday list for 2027 not loaded: check nseindia.com.", text)
        with open(self.cfg, "w") as fh:
            json.dump({"holidays": ["2026-12-24"]}, fh)
        code, text, _ = run(["clock", "--now", "2026-12-24 10:00", "--config", self.cfg])
        self.assertEqual(code, 1)
        self.assertIn("NSE closed, holiday in config.json.", text)
        code, text, _ = run(["clock", "--now", "2026-12-24 10:00", "--config", self.cfg, "--json"])
        self.assertEqual(json.loads(text)["next_open"], "2026-12-28 09:15")

    def test_errors_are_one_plain_line(self):
        with open(self.cfg, "w") as fh:
            json.dump({"holidays": ["Christmas"]}, fh)
        for argv in (["check", "--rules", self.rules, "--offline", QDIR],
                     ["clock", "--config", self.cfg],
                     ["quote", "STATE BANK", "--offline", QDIR],
                     ["clock", "--now", "noon"]):
            code, _, err = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertTrue(err.startswith("alerts.py: "), err)


if __name__ == "__main__":
    unittest.main()
