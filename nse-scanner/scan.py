#!/usr/bin/env python3
"""Chart Scanner for nse-scanner: scan hundreds of NSE charts for setups. Standard library only.

    python3 scan.py universe                          # Nifty 500 list -> trader/universe.txt
    python3 scan.py universe --list all               # every EQ-series stock on NSE instead
    python3 scan.py daily                             # before the open: daily setups -> trader/watchlist.json
    python3 scan.py daily --symbols RELIANCE,SBIN,M&M # or --watchlist FILE, or --limit 200
    python3 scan.py orb                               # after 9:35 AM IST: opening-range breakouts so far
    python3 scan.py orb --at 10:30 --or-minutes 15    # cut the clock at 10:30 AM IST
    python3 scan.py orb --offline wall5m.json.gz --now "2026-11-16 10:30" --min-price 0

universe  Downloads a symbol list and keeps the stocks you can trade intraday:
            --list nifty500 (default)  niftyindices.com's Nifty 500 constituents
            --list all                 NSE's EQUITY_L.csv, every listed equity
          The rule, in order:
            1. series EQ only (BE and BZ are trade-for-trade, no intraday;
               RR is a REIT)
            2. not a placeholder row (a "Dummy" name or a DUM... ISIN, like
               "Dummy HEG Ltd." in the Nifty 500 file of 2026-11-17)
            3. a plain NSE symbol: capitals, digits, & and - (M&M, BAJAJ-AUTO)

daily     Pre-market scan of daily CLOSES (Yahoo spark: 20 symbols a request,
          3 months of 1-day bars, closes only). Liquidity from the spark meta:
          regularMarketPrice >= --min-price (₹10) and price x
          regularMarketVolume >= --min-value (₹10 crore traded);
          --min-volume (shares, default 0) is there too. Setups, from closes only:
            breakout_20d     the last close is above every close of the 20 before it
            bull_flag        up more than 10 % over the 10 sessions before the
                             last 5, then the last 5 closes inside a 5 % range
            reclaim_ma20     closed under the 20-day average, then closed above it
            oversold         RSI 14 (Wilder) under 30
            oversold_bounce  RSI 14 was under 30 yesterday and today closed up
          Today's daily bar is dropped until the 3:30 PM close. Yahoo leaves a
          recent daily close of many NSE stocks null for a while (201 of 495 at
          11:00 AM on 2026-11-17 had no 2026-11-16 close): those charts end a
          session early, and the scan counts them and says so.

orb       Opening-range breakouts, 5-minute bars. The range is the first
          --or-minutes (15) of the session, 9:15 to 9:30 AM. Stage 1: spark
          closes for every symbol (1 day of 5-minute bars, 20 a request); a
          chart goes on only if a completed bar after the range closed at or
          above the highest close of the range (a necessary condition).
          Stage 2: each candidate's v8 chart (real highs and lows): range high
          = the highest high of the range bars, a hit = the first completed
          bar to CLOSE above it, at that bar's close time. Stage 2 fetches at
          most --max-confirm (500) charts, the most traded candidates first
          (price x volume so far), and says so when it stops short. A chart is
          usable only when every 5-minute bar from 9:15 to the cutoff is there.
          Yahoo's spark often skips one 5-minute bar of an NSE stock that the
          v8 chart has (61 of 495 Nifty 500 charts at 10:24 AM on 2026-11-17),
          so a spark chart with a gap goes to stage 2 as well; a bar missing
          from the v8 chart too is a halt or no trades, and the chart is out.
          Floors: --min-price 10 (last close), --min-volume 0, --min-value 0.
          No new hits after no_entries_after (trader/config.json, 14:30).

Bars: null closes are dropped, a bar off the 5-minute grid (the live tick,
like 9:48:39) is dropped, and a bar counts only once it is complete
(start + 5 minutes <= the clock). A full session is 75 bars (9:15 to the bar
that starts 3:25 PM). Times are IST (UTC+05:30). --now pins the clock
("2026-11-16 10:30", IST; ...Z for UTC). NSE holidays for 2026 are built in,
plus any "holidays" in trader/config.json (orb); another year gets a warning.

Yahoo's endpoints are free, unofficial and can lag. query2 first, query1 on
a 429 or 5xx, one retry after a pause on a 429. A polite pause between
requests (--pause). Partial results are reported as partial.

--offline PATH reads files instead of the network: universe wants the CSV
(or a folder holding it); daily and orb take a folder of <SYMBOL>.json v8
chart files or spark responses, or one file that is a spark response, a v8
chart, or a bundle {"RELIANCE": <v8 json>, ...}. .json.gz is fine.

A scan is a list of charts that match a rule, not a list of stocks to buy.
Exit code 0 when the scan found something, 1 when it did not or could not run.
"""

import argparse
import csv
import datetime
import gzip
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
from decimal import Decimal, ROUND_HALF_UP

UA = "Mozilla/5.0 (compatible; nse-day-trader/1.0)"
YAHOO_HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")
LISTS = {  # --list name -> (download URL, file names accepted offline, label)
    "nifty500": ("https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
                 ("ind_nifty500list.csv", "nifty500list.csv"), "Nifty 500 constituents (niftyindices.com)"),
    "all": ("https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
            ("EQUITY_L.csv",), "NSE equity list (EQUITY_L.csv)"),
}
SPARK_MAX = 20      # Yahoo's spark answers 400 above 20 symbols (checked for .NS on 2026-11-17)
PAUSE = 0.3         # seconds between requests: be polite to free endpoints
BACKOFF = 5.0       # seconds to wait before the one retry after a 429
SLEEP = time.sleep  # tests swap this out
BAR_MIN = 5
MAX_CONFIRM = 500   # stage-2 v8 charts per orb scan, most traded first (all of a Nifty 500 scan)
MIN_DAILY_BARS = 30
MIN_PRICE = 10.0     # rupees: at ₹10 the 1-paisa tick is 0.1 % of the price
MIN_VALUE = 1e8      # daily: ₹10 crore traded (price x volume)
DEFAULT_UNIVERSE = "trader/universe.txt"
DEFAULT_UNTIL = "14:30"   # no_entries_after when trader/config.json has none
NOTE = "A scan is a list of charts that match a rule, not a list of stocks to buy."
SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&-]{0,19}$")

