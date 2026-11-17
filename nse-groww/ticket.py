#!/usr/bin/env python3
"""Groww order tickets and the order log for nse-groww. Standard library only.

This tool never talks to Groww or to anything else on the network, and it
never places anything. You place every order yourself, by hand, in the Groww
app. The tool does its work around that one moment:

  show      renders each proposed order as a plain ticket to type into the
            Groww app: the buy, its stoploss and target (Groww's "Add SL/TGT"
            for intraday), the max loss at the stop with Groww's charges, the
            estimated charges and the breakeven. An order is READY only if
            nse-risk passed THIS exact order today (matched by fingerprint)
            and nothing has been recorded for it yet.
  placed    records your explicit yes for ONE order, word for word. Yes means
            "I placed it in Groww". It refuses "yes to all", "sure", "ok", a
            yes that names a different stock or amount, and any order that is
            not READY. (approve is the same command.)
  decline   records a no: you did not place it.
  result    records that Groww rejected an order you placed, or that you
            cancelled it before it filled: it stops counting for today.
  fill      records an executed order in trader/portfolio.json (cash, shares
            and Groww's estimated charges) and in the log.
  flatten   builds a sell ticket that closes a symbol's intraday position by
            flat_by. It still goes through nse-risk and your yes.
  log       prints today's records.

    python3 ticket.py show                    # every order, READY or not
    python3 ticket.py show --index 1 --json   # one ticket as JSON
    python3 ticket.py show --index 1 --separate-stop   # no "Add SL/TGT" in your app
    python3 ticket.py placed --index 1 --reply "yes"
    python3 ticket.py fill --index 1 --price 1105.90
    python3 ticket.py fill --symbol SBIN --side sell --qty 33 --price 1114.70
    python3 ticket.py flatten --symbol SBIN --qty 33
    python3 ticket.py log

Everything is written to trader/state/broker.json (append only). Dates are
IST dates; --now "2026-11-17 09:45" pins the clock.
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
# the fields that make an order what it is; nse-risk/check.py fingerprints the same ones
ORDER_FIELDS = ("symbol", "exchange", "product", "side", "qty", "type", "validity", "limit_price",
                "trigger_price", "stop_loss_price", "target_price", "tick")
SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&-]{0,19}$")
VALIDITY_WORDS = {"day": "Day", "ioc": "IOC (immediate or cancel)"}
YES_WORDS = ("yes", "y")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later",
               "hold", "unless", "if"}
IST = datetime.timedelta(hours=5, minutes=30)
PAISA = Decimal("0.01")
TICK_BANDS = ((Decimal("250"), Decimal("0.01")), (Decimal("1000"), Decimal("0.05")),
              (Decimal("5000"), Decimal("0.10")), (Decimal("10000"), Decimal("0.50")),
              (Decimal("20000"), Decimal("1.00")))
TOP_TICK = Decimal("5.00")
SQUARE_OFF_NOTE = ("Groww stops new intraday orders at 15:05 for F&O stocks (15:20 for the rest) and squares off "
                   "open intraday positions from 15:20 for ₹50 + GST each")


class TicketError(Exception):
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
        raise TicketError("--now wants a time like \"2026-11-17 09:45\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def ist_iso(now_utc):
    """naive UTC -> "2026-11-17T09:45:00+05:30" (the IST wall clock with its offset)."""
    return to_ist(now_utc).replace(microsecond=0).isoformat() + "+05:30"


# ---------------------------------------------------------------- money, the tick grid, charges

def D(x):
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
    """A price as you type it into the app: 1105.7 -> "1105.70"."""
    return format(q2(x), "f")


def tick_for(price):
    p = D(price)
    for edge, tick in TICK_BANDS:
        if p < edge:
            return tick
    return TOP_TICK


def snap(price, tick, how="nearest"):
    p, t = D(price), D(tick)
    mode = {"down": ROUND_FLOOR, "up": ROUND_CEILING}.get(how, ROUND_HALF_UP)
    return ((p / t).quantize(Decimal("1"), rounding=mode) * t).quantize(PAISA)


def _costs(costs):
    c = dict(COSTS)
    c.update(costs or {})
    try:
        return {k: D(v) for k, v in c.items()}
    except Exception:
        raise TicketError("\"costs\" in the config must be numbers")


def brokerage(value, c):
    if value <= 0:
        return Decimal("0")
    b = min(c["brokerage_cap"], value * c["brokerage_pct"] / 100)
    if b < c["brokerage_min"]:
        b = min(c["brokerage_min"], value * c["brokerage_min_pct"] / 100)
    return b


def _charges(buy_value, sell_value, costs):
    c = _costs(costs)
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


def round_trip_costs(entry, exit, qty, costs=None):
    """Groww's charges for buying qty at entry and selling them at exit. Same as nse-budget/size.py."""
    q = D(qty)
    return _charges(D(entry) * q, D(exit) * q, costs)


