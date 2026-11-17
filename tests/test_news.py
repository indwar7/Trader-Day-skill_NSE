"""Tests for nse-news/news.py. Offline, standard library only.

Fixtures in tests/fixtures/nse-news/ are real responses captured on Tuesday
2026-11-17 (IST) with the tool's own User-Agent, cut down to whole <item>
elements (bytes unchanged):

  gnews-SBIN.xml                       10:39  Google News, "State Bank of India" when:24h, en-IN
  gnews-RELIANCE/HDFCBANK/M&M.xml      10:50  same, searched by company name
  chart-<SYMBOL>.json                  10:51  Yahoo v8 chart, range=1d interval=5m
  nse-announcements.xml                11:03  NSE Online_announcements.xml: every non-NAV item,
                                              the first 3 NAV items, the oldest item
  nse-actions.xml, nse-results.xml     11:03  NSE Corporate_action.xml, Financial_Results.xml, whole
  nse-meetings.xml                     11:03  NSE Board_Meetings.xml, whole (it was empty)
  nse-meetings-2026-10-28.xml          NSE Board_Meetings.xml as archived by the Wayback Machine
                                       (2026-10-28 12:02 UTC), first 12 items

The shared fixtures in tests/fixtures/nse/ (09:48 IST the same day) are read too.
"""

import datetime
import email.message
import io
import json
import os
import ssl
import tempfile
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from _load import FIX, load

news = load("nse-news", "news")
DIR = os.path.join(FIX, "nse-news")
SHARED = os.path.join(FIX, "nse")
NOW = "2026-11-17 11:03"  # IST
UTC = datetime.timezone.utc
_NAMES = []


def raw(name, folder=DIR):
    with open(os.path.join(folder, name), "rb") as fh:
        return fh.read()


def names():
    """{symbol: [names]} from the shared Nifty 500 list and EQUITY_L.csv."""
    if not _NAMES:
        _NAMES.append(news.merge_names([news.parse_nifty500(raw("nifty500list.csv", SHARED)),
                                        news.parse_equity_l(raw("EQUITY_L.csv", SHARED))]))
    return _NAMES[0]


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = news.main(args)
    return code, out.getvalue(), err.getvalue()


def own_lines(out):
    """The tool's own text: move lines, notes and the closing line (not third-party headlines)."""
    return [l for l in out.splitlines()
            if l.startswith("note:") or l.startswith("Checked") or "vs yesterday's close" in l]


class Time(unittest.TestCase):
    def test_ist_is_utc_plus_0530_all_year(self):
        self.assertEqual(news.to_ist(datetime.datetime(2026, 11, 17, 3, 45, tzinfo=UTC)),
                         datetime.datetime(2026, 11, 17, 9, 15))
        self.assertEqual(news.to_ist(datetime.datetime(2026, 1, 15, 10, 0)),  # naive = UTC
                         datetime.datetime(2026, 1, 15, 15, 30))
        self.assertEqual(news.to_ist(datetime.datetime(2026, 7, 15, 10, 0, tzinfo=UTC)).hour, 15)  # no DST
        self.assertEqual(news.ist_to_utc(datetime.datetime(2026, 11, 17, 15, 30)),
                         datetime.datetime(2026, 11, 17, 10, 0, tzinfo=UTC))
        self.assertEqual(news.to_ist(datetime.datetime(2026, 11, 17, 9, 15, tzinfo=news.IST)),
                         datetime.datetime(2026, 11, 17, 9, 15))
        # around midnight the IST date is a day ahead of the UTC date
        self.assertEqual(news.to_ist(datetime.datetime(2026, 11, 16, 19, 57, 1, tzinfo=UTC)),
                         datetime.datetime(2026, 11, 17, 1, 27, 1))

    def test_parse_times(self):
        self.assertEqual(news.parse_time("17-Nov-2026 09:31:00"),  # NSE feeds: IST
                         datetime.datetime(2026, 11, 17, 4, 1, tzinfo=UTC))
        self.assertEqual(news.parse_time("02-May-2026 16:46"),
                         datetime.datetime(2026, 5, 2, 11, 16, tzinfo=UTC))
        self.assertEqual(news.parse_time("Mon, 16 Nov 2026 15:49:20 GMT"),  # Google News
                         datetime.datetime(2026, 11, 16, 15, 49, 20, tzinfo=UTC))
        self.assertEqual(news.parse_time("Tue, 17 Nov 2026 09:46:11 +0530"),
                         datetime.datetime(2026, 11, 17, 4, 16, 11, tzinfo=UTC))
        self.assertEqual(news.parse_time("2026-11-17T10:15:00+05:30").hour, 4)
        self.assertEqual(news.parse_time("2026-11-17 10:15"),  # --now with no zone is IST
                         datetime.datetime(2026, 11, 17, 4, 45, tzinfo=UTC))
        for bad in ("soon", "", "31-Foo-2026", "31-Nov-2026 10:00"):
            self.assertIsNone(news.parse_time(bad), bad)
        self.assertEqual(news.parse_date("20-Nov-2026"), datetime.date(2026, 11, 20))
        self.assertIsNone(news.parse_date("-"))


