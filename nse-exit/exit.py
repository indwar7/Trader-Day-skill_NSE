#!/usr/bin/env python3
"""Exit Planner for nse-exit: sets the stop and the target BEFORE you enter an
NSE intraday trade, so you know when to get out. Standard library only. Long
side only. It never places an order: you place every order yourself, by hand,
in the Groww app.

    python3 exit.py plan                                    # from trader/entry.json: or-mid stop, 2R target
    python3 exit.py plan --stop or-mid --target 1R --write  # the reel: stop 1096.70, target 1114.70
    python3 exit.py plan --symbol SBIN --entry 1105.70 --or-high 1101.40 --or-low 1092.00
    python3 exit.py plan --stop atr:1.5 --offline SBIN.json # ATR needs the session's bars
    python3 exit.py plan --symbol "M&M" --entry 3392 --setup level --stop pct:1
    python3 exit.py show                                    # the current trader/plan.json

Prices sit on the NSE tick grid. The bands (NSE circular, effective
2025-04-15): under 250: 0.01; under 1,000: 0.05; under 5,000: 0.10; under
10,000: 0.50; under 20,000: 1.00; else 5.00. NSE reviews each stock's tick
monthly, from its close on the last trading day of the previous month
(NSE/CMTR/62174 as amended by CMTR/67133), so SBIN can trade on 0.05 ticks
above 1,000. The planner reads Yahoo's daily chart (range=3mo&interval=1d),
takes the last close in the month before the plan's date and picks the band
from THAT close. --tick X overrides it. With --offline, or when the daily
chart cannot be read, it falls back to the band of the entry price and says
so. plan.json carries "tick" and "tick_basis". Yahoo's float32 prices are
snapped to the paisa first (1214.5999755859375 is 1214.60). The entry rounds
half up to its tick.

Stops (a long's stop rounds DOWN to the tick, away from the entry):
  or-mid     the opening-range midpoint (the default for an orb entry)
  or-low     the opening-range low
  price:X    your price
  pct:X      X percent under the entry
  atr:K      K times the 14-bar ATR under the entry, on the session's bars up
             to the entry bar (--offline FILE or Yahoo). With fewer than 14
             bars by then it averages all of them and says so.
  A level setup has no opening-range default: give a stop.

Targets (round half up to the tick):
  2R (default), 1R, 1.5R ...   entry + R x risk, where risk = entry - stop
  price:X                      your price

Refused, with the reason: a stop at or above the entry, a target at or below
it, a risk under 0.05% of the price (too tight to survive the spread), an
entry.json that has not triggered (unless you give --entry), a short (this
pack plans longs only), a flat-by outside the 09:15-15:30 IST session.

flat_by (default 15:00 IST, or "flat_by" in trader/config.json): out by then,
win or lose. Groww stops new intraday orders in F&O stocks (most of the Nifty
500) at 15:05 and squares off intraday (MIS) positions still open at 15:20,
for Rs 50 + GST per position; a flat-by at or after 15:20 gets a note.
"min_reward_risk" in the config (default 1.0) only warns here; nse-risk
enforces it. "holidays" in the config adds closed days to the NSE 2026 list.

A stop is a plan, not a guarantee: a gap or a fast market can fill past it.
Exit codes: plan 0 = a plan, 1 = refused or an error. show 0 = a plan exists.
"""

import argparse
import datetime
import gzip
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP

UA = "Mozilla/5.0 (compatible; nse-day-trader/1.0)"
BACKOFF = 5.0
HOSTS = ("query2", "query1")
INTERVALS = {"1m": 60, "5m": 300}
DEFAULTS = {"flat_by": "15:00", "min_reward_risk": 1.0}
ATR_BARS = 14
MIN_RISK_PCT = Decimal("0.05")     # risk under 0.05% of the price is inside the spread's noise
PAISA = Decimal("0.01")
# NSE tick by price band (circular effective 2025-04-15): (band ceiling, tick); 5.00 above the last
TICK_BANDS = ((Decimal("250"), Decimal("0.01")), (Decimal("1000"), Decimal("0.05")),
              (Decimal("5000"), Decimal("0.10")), (Decimal("10000"), Decimal("0.50")),
              (Decimal("20000"), Decimal("1.00")))
TOP_TICK = Decimal("5.00")
TICKS = tuple(t for _, t in TICK_BANDS) + (TOP_TICK,)
MONTHLY = "NSE monthly review"
FALLBACK = "entry-price band (fallback)"
EPOCH = datetime.datetime(1970, 1, 1)
IST_OFFSET = datetime.timedelta(hours=5, minutes=30)
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9&-]{0,19}$")
INDICES = {"^NSEI": "NIFTY 50", "^NSEBANK": "NIFTY BANK"}
NOTE = "A stop is a plan, not a guarantee: a gap or a fast market can fill past it."
SQUARE_OFF_NOTE = ("Groww stops new intraday orders in F&O stocks at 15:05 and squares off intraday (MIS) positions "
                   "still open at 15:20, for ₹50 + GST per position: be out by your flat-by time.")

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
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
CALENDAR_YEARS = (2026,)
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
SQUARE_OFF = datetime.time(15, 20)   # Groww's auto square-off for MIS positions


