#!/usr/bin/env python3
"""Price alerts and the NSE clock for nse-alerts. Standard library only.

    python3 alerts.py clock                          # is NSE open? next open, Groww's 15:20 square-off
    python3 alerts.py quote RELIANCE --write         # quotes -> trader/prices.json
    python3 alerts.py quote --holdings --write       # every symbol in your portfolio and plan
    python3 alerts.py add --from-plan                # stop, target and flat-by from trader/plan.json
    python3 alerts.py add --symbol RELIANCE --stop 1337.7
    python3 alerts.py add --symbol RELIANCE --target 1345.7
    python3 alerts.py add --at 15:00 --label "flat by" --symbol RELIANCE
    python3 alerts.py add --symbol SBIN --pct 1.5
    python3 alerts.py add --opening-bell
    python3 alerts.py check                          # rings what hit, once; exit 1 if nothing rang
    python3 alerts.py check --now "2026-11-17 15:00" --offline tests/fixtures/nse-alerts
    python3 alerts.py list
    python3 alerts.py reset reliance-plan-stop

Rule types:
  above / target  the price is at or above "price"
  below / stop    the price is at or below "price"
  pct_move        the price moved "pct" percent or more from the previous close
                  (or from "ref" if you give one); "direction": "up" or "down"
  opening_bell    NSE opened today (09:15 IST; rings once per trading day)
  time            the IST clock passed "at" (HH:MM) on a trading day
                  (rings once per trading day)

A rule with "date" is only watched on that IST date (rules from a plan are
for the plan's day only). An above / below alert rings ONCE and then stays
quiet until you reset it, unless the rule has "rearm": true (it re-arms when
the price crosses back). A pct_move alert rings at most once per day.
Groww takes no new intraday orders on F&O stocks after 15:05 and squares off
intraday (MIS) positions still open at 15:20 for ₹50 + GST per position; the
plan's flat-by (default 15:00) comes before both.

Quotes (free, delayed, unofficial; for alerts only, never for timing an order):
  yahoo   https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=1d
          (M&M is sent as M%26M.NS; falls back to query1 on a 429 or a 5xx;
          one retry with a pause on a 429; one request per symbol, half a
          second apart). Stooq has no NSE coverage, so Yahoo is the only source.
  --offline DIR reads DIR/yahoo-<SYMBOL>.json (or .json.gz) instead.

NSE calendar: the 2026 capital-market holidays (NSE circulars CMTR/71775 and
CMTR/72260), plus any "holidays" in trader/config.json. NSE has no early
closes; special sessions (Diwali Muhurat trading, a Budget-day Sunday) are not
supported and count as closed. For a year without a holiday list, weekdays
count as trading days and the output says the list is not loaded.
Times are IST (UTC+05:30, no daylight saving); --now pins the clock.
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
from decimal import Decimal, ROUND_HALF_UP

UA = "Mozilla/5.0 (compatible; nse-day-trader/1.0)"
PAUSE = 0.5    # seconds between requests: be polite to free endpoints
BACKOFF = 2.0  # seconds to wait before the one retry after a 429
YAHOO_HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9&-]{0,19}$")
IST = datetime.timedelta(hours=5, minutes=30)

# NSE capital-market holidays (weekdays), NSE circulars CMTR/71775 and CMTR/72260
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
NO_NEW_ORDERS = datetime.time(15, 5)  # Groww's risk policy: no new intraday orders on F&O stocks after this
SQUARE_OFF = datetime.time(15, 20)   # Groww squares off intraday (MIS) positions still open
SQUARE_OFF_FEE = "₹50 + GST per position"
DEFAULT_FLAT_BY = "15:00"


class AlertError(Exception):
    pass


# ---------------------------------------------------------------- time (IST, fixed UTC+05:30)

def to_ist(utc):
    return utc.replace(tzinfo=None) + IST


def ist_to_utc(local):
    return local.replace(tzinfo=None) - IST


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as IST."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise AlertError("--now wants a time like \"2026-11-17 10:15\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def load_holidays(path):
    """Extra closed days from trader/config.json: {"holidays": ["YYYY-MM-DD", ...]} -> {date: reason}."""
    cfg = _read(path, {}) if path else {}
    days = []
    if isinstance(cfg, dict):
        days = cfg.get("holidays") or []
    if not isinstance(days, list):
        raise AlertError("%s: holidays must be a list of dates like \"2026-12-24\"" % path)
    out = {}
    for d in days:
        try:
            out[datetime.date.fromisoformat(str(d)).isoformat()] = "holiday in %s" % os.path.basename(path)
        except ValueError:
            raise AlertError("%s: holiday %r is not a date like 2026-12-24" % (path, d))
    return out


def trading_day(d, extra=None):
    """(is_trading_day, reason). Weekends, the NSE holiday list and any extra closed days."""
    if d.weekday() >= 5:
        return False, "weekend"
    name = NSE_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "NSE holiday: " + name
    name = (extra or {}).get(d.isoformat())
    if name:
        return False, name
    return True, ""


def session(d, extra=None):
    """(open_time, close_time) in IST, or None if closed all day. NSE has no early closes."""
    ok, _ = trading_day(d, extra)
    return (OPEN, CLOSE) if ok else None


def calendar_warning(year):
    return "NSE holiday list for %d not loaded: check nseindia.com" % year


def market_status(now_utc, extra=None):
    local = to_ist(now_utc)
    d = local.date()
    ok, reason = trading_day(d, extra)
    st = {"now_ist": local.strftime("%Y-%m-%d %H:%M"), "date": d.isoformat(),
          "weekday": local.strftime("%A"), "calendar_known": d.year in CALENDAR_YEARS,
          "open": False, "reason": reason, "warnings": []}
    s = session(d, extra)
    if s:
        o, c = s
        st["opens"], st["closes"] = o.strftime("%H:%M"), c.strftime("%H:%M")
        st["no_new_orders"] = NO_NEW_ORDERS.strftime("%H:%M")
        st["square_off"] = SQUARE_OFF.strftime("%H:%M")
        if o <= local.time() < c:
            st["open"] = True
            st["reason"] = "regular session"
        elif local.time() < o:
            st["reason"] = "before the open"
        else:
            st["reason"] = "after the close"
    # next open
    nd = d if (s and local.time() < s[0]) else d + datetime.timedelta(days=1)
    for _ in range(15):
        if session(nd, extra):
            st["next_open"] = "%s %s" % (nd.isoformat(), OPEN.strftime("%H:%M"))
            break
        nd += datetime.timedelta(days=1)
    for y in sorted({d.year, nd.year}):
        if y not in CALENDAR_YEARS:
            st["warnings"].append(calendar_warning(y))
    return st


# ---------------------------------------------------------------- symbols and money

def norm_symbol(text):
    """'reliance', 'RELIANCE.NS', 'm&m' -> 'RELIANCE', 'RELIANCE', 'M&M'. None if not an NSE symbol."""
    s = str(text or "").strip().upper()
    if s.endswith(".NS"):
        s = s[:-3]
    return s if SYMBOL_RE.match(s) else None


def need_symbol(text):
    s = norm_symbol(text)
    if not s:
        raise AlertError("%r is not an NSE symbol (like RELIANCE, M&M or BAJAJ-AUTO)" % text)
    return s


def inr(x):
    """123456.5 -> '₹1,23,456.50', -1250 -> '-₹1,250.00' (Indian digit grouping)."""
    d = Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    neg = d < 0
    whole, frac = ("%.2f" % abs(d)).split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return "%s₹%s.%s" % ("-" if neg else "", whole, frac)


def ist_clock(stamp):
    """'2026-11-17T05:19:04Z' -> '10:49 IST' (empty if not a stamp)."""
    try:
        utc = datetime.datetime.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return ""
    return to_ist(utc).strftime("%H:%M IST")


# ---------------------------------------------------------------- quotes

def yahoo_url(sym, host=YAHOO_HOSTS[0]):
    return "https://%s/v8/finance/chart/%s?range=1d&interval=1d" % (host, urllib.parse.quote(sym + ".NS"))


def parse_yahoo(text, sym):
    ysym = sym + ".NS"
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise AlertError("Yahoo returned something unexpected for %s" % ysym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise AlertError("Yahoo has no data for %s: %s (check the NSE symbol)"
                         % (ysym, err.get("description", "unknown symbol")))
    m = res[0].get("meta", {})
    got = str(m.get("symbol") or "").upper()
    if got and got != ysym:
        raise AlertError("Yahoo answered for %s, not %s" % (got, ysym))
    if m.get("currency") and m["currency"] != "INR":
        raise AlertError("Yahoo quotes %s in %s, not INR: not an NSE listing" % (ysym, m["currency"]))
    price = m.get("regularMarketPrice")
    if price is None:
        raise AlertError("Yahoo has no price for %s" % ysym)
    t = m.get("regularMarketTime")
    when = (datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if t else "")
    q = (res[0].get("indicators", {}).get("quote") or [{}])[0]
    opens = [x for x in (q.get("open") or []) if x is not None]
    return {"symbol": sym, "price": float(price), "open": opens[0] if opens else None,
            "high": m.get("regularMarketDayHigh"), "low": m.get("regularMarketDayLow"),
            "prev_close": m.get("previousClose") or m.get("chartPreviousClose"), "time": when,
            "currency": m.get("currency"), "source": "yahoo"}


_SSL = None


def _ssl_context():
    """python.org builds on macOS ship without a CA bundle: fall back to the system's. Never unverified."""
    global _SSL
    if _SSL is None:
        ctx = ssl.create_default_context()
        if not ctx.get_ca_certs() and not ctx.cert_store_stats().get("x509_ca"):
            for path in ("/etc/ssl/cert.pem", "/private/etc/ssl/cert.pem"):
                if os.path.exists(path):
                    ctx.load_verify_locations(cafile=path)
                    break
        _SSL = ctx
    return _SSL