class Symbols(unittest.TestCase):
    def test_clean_symbol(self):
        self.assertEqual(news.clean_symbol("reliance.ns"), "RELIANCE")
        self.assertEqual(news.clean_symbol(" m&m "), "M&M")
        self.assertEqual(news.clean_symbol("bajaj-auto"), "BAJAJ-AUTO")
        self.assertEqual(news.clean_symbol("20MICRONS"), "20MICRONS")
        for bad in ("BRK.B", "", None, "A" * 21, "-ABC", "M M"):
            self.assertIsNone(news.clean_symbol(bad), bad)

    def test_yahoo_symbols(self):
        self.assertEqual(news.yahoo_symbol("M&M"), "M%26M.NS")
        self.assertEqual(news.yahoo_symbol("BAJAJ-AUTO"), "BAJAJ-AUTO.NS")
        self.assertEqual(news.yahoo_symbol("RELIANCE"), "RELIANCE.NS")

        class Recorder(object):
            offline = None
            urls = []

            def get(self, url, name, retry_429=False):
                self.urls.append(url)
                return raw("chart-M&M.json")

        f = Recorder()
        moves = news.collect_moves(f, {"M&M": None})
        self.assertEqual(f.urls, ["https://query2.finance.yahoo.com/v8/finance/chart/M%26M.NS"
                                  "?range=1d&interval=5m"])
        self.assertEqual(moves[0]["ticker"], "M&M")

    def test_google_news_searches_the_company_name(self):
        n = names()
        self.assertEqual(news.query_name("M&M", {"M&M": None}, n), "Mahindra & Mahindra")
        self.assertEqual(news.query_name("SBIN", {"SBIN": None}, n), "State Bank of India")
        self.assertEqual(news.query_name("HDFCBANK", {}, n), "HDFC Bank")
        self.assertEqual(news.query_name("RELIANCE", {}, n), "Reliance Industries")
        self.assertEqual(news.query_name("BAJAJ-AUTO", {}, n), "Bajaj Auto")
        self.assertEqual(news.query_name("SBIN", {"SBIN": "SBI"}, n), "SBI")  # the watchlist's name wins
        self.assertIsNone(news.query_name("ZZZZ", {}, n))
        url = news.gnews_url("Mahindra & Mahindra", "M&M", 24)
        self.assertEqual(url, "https://news.google.com/rss/search?q=%22Mahindra+%26+Mahindra%22+when%3A24h"
                              "&hl=en-IN&gl=IN&ceid=IN:en")
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(q["q"], ['"Mahindra & Mahindra" when:24h'])
        self.assertEqual(q["ceid"], ["IN:en"])
        self.assertIn("when%3A13h", news.gnews_url("HDFC Bank", "HDFCBANK", 12.5))
        self.assertIn("when%3A3d", news.gnews_url("HDFC Bank", "HDFCBANK", 72))
        self.assertIn("q=%22ZZZZ%22+NSE+when%3A48h", news.gnews_url(None, "ZZZZ", 48))

    def test_inr(self):
        self.assertEqual(news.inr(123456.5), "₹1,23,456.50")
        self.assertEqual(news.inr(-1250), "-₹1,250.00")
        self.assertEqual(news.inr(129245), "₹1,29,245.00")
        self.assertEqual(news.inr(10000000), "₹1,00,00,000.00")
        self.assertEqual(news.inr(999), "₹999.00")
        self.assertEqual(news.inr(0.5), "₹0.50")
        self.assertEqual(news.inr(1110.3), "₹1,110.30")