# NSE capital-market segment trading holidays on weekdays, 2026
# (NSE circulars CMTR/71775 and CMTR/72260).
NSE_HOLIDAYS = {
    "2026-01-15": "Maharashtra municipal elections",
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali Balipratipada",
    "2026-11-24": "Guru Nanak Jayanti",
    "2026-12-25": "Christmas",
}
CALENDAR_YEARS = (2026,)
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
IST = datetime.timedelta(hours=5, minutes=30)   # fixed, no daylight saving
EPOCH = datetime.datetime(1970, 1, 1)

SETUPS = [
    ("breakout_20d", "20-day closing-high breakout"),
    ("bull_flag", "bull flag"),
    ("reclaim_ma20", "reclaimed the 20-day average"),
    ("oversold", "oversold (RSI under 30)"),
    ("oversold_bounce", "oversold bounce"),
]
LABEL = dict(SETUPS)


class ScanError(Exception):
    pass


# ---------------------------------------------------------------- time

def to_ist(utc):
    """Naive UTC -> naive IST (UTC+05:30 all year)."""
    return utc.replace(tzinfo=None) + IST


def ist_to_utc(local):
    return local.replace(tzinfo=None) - IST


def ts_to_ist(ts):
    """Unix seconds -> naive IST. Never uses the machine's time zone."""
    return to_ist(EPOCH + datetime.timedelta(seconds=int(ts)))


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as IST."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise ScanError('--now wants a time like "2026-11-16 10:30" (IST) or ...Z (UTC)')
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, flag="--at"):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", text or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ScanError("%s wants an IST time like 10:30" % flag)
    return datetime.time(int(m.group(1)), int(m.group(2)))


def trading_day(d, holidays=()):
    """(is_trading_day, reason). Weekends, the NSE holiday list and extra dates
    (trader/config.json "holidays"). A year outside CALENDAR_YEARS: weekdays trade
    (see calendar_warning)."""
    if d.weekday() >= 5:
        return False, "weekend"
    iso = d.isoformat()
    name = NSE_HOLIDAYS.get(iso)
    if name:
        return False, "NSE holiday: " + name
    if iso in holidays:
        return False, "a holiday in trader/config.json"
    return True, ""


def calendar_warning(d):
    """One line when the built-in holiday list does not cover d's year, else None."""
    if d.year in CALENDAR_YEARS:
        return None
    return "NSE holiday list for %d not loaded: check nseindia.com" % d.year


def clock_label(t):
    """datetime or time -> '10:15 AM'."""
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


def day_label(d):
    return "%s %s" % (d.strftime("%a"), d.isoformat())


# ---------------------------------------------------------------- network

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


def http_get(url, timeout=20):
    if not _CTX:
        _CTX.append(_ssl_context())
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX[0]) as r:
        return r.read().decode("utf-8", "replace")


def yahoo_get(path):
    """GET a Yahoo Finance path. query2 first, query1 on a 429 or 5xx, and one
    more round after BACKOFF seconds if a 429 was seen. 4xx other than 429 is final."""
    last = "no answer"
    for attempt in (0, 1):
        saw_429 = False
        for host in YAHOO_HOSTS:
            try:
                return http_get(host + path)
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                try:
                    body = e.read().decode("utf-8", "replace") if 400 <= e.code < 500 and e.code != 429 else ""
                except Exception:
                    body = ""
                finally:
                    e.close()
                if e.code == 429:
                    saw_429 = True
                    continue
                if e.code >= 500:
                    continue
                if body.strip().startswith("{"):
                    return body  # Yahoo's JSON error ("No data found"), parsed by the caller
                raise ScanError("Yahoo answered %s for %s" % (last, path.split("?")[0]))
            except (urllib.error.URLError, OSError) as e:
                last = str(getattr(e, "reason", e))
                continue
        if attempt == 0 and saw_429:
            SLEEP(BACKOFF)
            continue
        break
    raise ScanError("Yahoo answered %s for %s" % (last, path.split("?")[0]))


def norm_symbol(text):
    """User input -> NSE symbol: trimmed, upper case, a trailing .NS dropped."""
    s = str(text).strip().upper()
    return s[:-3] if s.endswith(".NS") else s


def yahoo_symbol(sym):
    """M&M -> M&M.NS, the form Yahoo answers with (meta.symbol, spark's symbol)."""
    return norm_symbol(sym) + ".NS"


def to_yahoo(sym):
    """The URL form: M&M -> M%26M.NS. An unquoted & ends the symbols parameter."""
    return urllib.parse.quote(yahoo_symbol(sym), safe="")


def from_yahoo(ysym):
    """M&M.NS (or M%26M.NS) -> M&M."""
    return norm_symbol(urllib.parse.unquote(ysym or ""))


def spark_path(symbols, rng, interval):
    return "/v7/finance/spark?symbols=%s&range=%s&interval=%s" % (
        ",".join(to_yahoo(s) for s in symbols), rng, interval)


def chart_path(sym, rng, interval):
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (to_yahoo(sym), rng, interval)


# ---------------------------------------------------------------- parsing