_urlopen = urllib.request.urlopen   # tests swap these two
_sleep = time.sleep


def _get(url, timeout=20):
    """(HTTP status, body). A network failure is an AlertError."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with _urlopen(req, timeout=timeout, context=_ssl_context()) as r:
            return 200, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        finally:
            try:
                e.close()
            except Exception:
                pass
        return e.code, body
    except urllib.error.URLError as e:
        raise AlertError("could not reach %s: %s" % (url.split("/")[2], e.reason))


def fetch_yahoo(sym, timeout=20):
    """query2 first. A 429 waits BACKOFF seconds and tries once more; a 429 or a 5xx
    after that moves on to query1. A 404 comes back as Yahoo's own error JSON."""
    last = None
    for host in YAHOO_HOSTS:
        url = yahoo_url(sym, host)
        code, text = _get(url, timeout)
        if code == 429:
            _sleep(BACKOFF)
            code, text = _get(url, timeout)
        if code == 200:
            return text
        if code == 404 and text.strip().startswith("{"):
            return text
        last = (url, code)
        if code != 429 and code < 500:
            break
    raise AlertError("%s answered HTTP %d" % (last[0].split("?")[0], last[1]))


def _offline_text(folder, sym):
    base = os.path.join(folder, "yahoo-%s.json" % sym)
    if os.path.exists(base):
        with open(base, encoding="utf-8") as fh:
            return fh.read()
    if os.path.exists(base + ".gz"):
        with gzip.open(base + ".gz", "rt", encoding="utf-8") as fh:
            return fh.read()
    raise AlertError("no offline quote file %s" % base)


