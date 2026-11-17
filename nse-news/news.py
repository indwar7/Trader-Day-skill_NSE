#!/usr/bin/env python3
"""News monitor for nse-news. Standard library only.

Watches exchange filings and headlines for the NSE stocks you watch or hold and
prints only what is new since the last check, newest first, with how far each
stock has moved today and a flag on anything that touches your stocks.

    python3 news.py                                    # watchlist + plan + portfolio stocks
    python3 news.py --tickers RELIANCE SBIN "M&M"
    python3 news.py --tickers SBIN --all --hours 72    # everything in the last 3 days, seen or not
    python3 news.py --no-nse                           # Google News headlines only
    python3 news.py --no-google                        # NSE feeds only
    python3 news.py --no-moves                         # skip the price line per stock

Your stocks, when --tickers is not given: every hit in trader/watchlist.json
(nse-scanner), the symbol in trader/plan.json (nse-exit) and the positions in
trader/portfolio.json. Symbols are NSE symbols (RELIANCE, M&M, BAJAJ-AUTO); a
trailing .NS is dropped.

Sources:
  - NSE's public RSS feeds on nsearchives.nseindia.com, one request each per
    run, whole market, kept only where they touch your stocks:
      announcements  content/RSS/Online_announcements.xml  (filings, with a SUBJECT)
      actions        content/RSS/Corporate_action.xml      (upcoming ex-dates)
      meetings       content/RSS/Board_Meetings.xml        (board meeting notices)
      results        content/RSS/Financial_Results.xml     (results filed)
    The announcements feed holds only the latest few hundred filings (mostly
    mutual-fund NAVs), often just the last few hours: the tool says how far
    back it reached.
  - Google News RSS, one request per stock, searching for the company name
    (India edition, limited to --hours):
    https://news.google.com/rss/search?q="Reliance Industries" when:24h&hl=en-IN&gl=IN&ceid=IN:en
    Company names come from the Nifty 500 list (niftyindices.com) and NSE's
    EQUITY_L.csv, cached for a week in trader/state/nse-names.json. Searches
    are 3 s apart; on HTTP 429 the tool waits once (Retry-After, else 20 s),
    retries, and if Google still refuses it skips Google for this check and
    says so. At most --max-headlines (20) headlines per stock are shown.
  - The move: Yahoo Finance's chart endpoint (unofficial, can lag), the last
    price against yesterday's close:
    https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m

Times print in IST (UTC+05:30). State: trader/state/news.json remembers what
you have seen (30 days), so an item shows once. Offline: --offline DIR reads
gnews-<SYMBOL>.xml, nse-<feed>.xml and chart-<SYMBOL>.json from DIR (and the
two name lists, nifty500list.csv and EQUITY_L.csv, if there is no names
cache). The tests use it.

Headlines are leads, not facts: open the story or the filing before acting,
and treat nothing here as advice.
"""

import argparse
import csv
import datetime as _dt
import email.utils
import gzip
import hashlib
import io
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from decimal import ROUND_HALF_UP, Decimal

UA = "Mozilla/5.0 (compatible; nse-day-trader/1.0)"
NSE_RSS = "https://nsearchives.nseindia.com/content/RSS/%s.xml"
NSE_FEEDS = (  # key, file on nsearchives, source label
    ("announcements", "Online_announcements", "NSE filing"),
    ("actions", "Corporate_action", "NSE action"),
    ("meetings", "Board_Meetings", "NSE meeting"),
    ("results", "Financial_Results", "NSE results"),
)
NSE_ANN_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
GENERIC_LINK = re.compile(r"^https?://www\.nseindia\.com/companies-listing/")  # one page for many items
GNEWS = "https://news.google.com/rss/search?q=%s&hl=en-IN&gl=IN&ceid=IN:en"
CHART = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m"
NAME_LISTS = (  # url, offline file name, parser key
    ("https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv", "nifty500list.csv", "nifty500"),
    ("https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv", "EQUITY_L.csv", "equity_l"),
)
NAMES_MAX_AGE_DAYS = 7
HOST_PAUSE = {"news.google.com": 3.0}  # seconds between requests; Google News answers bursts with HTTP 429
PAUSE = 0.3                            # every other host
MAX_HEADLINES = 20                     # Google News headlines shown per stock per check
KEEP_DAYS = 30
MOVE_PCT = 3.0  # a stock this far from yesterday's close is flagged as moving
IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9&-]{0,19}$")
MONTHS = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}

# NSE announcement SUBJECTs (and corporate-action / board-meeting purposes) that
# earn the "!" mark: the filings most likely to move a stock the same day.
IMPORTANT_SUBJECTS = [
    r"financial results?|outcome of board meeting|board meeting outcome|integrated filing.{0,3}financial",
    r"dividend|bonus|\bsplit\b|sub-?division|consolidation of shares|buy[\s-]?back",
    r"rights issue|qualified institution|\bqip\b|preferential issue|raising of funds",
    r"acquisition|amalgamation|merger|scheme of arrangement|takeover|open offer|disinvestment",
    r"change in management|change in directors?|resignation|cessation|change in (?:statutory )?auditors?",
    r"credit rating",
    r"award(?:ing)? of (?:order|contract)|receipt of order|bagging|orders?/contracts?",
    r"action\(s\)|actions? (?:initiated|taken)|orders? passed|penalt|show cause",
    r"pledge|encumb|invocation",
    r"insolvency|bankruptcy|resolution plan|\bcirp\b|\bnclt\b|default|fraud|winding up|liquidation",
    r"clarification|news verification|spurt in volume|price movement",
    r"cyber|\bfire\b|accident|\bstrike\b|lockout|disruption|shutdown|force majeure",
    r"suspension|delisting",
]
IMPORTANT_RX = re.compile("|".join("(?:%s)" % p for p in IMPORTANT_SUBJECTS), re.I)