def series_from_result(res):
    """A Yahoo chart result (v8 or one spark response) -> plain lists."""
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    t = res.get("timestamp") or []
    n = len(t)

    def col(k):
        v = q.get(k)
        return list(v) + [None] * (n - len(v)) if isinstance(v, list) else None
    return {"t": list(t), "c": col("close") or [None] * n, "o": col("open"), "h": col("high"),
            "l": col("low"), "v": col("volume"), "meta": res.get("meta") or {}}


def parse_spark(text_or_doc, wanted=None):
    """Spark JSON -> {SYM: series}. Symbols Yahoo does not know are simply missing
    from the answer. `wanted` maps Yahoo symbols back to ours (M&M.NS -> M&M)."""
    doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
    sp = (doc or {}).get("spark") if isinstance(doc, dict) else None
    if not isinstance(sp, dict):
        raise ScanError("Yahoo's spark answer is not the shape this tool knows")
    err = sp.get("error")
    out = {}
    for item in sp.get("result") or []:
        resp = item.get("response") or []
        if not resp:
            continue
        ysym = item.get("symbol") or (resp[0].get("meta") or {}).get("symbol") or ""
        sym = (wanted or {}).get(urllib.parse.unquote(ysym).upper()) or from_yahoo(ysym)
        out[sym] = series_from_result(resp[0])
    if err and not out:
        raise ScanError("Yahoo spark: %s" % (err.get("description") or err.get("code") or err))
    return out


def parse_chart(text_or_doc, sym=""):
    """v8 chart JSON -> series. Raises ScanError for Yahoo's 'no data' answers."""
    try:
        doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
        chart = doc["chart"]
    except (ValueError, KeyError, TypeError):
        raise ScanError("Yahoo returned something unexpected for %s" % sym)
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise ScanError("no data for %s: %s" % (sym, err.get("description", "unknown symbol")))
    return series_from_result(res[0])