def get_quotes(symbols, offline=None, pause=PAUSE):
    """{SYM: quote} and [errors]. One request per symbol, with a pause between."""
    quotes, errors = {}, []
    n = 0
    for raw in symbols:
        sym = norm_symbol(raw)
        if not sym:
            errors.append("%r is not an NSE symbol" % raw)
            continue
        try:
            if offline:
                text = _offline_text(offline, sym)
            else:
                if n and pause:
                    _sleep(pause)
                n += 1
                text = fetch_yahoo(sym)
            quotes[sym] = parse_yahoo(text, sym)
        except AlertError as e:
            errors.append(str(e))
    return quotes, errors


def write_prices(path, quotes, now_utc, source="yahoo"):
    old = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                old = json.load(fh)
        except (ValueError, OSError):
            old = {}
    prices = dict(old.get("prices", {}))
    detail = dict(old.get("detail", {}))
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    for s, q in quotes.items():
        prices[s] = q["price"]
        detail[s] = {"prev_close": q.get("prev_close"), "time": q.get("time"), "source": q.get("source"),
                     "fetched": stamp}
    doc = {"as_of": stamp, "source": source,
           "note": ("Delayed quotes in INR for alerts and sizing checks. The price in Groww is the one that fills. "
                    "detail.fetched is when each price was read (UTC)."),
           "prices": prices, "detail": detail}
    _write(path, doc)
    return doc