class PlanError(Exception):
    pass


# ---------------------------------------------------------------- time (IST)

def to_ist(utc):
    """Naive UTC -> naive IST. India keeps UTC+05:30 all year."""
    return utc.replace(tzinfo=None) + IST_OFFSET


def ist_to_utc(local):
    return local.replace(tzinfo=None) - IST_OFFSET


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


def parse_now(text):
    """--now -> naive UTC. A time without an offset is IST."""
    if not text:
        return utc_now()
    t = text.strip()
    if t.upper().endswith(" IST"):
        t = t[:-4].strip()
    if re.match(r"^\d{1,2}:\d{2}$", t):
        t = "%s %s" % (to_ist(utc_now()).date().isoformat(), t.zfill(5))
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise PlanError('--now wants an IST time like "2026-11-17 09:46" (or ...Z for UTC)')
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise PlanError("%s wants an IST time like 15:00, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def parse_clock(text, what):
    """9:45 AM, 09:45, 2:05 PM -> time."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\s*$", str(text))
    if not m:
        raise PlanError("%s wants a time like 9:45 AM, not %r" % (what, text))
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3):
        if not 1 <= h <= 12:
            raise PlanError("%s wants a time like 9:45 AM, not %r" % (what, text))
        h = h % 12 + (12 if m.group(3).upper() == "PM" else 0)
    if h > 23 or mi > 59:
        raise PlanError("%s wants a time like 9:45 AM, not %r" % (what, text))
    return datetime.time(h, mi)


def hm(t):
    return "%02d:%02d" % (t.hour, t.minute)


def ampm(t):
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


# ---------------------------------------------------------------- calendar

def trading_day(d, extra=None):
    """(is_trading_day, reason): weekends, the NSE holiday list and the config's extra dates."""
    if d.weekday() >= 5:
        return False, "weekend"
    name = NSE_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "NSE holiday: " + name
    if extra and d.isoformat() in extra:
        return False, "a holiday in your config"
    return True, ""


def calendar_note(d):
    """The one-line warning for a date outside the loaded holiday list, or ""."""
    if d.year in CALENDAR_YEARS:
        return ""
    return "NSE holiday list for %d not loaded: check nseindia.com" % d.year


def session(d, extra=None):
    """(open_time, close_time) in IST, or None if the NSE is closed all day."""
    ok, _ = trading_day(d, extra)
    return (OPEN, CLOSE) if ok else None


# ---------------------------------------------------------------- money math (Decimal, on the tick grid)

def D(x, what="a number"):
    try:
        if isinstance(x, Decimal):
            d = x
        elif isinstance(x, float):
            d = Decimal(repr(x))
        else:
            s = str(x).strip().replace(",", "")
            for sign in ("₹", "INR", "Rs.", "Rs", "inr", "rs.", "rs"):
                if s.startswith(sign):
                    s = s[len(sign):].strip()
                    break
            d = Decimal(s)
    except (InvalidOperation, ValueError):
        raise PlanError("%s is not a number: %r" % (what, x))
    if not d.is_finite():
        raise PlanError("%s is not a number: %r" % (what, x))
    return d


def paise(x):
    """Round half up to the paisa (0.01)."""
    return D(x).quantize(PAISA, rounding=ROUND_HALF_UP)


def tick_for(price):
    """The NSE tick for a price band."""
    p = D(price)
    for ceiling, tick in TICK_BANDS:
        if p < ceiling:
            return tick
    return TOP_TICK


def to_tick(x, tick, rounding):
    """x on the tick grid: ROUND_FLOOR for a long's stop, ROUND_HALF_UP for the entry and target."""
    return ((D(x) / tick).quantize(Decimal("1"), rounding=rounding) * tick).quantize(PAISA)


def entry_on_grid(x, tick=None):
    """(entry, tick): the entry, snapped to the paisa, rounded half up to its tick. Without a
    tick (the fallback) the band of the entry price is used; a round that lands on a band
    edge (249.996 -> 250.00) takes the new band's tick."""
    x = paise(x)
    if tick is not None:
        return to_tick(x, tick, ROUND_HALF_UP), tick
    tick = tick_for(x)
    e = to_tick(x, tick, ROUND_HALF_UP)
    if tick_for(e) != tick:
        tick = tick_for(e)
        e = to_tick(e, tick, ROUND_HALF_UP)
    return e, tick


def parse_tick(text):
    """--tick X: one of NSE's ticks."""
    t = D(text, "--tick")
    for tick in TICKS:
        if t == tick:
            return tick
    raise PlanError("--tick is one of NSE's ticks: 0.01, 0.05, 0.10, 0.50, 1 or 5 (got %r)" % (text,))


def _indian(whole):
    """1234567 -> 12,34,567: the last three digits, then pairs."""
    if len(whole) <= 3:
        return whole
    head, tail = whole[:-3], whole[-3:]
    pairs = []
    while len(head) > 2:
        pairs.insert(0, head[-2:])
        head = head[:-2]
    return ",".join([head] + pairs + [tail])


def inr(x):
    """1,23,456.50 rupees as "₹1,23,456.50"; -1250 as "-₹1,250.00"."""
    d = paise(x)
    whole, frac = format(abs(d), "f").split(".")
    return "%s₹%s.%s" % ("-" if d < 0 else "", _indian(whole), frac)


def inr_exact(x):
    """Like inr() but keeps every decimal the math produced (₹1,094.643), at least two."""
    d = D(x)
    if d == d.to_integral_value() or d.as_tuple().exponent >= -2:
        return inr(d)
    whole, frac = format(abs(d.normalize()), "f").split(".")
    return "%s₹%s.%s" % ("-" if d < 0 else "", _indian(whole), frac)


def num(x):
    """1, 1.5, 2.25: a Decimal without trailing zeros."""
    return format(D(x).normalize(), "f")


def ratio(x):
    """reward:risk for people: 1.0, 1.5, 1.37."""
    s = "%.2f" % float(x)
    return s[:-1] if s.endswith("0") else s


WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}
MONEY = r"(?:₹|rs\.?|inr)?\s*([0-9][0-9,]*\.?[0-9]*)"