def _read_maybe_gz(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _stem(name):
    for ext in (".json.gz", ".json"):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def _chart_symbol(doc):
    try:
        return (doc["chart"].get("result") or [{}])[0]["meta"]["symbol"]
    except (AttributeError, KeyError, TypeError, IndexError):
        return ""


def _absorb(doc, name_hint, data, errors):
    """One parsed offline JSON doc -> data / errors."""
    if isinstance(doc, dict) and "spark" in doc:
        data.update(parse_spark(doc))
    elif isinstance(doc, dict) and "chart" in doc:
        sym = name_hint or _chart_symbol(doc)
        if not sym:
            raise ScanError("an offline v8 chart without a symbol: name the file <SYMBOL>.json")
        sym = from_yahoo(sym)
        try:
            data[sym] = parse_chart(doc, sym)
        except ScanError as e:
            errors[sym] = str(e)
    elif isinstance(doc, dict):
        for k, v in doc.items():
            _absorb(v, k, data, errors)
    else:
        raise ScanError("offline file %s is not a spark answer, a v8 chart or a bundle" % name_hint)


def load_offline(path):
    """A folder of <SYM>.json(.gz) files or one file -> ({SYM: series}, {SYM: error})."""
    data, errors = {}, {}
    if os.path.isdir(path):
        names = sorted(f for f in os.listdir(path) if f.endswith((".json", ".json.gz")))
        if not names:
            raise ScanError("no .json files in %s" % path)
        for f in names:
            try:
                doc = json.loads(_read_maybe_gz(os.path.join(path, f)))
            except ValueError as e:
                raise ScanError("%s is not valid JSON: %s" % (f, e))
            if isinstance(doc, dict) and "chart" in doc:
                _absorb(doc, _chart_symbol(doc) or _stem(f), data, errors)  # the chart's own symbol first
            else:
                _absorb(doc, "", data, errors)
    elif os.path.isfile(path):
        try:
            doc = json.loads(_read_maybe_gz(path))
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        _absorb(doc, "", data, errors)
    else:
        raise ScanError("no offline file or folder at %s" % path)
    return data, errors


# ---------------------------------------------------------------- universe

def parse_list(text):
    """niftyindices.com's index CSV (Company Name, Industry, Symbol, Series, ISIN Code)
    or NSE's EQUITY_L.csv (SYMBOL, NAME OF COMPANY, SERIES, ...) -> rows
    {symbol, name, series, isin}."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    hdr = [h.strip().lower() for h in next(reader, [])]
    if "symbol" not in hdr or "series" not in hdr:
        raise ScanError("not an NSE symbol list (header %s)" % ",".join(hdr)[:60])
    name_col = "company name" if "company name" in hdr else "name of company"
    isin_col = "isin code" if "isin code" in hdr else "isin number"
    rows = []
    for rec in reader:
        if not any(x.strip() for x in rec):
            continue
        d = dict(zip(hdr, (x.strip() for x in rec)))
        rows.append({"symbol": d.get("symbol", "").upper(), "name": " ".join(d.get(name_col, "").split()),
                     "series": d.get("series", "").upper(), "isin": d.get(isin_col, "").upper()})
    return rows


def classify(row):
    """One list row -> 'keep' or the rule that drops it."""
    if row["series"] != "EQ":
        return "series %s" % (row["series"] or "blank")
    if row["isin"].startswith("DUM") or row["name"].lower().startswith("dummy"):
        return "placeholder"
    if not SYMBOL.match(row["symbol"]):
        return "not an NSE symbol"
    return "keep"


def build_universe(text):
    """CSV text -> (kept [(SYM, name)], counts)."""
    kept, counts = {}, {}
    rows = parse_list(text)
    counts["rows"] = len(rows)
    for r in rows:
        why = classify(r)
        counts[why] = counts.get(why, 0) + 1
        if why == "keep":
            kept[r["symbol"]] = r["name"]
    return sorted(kept.items()), counts


# ---------------------------------------------------------------- daily setups

def rsi(closes, n=14):
    """Wilder's RSI on closes."""
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def daily_closes(series, now_utc):
    """[(date, close)] of COMPLETE daily bars: null closes dropped, today's bar dropped
    until the session closes, anything dated after the clock dropped."""
    now = to_ist(now_utc)
    today = now.date()
    today_done = not trading_day(today)[0] or now.time() >= CLOSE
    out = {}
    for ts, c in zip(series["t"], series["c"]):
        if c is None:
            continue
        d = ts_to_ist(ts).date()
        if d > today or (d == today and not today_done):
            continue
        out[d] = float(c)  # one bar a day: a late duplicate (the live tick) wins
    return sorted(out.items())


def find_setups(closes):
    """closes (oldest first, >= 30) -> (setup keys, numbers)."""
    c = closes
    last = c[-1]
    hi20 = max(c[-21:-1])
    ma20 = sum(c[-20:]) / 20.0
    ma20_prev = sum(c[-21:-1]) / 20.0
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r = rsi(c)
    r_prev = rsi(c[:-1])
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    return tags, {"rsi": r, "rsi_prev": r_prev, "high20": hi20, "ma20": ma20,
                  "run_pct": run * 100, "range5_pct": rng5 * 100}


def _illiquid(price, vol, min_price, min_volume, min_value):
    if price is None or price < min_price:
        return True
    if (min_volume > 0 or min_value > 0) and vol is None:
        return True
    if min_volume > 0 and vol < min_volume:
        return True
    return min_value > 0 and price * vol < min_value


def scan_daily(data, symbols, now_utc, min_price, min_volume, min_value=0):
    """Pure. data = {SYM: series}. Returns (hits, counts, last_date)."""
    hits, dates = [], {}
    counts = {"requested": len(symbols), "fetched": 0, "illiquid": 0, "short": 0, "scanned": 0}
    for sym in symbols:
        s = data.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = daily_closes(s, now_utc)
        meta = s.get("meta") or {}
        price = meta.get("regularMarketPrice")
        if price is None and bars:
            price = bars[-1][1]
        vol = meta.get("regularMarketVolume")
        if _illiquid(price, vol, min_price, min_volume, min_value):
            counts["illiquid"] += 1
            continue
        if len(bars) < MIN_DAILY_BARS:
            counts["short"] += 1
            continue
        counts["scanned"] += 1
        d_last = bars[-1][0]
        dates[d_last] = dates.get(d_last, 0) + 1
        closes = [c for _, c in bars]
        tags, nums = find_setups(closes)
        if tags:
            hits.append({"symbol": sym, "setups": tags, "price": round(closes[-1], 2),
                         "close_date": d_last.isoformat(), "rsi": round(nums["rsi"], 1),
                         "volume": int(vol) if vol is not None else None})
    hits.sort(key=lambda h: (-(h["price"] * (h["volume"] or 0)), h["symbol"]))  # most rupees traded first
    last_date = max(dates, key=lambda d: (dates[d], d)) if dates else None
    # Yahoo leaves a recent daily close of many NSE stocks null for a while; those charts end a session early
    counts["stale"] = sum(n for d, n in dates.items() if d < last_date) if dates else 0
    counts["hits"] = len(hits)
    return hits, counts, last_date


# ---------------------------------------------------------------- opening-range breakout

def slot(day, k):
    return datetime.datetime.combine(day, OPEN) + datetime.timedelta(minutes=BAR_MIN * k)


SESSION_BARS = int((datetime.datetime.combine(EPOCH, CLOSE) - datetime.datetime.combine(EPOCH, OPEN))
                   .total_seconds() // (BAR_MIN * 60))   # 75


def session_bars(series, day, cutoff):
    """5-minute bars of `day` that are complete at `cutoff` (naive IST),
    on the grid, with a close. Sorted, one per slot."""
    out = {}
    hs, ls = series.get("h"), series.get("l")
    for i, ts in enumerate(series["t"]):
        c = series["c"][i]
        if c is None:
            continue
        lt = ts_to_ist(ts)
        if lt.date() != day or lt.second or lt.minute % BAR_MIN:
            continue  # another day, or the live tick off the grid
        if lt.time() < OPEN or lt.time() >= CLOSE:
            continue  # pre-open, the 3:30 PM closing print
        if lt + datetime.timedelta(minutes=BAR_MIN) > cutoff:
            continue  # still in progress at the cutoff
        v = series.get("v")
        out[lt] = {"start": lt, "c": float(c),
                   "h": None if not hs or hs[i] is None else float(hs[i]),
                   "l": None if not ls or ls[i] is None else float(ls[i]),
                   "v": (v[i] or 0) if v else 0}
    return [out[k] for k in sorted(out)]


def usable(bars, day, cutoff, n_range):
    """Every slot from 9:15 to the cutoff is there, and at least the range."""
    want = min(int((cutoff - slot(day, 0)).total_seconds() // (BAR_MIN * 60)), SESSION_BARS)
    if want < n_range or len(bars) != want:
        return False
    return all(b["start"] == slot(day, k) for k, b in enumerate(bars))


def orb_candidate(bars, n_range):
    """Stage 1, closes only: some completed bar after the range closed at or above
    the highest close of the range. Necessary for a breakout (range high >= any range close)."""
    top = max(b["c"] for b in bars[:n_range])
    return any(b["c"] >= top for b in bars[n_range:])


def orb_hit(bars, n_range):
    """Stage 2, real highs and lows: the first completed bar to CLOSE above the range high."""
    rng = bars[:n_range]
    if any(b["h"] is None or b["l"] is None for b in rng):
        return None
    oh = max(b["h"] for b in rng)
    ol = min(b["l"] for b in rng)
    for k in range(n_range, len(bars)):
        b = bars[k]
        if b["c"] > oh:
            closes_at = b["start"] + datetime.timedelta(minutes=BAR_MIN)
            return {"price": round(b["c"], 2), "at": clock_label(closes_at),
                    "bar_start": b["start"].strftime("%H:%M"), "bar_index": k,
                    "or_high": round(oh, 2), "or_low": round(ol, 2),
                    "pct_over": round((b["c"] / oh - 1) * 100, 2), "_t": closes_at}
    return None


def orb_window(now_utc, at, or_minutes, until, explicit_day=None, holidays=()):
    """-> (day, cutoff naive IST) or raise ScanError with the plain message."""
    now = to_ist(now_utc)
    day = explicit_day or now.date()
    if or_minutes < BAR_MIN or or_minutes % BAR_MIN or or_minutes > 120:
        raise ScanError("--or-minutes wants a multiple of 5 from 5 to 120 (15 is the classic)")
    ok, why = trading_day(day, holidays)
    if not ok:
        raise ScanError("No session on %s (%s): the market is closed. Run orb on a trading day "
                        "after the opening range, or replay a past day with --offline." % (day_label(day), why))
    range_end = slot(day, or_minutes // BAR_MIN)
    first_close = range_end + datetime.timedelta(minutes=BAR_MIN)
    close_dt = datetime.datetime.combine(day, CLOSE)
    if at is not None:
        cutoff = datetime.datetime.combine(day, at)
        if explicit_day is None and cutoff > now + datetime.timedelta(seconds=59):
            raise ScanError("--at %s is later than now (%s IST)." % (at.strftime("%H:%M"), clock_label(now)))
        if cutoff > close_dt:
            raise ScanError("--at %s is after the %s close on %s." % (at.strftime("%H:%M"), clock_label(close_dt),
                                                                       day_label(day)))
    else:
        cutoff = now.replace(second=0, microsecond=0)
        if cutoff >= close_dt:
            raise ScanError("The market closed at %s IST on %s. Replay the session with --at 10:30."
                            % (clock_label(close_dt), day_label(day)))
    if cutoff < first_close:
        raise ScanError("It is %s IST: the %d-minute opening range runs 9:15 to %s and the first bar after it "
                        "closes at %s. Run orb again after %s."
                        % (clock_label(cutoff), or_minutes, clock_label(range_end), clock_label(first_close),
                           clock_label(first_close)))
    if until is not None and cutoff.time() > until:
        raise ScanError("It is %s IST, after %s, your last time for new entries (no_entries_after): an "
                        "opening-range breakout found now is expired. Run orb tomorrow after %s, or replay "
                        "today with --at 10:30." % (clock_label(cutoff), until.strftime("%H:%M"),
                                                     clock_label(first_close)))
    return day, cutoff


def scan_orb(stage1, symbols, day, cutoff, or_minutes, min_price=0.0, min_volume=0, confirm=None,
             max_confirm=None, min_value=0):
    """stage1 = {SYM: series} with closes (spark or v8). confirm(sym) -> series with highs/lows
    (None = use the stage-1 series). With confirm and max_confirm, only the max_confirm most
    traded candidates (price x volume so far) are confirmed; the rest are counted as unconfirmed.
    With confirm, a stage-1 chart with a missing bar (a gap) is a candidate too: Yahoo's spark
    skips 5-minute bars of NSE stocks that the v8 chart has, so the v8 chart decides.
    Returns (hits, counts, failed {sym: why})."""
    n_range = or_minutes // BAR_MIN
    counts = {"requested": len(symbols), "fetched": 0, "unusable": 0, "illiquid": 0, "scanned": 0,
              "gaps": 0, "candidates": 0, "confirmed": 0, "unconfirmed": 0}
    hits, failed, cands = [], {}, []
    for sym in symbols:
        s = stage1.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = session_bars(s, day, cutoff)
        gap = not usable(bars, day, cutoff, n_range)
        if gap and (confirm is None or not bars):
            counts["unusable"] += 1
            continue
        vol = sum(b["v"] for b in bars) if s.get("v") else (s.get("meta") or {}).get("regularMarketVolume") or 0
        if _illiquid(bars[-1]["c"], vol, min_price, min_volume, min_value):
            counts["illiquid"] += 1
            continue
        if gap:
            counts["gaps"] += 1
            cands.append((sym, bars, bars[-1]["c"] * vol, True))
            continue
        counts["scanned"] += 1
        if orb_candidate(bars, n_range):
            cands.append((sym, bars, bars[-1]["c"] * vol, False))
    counts["candidates"] = len(cands)
    if confirm is not None and max_confirm and len(cands) > max_confirm:
        cands.sort(key=lambda x: -x[2])
        counts["unconfirmed"] = len(cands) - max_confirm
        cands = cands[:max_confirm]
    for sym, bars, _, gap in cands:
        full = bars
        if confirm is not None:
            try:
                s2 = confirm(sym)
            except ScanError as e:
                failed[sym] = str(e)
                continue
            full = session_bars(s2, day, cutoff)
            if not usable(full, day, cutoff, n_range):
                if gap:
                    counts["unusable"] += 1    # missing in both: a halt or no trades
                else:
                    failed[sym] = "the v8 chart has a missing bar"
                continue
            if gap:
                counts["scanned"] += 1
        counts["confirmed"] += 1
        h = orb_hit(full, n_range)
        if h:
            hits.append(dict({"symbol": sym, "setups": ["orb"]}, **h))
    hits.sort(key=lambda x: (x["_t"], x["symbol"]))
    for h in hits:
        del h["_t"]
    counts["hits"] = len(hits)
    return hits, counts, failed


# ---------------------------------------------------------------- fetching

def progress(a, msg):
    if not getattr(a, "json", False) and not getattr(a, "quiet", False):
        print(msg, file=sys.stderr)


def fetch_spark(symbols, rng, interval, pause, a=None):
    """Batches of 20. -> ({SYM: series}, {SYM: error})."""
    data, failed = {}, {}
    batches = [symbols[i:i + SPARK_MAX] for i in range(0, len(symbols), SPARK_MAX)]
    for k, batch in enumerate(batches):
        if k and pause:
            SLEEP(pause)
        wanted = {yahoo_symbol(s): s for s in batch}
        try:
            got = parse_spark(yahoo_get(spark_path(batch, rng, interval)), wanted)
        except (ScanError, ValueError) as e:
            for s in batch:
                failed[s] = str(e)
            continue
        for s in batch:
            if s in got:
                data[s] = got[s]
            else:
                failed[s] = "no data from Yahoo"
        if a is not None and len(batches) > 25 and (k + 1) % 50 == 0:
            progress(a, "  spark %d of %d requests" % (k + 1, len(batches)))
    return data, failed


def read_symbols(a, offline_syms=None):
    if a.symbols:
        syms = [norm_symbol(s) for s in re.split(r"[,\s]+", a.symbols) if s.strip()]
    elif a.watchlist:
        syms = symbols_from_file(a.watchlist)
    elif offline_syms is not None and not a.universe:
        # offline: every stock chart in the file, unless a list was given (an index like ^NSEI is skipped)
        syms = sorted(s for s in offline_syms if SYMBOL.match(s))
    else:
        path = a.universe or DEFAULT_UNIVERSE
        if not os.path.exists(path):
            raise ScanError("no %s yet: run `scan.py universe` first, or pass --symbols" % path)
        syms = symbols_from_file(path)
    if offline_syms is None or a.symbols or a.watchlist or a.universe:
        bad = [s for s in syms if not SYMBOL.match(s)]
        if bad:
            raise ScanError("not an NSE symbol: %s (like RELIANCE, M&M, BAJAJ-AUTO; an index such as "
                            "^NSEI is not tradable)" % ", ".join(bad[:5]))
    out = []
    for s in syms:
        if s not in out:
            out.append(s)
    if a.limit:
        out = out[:a.limit]
    if not out:
        raise ScanError("no symbols to scan")
    return out


def symbols_from_file(path):
    if not os.path.exists(path):
        raise ScanError("no file at %s" % path)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        items = doc.get("hits", []) if isinstance(doc, dict) else doc
        return [norm_symbol(x.get("symbol") if isinstance(x, dict) else x) for x in items if x]
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(norm_symbol(ln.split("|")[0]))
    return out


def read_config(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except ValueError as e:
        raise ScanError("%s is not valid JSON: %s" % (path, e))
    return cfg if isinstance(cfg, dict) else {}


def config_holidays(cfg):
    """trader/config.json "holidays": ["YYYY-MM-DD", ...] -> a set of ISO dates."""
    out = set()
    for d in cfg.get("holidays") or []:
        try:
            out.add(datetime.date.fromisoformat(str(d).strip()).isoformat())
        except ValueError:
            raise ScanError('"holidays" in trader/config.json wants dates like "2026-12-31", not %r' % d)
    return out


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def _group_in(digits):
    """'1234567' -> '12,34,567' (Indian grouping: the last three, then twos)."""
    if len(digits) <= 3:
        return digits
    head, parts = digits[:-3], [digits[-3:]]
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    return ",".join([head] + parts) if head else ",".join(parts)


def inr(x):
    """123456.5 -> '₹1,23,456.50'; -1250 -> '-₹1,250.00'."""
    d = Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    whole, frac = "{:.2f}".format(abs(d)).split(".")
    return "%s₹%s.%s" % ("-" if d < 0 else "", _group_in(whole), frac)


def count(n):
    """An integer count with Indian grouping: 2404 -> '2,404', 150000 -> '1,50,000'."""
    return ("-" if n < 0 else "") + _group_in(str(abs(int(n))))


def rupees_short(x):
    """A rupee threshold in words: 1e8 -> '₹10 crore', 5e5 -> '₹5 lakh', 20 -> '₹20'."""
    for unit, name in ((1e7, "crore"), (1e5, "lakh")):
        if x >= unit:
            return "₹%s %s" % (_n(x / unit), name)
    return "₹" + _n(x)


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def _floors(a):
    parts = []
    if a.min_price:
        parts.append("%s+" % rupees_short(a.min_price))
    if a.min_volume:
        parts.append("%s+ shares" % count(a.min_volume))
    if a.min_value:
        parts.append("%s+ traded" % rupees_short(a.min_value))
    return " and ".join(parts) if parts else "no floor"


# ---------------------------------------------------------------- commands

def cmd_universe(a):
    t0 = time.time()
    url, names, label = LISTS[a.list]
    if a.offline:
        p = a.offline
        if os.path.isdir(p):
            found = [os.path.join(p, n) for n in names if os.path.exists(os.path.join(p, n))]
            if not found:
                raise ScanError("no %s in %s" % (" or ".join(names), p))
            p = found[0]
        if not os.path.exists(p):
            raise ScanError("no offline file or folder at %s" % p)
        with open(p, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    else:
        host = urllib.parse.urlparse(url).netloc
        try:
            text = http_get(url, timeout=60)
        except urllib.error.HTTPError as e:
            raise ScanError("%s answered HTTP %d for %s" % (host, e.code, url.rsplit("/", 1)[1]))
        except (urllib.error.URLError, OSError) as e:
            raise ScanError("could not reach %s: %s" % (host, getattr(e, "reason", e)))
    kept, counts = build_universe(text)
    if not a.no_write:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            for sym, name in kept:
                fh.write("%s|%s\n" % (sym, name))
    dropped = {k: v for k, v in counts.items() if k not in ("keep", "rows")}
    if a.json:
        print(json.dumps({"list": a.list, "count": len(kept), "rows": counts.get("rows", 0),
                          "dropped": dropped, "out": None if a.no_write else a.out,
                          "seconds": round(time.time() - t0, 1)}, indent=2))
    else:
        print("%s: %s rows." % (label, count(counts.get("rows", 0))))
        print("Kept %s EQ-series stocks (intraday allowed on NSE)." % count(len(kept)))
        if dropped:
            print("Dropped: " + ", ".join("%s %s" % (count(v), k)
                                          for k, v in sorted(dropped.items(), key=lambda kv: (-kv[1], kv[0]))) + ".")
        if not a.no_write:
            print("Wrote %s (one SYMBOL|Name a line)." % a.out)
    return 0 if kept else 1


def cmd_daily(a):
    t0 = time.time()
    now = parse_now(a.now)
    local = to_ist(now)
    warn = calendar_warning(local.date())
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
    symbols = read_symbols(a, offline_syms)
    if not a.offline:
        progress(a, "Fetching daily closes for %s symbols (%d spark requests)..."
                 % (count(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(symbols, "3mo", "1d", a.pause, a)
    hits, counts, last_date = scan_daily(data, symbols, now, a.min_price, a.min_volume, a.min_value)
    nfailed = len(symbols) - counts["fetched"]
    as_of = ("%s close" % last_date.isoformat()) if last_date else None
    doc = {"created_by": "nse-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "daily",
           "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"], "failed": nfailed,
           "source": "yahoo", "filters": {"min_price": a.min_price, "min_volume": a.min_volume,
                                          "min_value": a.min_value},
           "note": NOTE, "hits": hits}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        extra = {"warning": warn} if warn else {}
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1), **extra), indent=2,
                         ensure_ascii=False))
        return 0 if hits else 1
    if warn:
        print(warn + ".")
    print("Daily scan, %s IST, closes through %s (Yahoo spark, daily closes only)." % (
        local.strftime("%a %Y-%m-%d %H:%M"), last_date.isoformat() if last_date else "nothing"))
    in_session = trading_day(local.date())[0] and OPEN <= local.time() < CLOSE
    print("Fetched %s of %s charts%s. %s passed the liquidity filter (%s%s)%s; %s scanned."
          % (count(counts["fetched"]), count(len(symbols)),
             (" (%s failed or had no data)" % count(nfailed)) if nfailed else "",
             count(counts["fetched"] - counts["illiquid"]), _floors(a),
             ", on today's volume so far" if in_session and (a.min_value or a.min_volume) else "",
             (", %d had under %d days" % (counts["short"], MIN_DAILY_BARS)) if counts["short"] else "",
             count(counts["scanned"])))
    if counts["stale"]:
        print("%s of the %s end before %s: Yahoo has no close for that session yet, so their setups use the "
              "closes before it." % (count(counts["stale"]), count(counts["scanned"]), last_date.isoformat()))
    if hits:
        per = [(k, sum(1 for h in hits if k in h["setups"])) for k, _ in SETUPS]
        print("%d chart%s matched a setup: %s." % (len(hits), "" if len(hits) == 1 else "s",
                                                  ", ".join("%d %s" % (n, LABEL[k]) for k, n in per if n)))
        print("Most traded first (price x volume):")
    else:
        print("No setups.")
    for h in hits[:a.show]:
        stale = last_date and h["close_date"] != last_date.isoformat()  # Yahoo skipped this chart's last session
        print("  %-10s %12s  RSI %5.1f  %s%s" % (h["symbol"], inr(h["price"]), h["rsi"],
                                                 ", ".join(LABEL[t] for t in h["setups"]),
                                                 ("  (closes through %s)" % h["close_date"]) if stale else ""))
    if len(hits) > a.show:
        print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    if not a.no_write:
        print("Wrote %s (mode daily, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /nse-news SYMBOL.")
    return 0 if hits else 1


def cmd_orb(a):
    t0 = time.time()
    now = parse_now(a.now)
    at = parse_hhmm(a.at) if a.at else None
    cfg = read_config(a.config)
    holidays = config_holidays(cfg)
    until = parse_hhmm(a.until or cfg.get("no_entries_after") or DEFAULT_UNTIL, "--until")
    data = failed = None
    explicit_day = None
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
        if not a.now:
            days = [ts_to_ist(s["t"][-1]).date() for s in data.values() if s["t"]]
            if days:
                explicit_day = max(days)
                if at is None:
                    raise ScanError("offline replay: give --at HH:MM or --now \"YYYY-MM-DD HH:MM\"")
    day, cutoff = orb_window(now, at, a.or_minutes, until, explicit_day, holidays)
    warn = calendar_warning(day)
    symbols = read_symbols(a, offline_syms)
    confirm = None
    if not a.offline:
        today = to_ist(parse_now(None)).date()
        if (today - day).days > 6:
            raise ScanError("Yahoo's free 5-minute history is short: replay %s with --offline" % day.isoformat())
        rng = "1d" if day == today else "5d"
        progress(a, "Stage 1: 5-minute closes for %s symbols (%d spark requests)..."
                 % (count(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(symbols, rng, "%dm" % BAR_MIN, a.pause, a)
        done = []

        def confirm(sym):
            if a.pause:
                SLEEP(a.pause)
            done.append(sym)
            if len(done) % 50 == 0:
                progress(a, "  stage 2: %d v8 charts so far" % len(done))
            return parse_chart(yahoo_get(chart_path(sym, rng, "%dm" % BAR_MIN)), sym)
    hits, counts, failed2 = scan_orb(data, symbols, day, cutoff, a.or_minutes, a.min_price, a.min_volume, confirm,
                                     a.max_confirm, a.min_value)
    failed = dict(failed or {})
    failed.update(failed2)
    nfailed = len([s for s in symbols if s in failed])
    as_of = "%s %s IST" % (day.isoformat(), cutoff.strftime("%H:%M"))
    doc = {"created_by": "nse-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "orb",
           "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"], "failed": nfailed,
           "unconfirmed": counts["unconfirmed"], "source": "yahoo", "or_minutes": a.or_minutes,
           "interval": "%dm" % BAR_MIN,
           "filters": {"min_price": a.min_price, "min_volume": a.min_volume, "min_value": a.min_value},
           "note": NOTE, "hits": hits}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        extra = {"warning": warn} if warn else {}
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1), **extra), indent=2,
                         ensure_ascii=False))
        return 0 if hits else 1
    if warn:
        print(warn + ".")
    print("Opening-range breakout scan, %s %s IST: %d-minute range (9:15 to %s), 5-minute bars, Yahoo."
          % (day_label(day), clock_label(cutoff), a.or_minutes, clock_label(slot(day, a.or_minutes // BAR_MIN))))
    print("Fetched %s of %s charts%s; %s usable%s; %s candidates%s, %s confirmed with real highs and lows."
          % (count(counts["fetched"]), count(len(symbols)),
             (" (%s failed or had no data)" % count(nfailed)) if nfailed else "",
             count(counts["scanned"]),
             (" (%s under the %s floor)" % (count(counts["illiquid"]), _floors(a))) if counts["illiquid"] else "",
             count(counts["candidates"]),
             (" (%s of them for a missing bar in Yahoo's spark closes)" % count(counts["gaps"]))
             if counts["gaps"] else "",
             count(counts["confirmed"])))
    if counts["unconfirmed"]:
        print("PARTIAL: %s more candidates were not checked (only the %s most traded are; --max-confirm 0 "
              "checks all)." % (count(counts["unconfirmed"]), count(a.max_confirm)))
    if hits:
        print("%d of %s charts closed above their opening-range high by %s:"
              % (len(hits), ("the %s checked" % count(counts["confirmed"])) if counts["unconfirmed"]
                 else count(counts["scanned"]), clock_label(cutoff)))
        for h in hits[:a.show]:
            print("  %8s  %-10s %12s  range %s to %s  %+.2f%% over" % (
                h["at"], h["symbol"], inr(h["price"]), _p(h["or_low"]), _p(h["or_high"]), h["pct_over"]))
        if len(hits) > a.show:
            print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    else:
        print("No chart closed above its opening-range high by %s." % clock_label(cutoff))
    if not a.no_write:
        print("Wrote %s (mode orb, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /nse-news SYMBOL and /nse-entry SYMBOL.")
    return 0 if hits else 1


def _p(x):
    """A price without the rupee sign, Indian grouping: 1342.2 -> '1,342.20'."""
    return inr(x).replace("₹", "")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Chart Scanner (nse-scanner): daily setups and opening-range "
                                             "breakouts on NSE stocks.")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("universe", help="download the Nifty 500 (or every EQ stock) -> trader/universe.txt")
    p.add_argument("--list", choices=sorted(LISTS), default="nifty500",
                   help="nifty500 (default) or all (NSE's EQUITY_L.csv, series EQ)")
    p.add_argument("--out", default=DEFAULT_UNIVERSE)
    p.add_argument("--offline", metavar="PATH", help="the CSV, or a folder holding it")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--json", action="store_true")

    def common(p, min_price, min_value):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--symbols", help="comma-separated NSE symbols instead of the universe (RELIANCE,M&M)")
        g.add_argument("--watchlist", metavar="FILE", help="a watchlist.json (hits) or a text file of symbols")
        p.add_argument("--universe", help="symbol list, one SYMBOL|Name a line (default %s)" % DEFAULT_UNIVERSE)
        p.add_argument("--limit", type=int, help="scan only the first N symbols")
        p.add_argument("--min-price", type=float, default=min_price, help="rupees (%s)" % _n(min_price))
        p.add_argument("--min-volume", type=int, default=0, help="shares (0)")
        p.add_argument("--min-value", type=float, default=min_value,
                       help="rupees traded, price x volume (%s)" % _n(min_value))
        p.add_argument("--offline", metavar="PATH", help="fixture folder or file instead of the network")
        p.add_argument("--now", help='pretend it is this time ("2026-11-16 10:30" IST, or ...Z)')
        p.add_argument("--out", default="trader/watchlist.json")
        p.add_argument("--no-write", action="store_true")
        p.add_argument("--pause", type=float, default=PAUSE, help="seconds between requests (%.1f)" % PAUSE)
        p.add_argument("--show", type=int, default=50, help="hits to print (all are written)")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("daily", help="pre-market setups from daily closes")
    common(p, MIN_PRICE, MIN_VALUE)
    p = sub.add_parser("orb", help="opening-range breakouts so far today")
    common(p, MIN_PRICE, 0.0)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--max-confirm", type=int, default=MAX_CONFIRM,
                   help="v8 charts to fetch in stage 2, most traded first (%d; 0 = all)" % MAX_CONFIRM)
    p.add_argument("--at", help="cut the clock at this IST time, HH:MM")
    p.add_argument("--until", help="no new entries after HH:MM (default: config no_entries_after, %s)"
                   % DEFAULT_UNTIL)
    p.add_argument("--config", default="trader/config.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"universe": cmd_universe, "daily": cmd_daily, "orb": cmd_orb}[a.cmd](a)
    except ScanError as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("scan.py: stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