KEYWORDS = [
    ("results", r"\bresults?\b|\bearnings\b|\bQ[1-4](?:\s?FY\s?\d{2,4})?\b|\bnet (?:profit|loss)\b"
                r"|\bPAT\b|\bEBITDA\b|\bguidance\b"),
    ("board meeting", r"\bboard meeting\b|\bmeeting of the board\b|\bboard (?:to|will) (?:meet|consider)"),
    ("dividend", r"\bdividends?\b"),
    ("bonus", r"\bbonus\b"),
    ("split", r"\bsplit\b|\bsub-?division\b"),
    ("buyback", r"\bbuy[\s-]?backs?\b"),
    ("rights issue", r"\brights (?:issue|entitlement|offer)\b|\brights\s+\d+\s*:\s*\d+"),
    ("QIP", r"\bQIPs?\b|\bqualified institution(?:al)? placement"),
    ("fund raising", r"\bfund[\s-]?rais|\braising of funds\b|\bpreferential (?:issue|allotment)\b"
                     r"|\braises?\b.{0,40}\b(?:crore|cr|bn|billion|million|funds?|capital)\b|\bNCDs?\b|\bdebentures?\b"
                     r"|\bIPOs?\b"),
    ("order win", r"\borders? (?:win|worth|from|inflow|book)\b|\b(?:wins?|bags?|secures?|receives?)\b.{0,60}\b(?:orders?|contracts?)\b"
                  r"|\bletter of (?:award|intent)\b|\b(?:award|receipt|bagging) of (?:orders?|contracts?)"),
    ("SEBI/penalty", r"\bSEBI\b.{0,80}\b(?:order|bans?|barred|bars|probe|investigat|settle|impound|adjudicat|notice)"
                     r"|\bpenalt(?:y|ies)\b|\bfined\b|\bshow[- ]cause\b|\baction\(s\) (?:initiated|taken)|\borders? passed\b"),
    ("block/bulk deal", r"\bblock deals?\b|\bbulk deals?\b|\bblock trades?\b|\bstake sale\b|\b(?:sells?|offloads?|buys?) (?:a )?(?:\d+(?:\.\d+)?% )?stake\b"
                        r"|\boffer for sale\b|\bOFS\b"),
    ("pledge", r"\bpledg|\bencumb|\binvocation\b"),
    ("rating", r"\bcredit rating|\b(?:CRISIL|ICRA|CARE Ratings|India Ratings|Acuite|Brickwork|Moody'?s|Fitch)\b"
               r"|\bratings? (?:upgrade|downgrade|reaffirm|revis|action|outlook)"),
    ("analyst", r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|\btarget price\b|\bprice target\b|\boutperform\b|\bunderperform\b"
                r"|\boverweight\b|\bunderweight\b|\b(?:buy|sell|hold|accumulate|reduce) (?:rating|call)\b|\bbrokerages?\b"
                r"|\btop picks?\b|\bstocks? to buy\b|\bstock ideas?\b"),
    ("deal", r"\bacqui(?:re|res|red|ring|sition)s?\b|\bmergers?\b|\bamalgamation\b|\bdemerger\b|\bscheme of arrangement\b"
             r"|\btakeover\b|\bopen offer\b|\bjoint venture\b|\bJV\b"),
    ("management", r"\bCEO\b|\bCFO\b|\bmanaging director\b|\bchairman\b|\bchairperson\b|\bresign|\bsteps? down\b"
                   r"|\b(?:re-?)?appoint|\bcessation\b|\bchange in (?:senior )?management\b|\bKMP\b"),
    ("resolution plan", r"\bresolution plan\b|\binsolvency\b|\bIBC\b|\bNCLT\b|\bNCLAT\b|\bCIRP\b|\bbankrupt|\bliquidation\b|\bdefault(?:s|ed)?\b"),
    ("legal", r"\blawsuit\b|\bcourt\b|\barbitration\b|\bprobe\b|\binvestigation\b|\braids?\b|\blitigation\b"),
    ("halt", r"\bsuspen(?:d|ded|sion)\b|\btrading halt|\bhalted\b|\b(?:upper|lower) circuit\b|\bF&O ban\b|\bASM\b|\bGSM\b"),
    ("risk", r"\bfire\b|\baccident\b|\bexplosion\b|\bstrike\b|\blockout\b|\bshutdown\b|\bcyber|\bhack|\bbreach\b|\boutage\b|\bfraud\b|\brecall\b|\bdelist"),
    ("regulatory", r"\bRBI\b(?! policy)|\bCCI\b|\bUSFDA\b|\bFDA\b|\bwarning letter\b|\bDGCA\b|\btax demand\b|\bGST (?:notice|demand)\b"),
    ("macro", r"\bRBI policy\b|\brepo rate\b|\bMPC\b|\bCPI\b|\binflation\b|\bGDP\b|\bFIIs?\b|\bFPIs?\b|\bDIIs?\b|\bGIFT Nifty\b"
              r"|\brupee\b|\bcrude\b|\btariffs?\b|\bFed\b|\bUnion Budget\b"),
]