def order_costs(price, qty, side, costs=None):
    """Groww's charges on ONE executed order."""
    v = D(price) * D(qty)
    return _charges(v if side == "buy" else Decimal("0"), v if side == "sell" else Decimal("0"), costs)


def breakeven(entry, qty, costs=None, tick=None):
    """The lowest sell price on the tick grid (the order's tick, else the price band's) at which
    the round trip stops losing money."""
    e, q = D(entry), D(qty)
    t = D(tick) if tick else tick_for(e)

    def net(p):
        return (p - e) * q - D(round_trip_costs(e, p, q, costs)["total"])

    p = snap(e + D(round_trip_costs(e, e, q, costs)["total"]) / q, t, "up")
    for _ in range(100000):
        if net(p) >= 0:
            break
        p += t
    while p - t > e and net(p - t) >= 0:
        p -= t
    return float(p)


# ---------------------------------------------------------------- files

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise TicketError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise TicketError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def rec_day(rec):
    """The trading day a record belongs to: its "date", else the day of "at"."""
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


# ---------------------------------------------------------------- the order

def fingerprint(order):
    """Same function as nse-risk/check.py: identifies the exact order."""
    core = {k: order[k] for k in ORDER_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def nse_symbol(text):
    """" m&m.ns " -> "M&M"."""
    s = str(text or "").strip().upper()
    if s.endswith(".NS"):
        s = s[:-3]
    return s


def _str_num(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        f = float(v)
        return str(int(f)) if f == int(f) else repr(f)
    return str(v)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def tick_of(order):
    """The stock's NSE tick the order carries (nse-budget copies it from the plan), or None."""
    t = order.get("tick")
    if t is None:
        t = (order.get("plan") or {}).get("tick")
    t = _f(t)
    return t if t and t > 0 else None


def entry_price(order):
    """What one share is expected to cost: the limit price, else the plan's entry."""
    if str(order.get("type", "market")).lower() in ("limit", "sl") and _f(order.get("limit_price")):
        return _f(order["limit_price"])
    if str(order.get("type", "")).lower() == "sl-m" and _f(order.get("trigger_price")):
        return _f(order["trigger_price"])
    return _f((order.get("plan") or {}).get("entry"))


def max_loss(order, costs=None):
    """(rupees lost if the stop fills exactly at the stop, Groww charges included, stop), or (None, stop)."""
    plan = order.get("plan") or {}
    stop = _f(order.get("stop_loss_price")) or _f(plan.get("stop"))
    q = _f(order.get("qty"))
    if str(order.get("side", "")).lower() != "buy" or not stop or not q:
        return None, stop
    if str(order.get("type", "market")).lower() == "market" and _f(plan.get("max_loss")) is not None:
        return round(_f(plan["max_loss"]), 2), stop
    entry = entry_price(order)
    if not entry or entry <= stop:
        return None, stop
    loss = (D(entry) - D(stop)) * D(q) + D(round_trip_costs(entry, stop, q, costs)["total"])
    return float(q2(loss)), stop


def how_words(order):
    typ = str(order.get("type", "market")).lower()
    if typ == "limit":
        return "limit %s" % px(order.get("limit_price"))
    if typ == "sl":
        return "stop-loss (SL), trigger %s, limit %s" % (px(order.get("trigger_price")), px(order.get("limit_price")))
    if typ == "sl-m":
        return "stop-loss market (SL-M), trigger %s" % px(order.get("trigger_price"))
    return "market"


def plain_parts(order, loss=None, with_loss=True):
    """The order in plain words, one part per line."""
    side = str(order.get("side", "")).upper()
    sym = nse_symbol(order.get("symbol")) or "?"
    validity = VALIDITY_WORDS.get(str(order.get("validity", "day")).lower(), order.get("validity"))
    parts = ["%s %s %s, %s, %s %s, validity %s" % (side, _str_num(order.get("qty")), sym, how_words(order),
                                                  str(order.get("exchange") or "?").upper(),
                                                  "intraday (MIS)" if str(order.get("product", "")).upper() == "MIS"
                                                  else "product %s" % order.get("product"), validity)]
    legs = []
    if order.get("stop_loss_price") is not None:
        legs.append("stoploss %s" % px(order["stop_loss_price"]))
    if order.get("target_price") is not None:
        legs.append("target %s" % px(order["target_price"]))
    tick = tick_of(order)
    if tick and (legs or order.get("limit_price") is not None or order.get("trigger_price") is not None):
        legs.append("NSE tick ₹%s" % px(tick))
    if legs:
        parts.append(", ".join(legs))
    if not with_loss:
        return parts
    if loss is None:
        loss, _ = max_loss(order)
    if loss is not None:
        parts.append("max loss about %s incl. Groww charges" % inr(loss))
    return parts


def plain(order, loss=None):
    """BUY 33 SBIN, market, NSE intraday (MIS), validity Day / stoploss 1096.70, target 1114.70 / max loss ..."""
    return " / ".join(plain_parts(order, loss))


def amount_numbers(order, loss=None):
    nums = []
    for k in ("qty", "limit_price", "trigger_price", "stop_loss_price", "target_price"):
        if order.get(k) is not None:
            v = _f(order[k])
            if v is not None:
                nums.append(v)
    if loss is None:
        loss, _ = max_loss(order)
    if loss is not None:
        nums.append(loss)
    return nums


def parse_yes(reply, order, batch_symbols=(), loss=None):
    """Is this reply an explicit yes ("I placed it in Groww") for THIS one order? Returns (ok, why)."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-z']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" counts (not ok, sure, done, go)"
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    sym = nse_symbol(order.get("symbol"))
    upper = raw.upper()
    tokens = {nse_symbol(t) for t in re.findall(r"[A-Z0-9&][A-Z0-9&.\-]*", upper)}
    others = {nse_symbol(s) for s in batch_symbols} - {sym}
    named = tokens & others
    if named:
        return False, "the reply names %s, but this order is for %s" % (", ".join(sorted(named)), sym)
    rest = upper
    for s in sorted({sym} | others, key=len, reverse=True):
        if s:
            rest = re.sub(r"(?<![A-Z0-9&])%s(\.NS)?(?![A-Z0-9&])" % re.escape(s), " ", rest)
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", rest)]
    if nums:
        want = amount_numbers(order, loss)
        if not any(abs(n - w) < 0.005 for n in nums for w in want):
            return False, "the reply says %s, but this order is %s" % (
                ", ".join(_str_num(n) for n in nums), plain(order, loss))
    return True, "explicit yes: placed in Groww: %s" % plain(order, loss)


# ---------------------------------------------------------------- state

def load_state(path):
    return read_json(path, "Groww order log", required=False) or {"orders": []}


def latest(state, fp, today):
    """The latest record made today for this fingerprint, or None."""
    found = None
    for rec in state.get("orders", []):
        if rec.get("fingerprint") == fp and rec_day(rec) == today and rec.get("status") != "filled":
            found = rec
    return found


def risk_verdict(risk, fp, today):
    if not risk:
        return None, "nse-risk has not checked these orders (run check.py)"
    if risk.get("date") != today:
        return None, "the risk check is from %s, not today; run check.py again" % risk.get("date")
    for r in risk.get("results", []):
        if r.get("fingerprint") == fp:
            return r, None
    return None, "this exact order was not in the last risk check (it changed or is new); run check.py again"


def status_of(order, risk, state, today):
    """(ready, label, risk_result)"""
    fp = fingerprint(order)
    rv, why = risk_verdict(risk, fp, today)
    if rv is None:
        return False, "NOT READY: " + why, None
    if rv.get("verdict") != "PASS":
        return False, "BLOCKED by nse-risk: " + "; ".join(rv.get("reasons", [])), rv
    rec = latest(state, fp, today)
    if rec:
        return False, "DONE: %s at %s" % (rec.get("status"), rec.get("at")), rv
    return True, "READY: nse-risk PASS, waiting for you to place it in Groww", rv


def kind_of(order, rv):
    """entry or exit, as nse-risk saw it (without a verdict: a buy is an entry)."""
    if rv and rv.get("kind"):
        return rv["kind"]
    return "entry" if str(order.get("side", "")).lower() == "buy" else "exit"


def loss_of(order, rv, costs=None):
    """The max loss nse-risk passed (it measures from the higher of the plan's entry and a fresh
    price), else the ticket's own estimate."""
    if rv and rv.get("max_loss") is not None:
        return round(float(rv["max_loss"]), 2)
    return max_loss(order, costs)[0]


# ---------------------------------------------------------------- the Groww steps

def groww_steps(order, separate_stop=False):
    """What to tap in the Groww app, in order. Groww's own words where its help pages give them."""
    sym = nse_symbol(order.get("symbol"))
    side = str(order.get("side", "")).lower()
    q = _str_num(order.get("qty"))
    typ = str(order.get("type", "market")).lower()
    stop, target = order.get("stop_loss_price"), order.get("target_price")
    tick = tick_of(order)
    steps_of = (" (Groww takes prices in steps of ₹%s, this stock's NSE tick)" % px(tick)) if tick else ""
    if typ == "limit":
        price = "Limit, price %s%s" % (px(order.get("limit_price")), steps_of)
    elif typ == "sl":
        price = "stop-loss (SL): trigger price %s, limit price %s" % (px(order.get("trigger_price")),
                                                                       px(order.get("limit_price")))
    elif typ == "sl-m":
        price = "stop-loss with no limit price (SL-M): trigger price %s" % px(order.get("trigger_price"))
    else:
        price = "Market"
    if side == "sell":
        first = ("Orders tab: if a stoploss or target order for %s is still pending, cancel it first. Left open, "
                 "it can fill after this sell and sell shares you no longer hold: an intraday short." % sym)
        if typ != "market":
            return [first, "Search %s, tap SELL, choose Intraday and NSE, qty %s, order type: %s. Place it."
                    % (sym, q, price),
                    "Until it fills you still hold the shares: watch it, and be out by the flat-by time."]
        return [
            first,
            "Positions tab: %s (Intraday) > Exit. Or: search %s, tap SELL, choose Intraday and NSE, qty %s, %s."
            % (sym, sym, q, price),
            "Check the Positions tab: no %s shares left open." % sym,
        ]
    steps = ["Search %s and tap BUY. Choose Intraday (not Delivery) and NSE." % sym,
             "Qty %s. Order type: %s." % (q, price)]
    if stop is None and target is None:
        steps.append("Check the order screen says Intraday, NSE, qty %s, then place it." % q)
        return steps
    if not separate_stop:
        legs = []
        if stop is not None:
            legs.append("Stoploss %s" % px(stop))
        if target is not None:
            legs.append("Target %s" % px(target))
        steps.append("Tap \"Add SL/TGT\": %s%s. Leave Trail off. When one of them fills, Groww cancels the other."
                     % (", ".join(legs), steps_of))
        steps.append("Check the order screen says Intraday, NSE, qty %s, then place it." % q)
        steps.append("Do NOT also place a separate stop-loss sell or a target sell for these shares: two resting "
                     "sells for the same %s shares can both fill and leave you short." % q)
        return steps
    steps.append("Check the order screen says Intraday, NSE, qty %s, then place it. No SL/TGT on this screen." % q)
    if stop is not None:
        steps.append("When Groww shows the buy executed: search %s, tap SELL, Intraday, NSE, qty %s, stop-loss "
                     "order with trigger price %s and no limit price (it sells at market once triggered%s). "
                     "Place it." % (sym, q, px(stop), ("; prices in steps of ₹%s" % px(tick)) if tick else ""))
    if target is not None:
        steps.append("Do NOT place a target sell at %s as well. A resting target sell AND a resting stop-loss "
                     "sell for the same %s shares can BOTH fill: the second one sells shares you no longer hold, "
                     "which on Groww intraday is an unintended short. nse-alerts watches the target instead "
                     "(alerts.py add --from-plan)." % (px(target), q))
        steps.append("When the target alert rings: Orders tab, open the pending %s stop-loss order and choose "
                     "\"Sell at market price\". That one order becomes your exit; no second sell." % sym)
    return steps


# ---------------------------------------------------------------- rendering

def money_lines(order, costs, loss):
    """Charges and breakeven for the ticket."""
    q = _f(order.get("qty"))
    side = str(order.get("side", "")).lower()
    entry = entry_price(order)
    stop, target = _f(order.get("stop_loss_price")) or _f((order.get("plan") or {}).get("stop")), \
        _f(order.get("target_price")) or _f((order.get("plan") or {}).get("target"))
    lines = []
    if not q or q <= 0:
        return lines, {}
    info = {}
    if side == "buy" and entry:
        if stop and stop < entry:
            at_stop = round_trip_costs(entry, stop, q, costs)
            info["charges_at_stop"] = at_stop["total"]
            if loss is not None:
                lines.append("max loss about %s at the stop: %s + %s Groww charges (a stop can fill worse "
                             "in a fast market)" % (inr(loss), inr(loss - at_stop["total"]), inr(at_stop["total"])))
        parts = []
        if "charges_at_stop" in info:
            parts.append("%s if the stop fills" % inr(info["charges_at_stop"]))
        if target and target > entry:
            info["charges_at_target"] = round_trip_costs(entry, target, q, costs)["total"]
            parts.append("%s at the target" % inr(info["charges_at_target"]))
        if parts:
            info["breakeven"] = breakeven(entry, q, costs, tick_of(order))
            lines.append("Groww charges, estimated, buy + sell: %s; breakeven %s (%s a share above %s)"
                         % (", ".join(parts), px(info["breakeven"]), inr(info["breakeven"] - entry), px(entry)))
        else:
            info["charges_buy"] = order_costs(entry, q, "buy", costs)["total"]
            lines.append("Groww charges on this buy, estimated: %s (the sell adds its own)" % inr(info["charges_buy"]))
    elif side == "sell":
        ref = entry or _f((order.get("plan") or {}).get("entry"))
        if ref:
            info["charges_sell"] = order_costs(ref, q, "sell", costs)["total"]
            lines.append("Groww charges on this sell, estimated at %s: %s" % (px(ref), inr(info["charges_sell"])))
        else:
            lines.append("Groww charges on this sell: brokerage up to ₹20, STT 0.025%, plus the small fees; "
                         "fill records the estimate")
    return lines, info


def render(order, idx, total, cfg, risk, state, today, separate_stop=False):
    ready, label, rv = status_of(order, risk, state, today)
    costs = cfg.get("costs") or COSTS
    loss = loss_of(order, rv, costs)
    lines = ["ORDER %d of %d: NSE intraday, real money. You place it yourself in the Groww app." % (idx, total)]
    for part in plain_parts(order, loss, with_loss=False):
        lines.append("  " + part)
    mlines, _ = money_lines(order, costs, loss)
    for l in mlines:
        lines.append("  " + l)
    if order.get("why"):
        lines.append("  why: " + order["why"])
    lines.append("  " + label)
    for w in (rv or {}).get("warnings", []):
        lines.append("  note: " + w)
    if ready:
        lines.append("  In the Groww app:")
        for n, step in enumerate(groww_steps(order, separate_stop), 1):
            lines.append("    %d. %s" % (n, step))
        side = str(order.get("side", "")).lower()
        if side == "buy" and not separate_stop and (order.get("stop_loss_price") or order.get("target_price")):
            lines.append("  No \"Add SL/TGT\" on your screen? ticket.py show --index %d --separate-stop" % idx)
        if side == "buy":
            flat = (order.get("plan") or {}).get("flat_by") or cfg.get("flat_by") or "15:00"
            lines.append("  Out by %s IST, win or lose (nse-alerts rings, then ticket.py flatten): %s."
                         % (flat, SQUARE_OFF_NOTE))
        lines.append("  Placed it in Groww? Reply \"yes\". Did not place it? Reply \"no\".")
    return "\n".join(lines), ready


def ticket_json(order, idx, cfg, risk, state, today, separate_stop=False):
    ready, label, rv = status_of(order, risk, state, today)
    costs = cfg.get("costs") or COSTS
    loss = loss_of(order, rv, costs)
    _, money = money_lines(order, costs, loss)
    out = {"index": idx, "ready": ready, "status": label, "plain": plain(order, loss),
           "fingerprint": fingerprint(order), "symbol": nse_symbol(order.get("symbol")),
           "exchange": order.get("exchange"), "product": order.get("product"),
           "steps": groww_steps(order, separate_stop) if ready else [], "why": order.get("why", ""),
           "max_loss": loss, "stop": _f(order.get("stop_loss_price")), "target": _f(order.get("target_price")),
           "tick": tick_of(order)}
    out.update(money)
    return out


# ---------------------------------------------------------------- fills -> portfolio

def apply_fill(portfolio_path, cfg, symbol, side, qty, price, date, charges=True):
    """Record an executed order in trader/portfolio.json: cash and shares, with a lot per buy
    (oldest lots are relieved first on a sell). Groww's estimated charges come off the cash.
    Returns (portfolio, charges in rupees)."""
    pf = read_json(portfolio_path, "portfolio", required=False)
    if pf is None:
        budget = cfg.get("budget")
        if not budget:
            raise TicketError("no %s yet and no budget in the config to start it from" % portfolio_path)
        pf = {"cash": float(budget), "positions": []}
    symbol = nse_symbol(symbol)
    if not SYMBOL.match(symbol):
        raise TicketError("%r is not an NSE symbol (like SBIN, M&M, BAJAJ-AUTO)" % symbol)
    qty, price = _f(qty), _f(price)
    if qty is None or price is None or qty <= 0 or price <= 0:
        raise TicketError("a fill needs a positive qty and price")
    if qty != int(qty):
        raise TicketError("NSE shares fill in whole numbers; got qty %s" % _str_num(qty))
    cost = float(q2(D(qty) * D(price)))
    fee = order_costs(price, qty, side, cfg.get("costs") or COSTS)["total"] if charges else 0.0
    pos = None
    for p in pf.setdefault("positions", []):
        if nse_symbol(p.get("ticker") or p.get("symbol")) == symbol:
            pos = p
    if side == "buy":
        if pos is None:
            pos = {"ticker": symbol, "shares": 0.0, "lots": []}
            pf["positions"].append(pos)
        lots = pos.setdefault("lots", [])
        lots.append({"id": "%s-%d" % (symbol, len(lots) + 1), "date": date, "shares": qty, "cost": cost,
                     "charges": fee, "product": "MIS"})
        pos["shares"] = round(float(pos.get("shares", 0)) + qty, 6)
        pf["cash"] = float(q2(D(pf.get("cash", 0)) - D(cost) - D(fee)))
    else:
        if pos is None or float(pos.get("shares", 0)) + 1e-9 < qty:
            raise TicketError("cannot record selling %s %s: the portfolio holds %s (this pack is long-only)"
                              % (_str_num(qty), symbol, _str_num(pos.get("shares", 0)) if pos else 0))
        left = qty
        for lot in sorted(pos.get("lots", []), key=lambda l: l.get("date", "")):
            if left <= 1e-9:
                break
            take = min(left, float(lot["shares"]))
            per = float(lot["cost"]) / float(lot["shares"]) if float(lot["shares"]) else 0
            lot["cost"] = round(float(lot["cost"]) - per * take, 2)
            lot["shares"] = round(float(lot["shares"]) - take, 6)
            left -= take
        pos["lots"] = [l for l in pos.get("lots", []) if float(l["shares"]) > 1e-9]
        pos["shares"] = round(float(pos["shares"]) - qty, 6)
        if pos["shares"] <= 1e-9:
            pf["positions"].remove(pos)
        pf["cash"] = float(q2(D(pf.get("cash", 0)) + D(cost) - D(fee)))
    write_json(portfolio_path, pf)
    return pf, fee


def log_fill(state_path, now, today, symbol, side, qty, price, charges, fp=None, index=None):
    """A fill in the log, next to the placed record it belongs to."""
    state = load_state(state_path)
    rec = {"at": ist_iso(now), "date": today, "status": "filled", "symbol": nse_symbol(symbol), "side": side,
           "qty": _str_num(float(qty)), "price": float(price), "charges": charges,
           "plain": "%s %s %s at %s" % (side.upper(), _str_num(float(qty)), nse_symbol(symbol), px(price))}
    if fp:
        rec["fingerprint"] = fp
    if index is not None:
        rec["index"] = index
    state.setdefault("orders", []).append(rec)
    write_json(state_path, state)


# ---------------------------------------------------------------- commands

def _clock(a):
    now = parse_now(getattr(a, "now", None))
    today = getattr(a, "today", None) or to_ist(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", today):
        raise TicketError("--today wants a date like 2026-11-17")
    return now, today


def _load(a):
    doc = read_json(a.orders, "orders file (run nse-budget, or ticket.py flatten, first)")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise TicketError("no orders in %s" % a.orders)
    cfg = read_json(a.config, "config", required=False) or {}
    risk = read_json(a.risk, "risk check", required=False)
    state = load_state(a.state)
    now, today = _clock(a)
    return orders, cfg, risk, state, today, now


def _pick(orders, index):
    if index is None:
        raise TicketError("say which order: --index N")
    if not 1 <= index <= len(orders):
        raise TicketError("there is no order %d (there are %d)" % (index, len(orders)))
    return orders[index - 1]


def cmd_show(a):
    orders, cfg, risk, state, today, now = _load(a)
    picks = [(a.index, _pick(orders, a.index))] if a.index else list(enumerate(orders, 1))
    if a.json:
        out = [ticket_json(o, i, cfg, risk, state, today, a.separate_stop) for i, o in picks]
        print(json.dumps(out[0] if a.index else out, indent=2, ensure_ascii=False))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    for i, o in picks:
        text, ready = render(o, i, len(orders), cfg, risk, state, today, a.separate_stop)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    print("\nOne order at a time: a yes covers one ticket, never the batch. Nothing here places an order; "
          "you do, in Groww. Education and tooling, not financial advice.")
    return 0 if any_ready else 1


def cmd_placed(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    ready, label, rv = status_of(order, risk, state, today)
    if not ready:
        raise TicketError("order %d is not ready: %s" % (a.index, label))
    loss = loss_of(order, rv, cfg.get("costs") or COSTS)
    ok, why = parse_yes(a.reply, order, [o.get("symbol", "") for o in orders], loss)
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for order %d." % (why, a.index))
        return 1
    _, stop = max_loss(order)
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": nse_symbol(order.get("symbol")), "side": order.get("side"), "qty": order.get("qty"),
           "kind": kind_of(order, rv), "max_loss": loss, "stop": stop,
           "target": _f(order.get("target_price")), "plain": plain(order, loss), "status": "placed",
           "reply": a.reply, "where": "Groww app, by hand"}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded: you placed order %d in Groww: %s." % (a.index, plain(order, loss)))
    print("When Groww shows it executed, record the fill (its average price, from the order details):")
    print("  ticket.py fill --index %d --price P" % a.index)
    print("If Groww rejected it, or you cancelled it before it filled: ticket.py result --index %d "
          "--status rejected|cancelled" % a.index)
    return 0


def cmd_decline(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": nse_symbol(order.get("symbol")), "side": order.get("side"),
           "plain": plain(order), "status": "declined", "reply": a.reply or "no"}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded: you did not place order %d (%s). Nothing was placed." % (a.index, plain(order)))
    return 0


def cmd_result(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    fp = fingerprint(order)
    rec = latest(state, fp, today)
    if not rec or rec.get("status") != "placed":
        raise TicketError("order %d is not recorded as placed today; a result can only follow your yes" % a.index)
    filled = [r for r in state.get("orders", []) if r.get("fingerprint") == fp and r.get("status") == "filled"
              and rec_day(r) == today]
    if filled:
        raise TicketError("order %d already has a fill recorded; a filled order is not %s. Record the exit "
                          "with ticket.py fill" % (a.index, a.status))
    new = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fp, "symbol": rec["symbol"],
           "side": rec["side"], "plain": rec["plain"], "status": a.status}
    for k in ("qty", "kind", "max_loss", "stop"):
        if rec.get(k) is not None:
            new[k] = rec[k]
    if a.note:
        new["note"] = a.note
    state["orders"].append(new)
    write_json(a.state, state)
    print("Recorded order %d as %s. It no longer counts toward today's trades and daily loss." % (a.index, a.status))
    print("A new try is a new order: run nse-budget, nse-risk and show again.")
    return 0


def cmd_fill(a):
    cfg = read_json(a.config, "config", required=False) or {}
    now = parse_now(a.now)
    date = a.date or to_ist(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise TicketError("--date wants a date like 2026-11-17")
    fp = None
    symbol, side, qty = a.symbol, a.side, a.qty
    if a.index is not None:
        doc = read_json(a.orders, "orders file")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        order = _pick(orders, a.index)
        fp = fingerprint(order)
        state = load_state(a.state)
        rec = latest(state, fp, date)
        if not rec or rec.get("status") != "placed":
            raise TicketError("order %d is not recorded as placed on %s: say yes first (ticket.py placed)"
                              % (a.index, date))
        if symbol and nse_symbol(symbol) != nse_symbol(order.get("symbol")):
            raise TicketError("order %d is for %s, not %s" % (a.index, nse_symbol(order.get("symbol")),
                                                              nse_symbol(symbol)))
        symbol = order.get("symbol")
        side = side or str(order.get("side", "")).lower()
        done = sum(_f(r.get("qty")) or 0 for r in state.get("orders", [])
                   if r.get("fingerprint") == fp and r.get("status") == "filled" and rec_day(r) == date)
        left = (_f(order.get("qty")) or 0) - done
        if left <= 1e-9:
            raise TicketError("order %d is already recorded as filled (%s of %s shares). Record the exit "
                              "with its own ticket, or --symbol/--side/--qty" % (a.index, _str_num(done),
                                                                                 _str_num(order.get("qty"))))
        qty = qty or _str_num(left)
        if (_f(qty) or 0) > left + 1e-9:
            raise TicketError("order %d is for %s shares and %s are already recorded as filled; %s more is too "
                              "many" % (a.index, _str_num(order.get("qty")), _str_num(done), _str_num(_f(qty))))
    if not symbol or side not in ("buy", "sell") or qty is None:
        raise TicketError("say what filled: --index N, or --symbol S --side buy|sell --qty Q")
    pf, fee = apply_fill(a.portfolio, cfg, symbol, side, qty, a.price, date, not a.no_charges)
    log_fill(a.state, now, date, symbol, side, qty, a.price, fee, fp, a.index)
    print("Recorded %s %s %s at %s on %s in %s. Groww charges on it, estimated: %s. Cash now %s."
          % (side, _str_num(_f(qty)), nse_symbol(symbol), px(a.price), date, a.portfolio, inr(fee), inr(pf["cash"])))
    if fee:
        print("The charges are an estimate; your Groww contract note has the exact figure.")
    if side == "buy":
        print("Out by %s IST, win or lose: %s." % (cfg.get("flat_by") or "15:00", SQUARE_OFF_NOTE))
    return 0


def cmd_flatten(a):
    cfg = read_json(a.config, "config", required=False) or {}
    sym = nse_symbol(a.symbol)
    if not SYMBOL.match(sym):
        raise TicketError("%r is not an NSE symbol (like SBIN, M&M, BAJAJ-AUTO)" % a.symbol)
    now, today = _clock(a)
    source = "you (--qty)"
    if a.qty is not None:
        qty = _f(a.qty)
        if qty is None or qty <= 0 or qty != int(qty):
            raise TicketError("--qty wants the whole number of shares you hold, like --qty 33")
    else:
        pf = read_json(a.portfolio, "portfolio", required=False) or {}
        qty = sum(_f(p.get("shares", p.get("qty"))) or 0 for p in pf.get("positions", [])
                  if nse_symbol(p.get("ticker") or p.get("symbol")) == sym)
        source = a.portfolio
        if qty <= 1e-9:
            raise TicketError("nothing to flatten: no %s shares in %s. Check the Positions tab in Groww and "
                              "pass --qty if you do hold some" % (sym, a.portfolio))
    plan = read_json(a.plan, "plan", required=False) or {}
    flat_by = (plan.get("flat_by") if nse_symbol(plan.get("symbol")) == sym else None) \
        or cfg.get("flat_by") or "15:00"
    order = {"symbol": sym, "exchange": "NSE", "product": "MIS", "side": "sell", "qty": _str_num(qty),
             "type": "market", "validity": "day",
             "why": "flat by %s IST: closes the %s intraday position before Groww's 15:05 order cut-off and 15:20 "
                    "square-off (qty from %s)"
             % (flat_by, sym, source)}
    old = read_json(a.orders, "orders", required=False)
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    doc = {"created_by": "nse-groww", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "proposed",
           "note": "Flatten ticket. Nothing is placed: nse-risk checks it, then you place it yourself in Groww "
                   "and say yes.",
           "source": "ticket.py flatten %s on %s" % (sym, today), "orders": [order]}
    write_json(a.orders, doc)
    print("Flatten %s: %s (flat by %s IST)." % (sym, plain(order), flat_by))
    print("Wrote it to %s%s." % (a.orders, (" (it replaced %d earlier proposal%s)"
                                             % (replaced, "" if replaced == 1 else "s")) if replaced else ""))
    if a.qty is None:
        print("The qty comes from %s. Check it against the Positions tab in Groww before you sell." % source)
    print("Next:")
    print("  1. nse-risk: check.py. A sell that only closes what you hold is never blocked by the daily")
    print("     limits or the entry window.")
    print("  2. ticket.py show: cancel any pending stoploss/target for %s first, then sell, in Groww." % sym)
    print("  3. Your yes for that one order, then ticket.py fill --index 1 --price P.")
    print("  %s." % SQUARE_OFF_NOTE)
    return 0


def cmd_log(a):
    state = load_state(a.state)
    now, today = _clock(a)
    rows = [r for r in state.get("orders", []) if a.all or rec_day(r) == today]
    if not rows:
        print("No records%s." % ("" if a.all else " today"))
        return 1
    for r in rows:
        reply = (' reply "%s"' % r["reply"]) if r.get("reply") and r["status"] in ("placed", "declined") else ""
        fee = (" charges %s" % inr(r["charges"])) if r.get("charges") and r["status"] == "filled" else ""
        note = (" (%s)" % r["note"]) if r.get("note") else ""
        idx = ("#%s" % r["index"]) if r.get("index") is not None else "  "
        print("%s  %s %-9s %s%s%s%s" % (r.get("at"), idx, r.get("status"), r.get("plain"), reply, fee, note))
    placed = sum(1 for r in rows if r.get("status") == "placed")
    print("%d placed%s." % (placed, "" if a.all else " today"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Groww order tickets and your yes (nse-groww). Never places orders.")
    sub = ap.add_subparsers(dest="cmd")

    def clock(p):
        p.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-11-17 09:45\"")
        p.add_argument("--today", help="YYYY-MM-DD (default: today in India)")

    def common(p):
        p.add_argument("--orders", default="trader/orders.json")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--risk", default="trader/state/risk.json")
        p.add_argument("--state", default="trader/state/broker.json")
        clock(p)

    p = sub.add_parser("show", help="render the tickets")
    common(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p.add_argument("--separate-stop", action="store_true",
                   help="steps for an app without Add SL/TGT: the buy, then one stop-loss sell")
    p = sub.add_parser("placed", aliases=["approve"], help="record the user's yes: placed in Groww")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply", required=True, help="the user's reply, word for word")
    p = sub.add_parser("decline", help="record a no: not placed")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply")
    p = sub.add_parser("result", help="Groww rejected it, or you cancelled it before it filled")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--status", required=True, choices=["rejected", "cancelled"])
    p.add_argument("--note")
    p = sub.add_parser("fill", help="record an executed order in trader/portfolio.json")
    p.add_argument("--index", type=int, help="the order in trader/orders.json you placed (symbol, side, qty)")
    p.add_argument("--symbol")
    p.add_argument("--side", choices=["buy", "sell"])
    p.add_argument("--qty")
    p.add_argument("--price", required=True, help="the average price Groww shows for the executed order")
    p.add_argument("--date", help="YYYY-MM-DD (default: today in India)")
    p.add_argument("--now")
    p.add_argument("--no-charges", action="store_true", help="do not take Groww's estimated charges off the cash")
    p.add_argument("--orders", default="trader/orders.json")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--state", default="trader/state/broker.json")
    p = sub.add_parser("flatten", help="a sell ticket that closes a symbol's intraday position by flat_by")
    p.add_argument("--symbol", required=True)
    p.add_argument("--qty", help="shares you hold, from Groww's Positions tab (default: trader/portfolio.json)")
    p.add_argument("--orders", default="trader/orders.json", help="where the ticket is written")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json")
    clock(p)
    p = sub.add_parser("log", help="today's records")
    p.add_argument("--state", default="trader/state/broker.json")
    clock(p)
    p.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "placed": cmd_placed, "approve": cmd_placed, "decline": cmd_decline,
                "result": cmd_result, "fill": cmd_fill, "flatten": cmd_flatten, "log": cmd_log}[a.cmd](a)
    except TicketError as e:
        print("ticket.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