def parse_stop(text):
    t = str(text).strip().lower()
    if t in ("or-mid", "or_mid", "mid", "or-midpoint"):
        return "or_mid", None
    if t in ("or-low", "or_low", "low"):
        return "or_low", None
    m = re.match(r"^(price|pct|atr):\s*" + MONEY + r"$", t)
    if not m:
        raise PlanError("--stop is or-mid, or-low, price:X, pct:X or atr:K (got %r)" % text)
    kind, val = m.group(1), D(m.group(2), "--stop")
    if val <= 0:
        raise PlanError("--stop %s needs a number above 0" % kind)
    if kind == "pct" and val >= 100:
        raise PlanError("--stop pct:X needs X under 100")
    return kind, val


def parse_target(text):
    t = str(text).strip()
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*[Rr]$", t)
    if m:
        r = D(m.group(1), "--target")
        if r <= 0:
            raise PlanError("--target needs an R multiple above 0, like 1R or 2R")
        return "r", r
    m = re.match(r"^price:\s*" + MONEY + r"$", t.lower())
    if m:
        return "price", D(m.group(1), "--target")
    raise PlanError("--target is 2R, 1R, 1.5R ... or price:X (got %r)" % text)


def r_label(r):
    n = int(r) if r == r.to_integral_value() else None
    return "%sR (%s times the risk)" % (num(r), WORDS.get(n, num(r)))


def compute(entry, stop_rule, stop_param, target_rule, target_param, or_high=None, or_low=None,
            atr=None, atr_bars=None, tick=None):
    """The stop, the target and the risk, on the stock's NSE tick grid (`tick`, or the band of
    the entry price when it is None). Raises PlanError with the reason."""
    if D(entry, "the entry") <= 0:
        raise PlanError("the entry price must be above ₹0")
    e, tick = entry_on_grid(entry, tick)
    entry_detail = ""
    if paise(entry) != e:
        entry_detail = "the entry %s is off the %s tick: planned at %s" % (inr(entry), inr(tick), inr(e))
    detail = ""
    if stop_rule in ("or_mid", "or_low"):
        if or_high is None or or_low is None:
            raise PlanError("an %s stop needs the opening range (--or-high and --or-low)" % stop_rule.replace("_", "-"))
        oh, ol = paise(or_high), paise(or_low)
        if oh <= ol:
            raise PlanError("the opening-range high %s must be above its low %s" % (inr(oh), inr(ol)))
        if stop_rule == "or_mid":
            raw = (oh + ol) / 2
            label = "opening-range midpoint"
            detail = "(%s + %s) / 2 = %s" % (inr(oh), inr(ol), inr_exact(raw))
        else:
            raw, label = ol, "opening-range low"
    elif stop_rule == "price":
        raw, label = stop_param, "your price"
    elif stop_rule == "pct":
        raw = e * (1 - stop_param / 100)
        label = "%s%% under the entry" % num(stop_param)
        detail = "%s x (1 - %s%%) = %s" % (inr(e), num(stop_param), inr_exact(raw))
    elif stop_rule == "atr":
        if atr is None:
            raise PlanError("an atr stop needs the session's bars")
        a = D(round(atr, 6))
        raw = e - stop_param * a
        label = "%s x ATR(%d) under the entry" % (num(stop_param), atr_bars)
        detail = "ATR %s over the %d bars to the entry%s" % (
            inr_exact(a.quantize(Decimal("0.0001"))), atr_bars, " (fewer than 14 so far)" if atr_bars < ATR_BARS else "")
    else:
        raise PlanError("unknown stop rule %r" % stop_rule)
    stop = to_tick(raw, tick, ROUND_FLOOR)
    if stop != D(raw):
        if detail:
            detail += ", rounded down to the %s tick" % inr(tick)
        else:
            detail = "%s rounded down to the %s tick" % (inr_exact(raw), inr(tick))
    if stop >= e:
        raise PlanError("the stop %s (%s) is at or above the entry %s: a long's stop goes under the entry"
                        % (inr(stop), label, inr(e)))
    if stop <= 0:
        raise PlanError("the stop works out to %s: pick a stop above ₹0" % inr(stop))
    risk = e - stop
    floor_risk = e * MIN_RISK_PCT / 100
    if risk < floor_risk:
        raise PlanError("the stop is %s under the entry, less than 0.05%% of the price (%s on %s): too tight to "
                        "survive the spread, normal wiggle would stop you out. Widen it."
                        % (inr(risk), inr(floor_risk), inr(e)))
    if target_rule == "r":
        target = to_tick(e + target_param * risk, tick, ROUND_HALF_UP)
        t_label = r_label(target_param)
    else:
        target = to_tick(target_param, tick, ROUND_HALF_UP)
        t_label = None
    if target <= e:
        raise PlanError("the target %s is at or below the entry %s: a long's target goes above it"
                        % (inr(target), inr(e)))
    rr = ((target - e) / risk).quantize(PAISA, rounding=ROUND_HALF_UP)
    if t_label is None:
        t_label = "your price (%sR)" % ratio(rr)
    return {"entry": e, "tick": tick, "entry_detail": entry_detail, "stop": stop, "stop_label": label,
            "stop_detail": detail, "target": target, "target_r": target_param if target_rule == "r" else rr,
            "target_label": t_label, "risk": risk, "reward_risk": rr}