class NewsError(Exception):
    pass


# ---------------------------------------------------------------- time

def to_ist(utc):
    """Aware or naive UTC -> naive IST wall-clock time (UTC+05:30 all year, no daylight saving)."""
    if utc.tzinfo is None:
        utc = utc.replace(tzinfo=_dt.timezone.utc)
    return utc.astimezone(IST).replace(tzinfo=None)


def ist_to_utc(local):
    """Naive IST wall-clock time -> aware UTC."""
    return local.replace(tzinfo=IST).astimezone(_dt.timezone.utc)


def _nse_time(value):
    """NSE feed times: '17-Nov-2026 09:31:00', '17-Nov-2026 16:46', '17-Nov-2026' (IST)."""
    m = re.match(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$", value)
    if not m or m.group(2).lower() not in MONTHS:
        return None
    try:
        local = _dt.datetime(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)),
                             int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0))
    except ValueError:
        return None
    return ist_to_utc(local)


def parse_time(value):
    """RFC 822 (RSS), ISO 8601 or NSE's DD-Mon-YYYY -> aware UTC datetime, or None.
    A time with no zone is IST."""
    value = (value or "").strip()
    if not value:
        return None
    d = _nse_time(value)
    if d is not None:
        return d
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        return ist_to_utc(d)
    return d.astimezone(_dt.timezone.utc)


def parse_date(value):
    """'20-Nov-2026' -> date, or None."""
    d = _nse_time((value or "").strip())
    return to_ist(d).date() if d else None


# ---------------------------------------------------------------- money and symbols

def _group_in(digits):
    """'1234567' -> '12,34,567' (Indian grouping)."""
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


def inr(x):
    """123456.5 -> '₹1,23,456.50'; -1250 -> '-₹1,250.00'."""
    d = Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    whole, frac = "{:.2f}".format(abs(d)).split(".")
    return "%s₹%s.%s" % ("-" if d < 0 else "", _group_in(whole), frac)


def clean_symbol(text):
    """User input -> NSE symbol (upper case, a trailing .NS dropped), or None if it is not one."""
    s = str(text or "").strip().upper()
    if s.endswith(".NS"):
        s = s[:-3]
    return s if SYMBOL_RE.match(s) else None


def yahoo_symbol(sym):
    """M&M -> M%26M.NS: the URL form of the Yahoo symbol."""
    return urllib.parse.quote(sym + ".NS", safe="")


# ---------------------------------------------------------------- inputs

def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def watch_from_files(portfolio="trader/portfolio.json", watchlist="trader/watchlist.json",
                     plan="trader/plan.json"):
    """{symbol: name or None}: the scanner's hits, the planned trade and your holdings."""
    out = {}
    wl = load_json(watchlist) or {}
    for h in wl.get("hits", []):
        sym = clean_symbol(h.get("symbol") or h.get("ticker"))
        if sym:
            out[sym] = h.get("name")
    pl = load_json(plan) or {}
    sym = clean_symbol(pl.get("symbol"))
    if sym:
        out.setdefault(sym, pl.get("name"))
    pf = load_json(portfolio) or {}
    for p in pf.get("positions", []):
        sym = clean_symbol(p.get("ticker") or p.get("symbol"))
        if sym:
            out[sym] = p.get("name") or out.get(sym)
    return out


# ---------------------------------------------------------------- fetching

_CTX = []


def _ssl_context():
    """Verified TLS. python.org builds on macOS ship no CA bundle: fall back to the system one."""
    ctx = ssl.create_default_context()
    if not ctx.get_ca_certs() and not ctx.cert_store_stats().get("x509_ca"):
        for path in ("/etc/ssl/cert.pem", "/private/etc/ssl/cert.pem"):
            if os.path.exists(path):
                ctx.load_verify_locations(cafile=path)
                break
    return ctx


def _retry_after(value, default=20.0, cap=60.0):
    """Seconds to wait from a Retry-After header (seconds form only), capped."""
    try:
        return max(1.0, min(cap, float(value)))
    except (TypeError, ValueError):
        return default


class _TooMany(Exception):
    """HTTP 429, with the server's Retry-After (or None)."""


class Fetcher(object):
    def __init__(self, offline=None, pause=PAUSE, sleep=time.sleep):
        self.offline = offline
        self.pause = pause
        self.sleep = sleep
        self._last = {}

    def get(self, url, name, retry_429=False):
        """The body of `url` (or of `name` in the offline folder). Requests to one host
        are spaced (HOST_PAUSE); with retry_429, an HTTP 429 is retried once after
        Retry-After (20 s if none)."""
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise NewsError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        host = urllib.parse.urlsplit(url).netloc
        wait = HOST_PAUSE.get(host, self.pause)
        gap = time.time() - self._last.get(host, 0.0)
        if gap < wait:
            self.sleep(wait - gap)
        for attempt in (1, 2):
            try:
                return self._open(url, host)
            except _TooMany as exc:
                if attempt == 2 or not retry_429:
                    raise NewsError("HTTP 429 for %s" % url)
                self.sleep(_retry_after(exc.args[0]))

    def _open(self, url, host):
        if not _CTX:
            _CTX.append(_ssl_context())
        self._last[host] = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(req, timeout=30, context=_CTX[0]) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            code, retry = exc.code, (exc.headers.get("Retry-After") if exc.headers else None)
            try:
                exc.close()
            except Exception:  # Python 3.9 raises KeyError closing an HTTPError with no body
                pass
            if code == 429:
                raise _TooMany(retry)
            raise NewsError("HTTP %s for %s" % (code, url))
        except urllib.error.URLError as exc:
            raise NewsError("could not reach %s: %s" % (url, exc.reason))
        except OSError as exc:
            raise NewsError("could not read %s: %s" % (url, exc))