def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def _read(path, default):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise AlertError("%s is not valid JSON: %s" % (path, e))


# ---------------------------------------------------------------- rules

PRICE_TYPES = {"above", "below", "stop", "target", "pct_move", "opening_bell", "time"}
NO_QUOTE = {"opening_bell", "time"}


def _hhmm(text):
    m = HHMM.match(str(text or "").strip())
    if not m:
        return None
    return datetime.time(int(m.group(1)), int(m.group(2)))


def validate(rule):
    t = rule.get("type")
    if t not in PRICE_TYPES:
        raise AlertError("rule %s: unknown type %r" % (rule.get("id"), t))
    if t not in NO_QUOTE and not rule.get("symbol"):
        raise AlertError("rule %s needs a symbol" % rule.get("id"))
    if rule.get("symbol") and not norm_symbol(rule["symbol"]):
        raise AlertError("rule %s: %r is not an NSE symbol" % (rule.get("id"), rule["symbol"]))
    if t in ("above", "below", "stop", "target") and not isinstance(rule.get("price"), (int, float)):
        raise AlertError("rule %s needs a numeric price" % rule.get("id"))
    if t == "pct_move" and not isinstance(rule.get("pct"), (int, float)):
        raise AlertError("rule %s needs a numeric pct" % rule.get("id"))
    if t == "time" and _hhmm(rule.get("at")) is None:
        raise AlertError("rule %s needs \"at\" as an IST time like \"15:00\"" % rule.get("id"))
    if rule.get("date") and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(rule["date"])):
        raise AlertError("rule %s: date must be YYYY-MM-DD" % rule.get("id"))


def active(rule, today):
    """A rule with a "date" is watched on that IST date only."""
    return not rule.get("date") or str(rule["date"]) == today


def _rang_today(stamp, today):
    """Did a rule that rang at this UTC stamp ring on this IST date?"""
    if not stamp:
        return False
    try:
        utc = datetime.datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return stamp[:10] == today
    return to_ist(utc).date().isoformat() == today


def square_off_line(sym, local):
    """What a flat-by alert tells you to do about a symbol, before or after Groww's 15:20 square-off."""
    if local.time() < SQUARE_OFF:
        return (" If %s is still open, close it now: Groww takes no new intraday orders on F&O stocks after 15:05 "
                "and squares off intraday (MIS) positions still open at 15:20 (%s). nse-groww's ticket.py flatten "
                "--symbol %s builds the sell ticket, then nse-risk, then you place it in the Groww app."
                % (sym, SQUARE_OFF_FEE, sym))
    return (" It is past 15:20: Groww squares off intraday (MIS) positions still open then (%s). Check your "
            "Groww positions and orders for %s." % (SQUARE_OFF_FEE, sym))