class Names(unittest.TestCase):
    def test_name_lists(self):
        nifty = news.parse_nifty500(raw("nifty500list.csv", SHARED))
        self.assertEqual(len(nifty), 501)
        self.assertEqual(nifty["M&M"], "Mahindra & Mahindra Ltd.")
        eq = news.parse_equity_l(raw("EQUITY_L.csv", SHARED))
        self.assertEqual(eq["BAJAJ-AUTO"], "Bajaj Auto Limited")
        self.assertEqual(names()["M&M"], ["Mahindra & Mahindra Ltd.", "Mahindra & Mahindra Limited"])
        self.assertEqual(names()["SBIN"], ["State Bank of India"])

    def test_short_and_normal_names(self):
        self.assertEqual(news.short_name("Reliance Industries Ltd."), "Reliance Industries")
        self.assertEqual(news.short_name("Bajaj Auto Limited"), "Bajaj Auto")
        self.assertEqual(news.short_name("State Bank of India"), "State Bank of India")
        self.assertEqual(news.norm_name("Mahindra & Mahindra Limited"), "mahindra and mahindra")
        self.assertEqual(news.norm_name("Mahindra & Mahindra Ltd."), "mahindra and mahindra")
        self.assertEqual(news.norm_name("SIGMA ADVANCED SYSTEMS LIMITED"), "sigma advanced systems")
        self.assertEqual(news.norm_name("Ingersoll Rand (India) Limited"), "ingersoll rand india")

    def test_offline_lists_without_a_cache(self):
        errors = []
        got = news.load_names(news.Fetcher(offline=SHARED), None, errors)
        self.assertEqual(got, names())
        self.assertEqual(errors, [])

    def test_cache_is_refreshed_weekly_and_only_when_complete(self):
        class Lists(object):
            offline = None

            def __init__(self, fail=()):
                self.calls, self.fail = [], fail

            def get(self, url, name, retry_429=False):
                self.calls.append(name)
                if name in self.fail:
                    raise news.NewsError("HTTP 503 for %s" % url)
                return raw(name, SHARED)

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state", "nse-names.json")
            now = datetime.datetime(2026, 11, 17, 5, 0, tzinfo=UTC)
            f = Lists()
            self.assertEqual(news.load_names(f, path, [], now_real=now)["RELIANCE"][0], "Reliance Industries Ltd.")
            self.assertEqual(f.calls, ["nifty500list.csv", "EQUITY_L.csv"])
            f = Lists()
            news.load_names(f, path, [], now_real=now + datetime.timedelta(days=6))
            self.assertEqual(f.calls, [])  # fresh: no download
            f, errors = Lists(fail=("EQUITY_L.csv",)), []
            got = news.load_names(f, path, errors, now_real=now + datetime.timedelta(days=8))
            self.assertEqual(f.calls, ["nifty500list.csv", "EQUITY_L.csv"])
            self.assertIn("PARKHOTELS", got)  # not in the Nifty 500: the complete week-old cache is used
            self.assertIn("company names (EQUITY_L.csv): HTTP 503", errors[0])
            self.assertEqual(errors[1], "company names: using the cache from %s" % now.isoformat())
            with open(path) as fh:
                self.assertEqual(json.load(fh)["fetched"], now.isoformat())  # the half download was not cached
        with tempfile.TemporaryDirectory() as tmp:  # no cache at all: the half list is better than none
            got = news.load_names(Lists(fail=("EQUITY_L.csv",)), os.path.join(tmp, "n.json"), [], now_real=now)
            self.assertIn("RELIANCE", got)
            self.assertNotIn("PARKHOTELS", got)
            self.assertFalse(os.path.exists(os.path.join(tmp, "n.json")))