# ---------------------------------------------------------------- company names

def short_name(name):
    """'Reliance Industries Ltd.' -> 'Reliance Industries'."""
    n = re.sub(r"\s+", " ", (name or "").strip())
    n = re.sub(r"\s+(?:limited|ltd)\.?$", "", n, flags=re.I)
    return n.strip().rstrip(",").strip()


def norm_name(name):
    """A company name reduced for matching: lower case, & = and, no punctuation, no 'Limited'."""
    n = (name or "").lower().replace("&", " and ")
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    n = re.sub(r"^the ", "", n)
    return re.sub(r" (?:limited|ltd)$", "", n)


def _csv_rows(data):
    text = data.decode("utf-8-sig", "replace") if isinstance(data, bytes) else data
    for row in csv.DictReader(io.StringIO(text)):
        yield {(k or "").strip().upper(): (v or "").strip() for k, v in row.items()}


def parse_nifty500(data):
    """niftyindices.com's ind_nifty500list.csv -> {symbol: company name}."""
    out = {}
    for row in _csv_rows(data):
        sym = clean_symbol(row.get("SYMBOL"))
        if sym and row.get("COMPANY NAME"):
            out[sym] = row["COMPANY NAME"]
    return out


def parse_equity_l(data):
    """NSE's EQUITY_L.csv -> {symbol: company name} (every series: names only)."""
    out = {}
    for row in _csv_rows(data):
        sym = clean_symbol(row.get("SYMBOL"))
        if sym and row.get("NAME OF COMPANY"):
            out[sym] = row["NAME OF COMPANY"]
    return out


PARSE_LIST = {"nifty500": parse_nifty500, "equity_l": parse_equity_l}


def merge_names(parts):
    """[{sym: name}, ...] -> {sym: [name, ...]}, earlier lists first."""
    out = {}
    for part in parts:
        for sym, name in part.items():
            lst = out.setdefault(sym, [])
            if name not in lst:
                lst.append(name)
    return out


def load_names(fetcher, path, errors, now_real=None):
    """{symbol: [company names]} from the cache, refreshed when older than a week."""
    now_real = now_real or _dt.datetime.now(_dt.timezone.utc)
    cache = load_json(path) or {}
    names = cache.get("names") or {}
    fetched = parse_time(cache.get("fetched")) if cache.get("fetched") else None
    fresh = fetched is not None and now_real - fetched < _dt.timedelta(days=NAMES_MAX_AGE_DAYS)
    if names and (fresh or fetcher.offline):
        return names
    parts, complete = [], True
    for url, fname, key in NAME_LISTS:
        try:
            part = PARSE_LIST[key](fetcher.get(url, fname))
        except (NewsError, csv.Error) as exc:
            part = {}
            if not (fetcher.offline and "no file" in str(exc)):
                errors.append("company names (%s): %s" % (fname, exc))
        if part:
            parts.append(part)
        else:
            complete = False
    got = merge_names(parts)
    if names and (not got or not complete):  # a complete older list beats a partial new one
        errors.append("company names: using the cache from %s" % (cache.get("fetched") or "an unknown date"))
        return names
    if not got:
        return names
    if complete and not fetcher.offline and path:  # a half download is used once, not cached
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"fetched": now_real.isoformat(), "names": got}, fh, indent=1, ensure_ascii=False)
            fh.write("\n")
    return got


def query_name(sym, watch, names):
    """The name to search Google News for: the watchlist's name, else the Nifty 500 / EQUITY_L name."""
    if watch.get(sym):
        return short_name(watch[sym])
    for n in names.get(sym, []):
        if n:
            return short_name(n)
    return None


