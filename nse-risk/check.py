#!/usr/bin/env python3
"""Pre-trade risk check for nse-risk. Standard library only, no network.

Reads the proposed orders (trader/orders.json) and checks every one against
your rules (trader/config.json), your account (trader/portfolio.json),
prices (trader/prices.json) and what you placed in Groww today
(trader/state/broker.json, kept by nse-groww). Each order gets PASS or BLOCK
with the reasons. Orders are checked in file order as if each PASS had
filled, so buys that are fine one by one but break a limit together are caught.

    python3 check.py                                 # the trader/ files, writes trader/state/risk.json
    python3 check.py --now "2026-11-17 09:45"        # pin the clock (IST)
    python3 check.py --orders trader/orders.json --json
    python3 check.py --no-write                      # check without recording the verdicts

Entries and exits. An ENTRY opens or adds to a position (a buy, or a sell
that would go short). An EXIT only reduces what you hold. Getting out must
always be possible: an exit is checked for format, tick grid, asset and short
sales only; every other rule turns into a warning for it.

What blocks an order (every order):

  format        missing symbol or side, qty that is not a whole number of
                shares, an amount instead of a qty, exchange not NSE, product
                not MIS (intraday), unknown type or validity, a limit or SL
                order without its prices, a stoploss/target on a sell
  tick          a price off the stock's NSE tick. The order's "tick" is used
                when it has one (nse-exit takes it from the month-end close,
                the way NSE sets it each month); without one, the band of the
                entry price (0.01 under 250, 0.05 under 1,000, 0.10 under
                5,000, 0.50 under 10,000, 1.00 under 20,000, else 5.00), an
                approximation. A "tick" that is not an NSE tick size
  asset         an index (NIFTY, BANKNIFTY ...), an F&O contract, or anything
                that is not an NSE symbol (A-Z, 0-9, & and -, like M&M)
  short         selling more than you hold: this pack is long-only

What blocks an entry (on top):

  blocked       a symbol on your "blocked" list
  size          under min_order_inr, over max_order_inr
  position      the stock would be over max_position_pct of the account
  cash          the buy (its margin, value / mis_leverage) would dip into your
                cash buffer or need more cash than you have
  duplicate     the same symbol and side twice in this batch, or already
                placed today
  daily limit   max_orders_per_day orders placed today
  stop          no stop while "require_stop": true (the order's
                stop_loss_price or the plan's stop); a stop not below the entry
  loss          qty x (entry - stop) + Groww's round-trip charges at the stop
                over max_loss_per_trade_inr. The entry is the limit price for a
                limit order, else the HIGHER of the plan's entry and a fresh
                price (under 15 minutes old), so a stale plan cannot
                understate the risk
  reward:risk   (target - entry) / (entry - stop) under min_reward_risk
  daily loss    the max losses at the stops of today's entries plus this one
                over max_daily_loss_inr (win or lose, each counts in full)
  trades        max_trades_per_day entries already placed today
  window        IST: before no_entries_before (09:30), after no_entries_after
                (14:30), at or after flat_by (15:00), or the market is closed
                (weekend, NSE holiday, outside 09:15 to 15:30)

Exit code 0 when every order passes, 1 when any order is blocked.
The verdicts go to trader/state/risk.json with a fingerprint of each order,
so nse-groww only shows an order as ready if THIS exact order passed today.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

COSTS = {"brokerage_pct": 0.1, "brokerage_cap": 20, "brokerage_min": 5, "brokerage_min_pct": 2.5,
         "stt_sell_pct": 0.025, "exchange_pct": 0.00307, "sebi_pct": 0.0001, "ipft_pct": 0.0001,
         "stamp_buy_pct": 0.003, "gst_pct": 18}
DEFAULTS = {
    "budget": None, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_inr": 40000,
    "min_order_inr": 5000, "max_loss_per_trade_inr": 500, "max_daily_loss_inr": 1500,
    "max_trades_per_day": 3, "max_orders_per_day": 10, "min_reward_risk": 1.0, "require_stop": True,
    "no_entries_before": "09:30", "no_entries_after": "14:30", "flat_by": "15:00",
    "product": "MIS", "exchange": "NSE", "mis_leverage": 1, "blocked": [], "allow_short": False,
    "holidays": [], "costs": dict(COSTS),
}
# the fields that make an order what it is; nse-groww/ticket.py fingerprints the same ones
ORDER_FIELDS = ("symbol", "exchange", "product", "side", "qty", "type", "validity", "limit_price",
                "trigger_price", "stop_loss_price", "target_price", "tick")
PRICE_FIELDS = ("limit_price", "trigger_price", "stop_loss_price", "target_price")
TYPES = {"market", "limit", "sl", "sl-m"}
VALIDITIES = {"day", "ioc"}
SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&-]{0,19}$")
FNO = re.compile(r"(FUT|\d(CE|PE))$|\d{2}(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)")
INDEX_NAMES = {"NIFTY", "NIFTY50", "BANKNIFTY", "NIFTYBANK", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
               "INDIAVIX", "SENSEX", "BANKEX"}
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
PAISA_TOL = 0.005
FRESH_MINUTES = 15
REF_WORDS = {"plan": "the plan's entry", "price": "the latest price", "limit": "the limit price",
             "trigger price": "the trigger price", "stale price": "an old price"}
IST = datetime.timedelta(hours=5, minutes=30)
PAISA = Decimal("0.01")
TICK_BANDS = ((Decimal("250"), Decimal("0.01")), (Decimal("1000"), Decimal("0.05")),
              (Decimal("5000"), Decimal("0.10")), (Decimal("10000"), Decimal("0.50")),
              (Decimal("20000"), Decimal("1.00")))
TOP_TICK = Decimal("5.00")
NSE_TICKS = (Decimal("0.01"), Decimal("0.05"), Decimal("0.10"), Decimal("0.50"), Decimal("1.00"), Decimal("5.00"))

# NSE capital-market segment trading holidays on weekdays, 2026 (NSE circulars CMTR/71775 and CMTR/72260)
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
    "2026-11-24": "Prabhu Shri Guru Nanak Dev Jayanti",
    "2026-12-25": "Christmas",
}
CALENDAR_YEARS = (2026,)
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
GROWW_FNO_CUTOFF = datetime.time(15, 5)   # groww.in/p/risk-policy 6.3: new intraday orders stop (F&O stocks)


class RiskError(Exception):
    pass


# ---------------------------------------------------------------- time (IST, fixed UTC+05:30)

def to_ist(utc):
    """naive UTC -> naive IST wall clock (India has no daylight saving)."""
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
        raise RiskError("--now wants a time like \"2026-11-17 09:45\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_utc(text):
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def holidays_from(cfg):
    """The built-in NSE list plus any "holidays": ["YYYY-MM-DD", ...] in the config."""
    out = dict(NSE_HOLIDAYS)
    for h in (cfg or {}).get("holidays") or []:
        h = str(h).strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", h):
            raise RiskError("\"holidays\" in the config wants dates like \"2027-01-26\", got %r" % h)
        out.setdefault(h, "a holiday in your config")
    return out


def trading_day(d, holidays=None):
    """(is_trading_day, reason). Weekends and the NSE holiday list (plus your config's)."""
    if d.weekday() >= 5:
        return False, "weekend"
    name = (holidays if holidays is not None else NSE_HOLIDAYS).get(d.isoformat())
    if name:
        return False, "NSE holiday: " + name
    return True, ""


def calendar_warning(d):
    if d.year not in CALENDAR_YEARS:
        return "NSE holiday list for %d not loaded: check nseindia.com" % d.year
    return None


def hhmm(value, key):
    """"09:30" -> time(9, 30); None or "" -> None (that bound is off)."""
    if value in (None, "", False):
        return None
    m = HHMM.match(str(value).strip())
    if not m:
        raise RiskError("%s must be an IST time like \"09:30\", got %r" % (key, value))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def window_check(now_ist, c):
    """When an entry may be placed (IST). Returns {"closed", "entry", "past_flat", "warnings"}:
    closed     why the market is closed now, or None
    entry      why no new entry may be placed now (closed included), or None
    past_flat  True at or after flat_by"""
    before = hhmm(c.get("no_entries_before"), "no_entries_before")
    after = hhmm(c.get("no_entries_after"), "no_entries_after")
    flat = hhmm(c.get("flat_by"), "flat_by")
    out = {"closed": None, "entry": None, "past_flat": False, "warnings": []}
    d, t = now_ist.date(), now_ist.time().replace(second=0, microsecond=0)
    w = calendar_warning(d)
    if w:
        out["warnings"].append(w)
    stamp = now_ist.strftime("%a %Y-%m-%d %H:%M")
    ok, why = trading_day(d, holidays_from(c))
    if not ok:
        out["closed"] = "the market is closed today (%s, %s IST)" % (why, stamp)
    elif t < OPEN or now_ist.time() >= CLOSE:
        out["closed"] = "the market is closed (%s IST; the NSE session is 09:15 to 15:30)" % stamp
    out["past_flat"] = flat is not None and t >= flat
    if before is None and after is None and flat is None:
        out["warnings"].append("the entry window is off (no_entries_before, no_entries_after and flat_by are null)")
        return out
    if after is None or after > GROWW_FNO_CUTOFF:
        out["warnings"].append("Groww stops new intraday orders at 15:05 for F&O stocks and at 15:20 for the "
                               "rest (groww.in risk policy); your no_entries_after is %s"
                               % (after.strftime("%H:%M") if after else "off"))
    if flat is not None and flat > GROWW_FNO_CUTOFF:
        out["warnings"].append("flat_by %s is after Groww's 15:05 cut-off for new intraday orders in F&O stocks: "
                               "be flat by 15:00, before it" % flat.strftime("%H:%M"))
    if out["closed"]:
        out["entry"] = "window: " + out["closed"]
    elif before is not None and t < before:
        out["entry"] = "window: no new entries before %s IST (it is %s)" % (before.strftime("%H:%M"),
                                                                             t.strftime("%H:%M"))
    elif out["past_flat"]:
        out["entry"] = ("window: it is %s, at or past your flat-by %s IST: exits only"
                        % (t.strftime("%H:%M"), flat.strftime("%H:%M")))
    elif after is not None and t > after:
        out["entry"] = "window: no new entries after %s IST (it is %s)" % (after.strftime("%H:%M"),
                                                                            t.strftime("%H:%M"))
    return out


# ---------------------------------------------------------------- money and the tick grid

def D(x):
    """A Decimal from a number or string, without binary float noise (1096.6999999999998 -> 1096.7)."""
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def q2(x):
    return D(x).quantize(PAISA, rounding=ROUND_HALF_UP)


def _group(whole):
    if len(whole) <= 3:
        return whole
    head, tail = whole[:-3], whole[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


def inr(x):
    """123456.5 -> "₹1,23,456.50"; -1250 -> "-₹1,250.00"."""
    d = q2(x)
    whole, frac = format(abs(d), "f").split(".")
    return ("-" if d < 0 else "") + "₹" + _group(whole) + "." + frac


def px(x):
    return format(q2(x), "f")


def tick_for(price):
    """NSE tick size for a price (NSE circular, effective 2025-04-15)."""
    p = D(price)
    for edge, tick in TICK_BANDS:
        if p < edge:
            return tick
    return TOP_TICK


def snap(price, tick, how="nearest"):
    p, t = D(price), D(tick)
    mode = {"down": ROUND_FLOOR, "up": ROUND_CEILING}.get(how, ROUND_HALF_UP)
    return ((p / t).quantize(Decimal("1"), rounding=mode) * t).quantize(PAISA)


def on_grid(price, tick):
    return (D(price) / D(tick)) % 1 == 0


def _costs(costs):
    c = dict(COSTS)
    c.update(costs or {})
    try:
        return {k: D(v) for k, v in c.items()}
    except Exception:
        raise RiskError("\"costs\" in the config must be numbers")


def brokerage(value, c):
    if value <= 0:
        return Decimal("0")
    b = min(c["brokerage_cap"], value * c["brokerage_pct"] / 100)
    if b < c["brokerage_min"]:
        b = min(c["brokerage_min"], value * c["brokerage_min_pct"] / 100)
    return b


def round_trip_costs(entry, exit, qty, costs=None):
    """Groww's charges for buying qty at entry and selling them at exit (two executed orders).
    Same function as nse-budget/size.py. Returns each charge and the total, in rupees."""
    c = _costs(costs)
    q = D(qty)
    buy_value, sell_value = D(entry) * q, D(exit) * q
    turnover = buy_value + sell_value
    raw = {
        "brokerage": brokerage(buy_value, c) + brokerage(sell_value, c),
        "stt": sell_value * c["stt_sell_pct"] / 100,
        "exchange": turnover * c["exchange_pct"] / 100,
        "sebi": turnover * c["sebi_pct"] / 100,
        "ipft": turnover * c["ipft_pct"] / 100,
        "stamp": buy_value * c["stamp_buy_pct"] / 100,
    }
    out = {k: q2(v) for k, v in raw.items()}
    out["gst"] = q2((out["brokerage"] + out["exchange"] + out["sebi"] + out["ipft"]) * c["gst_pct"] / 100)
    out["total"] = sum(out[k] for k in ("brokerage", "stt", "exchange", "sebi", "ipft", "stamp", "gst"))
    return {k: float(v) for k, v in out.items()}


# ---------------------------------------------------------------- files and helpers

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise RiskError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise RiskError("%s is not valid JSON: %s" % (path, e))


def fingerprint(order):
    """Same function in nse-groww/ticket.py: the exact order that was checked."""
    core = {k: order[k] for k in ORDER_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def nse_symbol(text):
    """" m&m.ns " -> "M&M"."""
    s = str(text or "").strip().upper()
    if s.endswith(".NS"):
        s = s[:-3]
    return s


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _pct(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.2f" % x)


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0")


def account(cfg, portfolio, prices):
    """Starting account: cash and position values. No portfolio = a fresh
    account funded with the budget."""
    warnings = []
    if portfolio is None:
        budget = _f(cfg.get("budget"))
        if not budget:
            raise RiskError("no trader/portfolio.json and no budget in the config: "
                            "nothing to check the orders against")
        return {"cash": budget, "shares": {}, "values": {}}, warnings
    cash = _f(portfolio.get("cash"))
    if cash is None:
        raise RiskError("portfolio.json needs a \"cash\" number")
    shares, values = {}, {}
    for p in portfolio.get("positions", []):
        t = nse_symbol(p.get("ticker") or p.get("symbol"))
        if not t:
            continue
        n = _f(p.get("shares", p.get("qty")))
        if n is None:
            n = sum(_f(l.get("shares")) or 0 for l in p.get("lots", []))
        shares[t] = shares.get(t, 0) + n
        if prices.get(t):
            values[t] = shares[t] * prices[t]
        else:
            basis = sum(_f(l.get("cost")) or 0 for l in p.get("lots", []))
            values[t] = basis
            warnings.append("no price for %s: valued at its cost %s" % (t, inr(basis)))
    return {"cash": cash, "shares": shares, "values": values}, warnings


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def rec_is_entry(rec):
    """nse-groww logs the kind nse-risk gave; without one, a buy is an entry."""
    k = rec.get("kind")
    if k:
        return k == "entry"
    return str(rec.get("side", "")).lower() == "buy"


def placed_today(state, today):
    """Orders you said you placed in Groww today, minus any you later marked rejected or cancelled."""
    placed = {}
    for n, rec in enumerate((state or {}).get("orders", [])):
        if rec_day(rec) != today:
            continue
        st, fp = rec.get("status"), rec.get("fingerprint")
        if st == "placed":
            placed[fp or "#%d" % n] = rec
        elif st in ("rejected", "cancelled") and fp in placed:
            del placed[fp]
    return list(placed.values())


# ---------------------------------------------------------------- the check

def check(orders, cfg, portfolio=None, prices=None, state=None, today=None, now=None, price_times=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...]}.

    now          naive UTC (default: the real clock); its IST date is "today"
    price_times  {SYM: naive UTC time of that price}; None = every price counts as fresh"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    costs = c.get("costs") or COSTS
    prices = {nse_symbol(k): float(v) for k, v in (prices or {}).items() if _f(v)}
    if price_times is not None:
        price_times = {nse_symbol(k): v for k, v in price_times.items()}
    now_utc = now or parse_now(None)
    now_ist = to_ist(now_utc)
    today = today or now_ist.date().isoformat()
    acct, top_warn = account(c, portfolio, prices)
    blocked = {nse_symbol(b) for b in c.get("blocked") or []}
    lev = _f(c.get("mis_leverage", 1))
    if lev is None or lev < 1:
        raise RiskError("mis_leverage must be 1 (no leverage) or more, got %r" % c.get("mis_leverage"))
    done_today = placed_today(state, today)
    count_today = len(done_today)
    entries_today = [r for r in done_today if rec_is_entry(r)]
    loss_today = 0.0
    for r in entries_today:
        ml = _f(r.get("max_loss"))
        if ml is None:
            top_warn.append("an entry placed today (%s %s) has no max loss in the log; not counted in "
                            "the daily loss" % (r.get("side"), r.get("symbol")))
        else:
            loss_today += ml
    placed_keys = {(nse_symbol(r.get("symbol")), r.get("side")) for r in done_today}
    win = window_check(now_ist, c)
    seen = set()
    results = []
    batch_entries, batch_loss = 0, 0.0
    top_warn.extend(win["warnings"])
    if lev > 1:
        top_warn.append("mis_leverage %s: Groww blocks only 1/%s of each buy as margin, but the loss at the "
                        "stop is yours in full" % (_num(lev), _num(lev)))
    if c.get("allow_short"):
        top_warn.append("\"allow_short\" is ignored: this pack is long-only")

    def fresh(sym):
        if sym not in prices:
            return None
        if price_times is None:
            return prices[sym]
        t = price_times.get(sym)
        if t is None:
            return None
        age = (now_utc - t).total_seconds()
        return prices[sym] if -60 <= age <= FRESH_MINUTES * 60 else None

    for i, o in enumerate(orders, 1):
        reasons, warns, info = [], [], []
        later = []                       # entry-only reasons, turned into warnings for an exit
        if not isinstance(o, dict):
            o = {}
        sym = nse_symbol(o.get("symbol"))
        side = str(o.get("side", "")).strip().lower()
        typ = str(o.get("type", "market")).strip().lower()
        validity = str(o.get("validity", "day")).strip().lower()
        exch = str(o.get("exchange") or "").strip().upper()
        product = str(o.get("product") or "").strip().upper()
        qty = o.get("qty")
        plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
        q = _f(qty)
        value = None
        tick_in = o.get("tick", plan.get("tick"))
        order_tick = None
        if tick_in is not None:
            if _f(tick_in) is None or D(tick_in) not in NSE_TICKS:
                reasons.append("format: tick %r is not an NSE tick size (0.01, 0.05, 0.10, 0.50, 1.00 or 5.00)"
                               % (tick_in,))
            else:
                order_tick = D(tick_in).quantize(PAISA)

        # format
        if not sym:
            reasons.append("format: no symbol")
        if side not in ("buy", "sell"):
            reasons.append("format: side must be buy or sell, got %r" % o.get("side"))
        if o.get("notional") is not None:
            reasons.append("format: NSE shares are ordered by quantity: give qty, not an amount")
        if qty is None:
            reasons.append("format: no qty (whole shares)")
            q = None
        elif q is None or q <= 0 or q != int(q):
            reasons.append("format: qty must be a whole number of shares, got %r" % qty)
            q = None
        if exch != "NSE":
            reasons.append("format: exchange must be NSE, got %r; this pack trades NSE only" % (o.get("exchange"),))
        if product != "MIS":
            reasons.append("format: product must be MIS (intraday), got %r%s" % (
                o.get("product"), "; CNC is delivery" if product == "CNC" else ""))
        if typ not in TYPES:
            reasons.append("format: unknown order type %r (market, limit, sl or sl-m)" % typ)
        if validity not in VALIDITIES:
            reasons.append("format: unknown validity %r (day or ioc)" % validity)
        for k in PRICE_FIELDS:
            if o.get(k) is not None:
                v = _f(o.get(k))
                if v is None or v <= 0:
                    reasons.append("format: %s must be a positive number, got %r" % (k, o.get(k)))
        if typ in ("limit", "sl") and o.get("limit_price") is None:
            reasons.append("format: %s order needs a limit_price" % ("an SL" if typ == "sl" else "a limit"))
        if typ in ("sl", "sl-m") and o.get("trigger_price") is None:
            reasons.append("format: an %s order needs a trigger_price" % typ.upper())
        if typ in ("market", "sl-m") and o.get("limit_price") is not None:
            reasons.append("format: a %s order has no limit_price" % ("market" if typ == "market" else "SL-M"))
        legs = o.get("stop_loss_price") is not None or o.get("target_price") is not None
        if legs and side == "sell":
            reasons.append("format: a stoploss/target belongs on the buy that opens the trade, not on a sell")

        # asset
        if sym:
            if sym in INDEX_NAMES:
                reasons.append("asset: %s is an index, not a share; this pack trades NSE cash-segment shares "
                               "only" % sym)
            elif FNO.search(sym):
                reasons.append("asset: %s looks like an F&O contract; this pack trades NSE cash-segment shares "
                               "only" % sym)
            elif not SYMBOL.match(sym):
                reasons.append("asset: %r is not an NSE symbol (A-Z, 0-9, & and -, like RELIANCE, M&M, "
                               "BAJAJ-AUTO)" % sym)

        # the reference price: what one share costs to get in
        plan_entry = _f(plan.get("entry"))
        live = fresh(sym)
        ref, ref_from = None, None
        if typ in ("limit", "sl") and _f(o.get("limit_price")):
            ref, ref_from = _f(o.get("limit_price")), "limit"
        elif typ == "sl-m" and _f(o.get("trigger_price")):
            ref, ref_from = _f(o.get("trigger_price")), "trigger price"
        if ref is None:
            cands = [(p, w) for p, w in ((plan_entry, "plan"), (live, "price")) if p and p > 0]
            if cands:
                ref, ref_from = max(cands, key=lambda x: x[0])
                ref = float(snap(ref, order_tick or tick_for(ref)))
                if live and ref_from == "price" and plan_entry and live > plan_entry + PAISA_TOL:
                    info.append("%s last %s is above the plan's entry %s: the risk is measured from %s"
                                % (sym, px(ref), px(plan_entry), px(ref)))
            elif sym in prices:
                ref, ref_from = prices[sym], "stale price"
        if plan_entry and live is None and sym in prices and price_times is not None and typ == "market":
            warns.append("the %s price in trader/prices.json is older than %d minutes: not used"
                         % (sym, FRESH_MINUTES))
        if q and ref:
            value = q * ref

        # the tick: the order's (NSE sets it monthly from the month-end close), else the entry's price band
        if order_tick:
            tick, tick_from = order_tick, "order"
        else:
            tick, tick_from = (tick_for(ref) if ref else None), "band"
        for k in PRICE_FIELDS:
            v = _f(o.get(k))
            if v and v > 0:
                t = tick if tick is not None else tick_for(v)
                if not on_grid(v, t):
                    lo, hi = px(snap(v, t, "down")), px(snap(v, t, "up"))
                    if order_tick:
                        reasons.append("tick: %s %s is off %s's %s tick this month (NSE sets it from the month-end "
                                       "close; Groww will not take it): use %s or %s"
                                       % (k, o.get(k), sym, px(t), lo, hi))
                    else:
                        reasons.append("tick: %s %s is not on the %s tick for this price band (the order carries "
                                       "no tick; NSE's real one comes from the month-end close): use %s or %s"
                                       % (k, o.get(k), px(t), lo, hi))

        # entry or exit
        held = acct["shares"].get(sym, 0.0)
        held_value = acct["values"].get(sym, 0.0)
        exiting = side == "sell" and held > 1e-9 and q is not None and q <= held + 1e-9
        kind = "exit" if exiting else "entry"
        if kind == "entry" and value is None and q:
            later.append("price: no price for %s to size a %s-share order; add it to trader/prices.json "
                         "(nse-alerts: alerts.py quote %s --write)" % (sym, qty, sym))

        # long-only: a sell of more than you hold
        if side == "sell" and sym and kind == "entry":
            reasons.append("short: selling %s %s but you hold %s; this pack is long-only (on Groww intraday that "
                           "sell would open a short). If you bought it, record the fill first (ticket.py fill)"
                           % (_num(q) if q else qty, sym, _num(held)))

        # blocked list and size
        if sym in blocked:
            later.append("blocked: %s is on your blocked list" % sym)
        if value is not None:
            if value < float(c.get("min_order_inr") or 0) - PAISA_TOL:
                later.append("size: %s is under your %s minimum order" % (inr(value), inr(c["min_order_inr"])))
            if c.get("max_order_inr") and value > float(c["max_order_inr"]) + PAISA_TOL:
                later.append("size: %s is over your %s max order" % (inr(value), inr(c["max_order_inr"])))

        # account effects, simulated in order
        cash = acct["cash"]
        total = cash + sum(acct["values"].values())
        buffer_inr = total * float(c.get("cash_buffer_pct") or 0) / 100.0
        if value is not None and side == "buy" and kind == "entry":
            need = value / lev
            after_cash = cash - need
            margin_note = (" (margin for %s at mis_leverage %s)" % (inr(value), _num(lev))) if lev > 1 else ""
            if after_cash < -PAISA_TOL:
                later.append("cash: needs %s%s but only %s cash" % (inr(need), margin_note, inr(max(cash, 0))))
            elif after_cash < buffer_inr - PAISA_TOL:
                later.append("cash: leaves %s cash, under your %s%% buffer of %s"
                             % (inr(max(after_cash, 0)), _pct(c["cash_buffer_pct"]), inr(buffer_inr)))
            pos_after = held_value + value
            limit = total * float(c.get("max_position_pct") or 100) / 100.0
            if total > 0 and pos_after > limit + PAISA_TOL:
                later.append("position: %s would be %s, %.1f%% of the %s account, over your %s%% cap"
                             % (sym, inr(pos_after), 100.0 * pos_after / total, inr(total),
                                _pct(c["max_position_pct"])))

        # intraday checks: stop, loss incl. charges, reward:risk, daily loss, trades, window
        max_loss = risk_ps = rr = stop = target = charges = None
        if kind == "entry" and side == "buy":
            leg_stop, plan_stop = _f(o.get("stop_loss_price")), _f(plan.get("stop"))
            stop = leg_stop if leg_stop else plan_stop
            target = _f(o.get("target_price")) or _f(plan.get("target"))
            if leg_stop and plan_stop and abs(leg_stop - plan_stop) > PAISA_TOL:
                warns.append("the order's stoploss %s differs from the plan's stop %s: the order's is the one "
                             "you type into Groww" % (px(leg_stop), px(plan_stop)))
            if stop and not leg_stop:
                warns.append("the stop %s is in the plan only, not on the order: add it in Groww (Add SL/TGT) "
                             "and set a stop alert (nse-alerts: alerts.py add --from-plan)" % px(stop))
            if not stop:
                if c.get("require_stop", True):
                    later.append("stop: no stop. Every entry needs one (\"require_stop\": true): a "
                                 "stop_loss_price, from nse-exit and nse-budget")
                else:
                    warns.append("no stop: the max loss is not capped, so it is not counted in the daily loss")
            elif ref and q:
                if stop >= ref - PAISA_TOL:
                    later.append("stop: the stop %s must be below the entry %s for a buy" % (px(stop), px(ref)))
                else:
                    risk_ps = float(D(ref) - D(stop))
                    charges = round_trip_costs(ref, stop, q, costs)
                    move = float(q2(D(risk_ps) * D(q)))
                    max_loss = float(q2(D(move) + D(charges["total"])))
                    cap = _f(c.get("max_loss_per_trade_inr"))
                    if cap and max_loss > cap + PAISA_TOL:
                        later.append("loss: %s shares x %s risk a share = %s + %s Groww charges = %s at the stop, "
                                     "over your %s max loss per trade"
                                     % (_num(q), inr(risk_ps), inr(move), inr(charges["total"]), inr(max_loss),
                                        inr(cap)))
                    min_rr = _f(c.get("min_reward_risk"))
                    if target:
                        if target <= ref + PAISA_TOL:
                            later.append("target: the target %s must be above the entry %s" % (px(target), px(ref)))
                        else:
                            rr = round(float((D(target) - D(ref)) / (D(ref) - D(stop))), 2)
                            if min_rr and rr < min_rr - 1e-9:
                                later.append("reward:risk: %.2f (target %s, entry %s, stop %s) is under your "
                                             "minimum %s" % (rr, px(target), px(ref), px(stop), _num(min_rr)))
                    elif min_rr:
                        later.append("reward:risk: no target to measure it against (min_reward_risk %s)"
                                     % _num(min_rr))
            daily = _f(c.get("max_daily_loss_inr"))
            if daily and max_loss is not None:
                used = loss_today + batch_loss
                if used + max_loss > daily + PAISA_TOL:
                    later.append("daily loss: %s already at risk today plus %s for this one = %s, over your %s "
                                 "daily loss limit" % (inr(used), inr(max_loss), inr(used + max_loss), inr(daily)))
            cap_t = int(c.get("max_trades_per_day") or 0)
            if cap_t and len(entries_today) + batch_entries >= cap_t:
                later.append("trades: %d entries placed today plus %d passed earlier in this batch, your limit "
                             "is %d a day" % (len(entries_today), batch_entries, cap_t))
            if stop and ref and risk_ps:
                info.append("entry %s (%s), stop %s, target %s: risk %s a share, NSE tick %s%s"
                            % (px(ref), REF_WORDS.get(ref_from, ref_from), px(stop),
                               px(target) if target else "none", inr(risk_ps), px(tick),
                               "" if order_tick else " (from the price band: approximate)"))
                info.append("max loss %s = %s + %s Groww charges%s, reward:risk %s%s"
                            % (inr(max_loss), inr(max_loss - charges["total"]), inr(charges["total"]),
                               (" (limit %s)" % inr(c["max_loss_per_trade_inr"]))
                               if _f(c.get("max_loss_per_trade_inr")) else "",
                               ("%.2f" % rr) if rr is not None else "n/a",
                               (" (min %s)" % _num(c["min_reward_risk"])) if _f(c.get("min_reward_risk")) else ""))
            if daily or cap_t:
                info.append("today before this: %d of %s trades, %s of %s daily loss"
                            % (len(entries_today) + batch_entries, cap_t or "unlimited",
                               inr(loss_today + batch_loss), inr(daily) if daily else "no limit on"))
        if kind == "entry" and win["entry"]:
            later.append(win["entry"])
        if kind == "exit":
            if win["closed"]:
                warns.append("exit: %s; Groww fills it only in the session" % win["closed"])
            elif win["past_flat"]:
                warns.append("exit: past your flat-by %s IST, get out now; Groww stops new intraday orders at "
                             "15:05 for F&O stocks and squares off open positions from 15:20 for ₹50 + GST each"
                             % c.get("flat_by"))

        # duplicates and the daily order limit
        key = (sym, side)
        if key in seen:
            later.append("duplicate: a second %s %s in this batch" % (side, sym))
        if key in placed_keys:
            later.append("duplicate: a %s %s was already placed today" % (side, sym))
        passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
        cap_n = int(c.get("max_orders_per_day") or 0)
        if cap_n and count_today + passed_so_far >= cap_n:
            later.append("daily limit: %d placed today plus %d passed earlier in this batch, "
                         "your limit is %d a day" % (count_today, passed_so_far, cap_n))
        seen.add(key)

        if kind == "entry":
            reasons.extend(later)
        else:
            warns.extend("exit, not blocked: " + x for x in later)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS":
            if kind == "entry" and side == "buy":
                batch_entries += 1
                batch_loss += max_loss or 0.0
            if side == "buy" and value is not None:
                acct["cash"] -= value / lev
                acct["values"][sym] = acct["values"].get(sym, 0.0) + value
                acct["shares"][sym] = held + (q or 0)
            elif side == "sell" and q is not None:
                per = (held_value / held) if held > 1e-9 else (ref or 0)
                acct["cash"] += value if value is not None else q * per
                acct["values"][sym] = max(acct["values"].get(sym, 0.0) - q * per, 0.0)
                acct["shares"][sym] = held - q
        results.append({
            "index": i, "symbol": sym, "side": side, "kind": kind,
            "amount": "%s shares" % qty,
            "value_inr": round(value, 2) if value is not None else None,
            "entry_ref": round(ref, 2) if ref else None, "entry_ref_from": ref_from,
            "tick": float(tick) if tick is not None else None, "tick_from": tick_from if tick is not None else None,
            "stop": stop, "target": target, "risk_per_share": risk_ps, "max_loss": max_loss,
            "charges_at_stop": charges, "reward_risk": rr, "fingerprint": fingerprint(o), "verdict": verdict,
            "reasons": reasons, "warnings": warns, "info": info,
        })

    return {
        "date": today,
        "now_ist": now_ist.strftime("%Y-%m-%d %H:%M"),
        "results": results,
        "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
        "warnings": top_warn,
        "cash_after": round(acct["cash"], 2),
        "today": {"orders_placed": count_today, "entries_placed": len(entries_today),
                  "loss_at_risk": round(loss_today, 2)},
    }


def report(res):
    lines = []
    for w in res["warnings"]:
        lines.append("WARNING: " + w)
    if res["warnings"]:
        lines.append("")
    for r in res["results"]:
        lines.append("%-5s #%d %s %s %s, %s" % (r["verdict"], r["index"], r["side"], r["symbol"], r["amount"],
                                                r["kind"]))
        for x in r.get("info", []):
            lines.append("          " + x)
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    lines.append("")
    lines.append("%d passed, %d blocked at %s IST. Cash after the passed orders: %s."
                 % (res["pass"], res["block"], res["now_ist"], inr(res["cash_after"])))
    if res["block"]:
        lines.append("Blocked orders get no ticket. Fix the order or your rules, then check again.")
    else:
        lines.append("Every order passed. nse-groww shows each ticket; you place it in the Groww app yourself, "
                     "one at a time.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def load_prices(pdoc):
    """prices.json -> ({SYM: price}, {SYM: price time as naive UTC or None}).

    The price time is the quote's own time (detail.time), else when it was fetched
    (detail.fetched), else the file's as_of."""
    if not isinstance(pdoc, dict):
        return {}, {}
    prices = pdoc.get("prices", pdoc)
    if not isinstance(prices, dict):
        return {}, {}
    out, times = {}, {}
    as_of = parse_utc(pdoc.get("as_of"))
    detail = pdoc.get("detail") or {}
    for s, p in prices.items():
        if _f(p) is None:
            continue
        d = detail.get(s) or {}
        s = nse_symbol(s)
        out[s] = float(p)
        times[s] = parse_utc(d.get("time")) or parse_utc(d.get("fetched")) or as_of
    return out, times


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check proposed NSE intraday orders against your limits (nse-risk).")
    ap.add_argument("--orders", default="trader/orders.json")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; if missing, a fresh account funded with the budget")
    ap.add_argument("--prices", default="trader/prices.json")
    ap.add_argument("--state", default="trader/state/broker.json", help="what you placed in Groww today (nse-groww)")
    ap.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-11-17 09:45\"")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today in India)")
    ap.add_argument("--out", default="trader/state/risk.json", help="where the verdicts are recorded")
    ap.add_argument("--no-write", action="store_true", help="do not record the verdicts")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        now = parse_now(a.now)
        if a.today and not re.match(r"^\d{4}-\d{2}-\d{2}$", a.today):
            raise RiskError("--today wants a date like 2026-11-17")
        doc = read_json(a.orders, "orders file (run nse-budget first)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % a.orders)
        cfg = read_json(a.config, "config", required=False) or {}
        portfolio = read_json(a.portfolio, "portfolio", required=False)
        pdoc = read_json(a.prices, "prices", required=False) or {}
        prices, times = load_prices(pdoc)
        state = read_json(a.state, "Groww order log", required=False)
        res = check(orders, cfg, portfolio, prices, state, a.today, now, times)
        if pdoc.get("as_of"):
            res["prices_as_of"] = pdoc["as_of"]
        if not a.no_write:
            d = os.path.dirname(a.out)
            if d:
                os.makedirs(d, exist_ok=True)
            rec = dict(res)
            rec["checked_at"] = datetime.datetime.now(datetime.timezone.utc).replace(
                microsecond=0, tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")
            rec["orders_file"] = a.orders
            tmp = a.out + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, a.out)
    except RiskError as e:
        print("check.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2, ensure_ascii=False) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    sys.exit(main())