class Feeds(unittest.TestCase):
    def test_google_news_rss(self):
        items = news.parse_gnews(raw("gnews-reliance.xml", SHARED), "RELIANCE")
        self.assertEqual(len(items), 27)
        first = items[0]
        self.assertEqual(first["title"], "Lcc Projects wins Rs 62.32 Cr order from Reliance Industries - scanx.trade")
        self.assertEqual(first["publisher"], "scanx.trade")
        self.assertEqual(first["time"], datetime.datetime(2026, 11, 16, 15, 49, 20, tzinfo=UTC))
        self.assertTrue(first["link"].startswith("https://news.google.com/rss/articles/"))
        self.assertTrue(first["id"].startswith("gnews:CBMi"))
        self.assertEqual(first["source"], "Google News")

    def test_nse_announcements(self):
        data = raw("nse-ann-rss.xml", SHARED)
        self.assertEqual(news.feed_span(data), (263, datetime.datetime(2026, 11, 16, 19, 57, 1, tzinfo=UTC)))
        items = news.parse_nse_announcements(data, news.Resolver(names()))
        self.assertEqual(len(items), 11)  # 252 mutual-fund NAV declarations dropped
        by = {i["company"]: i for i in items}
        cy = by["Cyient Limited"]
        self.assertEqual(cy["ticker"], "CYIENT")
        self.assertEqual(cy["subject"], "Change in Management")
        self.assertTrue(cy["important"])
        self.assertEqual(cy["title"], "Change in Management: Cyient Limited has informed the Exchange about "
                                      "Change in Senior Management Personnel")
        self.assertEqual(news.to_ist(cy["time"]), datetime.datetime(2026, 11, 17, 8, 41, 33))
        self.assertTrue(cy["link"].endswith("/corporate/CYIENT_17112026083916_Disclosure_CHROSigned.pdf"))
        self.assertFalse(by["IFGL Refractories Limited"]["important"])  # Investor Presentation
        self.assertEqual(by["IFGL Refractories Limited"]["ticker"], "IFGLEXPOR")
        self.assertEqual(by["Apeejay Surrendra Park Hotels Limited"]["ticker"], "PARKHOTELS")
        self.assertEqual(by["SIGMA ADVANCED SYSTEMS LIMITED"]["ticker"], "SIGMAADV")  # renamed from Megasoft
        self.assertEqual(by["R.K. Fashion Accessories Limited"]["ticker"], "")  # not on EQUITY_L

    def test_live_announcements_subjects(self):
        items = news.parse_nse_announcements(raw("nse-announcements.xml"), news.Resolver(names()))
        self.assertEqual(len(items), 54)  # 57 in the slice, 3 NAVs dropped
        imp = {(i["ticker"], i["subject"]): i["important"] for i in items}
        self.assertTrue(imp[("YESBANK", "News Verification")])
        self.assertTrue(imp[("STEL", "Price movement")])
        self.assertTrue(imp[("CARYSIL", "Resignation")])
        self.assertTrue(imp[("CARYSIL", "Change in Directors/KMP/SMP/Auditor/RTA")])
        self.assertFalse(imp[("BANKBARODA", "Record Date Updates")])
        self.assertFalse(imp[("INDUSINDBK", "Press Release")])
        self.assertEqual({i["ticker"] for i in items if i["company"] == "Brainbees Solutions Limited"}, {"FIRSTCRY"})

    def test_symbol_resolution(self):
        r = news.Resolver(names())
        self.assertEqual(r.symbol("Anything", "RELIANCE : x"), "RELIANCE")
        self.assertEqual(r.symbol("Mirae Asset Mutual Fund", "ITETF      : Mirae Asset"), "")  # an ETF, not on EQUITY_L
        self.assertEqual(news.Resolver({}).symbol("x", "ITETF      : Mirae Asset"), "ITETF")
        base = "https://nsearchives.nseindia.com/corporate/"
        self.assertEqual(r.symbol("Renamed Co Limited", "", base + "POWERMECH_17112026092607_CL_Trascript.pdf"),
                         "POWERMECH")
        self.assertEqual(r.symbol("Renamed Co Limited", "", base + "ATUL1_16112026154133_Intimation.pdf"), "")
        self.assertEqual(r.symbol("Atul Limited", "", base + "ATUL1_16112026154133_Intimation.pdf"), "ATUL")

    def test_corporate_actions(self):
        items = news.parse_nse_actions(raw("nse-actions.xml"), news.Resolver(names()))
        self.assertEqual(len(items), 23)
        by = {i["ticker"]: i for i in items}
        pfc = by["PFC"]
        self.assertEqual(pfc["ex_date"], datetime.date(2026, 11, 17))
        self.assertEqual(pfc["subject"], "INTERIM DIVIDEND - RS 4.10 PER SHARE")
        self.assertEqual(pfc["title"], "Power Finance Corporation Limited Ex-date 17-Nov-2026: "
                                       "INTERIM DIVIDEND - RS 4.10 PER SHARE (record date 17-Nov-2026)")
        self.assertIsNone(pfc["time"])  # the feed's pubDate is its build time, not the filing's
        self.assertEqual(by["ZEEL"]["subject"], "BUY BACK")
        self.assertEqual(by["ZEEL"]["ex_date"], datetime.date(2026, 11, 25))
        tagged = news.tag([by["PRAXIS"], by["ZEEL"]], {"PRAXIS": None, "ZEEL": None}, names())
        self.assertEqual(tagged[0]["tags"], ["rights issue"])
        self.assertEqual(tagged[1]["tags"], ["buyback"])

    def test_board_meetings(self):
        self.assertEqual(news.parse_nse_meetings(raw("nse-meetings.xml"), news.Resolver(names())), [])
        items = news.parse_nse_meetings(raw("nse-meetings-2026-10-28.xml"), news.Resolver(names()))
        self.assertEqual(len(items), 12)
        ufo = [i for i in items if i["ticker"] == "UFO"]
        self.assertEqual([u["subject"] for u in ufo], ["Financial Results", "Board Meeting Intimation"])
        self.assertEqual(ufo[0]["meeting_date"], datetime.date(2026, 11, 3))
        self.assertEqual(ufo[0]["title"], "UFO Moviez India Limited Board meeting on 03-Nov-2026: Financial Results")
        self.assertEqual(ufo[0]["id"], ufo[1]["id"])  # the PDF and its XBRL twin are one meeting
        self.assertEqual(news.to_ist(ufo[0]["time"]), datetime.datetime(2026, 10, 28, 13, 15, 14))
        news.tag(ufo, {"UFO": None}, names())
        self.assertEqual(len(news.dedupe(ufo)), 1)
        self.assertIn("board meeting", ufo[1]["tags"])

    def test_results_feed(self):
        items = news.tag(news.parse_nse_results(raw("nse-results.xml"), news.Resolver(names())), {}, names())
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["title"], "Gammon India Limited financial results: First Quarter, Unaudited, "
                                            "Non-Consolidated, period ended 30-Jun-2024")
        self.assertTrue(items[0]["important"])
        self.assertEqual(items[0]["tags"][0], "results")

    def test_bad_xml(self):
        with self.assertRaises(news.NewsError):
            news.parse_gnews(b"<html><title>Sorry...</title>", "SBIN")