def gnews_query(name, sym, hours):
    span = "when:%dh" % max(1, int(-(-hours // 1))) if hours <= 48 else "when:%dd" % int(-(-hours // 24))
    if name:
        return '"%s" %s' % (name, span)
    return '"%s" NSE %s' % (sym, span)


def gnews_url(name, sym, hours):
    return GNEWS % urllib.parse.quote_plus(gnews_query(name, sym, hours))


# ---------------------------------------------------------------- parsing

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def _items(data, what):
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("%s is not valid XML: %s" % (what, exc))
    return list(root.iter("item"))


def parse_gnews(data, ticker):
    """Google News RSS -> items."""
    out = []
    for it in _items(data, "Google News for %s" % ticker):
        title = _text(it, "title")
        link = _text(it, "link")
        guid = _text(it, "guid") or link
        out.append({"id": "gnews:" + guid, "source": "Google News", "kind": "headline",
                    "ticker": ticker, "title": title, "summary": "", "publisher": _text(it, "source"),
                    "link": link, "time": parse_time(_text(it, "pubDate")), "important": False})
    return out


class Resolver(object):
    """Company name in an NSE feed -> NSE symbol."""

    def __init__(self, names, watch=None):
        self.known = set(names)
        self.index = {}
        for sym, lst in names.items():
            for n in lst:
                self.index.setdefault(norm_name(n), sym)
        for sym, n in (watch or {}).items():
            if n:
                self.index.setdefault(norm_name(n), sym)

    def symbol(self, company, desc="", link=""):
        m = re.match(r"^([A-Z0-9][A-Z0-9&-]{0,19})\s*:\s", desc or "")
        if m and (not self.known or m.group(1) in self.known):
            return m.group(1)
        sym = self.index.get(norm_name(company))
        if sym:
            return sym
        # the PDF's file name often starts with the filer's symbol (sometimes a login id instead)
        m = re.search(r"/corporate/([A-Za-z0-9&-]+)_\d{14}_", link or "")
        if m and m.group(1).upper() in self.known:
            return m.group(1).upper()
        return ""


def _fields(desc):
    """'SERIES:EQ |PURPOSE:BUY BACK |...' -> {'SERIES': 'EQ', 'PURPOSE': 'BUY BACK', ...}."""
    out = {}
    for part in (desc or "").split("|"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip().upper()] = re.sub(r"\s+", " ", v).strip()
    return out


def feed_span(data):
    """(items in the feed, oldest time in it) for an NSE feed, NAVs included."""
    times = [parse_time(_text(it, "pubDate")) for it in _items(data, "NSE feed")]
    times = [t for t in times if t]
    return len(times), (min(times) if times else None)


def parse_nse_announcements(data, resolver):
    """NSE's Online_announcements.xml -> items (mutual-fund NAV declarations dropped)."""
    out = []
    for it in _items(data, "NSE announcements"):
        company = re.sub(r"\s+", " ", _text(it, "title"))
        desc = re.sub(r"\s+", " ", _text(it, "description"))
        link = _text(it, "link")
        subject = ""
        m = re.search(r"\|\s*SUBJECT\s*:\s*(.*)$", desc)
        if m:
            subject = m.group(1).strip()
            desc = desc[:m.start()].strip()
        if re.search(r"declaration of nav", subject, re.I):
            continue
        sym = resolver.symbol(company, desc, link)
        text = re.sub(r"^[A-Z0-9][A-Z0-9&-]{0,19}\s*:\s", "", desc).strip()
        when = parse_time(_text(it, "pubDate"))
        key = link or hashlib.sha1(("%s|%s|%s" % (company, subject, _text(it, "pubDate"))).encode("utf-8")).hexdigest()
        out.append({"id": "nse:" + key, "source": "NSE filing", "kind": "filing", "ticker": sym,
                    "company": company, "subject": subject,
                    "title": ("%s: %s" % (subject, text)) if subject else text,
                    "summary": "", "link": link or NSE_ANN_PAGE, "time": when,
                    "important": bool(subject and IMPORTANT_RX.search(subject))})
    return out


def parse_nse_actions(data, resolver):
    """NSE's Corporate_action.xml -> items with an ex-date (the feed's pubDate is its build time)."""
    out = []
    for it in _items(data, "NSE corporate actions"):
        raw = re.sub(r"\s+", " ", _text(it, "title"))
        m = re.match(r"^(.*?)\s*-\s*Ex-Date:\s*(\S+)", raw)
        company, ex = (m.group(1), parse_date(m.group(2))) if m else (raw, None)
        f = _fields(_text(it, "description"))
        purpose = f.get("PURPOSE", "")
        title = "Ex-date %s: %s" % (ex.strftime("%d-%b-%Y") if ex else "unknown", purpose or "corporate action")
        if f.get("RECORD DATE") and f["RECORD DATE"] != "-":
            title += " (record date %s)" % f["RECORD DATE"]
        out.append({"id": "nseca:%s|%s|%s" % (norm_name(company), ex, purpose.lower()),
                    "source": "NSE action", "kind": "action", "ticker": resolver.symbol(company),
                    "company": company, "subject": purpose, "series": f.get("SERIES", ""),
                    "title": "%s %s" % (company, title), "summary": "", "link": _text(it, "link"),
                    "time": None, "ex_date": ex, "important": False})
    return out


def parse_nse_meetings(data, resolver):
    """NSE's Board_Meetings.xml -> items ('purpose |Meeting Date: 02-Nov-2026')."""
    out = []
    for it in _items(data, "NSE board meetings"):
        company = re.sub(r"\s+", " ", _text(it, "title"))
        desc = re.sub(r"\s+", " ", _text(it, "description"))
        m = re.search(r"\|\s*Meeting Date\s*:\s*(\S+)", desc, re.I)
        meeting = parse_date(m.group(1)) if m else None
        purpose = desc[:m.start()].strip() if m else desc
        link = _text(it, "link")
        when = parse_time(_text(it, "pubDate"))
        title = "Board meeting on %s: %s" % (meeting.strftime("%d-%b-%Y") if meeting else "a date not given", purpose)
        # one item per company and meeting: the feed is newest first, and the PDF notice
        # (with the purpose) usually follows its XBRL twin ("Board Meeting Intimation")
        out.append({"id": "nsebm:%s|%s" % (norm_name(company), meeting or link or purpose.lower()),
                    "source": "NSE meeting", "kind": "meeting", "ticker": resolver.symbol(company, "", link),
                    "company": company, "subject": purpose, "title": "%s %s" % (company, title),
                    "summary": "", "link": link, "time": when, "meeting_date": meeting,
                    "important": False})
    return out


def parse_nse_results(data, resolver):
    """NSE's Financial_Results.xml -> items."""
    out = []
    for it in _items(data, "NSE financial results"):
        company = re.sub(r"\s+", " ", _text(it, "title"))
        f = _fields(_text(it, "description"))
        bits = [f.get(k) for k in ("RELATING TO", "AUDITED/UNAUDITED", "CONSOLIDATED/NON-CONSOLIDATED") if f.get(k)]
        if f.get("PERIOD ENDED"):
            bits.append("period ended " + f["PERIOD ENDED"])
        raw_time = _text(it, "pubDate")
        out.append({"id": "nsefr:%s|%s|%s" % (norm_name(company), raw_time, "|".join(bits)),
                    "source": "NSE results", "kind": "filing", "ticker": resolver.symbol(company),
                    "company": company, "subject": "Financial Results",
                    "title": "%s financial results: %s" % (company, ", ".join(bits)),
                    "summary": "", "link": _text(it, "link"), "time": parse_time(raw_time),
                    "important": True})
    return out


PARSE_FEED = {"announcements": parse_nse_announcements, "actions": parse_nse_actions,
              "meetings": parse_nse_meetings, "results": parse_nse_results}


def parse_chart(data, ticker):
    """Yahoo chart JSON -> {"ticker", "price", "prev_close", "pct", "time"} or raise."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        meta = doc["chart"]["result"][0]["meta"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise NewsError("chart for %s is not usable" % ticker)
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if not price or not prev:
        raise NewsError("chart for %s has no price" % ticker)
    when = meta.get("regularMarketTime")
    return {"ticker": ticker, "price": round(float(price), 2), "prev_close": round(float(prev), 2),
            "pct": round((float(price) / float(prev) - 1) * 100, 2),
            "time": _dt.datetime.fromtimestamp(when, _dt.timezone.utc) if when else None}


def collect_moves(fetcher, watch, errors=None):
    errors = [] if errors is None else errors
    out = []
    for t in sorted(watch):
        name = "chart-%s.json" % t
        try:
            try:
                data = fetcher.get(CHART % ("query2", yahoo_symbol(t)), name)
            except NewsError as exc:
                if fetcher.offline or not re.search(r"HTTP (429|5\d\d)", str(exc)):
                    raise
                data = fetcher.get(CHART % ("query1", yahoo_symbol(t)), name)
            out.append(parse_chart(data, t))
        except NewsError as exc:
            if not (fetcher.offline and "no file" in str(exc)):
                errors.append("price for %s: %s" % (t, exc))
    return out


# ---------------------------------------------------------------- tagging

def _name_regex(name, exclude=()):
    tokens = re.findall(r"[A-Za-z0-9]+|&", name)
    if not tokens:
        return None
    parts = [r"(?:&|and)" if t == "&" or t.lower() == "and" else re.escape(t) for t in tokens]
    rx = r"(?<![A-Za-z0-9])" + r"[\s.'’-]*".join(parts) + r"(?![A-Za-z0-9])"
    if exclude:
        rx += r"(?!\s+(?:%s)\b)" % "|".join(re.escape(w) for w in sorted(exclude))
    return rx


def mention_patterns(watch, names=None):
    """{symbol: compiled regex}: 'NSE: SBIN', '(SBIN)', 'M&M', and the company's name
    (not when it is the start of another listed company's name, e.g. M&M Financial)."""
    names = names or {}
    others = {}
    for sym, lst in names.items():
        for n in lst:
            others.setdefault(norm_name(n), set()).add(sym)
    pats = {}
    for t in watch:
        e = re.escape(t)
        alts = [r"\bNSE\s*:\s*%s(?![A-Z0-9&-])" % e, r"\(%s\)" % e]
        if re.search(r"[&-]", t):
            alts.append(r"(?<![A-Za-z0-9&-])%s(?![A-Za-z0-9&-])" % e)
        full = [watch[t]] if watch.get(t) else []
        full += names.get(t, [])
        seen = set()
        for n in full:
            s = short_name(n)
            k = norm_name(s)
            if len(s) < 4 or k in seen:
                continue
            seen.add(k)
            ext = set()
            for o, syms in others.items():
                if o.startswith(k + " ") and t not in syms:
                    ext.add(o[len(k) + 1:].split(" ")[0])
            rx = _name_regex(s, ext)
            if rx:
                alts.append("(?i:%s)" % rx)
        pats[t] = re.compile("|".join(alts))
    return pats


def tag(items, watch, names=None):
    """Which of your stocks each item touches, and why it matters."""
    pats = mention_patterns(watch, names)
    for it in items:
        hay = "%s %s" % (it["title"], it.get("summary") or "")
        if it.get("company"):  # a filer's own name says nothing about other companies
            hay = re.sub(re.escape(it["company"]), " ", hay, flags=re.I)
        touches = {it["ticker"]} if it["ticker"] in watch else set()
        for t, rx in pats.items():
            if rx.search(hay):
                touches.add(t)
        it["touches"] = sorted(touches)
        full = "%s %s %s" % (it["title"], it.get("summary") or "", it.get("subject") or "")
        it["tags"] = [label for label, rx in KEYWORDS if re.search(rx, full, re.I)]
        if it["source"] == "NSE results" and "results" not in it["tags"]:
            it["tags"].insert(0, "results")
    return items


def dedupe(items):
    """One item per story: same id, or same link, or same title the same day."""
    seen, out = {}, []
    for it in items:
        keys = [it["id"], "" if GENERIC_LINK.match(it["link"] or "") else it["link"],
                "%s|%s" % (re.sub(r"\W+", " ", it["title"].lower()).strip(),
                           it["time"].date() if it["time"] else it.get("ex_date") or "")]
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit is not None:
            for t in it.get("touches", []):
                if t not in hit["touches"]:
                    hit["touches"].append(t)
            hit["touches"].sort()
            continue
        for k in keys:
            if k:
                seen[k] = it
        out.append(it)
    return out


def fingerprint(it):
    return hashlib.sha1(it["id"].encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the check

def collect(fetcher, watch, names, hours=48, feeds=None, google=True, errors=None, since=None):
    """Every item from the feeds, before tagging. NSE feeds are whole-market; the
    caller keeps what touches your stocks. `since`: the earliest time the caller still
    needs (window start or last check); a note says if the announcements feed starts later."""
    errors = [] if errors is None else errors
    feeds = [k for k, _, _ in NSE_FEEDS] if feeds is None else feeds
    items = []
    if google:
        skipped = []
        for t in sorted(watch):
            if skipped:  # Google said 429 twice: stop asking for this run
                skipped.append(t)
                continue
            name = query_name(t, watch, names)
            if not name and not fetcher.offline:
                errors.append("no company name for %s: searched Google News for the symbol" % t)
            try:
                items += parse_gnews(fetcher.get(gnews_url(name, t, hours), "gnews-%s.xml" % t, retry_429=True), t)
            except NewsError as exc:
                if "HTTP 429" in str(exc):
                    skipped.append(t)
                else:
                    errors.append("Google News for %s: %s" % (t, exc))
        if skipped:
            errors.append("Google News is rate-limiting this connection (HTTP 429 twice): no headlines for %s "
                          "this time; try again in a few minutes" % ", ".join(skipped))
    resolver = Resolver(names, watch)
    files = {k: f for k, f, _ in NSE_FEEDS}
    for key in feeds:
        try:
            data = fetcher.get(NSE_RSS % files[key], "nse-%s.xml" % key)
            items += PARSE_FEED[key](data, resolver)
        except NewsError as exc:
            if not (fetcher.offline and "no file" in str(exc)):
                errors.append("NSE %s feed: %s" % (key, exc))
            continue
        if key == "announcements" and since is not None:
            n, oldest = feed_span(data)
            if oldest and oldest > since:
                errors.append("NSE's announcements feed reaches back only to %s (its latest %d filings); "
                              "filings from %s until then are not in this check"
                              % (to_ist(oldest).strftime("%d-%b %H:%M IST"), n,
                                 to_ist(since).strftime("%d-%b %H:%M IST")))
    return items


def select(items, state, now, hours=48, show_all=False):
    """Items within the window (corporate actions: ex-date today or later);
    only unseen ones unless show_all."""
    seen = state.setdefault("seen", {})
    cutoff = now - _dt.timedelta(hours=hours)
    ahead = now + _dt.timedelta(minutes=10)  # a pinned --now: nothing from after it
    today = to_ist(now).date()
    out = []
    for it in items:
        if it.get("kind") == "action":
            if it.get("ex_date") is None or it["ex_date"] < today:
                continue
            it["important"] = it["ex_date"] == today
        elif it["time"] is not None and (it["time"] < cutoff or it["time"] > ahead):
            continue
        if it.get("kind") == "meeting" and it.get("meeting_date") == today:
            it["important"] = True
        fp = fingerprint(it)
        it["new"] = fp not in seen
        if it["new"] or show_all:
            out.append(it)
    out.sort(key=lambda i: i.get("ex_date") or _dt.date.max)  # ex-dates soonest first...
    out.sort(key=lambda i: (i["time"] or now), reverse=True)  # ...then newest first (stable)
    return out


def cap_headlines(shown, limit=MAX_HEADLINES):
    """Keep the newest `limit` Google News headlines per stock (NSE items always stay).
    Returns (kept, {symbol: number dropped})."""
    if not limit or limit <= 0:
        return shown, {}
    count, dropped, out = {}, {}, []
    for it in shown:  # newest first
        if it["source"] == "Google News":
            k = it["ticker"]
            count[k] = count.get(k, 0) + 1
            if count[k] > limit:
                dropped[k] = dropped.get(k, 0) + 1
                continue
        out.append(it)
    return out, dropped


def remember(items, state, now):
    seen = state.setdefault("seen", {})
    stamp = now.isoformat()
    for it in items:
        seen.setdefault(fingerprint(it), stamp)
    cutoff = (now - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_check"] = stamp


def render_moves(moves, move_pct=MOVE_PCT):
    out = []
    for m in moves:
        flag = "MOVING" if abs(m["pct"]) >= move_pct else ""
        when = (" at %s IST" % to_ist(m["time"]).strftime("%H:%M")) if m.get("time") else ""
        out.append(("%-10s %s %+.2f%% vs yesterday's close %s%s  %s" % (
            m["ticker"], inr(m["price"]), m["pct"], inr(m["prev_close"]), when, flag)).rstrip())
    return out


def _clip(text, n=220):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[:n - 3].rstrip() + "..."


def render(items, errors, now, moves=None, move_pct=MOVE_PCT):
    out = render_moves(moves or [], move_pct)
    if out:
        out.append("")
    if not items:
        out.append("Nothing new.")
    for it in items:
        if it.get("kind") == "action":
            when = "ex-date %s" % it["ex_date"].strftime("%Y-%m-%d")
        elif it["time"]:
            when = to_ist(it["time"]).strftime("%Y-%m-%d %H:%M IST")
        else:
            when = "time unknown"
        mark = "!" if it.get("important") else " "
        who = ",".join(it["touches"]) or it["ticker"]
        out.append("%s %-20s  %-11s %-11s %s" % (mark, when, who, it["source"], _clip(it["title"])))
        extra = []
        if it["tags"]:
            extra.append("tags: " + ", ".join(it["tags"]))
        if it["link"]:
            extra.append(it["link"])
        if extra:
            out.append("      " + "  ".join(extra))
    for e in errors:
        out.append("note: %s" % e)
    out.append("Checked %s. Headlines are leads, not facts; open the story or the filing before acting. "
               "Not financial advice." % to_ist(now).strftime("%Y-%m-%d %H:%M IST"))
    return "\n".join(out) + "\n"


def _json_value(v):
    if isinstance(v, _dt.datetime):
        return v.isoformat()
    if isinstance(v, _dt.date):
        return v.isoformat()
    return v


def _json_ready(items):
    out = []
    for it in items:
        d = {k: _json_value(v) for k, v in it.items()}
        d["time_ist"] = to_ist(it["time"]).strftime("%Y-%m-%d %H:%M") if it.get("time") else None
        out.append(d)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tickers", nargs="*", help="NSE symbols; default: watchlist + plan + portfolio")
    ap.add_argument("--portfolio", default="trader/portfolio.json")
    ap.add_argument("--watchlist", default="trader/watchlist.json")
    ap.add_argument("--plan", default="trader/plan.json")
    ap.add_argument("--state", default="trader/state/news.json")
    ap.add_argument("--names", default="trader/state/nse-names.json", help="company-name cache")
    ap.add_argument("--hours", type=float, default=48, help="ignore items older than this")
    ap.add_argument("--all", action="store_true", help="show seen items too")
    ap.add_argument("--nse-feeds", default=",".join(k for k, _, _ in NSE_FEEDS),
                    help="NSE feeds to read, comma separated (default: all four)")
    ap.add_argument("--no-nse", action="store_true", help="Google News headlines only")
    ap.add_argument("--no-google", action="store_true", help="NSE feeds only")
    ap.add_argument("--no-moves", action="store_true", help="skip the price line per stock")
    ap.add_argument("--max-headlines", type=int, default=MAX_HEADLINES,
                    help="Google News headlines per stock per check, newest first (0: no limit)")
    ap.add_argument("--move-pct", type=float, default=MOVE_PCT, help="flag a move this big (percent)")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help="IST time 'YYYY-MM-DD HH:MM' (or ISO with an offset), for reproducible runs")
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    known = watch_from_files(args.portfolio, args.watchlist, args.plan)
    if args.tickers:
        bad = [t for t in args.tickers if not clean_symbol(t)]
        if bad:
            print("news: not an NSE symbol: %s (e.g. RELIANCE, M&M, BAJAJ-AUTO)" % ", ".join(bad),
                  file=sys.stderr)
            return 2
        watch = {clean_symbol(t): None for t in args.tickers}
        watch.update({t: known[t] for t in watch if known.get(t)})
    else:
        watch = known
    if not watch:
        print("news: no tickers. Pass --tickers, or run /nse-scanner first (it writes "
              "trader/watchlist.json).", file=sys.stderr)
        return 2
    feeds = [f.strip().lower() for f in args.nse_feeds.split(",") if f.strip()]
    unknown = [f for f in feeds if f not in PARSE_FEED]
    if unknown:
        print("news: unknown NSE feed %s (choose from %s)" % (", ".join(unknown), ", ".join(PARSE_FEED)),
              file=sys.stderr)
        return 2
    now = parse_time(args.now) if args.now else _dt.datetime.now(_dt.timezone.utc)
    if now is None:
        print("news: --now must look like '2026-11-17 10:15' (IST)", file=sys.stderr)
        return 2
    state = load_json(args.state) or {}
    errors = []
    fetcher = Fetcher(offline=args.offline)
    names = load_names(fetcher, args.names, errors)
    since = now - _dt.timedelta(hours=args.hours)
    last = parse_time(state.get("last_check")) if state.get("last_check") and not args.all else None
    if last and since < last <= now:
        since = last  # filings older than the last check were already seen
    items = collect(fetcher, watch, names, hours=args.hours, feeds=[] if args.no_nse else feeds,
                    google=not args.no_google, errors=errors, since=since)
    items = [i for i in tag(items, watch, names) if i["touches"]]
    items = dedupe(items)
    shown = select(items, state, now, hours=args.hours, show_all=args.all)
    shown, dropped = cap_headlines(shown, args.max_headlines)
    for t in sorted(dropped):
        errors.append("%s: %d older Google News headline%s not shown (--max-headlines %d)"
                      % (t, dropped[t], "" if dropped[t] == 1 else "s", args.max_headlines))
    moves = [] if args.no_moves else collect_moves(fetcher, watch, errors)
    if args.json:
        mv = [dict(m, time=m["time"].isoformat() if m.get("time") else None,
                   moving=abs(m["pct"]) >= args.move_pct) for m in moves]
        print(json.dumps({"checked": now.isoformat(), "checked_ist": to_ist(now).strftime("%Y-%m-%d %H:%M"),
                          "moves": mv, "items": _json_ready(shown), "notes": errors},
                         indent=2, ensure_ascii=False))
    else:
        sys.stdout.write(render(shown, errors, now, moves, args.move_pct))
    if not args.dry_run:
        remember(items, state, now)
        folder = os.path.dirname(args.state)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.write("\n")
    return 0 if any(i["new"] for i in shown) else 1


if __name__ == "__main__":
    sys.exit(main())