def evaluate(rules, quotes, status, state, now_utc):
    """Pure function. Returns (rings, new_state). Each ring: {id, symbol, message}."""
    state = json.loads(json.dumps(state or {}))
    rung = state.setdefault("rung", {})
    today = status["date"]
    local = to_ist(now_utc)
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    rings = []
    for r in rules:
        validate(r)
        rid = r.get("id") or "%s-%s" % (r.get("symbol", "market"), r["type"])
        if not active(r, today):
            continue
        typ = {"stop": "below", "target": "above"}.get(r["type"], r["type"])
        if typ == "opening_bell":
            if status["open"] and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": "", "message": "Opening bell: NSE is open (%s IST), regular session "
                              "%s to %s IST." % (status["now_ist"], status["opens"], status["closes"])})
                rung[rid] = stamp
            continue
        if typ == "time":
            at = _hhmm(r.get("at"))
            if status.get("opens") and local.time() >= at and not _rang_today(rung.get(rid), today):
                label = r.get("label") or "time alert"
                msg = "%s%s: it is %s IST (set for %s)" % (label[:1].upper(), label[1:],
                                                          local.strftime("%H:%M"), at.strftime("%H:%M"))
                msg += (", the market closes at %s." % status["closes"]) if status["open"] else \
                    ", the market has closed."
                sym = norm_symbol(r.get("symbol")) or ""
                if sym:
                    msg += square_off_line(sym, local)
                rings.append({"id": rid, "symbol": sym, "message": msg})
                rung[rid] = stamp
            continue
        sym = norm_symbol(r["symbol"])
        q = quotes.get(sym)
        if not q:
            continue
        p = q["price"]
        if typ in ("above", "below"):
            hit = p >= r["price"] if typ == "above" else p <= r["price"]
            if hit and rid not in rung:
                word = "at or above" if typ == "above" else "at or below"
                label = {"stop": "stop", "target": "target"}.get(r["type"], "alert level")
                rings.append({"id": rid, "symbol": sym, "message": "%s %s %s: %s your %s %s%s."
                              % (sym, "is" if status["open"] else "last", inr(p), word, label, inr(r["price"]),
                                 (" (" + r["note"] + ")") if r.get("note") else "")})
                rung[rid] = stamp
            elif not hit and rid in rung and r.get("rearm"):
                del rung[rid]
        elif typ == "pct_move":
            ref = r.get("ref") or q.get("prev_close") or q.get("open")
            if not ref:
                continue
            move = 100.0 * (p / float(ref) - 1)
            want = r.get("direction")
            hit = abs(move) >= r["pct"] and (not want or (want == "up") == (move > 0))
            if hit and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": sym, "message": "%s moved %+.2f%% to %s (from %s)."
                              % (sym, move, inr(p), inr(float(ref)))})
                rung[rid] = stamp
    state["last_check"] = stamp
    return rings, state


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def rules_from_plan(plan, cfg_flat_by=None):
    """trader/plan.json -> a stop alert, a target alert and a flat-by time alert, for the plan's day."""
    if not isinstance(plan, dict) or not plan.get("symbol"):
        raise AlertError("the plan has no symbol (run nse-exit first)")
    sym = need_symbol(plan["symbol"])
    out = []
    for key, typ, word in (("stop", "stop", "plan stop"), ("target", "target", "plan target")):
        v = plan.get(key)
        if not isinstance(v, (int, float)) or v <= 0:
            raise AlertError("the plan has no %s (run nse-exit)" % key)
        label = plan.get("%s_label" % key)
        out.append({"id": "%s-plan-%s" % (sym.lower(), key), "symbol": sym, "type": typ, "price": float(v),
                    "note": word + (": " + label if label else "")})
    flat = plan.get("flat_by") or cfg_flat_by or DEFAULT_FLAT_BY
    if _hhmm(flat) is None:
        raise AlertError("the plan's flat_by %r is not an IST time like 15:00" % flat)
    out.append({"id": "%s-flat-by" % sym.lower(), "symbol": sym, "type": "time", "at": flat, "label": "flat by"})
    if plan.get("date"):
        for r in out:
            r["date"] = str(plan["date"])
    return out