class Tagging(unittest.TestCase):
    def test_touches_and_tags_on_real_headlines(self):
        items = news.tag(news.parse_gnews(raw("gnews-RELIANCE.xml"), "RELIANCE"),
                         {"RELIANCE": None, "SBIN": None}, names())
        lcc = [i for i in items if i["title"].startswith("Lcc Projects wins")][0]
        self.assertEqual(lcc["touches"], ["RELIANCE"])
        self.assertEqual(lcc["tags"], ["order win"])

    def test_dedupe_merges_the_same_story(self):
        a = news.parse_gnews(raw("gnews-RELIANCE.xml"), "RELIANCE")
        b = news.parse_gnews(raw("gnews-SBIN.xml"), "SBIN")
        items = news.dedupe(news.tag(a + b, {"RELIANCE": None, "SBIN": None}, names()))
        self.assertEqual(len(items), len(a) + len(b) - 1)
        pat = [i for i in items if i["title"].startswith("Nifty50 PAT jumps 27%")]
        self.assertEqual(len(pat), 1)
        self.assertEqual(pat[0]["touches"], ["RELIANCE", "SBIN"])
        self.assertIn("results", pat[0]["tags"])

    def test_mentions(self):
        pats = news.mention_patterns({"M&M": None, "RELIANCE": None, "SBIN": None, "HDFCBANK": None,
                                      "BAJAJ-AUTO": None}, names())

        def hits(text):
            return sorted(t for t, rx in pats.items() if rx.search(text))

        self.assertEqual(hits("Mahindra & Mahindra Financial Services allots NCDs"), [])
        self.assertEqual(hits("Mahindra and Mahindra Q2 profit rises"), ["M&M"])
        self.assertEqual(hits("M&M shares jump 3% after SUV launch"), ["M&M"])
        self.assertEqual(hits("M&MFIN rises"), [])
        self.assertEqual(hits("Reliance Power wins order"), [])
        self.assertEqual(hits("RELIANCE INDUSTRIES LIMITED has informed the Exchange"), ["RELIANCE"])
        self.assertEqual(hits("Buy call on NSE: SBIN"), ["SBIN"])
        self.assertEqual(hits("State Bank of India hikes rates; SBI Life slips"), ["SBIN"])
        self.assertEqual(hits("HDFC Life and HDFC AMC"), [])
        self.assertEqual(hits("Results today (HDFCBANK)"), ["HDFCBANK"])
        self.assertEqual(hits("BAJAJ-AUTO hits a record; Bajaj Auto sales"), ["BAJAJ-AUTO"])

    def test_a_filers_own_name_is_not_a_mention(self):
        it = {"id": "nse:x", "source": "NSE filing", "kind": "filing", "ticker": "M&MFIN",
              "company": "Mahindra & Mahindra Financial Services Limited", "subject": "Allotment of Securities",
              "title": "Allotment of Securities: Mahindra & Mahindra Financial Services Limited has informed "
                       "the Exchange regarding allotment of NCDs", "summary": "", "link": "", "time": None}
        news.tag([it], {"M&M": None}, names())
        self.assertEqual(it["touches"], [])
        self.assertIn("fund raising", it["tags"])

    def test_indian_tags(self):
        cases = [  # real headlines from the fixtures first, then made-up ones for the rarer tags
            ("SML Mahindra to raise up to ₹1,450 crore through rights issue - ET Auto", {"rights issue", "fund raising"}),
            ("State Bank of India completes acquisition of 9.90% stake in CareEdge Global IFSC", {"deal"}),
            ("SBI has told the stock exchanges about its data breach, but not the customers whose data was leaked",
             {"risk"}),
            ("HDFC Bank Board Approves Proposed One-Year Reappointment of Rangan - TipRanks", {"management"}),
            ("SBI Q2 FY27: Profit and Net Interest Income Rise - Kalkine India", {"results"}),
            ("Mahindra & Mahindra's Electric Three-Wheeler Arm Weighs IPO: Report", {"fund raising"}),
            ("Bajaj Finance Limited has informed the Exchange regarding allotment of 114146341 securities "
             "pursuant to Qualified Institution Placement", {"QIP"}),
            ("Jefferies Prefers Large-Caps As FPIs Return To India; HDFC Bank, Adani Ports, Eternal Among Top Picks",
             {"analyst", "macro"}),
            ("SEBI bars promoter from the market and imposes a penalty of Rs 5 crore", {"SEBI/penalty"}),
            ("Block deal: promoter sells 2.5% stake for Rs 1,200 crore", {"block/bulk deal"}),
            ("Lenders invoke pledge on promoter shares", {"pledge"}),
            ("ICRA reaffirms credit rating at AA+", {"rating"}),
            ("NCLT approves resolution plan for the airline", {"resolution plan"}),
            ("Board meeting on 20 Nov to consider bonus issue and stock split", {"board meeting", "bonus", "split"}),
            ("Buyback record date fixed; interim dividend declared", {"buyback", "dividend"}),
            ("Stock hits upper circuit, enters F&O ban", {"halt"}),
            ("Company bags Rs 500 crore order from NHAI", {"order win"}),
        ]
        for title, want in cases:
            it = {"id": "x", "source": "Google News", "kind": "headline", "ticker": "X", "title": title,
                  "summary": "", "link": "", "time": None}
            got = set(news.tag([it], {"X": None})[0]["tags"])
            self.assertTrue(want <= got, "%s: %s" % (title, sorted(got)))

    def test_importance_by_subject(self):
        yes = ["Change in Management", "Outcome of Board Meeting", "Financial Result Updates",
               "Resignation of Director/KMP/SMP", "Change in Directors/KMP/SMP/Auditor/RTA", "News Verification",
               "Price movement", "Credit Rating", "Action(s) initiated or orders passed",
               "Award of Order / Receipt of Order", "Bagging/Receiving of orders/contracts", "Acquisition",
               "Dividend", "Buyback", "Disclosure of encumbered shares", "Insolvency and Bankruptcy",
               "Spurt in Volume", "Clarification - Financial Results"]
        no = ["Investor Presentation", "Analysts/Institutional Investor Meet/Con. Call Updates", "Press Release",
              "General Updates", "Record Date Updates", "Copy of Newspaper Publication", "Trading Window",
              "Certificate under SEBI (Depositories and Participants) Regulations, 2018", "Allotment of Securities",
              "Alteration Of Capital and Fund Raising-XBRL",
              "Statement of deviation(s) or variation(s) under Reg. 32", "Agreements"]
        for s in yes:
            self.assertTrue(news.IMPORTANT_RX.search(s), s)
        for s in no:
            self.assertFalse(news.IMPORTANT_RX.search(s), s)