# ---------------------------------------------------------------- bars for ATR (same reader as nse-entry)

def nse_symbol(text):
    """User input -> the NSE symbol: upper case, a trailing .NS dropped, checked."""
    s = str(text or "").strip().upper()
    if s.endswith(".NS"):
        s = s[:-3]
    if s.startswith("^"):
        raise PlanError("%s is an index (%s): context only, not tradable here" % (s, INDICES.get(s, "an index")))
    if not SYMBOL_RE.match(s):
        raise PlanError("%r is not an NSE symbol (like RELIANCE, M&M or BAJAJ-AUTO)" % (text,))
    return s


_CTX = []


def _ssl_context():
    """python.org builds on macOS ship no CA bundle: fall back to the system's. Never unverified."""
    if _CTX:
        return _CTX[0]
    ctx = ssl.create_default_context()
    if not ctx.get_ca_certs() and not ctx.cert_store_stats().get("x509_ca"):
        for path in ("/etc/ssl/cert.pem", "/private/etc/ssl/cert.pem"):
            if os.path.exists(path):
                ctx.load_verify_locations(cafile=path)
                break
    _CTX.append(ctx)
    return ctx


def _open_url(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as r:
        return r.read().decode("utf-8", "replace")


def _sleep(seconds):
    time.sleep(seconds)


def yahoo_get(path):
    """query2 first, query1 on 429 or 5xx; one retry after a pause on 429."""
    last = "no answer"
    for host in HOSTS:
        url = "https://%s.finance.yahoo.com%s" % (host, path)
        for attempt in (0, 1):
            try:
                return _open_url(url)
            except urllib.error.HTTPError as e:
                code, body = e.code, None
                try:
                    if code == 404:     # Yahoo says why in the body: an unknown symbol
                        body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = None
                finally:
                    e.close()
                if code == 404:
                    if body:
                        return body
                    raise PlanError("Yahoo has no chart at %s (HTTP 404)" % url.split("?")[0])
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise PlanError("Yahoo answered HTTP %d for %s" % (code, url.split("?")[0]))
            except urllib.error.URLError as e:
                last = "could not connect: %s" % (e.reason,)
                break
            except OSError as e:
                last = "network error: %s" % (e,)
                break
    raise PlanError("Yahoo did not answer (%s on query2 and query1): wait a minute and try again" % last)


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def load_chart(sym, interval, day, offline=None):
    sym = nse_symbol(sym)
    ysym = sym + ".NS"
    if offline:
        path = offline
        if os.path.isdir(offline):
            names = [b + ext for b in (sym, ysym) for ext in (".json", ".json.gz")]
            found = [os.path.join(offline, n) for n in names if os.path.exists(os.path.join(offline, n))]
            if not found:
                raise PlanError("no offline file for %s in %s (want %s.json)" % (sym, offline, sym))
            path = found[0]
        if not os.path.exists(path):
            raise PlanError("no offline file %s" % path)
        try:
            text = read_text(path)
        except (OSError, ValueError) as e:
            raise PlanError("could not read %s: %s" % (path, e))
    else:
        back = (to_ist(utc_now()).date() - day).days
        rng = "1d" if back <= 0 else "5d" if back <= 6 else "1mo" if back <= 28 else "60d"
        if interval == "1m" and back > 6:
            raise PlanError("Yahoo keeps 1-minute bars for about a week")
        text = yahoo_get("/v8/finance/chart/%s?range=%s&interval=%s" % (urllib.parse.quote(ysym), rng, interval))
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise PlanError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise PlanError("Yahoo has no data for %s: %s" % (sym, err.get("description") or "unknown symbol"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    if str(meta.get("symbol") or ysym).upper() != ysym:
        raise PlanError("the chart file holds %s, not %s" % (meta.get("symbol"), ysym))
    gran = meta.get("dataGranularity")
    if gran and gran != interval:
        raise PlanError("the chart for %s has %s bars, not %s" % (sym, gran, interval))
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        h, lo = at("high", i), at("low", i)
        bars.append({"t": int(t), "h": snap(c if h is None else h), "l": snap(c if lo is None else lo),
                     "c": snap(c)})
    return {"bars": bars, "data_time": meta.get("regularMarketTime")}


def snap(x):
    """Yahoo sends float32 prices (1214.60 arrives as 1214.5999755859375): back to the paisa."""
    return round(float(x), 2) + 0.0


def daily_range(day, today):
    """The smallest Yahoo daily range that still holds the month before `day`."""
    first_prev = (day.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
    back = (today - first_prev).days
    return "3mo" if back <= 85 else "1y" if back <= 360 else "5y"


def month_end_close(text, sym, day):
    """(close, date): the last daily close whose IST date is in the month before `day`,
    from a Yahoo daily chart (v8 JSON). Raises PlanError when there is none."""
    ysym = nse_symbol(sym) + ".NS"
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise PlanError("Yahoo's daily chart for %s was not JSON" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise PlanError("Yahoo has no daily chart for %s: %s" % (sym, err.get("description") or "unknown symbol"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    if str(meta.get("symbol") or ysym).upper() != ysym:
        raise PlanError("the daily chart holds %s, not %s" % (meta.get("symbol"), ysym))
    if meta.get("dataGranularity") not in (None, "1d"):
        raise PlanError("the daily chart for %s has %s bars, not 1d" % (sym, meta.get("dataGranularity")))
    closes = (((r.get("indicators") or {}).get("quote") or [{}])[0] or {}).get("close") or []
    prev = day.replace(day=1) - datetime.timedelta(days=1)
    best = None
    for i, t in enumerate(r.get("timestamp") or []):
        c = closes[i] if i < len(closes) else None
        if t is None or c is None:
            continue
        d = to_ist(utc_of(t)).date()
        if (d.year, d.month) == (prev.year, prev.month) and (best is None or d >= best[1]):
            best = (paise(snap(c)), d)
    if best is None:
        raise PlanError("Yahoo's daily chart for %s has no close in %s" % (sym, prev.strftime("%B %Y")))
    return best


def resolve_tick(sym, day, flag=None, offline=None):
    """(tick or None, tick_basis, why): the stock's tick for `day` under NSE's monthly review.
    None means: fall back to the band of the entry price (why says why)."""
    if flag is not None:
        return parse_tick(flag), {"rule": "flag"}, ""
    if offline:
        return None, {"rule": FALLBACK}, "--offline reads no daily chart"
    ysym = nse_symbol(sym) + ".NS"
    try:
        text = yahoo_get("/v8/finance/chart/%s?range=%s&interval=1d" % (
            urllib.parse.quote(ysym), daily_range(day, to_ist(utc_now()).date())))
        close, d = month_end_close(text, sym, day)
    except PlanError as e:
        return None, {"rule": FALLBACK}, str(e)
    return tick_for(close), {"rule": MONTHLY, "close": float(close), "date": d.isoformat()}, ""


def tick_words(plan):
    """The tick and where it came from, for people."""
    tick = plan.get("tick")
    basis = plan.get("tick_basis") or {}
    if tick is None:
        return "%s (from the entry price band)" % inr(tick_for(D(plan["entry"])))
    if basis.get("rule") == MONTHLY and basis.get("close") is not None and basis.get("date"):
        d = datetime.date.fromisoformat(basis["date"])
        return "%s, from the %s close on %d %s (NSE reviews ticks monthly)" % (
            inr(tick), inr(basis["close"]), d.day, d.strftime("%b"))
    if basis.get("rule") == "flag":
        return "%s, from --tick" % inr(tick)
    return "%s, from the entry price band (fallback: check the price ladder in Groww)" % inr(tick)


def session_bars(chart, day, interval, upto_slot=None, now_utc=None, holidays=None):
    """Closed, on-grid bars of one IST session, oldest first (the live tick is dropped)."""
    step = INTERVALS[interval]
    s = session(day, holidays)
    if not s:
        return []
    open_dt = datetime.datetime.combine(day, s[0])
    close_dt = datetime.datetime.combine(day, s[1])
    if now_utc is None and chart.get("data_time"):
        now_utc = utc_of(chart["data_time"])
    local = to_ist(now_utc) if now_utc else None
    out = {}
    for b in chart["bars"]:
        if b["t"] % step:
            continue
        at = to_ist(utc_of(b["t"]))
        if at.date() != day or not (open_dt <= at < close_dt):
            continue
        if local and at + datetime.timedelta(seconds=step) > local:
            continue
        slot = int((at - open_dt).total_seconds()) // step
        if upto_slot is not None and slot > upto_slot:
            continue
        out[slot] = b
    return [out[k] for k in sorted(out)]


def atr_of(bars, n=ATR_BARS):
    """Average true range of the last n bars (all of them if fewer). The first
    bar of the session uses its own high - low (no overnight gap)."""
    trs, prev = [], None
    for b in bars:
        tr = b["h"] - b["l"]
        if prev is not None:
            tr = max(tr, abs(b["h"] - prev), abs(b["l"] - prev))
        trs.append(tr)
        prev = b["c"]
    use = trs[-n:]
    return sum(use) / len(use), len(use)


# ---------------------------------------------------------------- plan

def read_json(path, what):
    if not path or not os.path.exists(path):
        raise PlanError("no %s at %s" % (what, path))
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise PlanError("%s is not valid JSON: %s" % (path, e))


def config_holidays(value, where="the config"):
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise PlanError('"holidays" in %s wants a list of "YYYY-MM-DD" dates' % where)
    out = []
    for v in value:
        try:
            out.append(datetime.date.fromisoformat(str(v).strip()).isoformat())
        except ValueError:
            raise PlanError('"holidays" in %s wants "YYYY-MM-DD" dates, not %r' % (where, v))
    return out


def read_config(path):
    cfg = dict(DEFAULTS)
    cfg["holidays"] = []
    if path and os.path.exists(path):
        doc = read_json(path, "config")
        if not isinstance(doc, dict):
            raise PlanError("%s should hold one JSON object" % path)
        for k in DEFAULTS:
            if doc.get(k) is not None:
                cfg[k] = doc[k]
        cfg["holidays"] = config_holidays(doc.get("holidays"), path)
    return cfg


def resolve_flat_by(day, explicit, cfg, entry_time):
    """(HH:MM, note). Inside the 09:15-15:30 session; a note when it is at or after
    Groww's 15:20 auto square-off."""
    what = "--flat-by" if explicit else "flat_by in the config"
    t = parse_hhmm(explicit or cfg.get("flat_by") or DEFAULTS["flat_by"], "--flat-by" if explicit else "flat_by")
    if t >= CLOSE:
        raise PlanError("%s %s is at or after the %s NSE close" % (what, hm(t), ampm(CLOSE)))
    if t <= OPEN:
        raise PlanError("%s %s is at or before the %s NSE open" % (what, hm(t), ampm(OPEN)))
    note = ""
    if t >= SQUARE_OFF:
        note = ("at or after Groww's 15:20 auto square-off: Groww closes MIS positions still open at 15:20 for "
                "₹50 + GST per position, so be out before then")
    if entry_time and entry_time >= t:
        raise PlanError("the entry at %s is at or after your flat-by %s: there is no time left to hold it"
                        % (ampm(entry_time), hm(t)))
    return hm(t), note


def gather(a):
    """The entry facts, from trader/entry.json or the flags."""
    if a.symbol and a.from_path:
        raise PlanError("use --from or --symbol, not both")
    if a.side in ("sell", "short"):
        raise PlanError("this pack plans longs only: no stops above a short entry. Plan a buy instead.")
    if a.symbol:
        if a.entry is None:
            raise PlanError("--symbol needs --entry PRICE")
        if (a.or_high is None) != (a.or_low is None):
            raise PlanError("give both --or-high and --or-low, or neither")
        info = {"symbol": nse_symbol(a.symbol), "entry": a.entry,
                "setup": a.setup or ("orb" if a.or_high is not None else "level"),
                "date": a.date, "entry_time": a.time, "entry_bar": a.entry_bar, "interval": a.interval or "5m",
                "or_high": a.or_high, "or_low": a.or_low, "or_minutes": a.or_minutes, "source": "flags"}
    else:
        path = a.from_path or "trader/entry.json"
        doc = read_json(path, "entry (run /nse-entry first, or give --symbol and --entry)")
        if str(doc.get("side") or "buy").lower() not in ("buy", "long"):
            raise PlanError("%s is a %s: this pack plans longs only" % (path, doc.get("side")))
        if doc.get("status") != "triggered" and a.entry is None:
            raise PlanError("%s says %s is %s, not triggered: plan once it triggers, or give --entry PRICE to plan ahead"
                            % (path, doc.get("symbol"), doc.get("status") or "unknown"))
        orr = doc.get("opening_range") or {}
        if not doc.get("symbol"):
            raise PlanError("%s has no symbol or price" % path)
        info = {"symbol": nse_symbol(doc.get("symbol")),
                "entry": a.entry if a.entry is not None else doc.get("price"),
                "setup": a.setup or doc.get("setup") or "orb", "date": a.date or doc.get("date"),
                "entry_time": a.time or (doc.get("time") if a.entry is None else None),
                "entry_bar": a.entry_bar if a.entry_bar is not None else (doc.get("bar_index") if a.entry is None else None),
                "interval": a.interval or doc.get("interval") or "5m",
                "or_high": a.or_high if a.or_high is not None else orr.get("high"),
                "or_low": a.or_low if a.or_low is not None else orr.get("low"),
                "or_minutes": orr.get("minutes") or a.or_minutes, "source": path}
        if info["entry"] is None:
            raise PlanError("%s has no symbol or price" % path)
    if info["interval"] not in INTERVALS:
        raise PlanError("interval is 5m or 1m")
    return info


def build_plan(a, now_utc):
    info = gather(a)
    cfg = read_config(a.config)
    holidays = cfg.get("holidays") or []
    stop_text = a.stop
    if not stop_text:
        if info["setup"] != "orb":
            raise PlanError("a %s setup has no opening-range stop to default to: give --stop price:X, pct:X or atr:K"
                            % info["setup"])
        stop_text = "or-mid"
    stop_rule, stop_param = parse_stop(stop_text)
    target_rule, target_param = parse_target(a.target or "2R")
    if info["date"]:
        try:
            day = datetime.date.fromisoformat(str(info["date"]))
        except ValueError:
            raise PlanError("date wants YYYY-MM-DD, not %r" % info["date"])
    else:
        day = to_ist(now_utc).date()
    entry_time = parse_clock(info["entry_time"], "the entry time") if info["entry_time"] else None
    tick, tick_basis, tick_why = resolve_tick(info["symbol"], day, a.tick, a.offline)
    atr = atr_n = None
    if stop_rule == "atr":
        chart = load_chart(info["symbol"], info["interval"], day, a.offline)
        bars = session_bars(chart, day, info["interval"], info["entry_bar"], parse_now(a.now) if a.now else None,
                            holidays)
        if len(bars) < 3:
            raise PlanError("an atr stop needs at least 3 closed %s bars of %s on %s by the entry; found %d"
                            % (info["interval"], info["symbol"], day, len(bars)))
        atr, atr_n = atr_of(bars)
    c = compute(info["entry"], stop_rule, stop_param, target_rule, target_param, info["or_high"], info["or_low"],
                atr, atr_n, tick)
    flat_by, flat_note = resolve_flat_by(day, a.flat_by, cfg, entry_time)
    orr = None
    if info["or_high"] is not None and info["or_low"] is not None:
        orr = {"high": float(paise(info["or_high"])), "low": float(paise(info["or_low"])),
               "minutes": int(info["or_minutes"] or 15)}
    plan = {"created_by": "nse-exit", "symbol": info["symbol"], "side": "buy", "date": day.isoformat(),
            "setup": info["setup"], "entry": float(c["entry"]),
            "entry_time": ampm(entry_time) if entry_time else None,
            "entry_bar": info["entry_bar"], "interval": info["interval"], "opening_range": orr,
            "stop": float(c["stop"]), "stop_rule": stop_rule, "stop_label": c["stop_label"],
            "target": float(c["target"]), "target_r": float(c["target_r"]), "target_label": c["target_label"],
            "risk_per_share": float(c["risk"]), "reward_risk": float(c["reward_risk"]), "flat_by": flat_by,
            "tick": float(c["tick"]), "tick_basis": tick_basis, "stop_detail": c["stop_detail"], "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "note": NOTE + " " + SQUARE_OFF_NOTE}
    if atr is not None:
        plan["atr"] = round(atr, 4)
        plan["atr_bars"] = atr_n
    warnings = []
    if tick_basis["rule"] == FALLBACK:
        warnings.append("Tick %s from the entry price band, a fallback (%s): NSE sets each stock's tick monthly from "
                        "its previous month-end close, so check the price ladder in Groww, or give --tick."
                        % (inr(c["tick"]), tick_why))
    if c["entry_detail"]:
        warnings.append(c["entry_detail"][0].upper() + c["entry_detail"][1:] + ".")
    if flat_note:
        warnings.append("Flat-by %s: %s." % (flat_by, flat_note))
    mrr = cfg.get("min_reward_risk")
    try:
        if mrr is not None and c["reward_risk"] < D(mrr):
            warnings.append("Reward:risk %s is under your min_reward_risk %s in the config: nse-risk checks that."
                            % (ratio(c["reward_risk"]), num(D(mrr))))
    except PlanError:
        pass
    ok, why = trading_day(day, holidays)
    if not ok:
        warnings.append("%s is not an NSE trading day (%s)." % (day.isoformat(), why))
    if calendar_note(day):
        warnings.append(calendar_note(day) + ".")
    today = to_ist(now_utc).date()
    if day != today:
        warnings.append("This plan is for %s, not today (%s)." % (day.isoformat(), today.isoformat()))
    return plan, warnings


def describe(plan):
    day = datetime.date.fromisoformat(plan["date"])
    setup = {"orb": "opening-range breakout", "level": "close above a level"}.get(plan["setup"], plan["setup"])
    e = D(plan["entry"])
    risk = D(plan["risk_per_share"])
    pct = (risk / e * 100).quantize(PAISA, rounding=ROUND_HALF_UP)
    when = ""
    if plan.get("entry_time"):
        when = " at %s" % plan["entry_time"]
        if plan.get("entry_bar") is not None:
            when += " (bar %d)" % plan["entry_bar"]
    out = ["Exit plan: %s long, intraday (MIS), %s, %s on %s bars" % (
        plan["symbol"], day.strftime("%a %Y-%m-%d"), setup, "5-minute" if plan["interval"] == "5m" else "1-minute")]
    out.append("  Entry         %s%s" % (inr(plan["entry"]), when))
    out.append("  Stop          %s  %s%s" % (inr(plan["stop"]), plan["stop_label"],
                                             (": " + plan["stop_detail"]) if plan.get("stop_detail") else ""))
    out.append("  Target        %s  %s" % (inr(plan["target"]), plan["target_label"]))
    out.append("  Risk a share  %s (%s%% of the entry)" % (inr(plan["risk_per_share"]), pct))
    out.append("  Reward:risk   %s" % ratio(plan["reward_risk"]))
    out.append("  Tick          %s" % tick_words(plan))
    out.append("  Flat by       %s IST, win or lose: no overnight hold" % plan["flat_by"])
    out.append("Set before the entry. If price hits the stop, you are out; never move it further away.")
    out.append(NOTE)
    out.append(SQUARE_OFF_NOTE)
    return out


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def cmd_plan(a):
    now = parse_now(a.now)
    plan, warnings = build_plan(a, now)
    if a.write:
        write_json(a.write, plan)
    if a.json:
        print(json.dumps(dict(plan, warnings=warnings), indent=2, ensure_ascii=False))
        return 0
    for line in describe(plan):
        print(line)
    for w in warnings:
        print("Note: " + w)
    if a.write:
        print("Wrote %s." % a.write)
    print("Next: /nse-timemachine replays it, then /nse-budget sizes it to your max loss per trade, Groww's "
          "charges included. Nothing was placed.")
    return 0


def cmd_show(a):
    if not os.path.exists(a.plan):
        print("No plan at %s yet. Make one: exit.py plan --write" % a.plan)
        return 1
    plan = read_json(a.plan, "plan")
    for k in ("symbol", "date", "entry", "stop", "target", "risk_per_share", "reward_risk", "flat_by"):
        if plan.get(k) is None:
            raise PlanError("%s is missing %r: make a new plan with exit.py plan --write" % (a.plan, k))
    if a.json:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        return 0
    for line in describe(dict({"setup": "orb", "interval": "5m", "stop_label": "", "target_label": ""}, **plan)):
        print(line)
    today = to_ist(parse_now(a.now)).date().isoformat()
    if plan["date"] != today:
        print("Note: this plan is for %s, not today (%s)." % (plan["date"], today))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Exit Planner (nse-exit): the stop and target before the entry.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("plan", help="set the stop and target")
    p.add_argument("--from", dest="from_path", metavar="PATH", help="entry.json to plan from (default trader/entry.json)")
    p.add_argument("--symbol", help="NSE symbol, e.g. SBIN (quote M&M in the shell)")
    p.add_argument("--entry", type=float, help="entry price in rupees (or a plan-ahead price with --from)")
    p.add_argument("--or-high", type=float)
    p.add_argument("--or-low", type=float)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--setup", choices=["orb", "level"])
    p.add_argument("--date", help="session date YYYY-MM-DD (default: the entry's, or today in IST)")
    p.add_argument("--time", help="entry time, e.g. 9:45 AM")
    p.add_argument("--entry-bar", type=int)
    p.add_argument("--interval", choices=["5m", "1m"])
    p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="buy only")
    p.add_argument("--stop", help="or-mid (orb default), or-low, price:X, pct:X, atr:K")
    p.add_argument("--target", help="2R (default), 1R, 1.5R, price:X")
    p.add_argument("--flat-by", help="HH:MM IST (default: config flat_by, 15:00)")
    p.add_argument("--tick", help="the stock's NSE tick (0.01, 0.05, 0.10, 0.50, 1, 5); default: from the "
                                  "previous month-end close on Yahoo's daily chart")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON for an atr stop")
    p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-11-17 09:46"')
    p.add_argument("--write", nargs="?", const="trader/plan.json", metavar="PATH",
                   help="save the plan (default trader/plan.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="print the current plan")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"plan": cmd_plan, "show": cmd_show}[a.cmd](a)
    except PlanError as e:
        print("exit.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