# ---------------------------------------------------------------- commands

def holdings(a):
    syms = []
    pf = _read(a.portfolio, {})
    for p in pf.get("positions", []) if isinstance(pf, dict) else []:
        syms.append(norm_symbol(p.get("ticker") or p.get("symbol")))
    plan = _read(a.plan, {})
    if isinstance(plan, dict) and plan.get("symbol"):
        syms.append(norm_symbol(plan["symbol"]))
    return [s for s in syms if s]


def _dedupe(items):
    out = []
    for s in items:
        if s and s not in out:
            out.append(s)
    return out


def groww_line(st):
    if not st.get("opens"):
        return None
    return ("Groww takes no new intraday orders on F&O stocks after %s and squares off intraday (MIS) positions "
            "still open at %s (%s); the pack's default flat-by is %s."
            % (st["no_new_orders"], st["square_off"], SQUARE_OFF_FEE, DEFAULT_FLAT_BY))


def cmd_clock(a):
    st = market_status(parse_now(a.now), load_holidays(a.config))
    if a.json:
        print(json.dumps(st, indent=2, ensure_ascii=False))
    else:
        print("IST %s (%s): NSE %s, %s." % (st["now_ist"], st["weekday"], "OPEN" if st["open"] else "closed",
                                            st["reason"]))
        if st.get("opens"):
            print("Today's session: %s to %s IST." % (st["opens"], st["closes"]))
            print(groww_line(st))
        if not st["open"] and st.get("next_open"):
            print("Next open: %s IST." % st["next_open"])
        for w in st["warnings"]:
            print(w + ".")
    return 0 if st["open"] else 1


def cmd_quote(a):
    syms = _dedupe([need_symbol(s) for s in a.symbols] + (holdings(a) if a.holdings else []))
    if not syms:
        raise AlertError("which symbols? alerts.py quote RELIANCE SBIN, or --holdings")
    now = parse_now(a.now)
    quotes, errors = get_quotes(syms, a.offline)
    if a.write and quotes:
        write_prices(a.write, quotes, now)
    if a.json:
        print(json.dumps({"quotes": quotes, "errors": errors}, indent=2, ensure_ascii=False))
    else:
        for s in syms:
            q = quotes.get(s)
            if q:
                chg = ""
                if q.get("prev_close"):
                    chg = " (%+.2f%% vs prev close %s)" % (100.0 * (q["price"] / q["prev_close"] - 1),
                                                           inr(q["prev_close"]))
                print("%-10s %s%s  %s  [%s]" % (s, inr(q["price"]), chg, ist_clock(q.get("time")), q["source"]))
        for e in errors:
            print("error: " + e)
        if a.write and quotes:
            print("Wrote %d price(s) to %s." % (len(quotes), a.write))
        print("Quotes are free, delayed and unofficial: for alerts only. The price in Groww is the one that fills.")
    return 0 if quotes else 1