class Check(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state", "news.json")
        cache = os.path.join(self.tmp.name, "state", "nse-names.json")
        os.makedirs(os.path.dirname(cache))
        with open(cache, "w") as fh:
            json.dump({"fetched": datetime.datetime.now(UTC).isoformat(), "names": names()}, fh)
        none = os.path.join(self.tmp.name, "none.json")
        self.base = ["--offline", DIR, "--state", self.state, "--names", cache, "--now", NOW,
                     "--portfolio", none, "--watchlist", none, "--plan", none]
        self.four = ["--tickers", "RELIANCE", "SBIN", "HDFCBANK", "M&M", "--hours", "24"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_items_once_newest_first(self):
        code, out, err = run(self.four + ["--json"] + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["checked_ist"], "2026-11-17 11:03")
        times = [i["time"] for i in data["items"]]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertEqual(len(data["items"]), 37)  # 38 headlines in the four feeds, one story in two
        first = data["items"][0]
        self.assertEqual((first["time_ist"], first["ticker"], first["publisher"]),
                         ("2026-11-17 10:39", "HDFCBANK", "Business Standard"))
        self.assertEqual(len(data["notes"]), 1)
        self.assertIn("NSE's announcements feed reaches back only to 17-Nov 01:27 IST", data["notes"][0])
        code, out, _ = run(self.four + self.base)
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", out)
        self.assertNotIn("reaches back only", out)  # the last check covered it

    def test_text_output_in_ist(self):
        code, out, err = run(self.four + ["--dry-run"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("  2026-11-17 09:11 IST  SBIN        Google News SBI has told the stock exchanges about "
                      "its data breach", out)
        lines = out.splitlines()
        breach = [i for i, l in enumerate(lines) if "about its data breach" in l][0]
        self.assertTrue(lines[breach + 1].startswith("      tags: risk  https://news.google.com/rss/articles/"))
        self.assertIn("  2026-11-16 14:36 IST  RELIANCE,SBIN Google News Nifty50 PAT jumps 27%", out)
        self.assertIn("SBIN       ₹1,110.30 +1.82% vs yesterday's close ₹1,090.50 at 10:51 IST", out)
        self.assertIn("M&M        ₹3,386.00 +1.41% vs yesterday's close ₹3,339.00 at 10:51 IST", out)
        self.assertNotIn("MOVING", out)
        self.assertIn("Checked 2026-11-17 11:03 IST.", out)
        self.assertIn("Not financial advice", out)
        for l in own_lines(out):
            self.assertNotIn(chr(0x2014), l)
            self.assertNotIn(" ET", l)
        self.assertFalse(os.path.exists(self.state))

    def test_a_pinned_clock_hides_later_items(self):
        code, out, err = run(["--tickers", "SBIN", "RELIANCE", "--hours", "6", "--no-moves", "--dry-run"]
                             + self.base + ["--now", "2026-11-17 09:20"])  # the last --now wins
        self.assertEqual(code, 0, err)
        self.assertIn("2026-11-17 09:11 IST  SBIN", out)
        self.assertIn("2026-11-17 09:18 IST  SBIN", out)  # within 10 minutes of the clock
        self.assertNotIn("09:57 IST", out)
        self.assertNotIn("10:13 IST", out)
        self.assertNotIn("2026-11-16", out)  # older than 6 hours
        self.assertIn("Checked 2026-11-17 09:20 IST", out)

    def test_moves(self):
        m = news.parse_chart(raw("chart-SBIN.json"), "SBIN")
        self.assertEqual((m["price"], m["prev_close"], m["pct"]), (1110.3, 1090.5, 1.82))
        self.assertEqual(news.to_ist(m["time"]).strftime("%H:%M"), "10:51")
        self.assertEqual(news.render_moves([m])[0],
                         "SBIN       ₹1,110.30 +1.82% vs yesterday's close ₹1,090.50 at 10:51 IST")
        self.assertTrue(news.render_moves([m], move_pct=1.5)[0].endswith("at 10:51 IST  MOVING"))
        down = news.parse_chart(raw("chart-RELIANCE.json"), "RELIANCE")
        self.assertEqual(down["pct"], -0.36)
        self.assertIn("MOVING", news.render_moves([dict(down, pct=-3.0)])[0])  # 3% either way
        with self.assertRaises(news.NewsError):
            news.parse_chart(b'{"chart": {"result": null}}', "ZZZZ")
        # no chart file offline: no move line, no error note
        code, out, _ = run(["--tickers", "CYIENT", "--hours", "24", "--dry-run", "--no-google"] + self.base)
        self.assertNotIn("vs yesterday's close", out)
        self.assertNotIn("price for", out)

    def test_nse_items_for_watched_stocks(self):
        tickers = ["PFC", "MRF", "ASHOKLEY", "CARYSIL", "YESBANK", "STEL", "CYIENT", "ZEEL", "RELIANCE"]
        code, out, err = run(["--tickers"] + tickers + ["--no-google", "--no-moves", "--hours", "24"] + self.base)
        self.assertEqual(code, 0, err)
        rows = [l for l in out.splitlines() if l[:2] in ("! ", "  ") and not l.startswith("      ")]
        self.assertEqual([r[2:22].strip() for r in rows[:4]],
                         ["ex-date 2026-11-17", "ex-date 2026-11-17", "ex-date 2026-11-20", "ex-date 2026-11-25"])
        self.assertTrue(rows[0].startswith("! ex-date 2026-11-17    MRF         NSE action  MRF Limited Ex-date"))
        self.assertTrue(rows[1].startswith("! ex-date 2026-11-17    PFC "))
        self.assertTrue(rows[2].startswith("  ex-date 2026-11-20    ASHOKLEY "))  # not today: no !
        self.assertIn("! 2026-11-17 10:52 IST  STEL        NSE filing  Price movement: Significant movement in price "
                      "has been observed in Stel Holdings Limited.", out)
        self.assertIn("! 2026-11-17 10:15 IST  YESBANK     NSE filing  News Verification: The Exchange has sought "
                      "clarification from Yes Bank Limited", out)
        self.assertIn("! 2026-11-17 10:13 IST  CARYSIL     NSE filing  Resignation: CARYSIL LIMITED has informed the "
                      "Exchange regarding Resignation of Mr. Anand Kumar Sharma as Executive Director and Group CFO",
                      out)
        self.assertIn("https://nsearchives.nseindia.com/corporate/CYIENT_17112026083916_Disclosure_CHROSigned.pdf", out)
        self.assertNotIn("RELIANCE", out)  # no filing, no ex-date today
        self.assertEqual(len(rows), 10)  # 4 ex-dates, 6 filings
        code, out, _ = run(["--tickers", "PFC", "--no-google", "--no-moves", "--json", "--all"] + self.base)
        item = json.loads(out)["items"][0]
        self.assertEqual((item["ex_date"], item["time"], item["important"], item["kind"]),
                         ("2026-11-17", None, True, "action"))

    def test_tickers_from_watchlist_and_plan(self):
        wl = os.path.join(self.tmp.name, "watchlist.json")
        with open(wl, "w") as fh:
            json.dump({"created_by": "nse-scanner", "hits": [{"symbol": "SBIN.NS", "setups": ["orb"]},
                                                             {"symbol": "bad.symbol"}]}, fh)
        plan = os.path.join(self.tmp.name, "plan.json")
        with open(plan, "w") as fh:
            json.dump({"created_by": "nse-exit", "symbol": "m&m"}, fh)
        self.assertEqual(sorted(news.watch_from_files("/nonexistent.json", wl, plan)), ["M&M", "SBIN"])
        args = list(self.base)
        args[args.index("--watchlist") + 1] = wl
        args[args.index("--plan") + 1] = plan
        code, out, err = run(["--json", "--dry-run", "--hours", "24", "--no-moves"] + args)
        self.assertEqual(code, 0, err)
        tickers = {t for i in json.loads(out)["items"] for t in i["touches"]}
        self.assertEqual(tickers, {"M&M", "SBIN"})
        self.assertFalse(os.path.exists(self.state))

    def test_bad_input(self):
        code, _, err = run(self.base)
        self.assertEqual(code, 2)
        self.assertIn("no tickers", err)
        self.assertIn("/nse-scanner", err)
        code, _, err = run(["--tickers", "BRK.B"] + self.base)
        self.assertEqual(code, 2)
        self.assertIn("not an NSE symbol: BRK.B", err)
        code, _, err = run(["--tickers", "SBIN", "--nse-feeds", "announcements,filings"] + self.base)
        self.assertEqual(code, 2)
        self.assertIn("unknown NSE feed filings", err)
        code, _, err = run(["--tickers", "SBIN"] + self.base + ["--now", "soon"])
        self.assertEqual(code, 2)

    def test_headline_cap(self):
        code, out, err = run(self.four + ["--max-headlines", "3", "--no-moves"] + self.base)
        self.assertEqual(code, 0, err)
        rows = [l for l in out.splitlines() if " Google News " in l and not l.startswith("note:")]
        self.assertEqual(len(rows), 12)
        self.assertIn("note: M&M: 7 older Google News headlines not shown (--max-headlines 3)", out)
        self.assertIn("note: SBIN: 6 older Google News headlines not shown (--max-headlines 3)", out)
        code, out, _ = run(self.four + ["--max-headlines", "3", "--no-moves"] + self.base)
        self.assertEqual(code, 1)  # the older ones count as seen: they do not come back
        self.assertIn("Nothing new.", out)

    def test_all_shows_seen_items_and_the_feed_note_again(self):
        run(self.four + ["--no-moves"] + self.base)
        code, out, _ = run(self.four + ["--no-moves", "--all"] + self.base)
        self.assertEqual(code, 1)  # nothing new, but shown
        self.assertIn("about its data breach", out)
        self.assertIn("reaches back only to 17-Nov 01:27 IST", out)

    def test_state_forgets_after_30_days(self):
        state = {"seen": {"old": "2026-10-01T00:00:00+00:00", "new": "2026-11-10T00:00:00+00:00"}}
        now = news.parse_time(NOW)
        news.remember([], state, now)
        self.assertEqual(sorted(state["seen"]), ["new"])
        self.assertEqual(state["last_check"], now.isoformat())


class _Resp(object):
    def __init__(self, data):
        self.data, self.headers = data, {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.data


def _http_429(url, retry_after=None):
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError(url, 429, "Too Many Requests", hdrs, None)


class Network(unittest.TestCase):
    """No real network: urlopen is replaced."""

    GURL = "https://news.google.com/rss/search?q=%22HDFC+Bank%22+when%3A24h&hl=en-IN&gl=IN&ceid=IN:en"

    def test_ssl_context_verifies(self):
        ctx = news._ssl_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)
        if os.path.exists("/etc/ssl/cert.pem"):
            self.assertGreater(ctx.cert_store_stats()["x509_ca"] + len(ctx.get_ca_certs()), 0)

    def test_honest_user_agent_and_verified_tls(self):
        seen = []

        def fake(req, timeout=None, context=None):
            seen.append((req.get_header("User-agent"), context, timeout))
            return _Resp(b"<rss/>")

        with mock.patch.object(news.urllib.request, "urlopen", side_effect=fake):
            news.Fetcher(sleep=lambda s: None).get(news.NSE_RSS % "Online_announcements", "x")
        ua, ctx, timeout = seen[0]
        self.assertEqual(ua, "Mozilla/5.0 (compatible; nse-day-trader/1.0)")
        self.assertIsInstance(ctx, ssl.SSLContext)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(timeout, 30)
        with open(news.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("/api/", src)  # nseindia.com's API is off limits
        for browser in ("Chrome/", "Safari/", "Firefox/", "AppleWebKit"):
            self.assertNotIn(browser, src)

    def test_google_is_paced_and_a_429_retried_once(self):
        sleeps = []
        answers = [_http_429(self.GURL, "7"), _Resp(b"<rss/>"), _Resp(b"<rss/>"), _http_429(self.GURL),
                   _http_429(self.GURL)]

        def fake(req, timeout=None, context=None):
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a

        f = news.Fetcher(sleep=sleeps.append)
        with mock.patch.object(news.urllib.request, "urlopen", side_effect=fake):
            self.assertEqual(f.get(self.GURL, "g", retry_429=True), b"<rss/>")
            self.assertEqual(sleeps, [7.0])  # Retry-After honoured, no extra pacing on the retry
            f.get(self.GURL, "g", retry_429=True)
            self.assertEqual(len(sleeps), 2)
            self.assertTrue(2.5 < sleeps[1] <= 3.0, sleeps)  # 3 s between Google searches
            with self.assertRaises(news.NewsError) as cm:
                f.get(self.GURL, "g", retry_429=True)
            self.assertIn("HTTP 429", str(cm.exception))
        self.assertEqual(len(sleeps), 4)
        self.assertEqual(sleeps[3], 20.0)  # no Retry-After: 20 s, then one more try only
        self.assertEqual(answers, [])

    def test_collect_stops_asking_google_after_a_second_429(self):
        class Stub(object):
            offline = None

            def __init__(self):
                self.calls = []

            def get(self, url, name, retry_429=False):
                self.calls.append(name)
                if "news.google.com" in url:
                    raise news.NewsError("HTTP 429 for %s" % url)
                if name == "nse-announcements.xml":
                    return raw("nse-announcements.xml")
                return b"<rss><channel/></rss>"

        f, errors = Stub(), []
        news.collect(f, {"SBIN": None, "HDFCBANK": None, "M&M": None}, names(), hours=24, errors=errors)
        self.assertEqual([c for c in f.calls if c.startswith("gnews-")], ["gnews-HDFCBANK.xml"])
        self.assertEqual(f.calls[1:], ["nse-announcements.xml", "nse-actions.xml", "nse-meetings.xml",
                                       "nse-results.xml"])
        self.assertEqual(errors, ["Google News is rate-limiting this connection (HTTP 429 twice): no headlines "
                                  "for HDFCBANK, M&M, SBIN this time; try again in a few minutes"])


if __name__ == "__main__":
    unittest.main()