def cmd_check(a):
    doc = _read(a.rules, None)
    if not doc or not doc.get("rules"):
        raise AlertError("no rules in %s: add some with alerts.py add --from-plan" % a.rules)
    rules = doc["rules"]
    now = parse_now(a.now)
    status = market_status(now, load_holidays(a.config))
    watched = [r for r in rules if active(r, status["date"])]
    syms = _dedupe([norm_symbol(r.get("symbol")) for r in watched if r.get("type") not in NO_QUOTE])
    quotes, errors = (get_quotes(syms, a.offline) if syms else ({}, []))
    state = _read(a.state, {})
    rings, state = evaluate(rules, quotes, status, state, now)
    if not a.no_write:
        _write(a.state, state)
        if quotes:
            write_prices(a.prices_out, quotes, now)
    if a.json:
        print(json.dumps({"market": status, "rings": rings, "errors": errors,
                          "quotes": {s: q["price"] for s, q in quotes.items()}}, indent=2, ensure_ascii=False))
    else:
        print("NSE %s (%s IST, %s)." % ("OPEN" if status["open"] else "closed", status["now_ist"], status["reason"]))
        for r in rings:
            print("ALERT " + r["message"])
        if not rings:
            print("Nothing hit. %d rule(s) watched today%s." % (
                len(watched), (", " + ", ".join("%s %s" % (s, inr(quotes[s]["price"])) for s in syms if s in quotes))
                if quotes else ""))
        for e in errors:
            print("error: " + e)
        for w in status["warnings"]:
            print(w + ".")
        if rings:
            print("An alert is information, not an order: nothing in this pack talks to Groww. If you placed a "
                  "stop-loss or target order in the Groww app, check the app for fills and record any with "
                  "nse-groww's ticket.py fill. To close a position yourself: ticket.py flatten builds the sell "
                  "ticket, then nse-risk, then you place it in Groww.")
    return 0 if rings else 1


def cmd_add(a):
    doc = _read(a.rules, {"rules": []})
    doc.setdefault("rules", [])
    if a.from_plan:
        plan = _read(a.plan, None)
        if plan is None:
            raise AlertError("no plan at %s (run nse-exit first)" % a.plan)
        cfg = _read(a.config, {}) if a.config else {}
        new = rules_from_plan(plan, (cfg or {}).get("flat_by") if isinstance(cfg, dict) else None)
        ids = {r["id"] for r in new}
        doc["rules"] = [r for r in doc["rules"] if r.get("id") not in ids] + new
        state = _read(a.state, {})
        if state.get("rung"):
            for i in ids:
                state["rung"].pop(i, None)
            _write(a.state, state)
        _write(a.rules, doc)
        for r in new:
            print("Added %s: %s" % (r["id"], json.dumps(r, ensure_ascii=False)))
        flat = _hhmm(new[-1]["at"])
        if flat >= SQUARE_OFF:
            print("Note: flat-by %s is at or after 15:20, when Groww squares off intraday (MIS) positions still "
                  "open (%s)." % (new[-1]["at"], SQUARE_OFF_FEE))
        print("Run alerts.py check every few minutes while the trade is open (a scheduled task or /loop). "
              "Free quotes are delayed, and an alert sells nothing: only an order in the Groww app does.")
        return 0
    if a.opening_bell:
        rule = {"id": a.id or "opening-bell", "type": "opening_bell"}
    elif a.at:
        if _hhmm(a.at) is None:
            raise AlertError("--at wants an IST time like 15:00")
        rule = {"id": a.id or ("time-%s" % a.at.replace(":", "")), "type": "time", "at": a.at,
                "label": a.label or "time alert"}
        if a.symbol:
            rule["symbol"] = need_symbol(a.symbol)
    else:
        if not a.symbol:
            raise AlertError("--symbol is required (or --from-plan, --at, --opening-bell)")
        sym = need_symbol(a.symbol)
        if a.above is not None:
            rule = {"type": "above", "price": a.above}
        elif a.below is not None:
            rule = {"type": "below", "price": a.below}
        elif a.stop is not None:
            rule = {"type": "stop", "price": a.stop}
        elif a.target is not None:
            rule = {"type": "target", "price": a.target}
        elif a.pct is not None:
            rule = {"type": "pct_move", "pct": a.pct}
            if a.direction:
                rule["direction"] = a.direction
        else:
            raise AlertError("give one of --above, --below, --stop, --target or --pct")
        val = rule.get("price", rule.get("pct"))
        rule = dict({"id": a.id or ("%s-%s-%s" % (sym.lower(), rule["type"], _n(val)))}, symbol=sym, **rule)
        if a.rearm:
            rule["rearm"] = True
        if a.note:
            rule["note"] = a.note
    if a.date:
        rule["date"] = a.date
    validate(rule)
    if any(r.get("id") == rule["id"] for r in doc["rules"]):
        raise AlertError("a rule with id %s already exists" % rule["id"])
    doc["rules"].append(rule)
    _write(a.rules, doc)
    print("Added %s: %s" % (rule["id"], json.dumps(rule, ensure_ascii=False)))
    return 0


def cmd_list(a):
    doc = _read(a.rules, {"rules": []})
    state = _read(a.state, {})
    if not doc.get("rules"):
        print("No rules yet.")
        return 1
    for r in doc["rules"]:
        rid = r.get("id")
        rung = state.get("rung", {}).get(rid)
        print("%-24s %s%s" % (rid, json.dumps({k: v for k, v in r.items() if k != "id"}, ensure_ascii=False),
                              ("  (rang %s, %s)" % (rung, ist_clock(rung))) if rung else ""))
    return 0


def cmd_reset(a):
    state = _read(a.state, {})
    if a.id not in state.get("rung", {}):
        print("%s has not rung (nothing to reset)." % a.id)
        return 1
    del state["rung"][a.id]
    _write(a.state, state)
    print("Re-armed %s." % a.id)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Price alerts and the NSE clock (nse-alerts).")
    sub = ap.add_subparsers(dest="cmd")

    def net(p):
        p.add_argument("--offline", metavar="DIR", help="read yahoo-<SYMBOL>.json files, no network")
        p.add_argument("--now", help="pretend it is this time (no offset = IST)")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("clock", help="is NSE open?")
    p.add_argument("--now", help="pretend it is this time (no offset = IST)")
    p.add_argument("--config", default="trader/config.json", help="extra closed days: \"holidays\"")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("quote", help="latest delayed quotes")
    net(p)
    p.add_argument("symbols", nargs="*")
    p.add_argument("--holdings", action="store_true", help="add symbols from your portfolio and plan")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--write", nargs="?", const="trader/prices.json", metavar="PATH",
                   help="save to prices.json (default trader/prices.json)")
    p = sub.add_parser("check", help="check every rule, ring what hit")
    net(p)
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--prices-out", default="trader/prices.json")
    p.add_argument("--config", default="trader/config.json", help="extra closed days: \"holidays\"")
    p.add_argument("--no-write", action="store_true")
    p = sub.add_parser("add", help="add a rule")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--symbol", help="NSE symbol, e.g. RELIANCE, M&M, BAJAJ-AUTO")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--above", type=float)
    g.add_argument("--below", type=float)
    g.add_argument("--stop", type=float)
    g.add_argument("--target", type=float)
    g.add_argument("--pct", type=float)
    g.add_argument("--opening-bell", action="store_true")
    g.add_argument("--at", metavar="HH:MM", help="a time alert, IST")
    g.add_argument("--from-plan", action="store_true", help="stop, target and flat-by from the plan")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json", help="flat_by when the plan has none")
    p.add_argument("--label", help="what a time alert says, e.g. \"flat by\"")
    p.add_argument("--date", help="watch this rule on this IST date only (YYYY-MM-DD)")
    p.add_argument("--direction", choices=["up", "down"])
    p.add_argument("--rearm", action="store_true")
    p.add_argument("--note")
    p.add_argument("--id")
    p = sub.add_parser("list", help="list the rules")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p = sub.add_parser("reset", help="re-arm a rule that rang")
    p.add_argument("id")
    p.add_argument("--state", default="trader/state/alerts.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"clock": cmd_clock, "quote": cmd_quote, "check": cmd_check, "add": cmd_add,
                "list": cmd_list, "reset": cmd_reset}[a.cmd](a)
    except AlertError as e:
        print("alerts.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
