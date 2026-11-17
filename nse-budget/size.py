#!/usr/bin/env python3
"""Position sizing for nse-budget. Standard library only, no network.

Sizes ONE intraday trade from trader/plan.json (written by nse-exit) to your
budget and your max loss per trade, Groww's charges included, then writes one
proposed NSE intraday (MIS) buy with its stoploss and target to
trader/orders.json. It never places anything: nse-risk checks the order and
nse-groww turns it into a ticket you type into the Groww app yourself.

    python3 size.py                                  # trader/plan.json + trader/config.json
    python3 size.py --write                          # write trader/orders.json
    python3 size.py --limit                          # a limit order at the plan's entry
    python3 size.py --limit 1105.70 --write          # a limit order at 1105.70
    python3 size.py --now "2026-11-17 09:45" --json  # pin the clock (IST)

How many shares. Whole shares only (NSE cash segment).

    shares = the smallest of
      max loss       the most shares whose loss at the stop, (entry - stop) x shares
                     PLUS Groww's round-trip charges at the stop, fits max_loss_per_trade_inr
      position cap   budget x max_position_pct / entry
      max order      max_order_inr / entry
      cash           (budget minus the cash buffer) x mis_leverage / entry
                     (and no more than the cash in trader/portfolio.json allows)

The entry it sizes at: a limit order's limit price; for a market order the
plan's entry, or the price in trader/prices.json if that is fresh (under 15
minutes old) and higher, because a price that ran up since the plan means a
bigger loss at the same stop. Zero shares (the stop is too far away for your
max loss) is a plain refusal: no order is written. So is a reward:risk under
min_reward_risk (the price ran toward the target since the plan).

Prices sit on the stock's NSE tick. NSE sets each stock's tick for the month
from its close on the last trading day of the previous month (NSE/CMTR/62174
as amended by CMTR/67133): under 250 0.01, under 1,000 0.05, under 5,000
0.10, under 10,000 0.50, under 20,000 1.00, else 5.00. nse-exit writes that
tick into the plan ("tick"); this tool uses it and carries it into the order.
Without one it falls back to the band of the entry price, which is only an
approximation (a stock that crossed a band edge since month-end keeps last
month's tick). A long's stop rounds DOWN onto the grid (away from the entry);
the entry and the target round to the nearest tick.

Groww intraday charges per executed order (groww.in/pricing/stocks, read
2026-11-17), all in config "costs": brokerage min(20, 0.1%) with a 5 / 2.5%
floor, STT 0.025% on the sell, NSE exchange 0.00307%, SEBI 0.0001%, IPFT
0.0001%, stamp duty 0.003% on the buy, GST 18% on brokerage + exchange + SEBI
+ IPFT. DP charges are zero for intraday.
"""

import argparse
import datetime
import json
import os
import sys
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

COSTS = {"brokerage_pct": 0.1, "brokerage_cap": 20, "brokerage_min": 5, "brokerage_min_pct": 2.5,
         "stt_sell_pct": 0.025, "exchange_pct": 0.00307, "sebi_pct": 0.0001, "ipft_pct": 0.0001,
         "stamp_buy_pct": 0.003, "gst_pct": 18}
# the shipped config.example.json; missing keys fall back to these
DEFAULTS = {
    "budget": None,
    "cash_buffer_pct": 10,
    "max_position_pct": 75,
    "max_order_inr": 40000,
    "min_order_inr": 5000,
    "max_loss_per_trade_inr": 500,
    "max_daily_loss_inr": 1500,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 1.0,
    "require_stop": True,
    "no_entries_before": "09:30",
    "no_entries_after": "14:30",
    "flat_by": "15:00",
    "product": "MIS",
    "exchange": "NSE",
    "mis_leverage": 1,
    "blocked": [],
    "allow_short": False,
    "holidays": [],
    "costs": dict(COSTS),
}
CHARGE_NAMES = (("brokerage", "brokerage"), ("stt", "STT (sell)"), ("exchange", "exchange (NSE)"),
                ("sebi", "SEBI turnover"), ("ipft", "IPFT"), ("stamp", "stamp duty (buy)"),
                ("gst", "GST"))
IST = datetime.timedelta(hours=5, minutes=30)
FRESH_MINUTES = 15
PAISA = Decimal("0.01")
SETUP_WORDS = {"orb": "opening-range breakout", "flag": "flag", "bounce": "oversold bounce"}
TICK_BANDS = ((Decimal("250"), Decimal("0.01")), (Decimal("1000"), Decimal("0.05")),
              (Decimal("5000"), Decimal("0.10")), (Decimal("10000"), Decimal("0.50")),
              (Decimal("20000"), Decimal("1.00")))
TOP_TICK = Decimal("5.00")
NSE_TICKS = (Decimal("0.01"), Decimal("0.05"), Decimal("0.10"), Decimal("0.50"), Decimal("1.00"), Decimal("5.00"))
TICK_WORDS = {"plan": "from the plan: NSE's tick for this month", "band": "from the entry's price band, an approximation"}


class SizeError(Exception):
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
        raise SizeError("--now wants a time like \"2026-11-17 09:45\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_utc(text):
    """"2026-11-17T04:15:13Z" -> naive UTC, or None."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def utc_iso(now_utc=None):
    now_utc = now_utc or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return now_utc.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- money and the tick grid

def D(x):
    """A Decimal from a number or string, without binary float noise (1096.6999999999998 -> 1096.7)."""
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def q2(x):
    return D(x).quantize(PAISA, rounding=ROUND_HALF_UP)


def _group(whole):
    """"12345678" -> "1,23,45,678" (Indian grouping: last three, then twos)."""
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
    """A price to type into the app: 1105.7 -> "1105.70"."""
    return format(q2(x), "f")


def tick_for(price):
    """NSE tick size for a price (NSE circular, effective 2025-04-15)."""
    p = D(price)
    for edge, tick in TICK_BANDS:
        if p < edge:
            return tick
    return TOP_TICK


def snap(price, tick, how="nearest"):
    """Put a price on the tick grid: "down" (a long's stop), "up", or "nearest" (half up)."""
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
        raise SizeError("\"costs\" in the config must be numbers")


def brokerage(value, c):
    """Groww intraday: min(cap, pct of value); if that is under the minimum, min(minimum, min_pct of value)."""
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
    out["total"] = sum(out[k] for k, _ in CHARGE_NAMES)
    return out


def round_trip_costs(entry, exit, qty, costs=None):
    """Groww's charges for buying qty at entry and selling them at exit (two executed orders).
    Returns {brokerage, stt, exchange, sebi, ipft, stamp, gst, total} in rupees, each to the paisa."""
    q = D(qty)
    return {k: float(v) for k, v in _charges(D(entry) * q, D(exit) * q, costs).items()}


def order_costs(price, qty, side, costs=None):
    """The charges on ONE executed order (a buy or a sell)."""
    value = D(price) * D(qty)
    return {k: float(v) for k, v in _charges(value if side == "buy" else Decimal("0"),
                                             value if side == "sell" else Decimal("0"), costs).items()}


def loss_at_stop(entry, stop, qty, costs=None):
    """(entry - stop) x qty plus the round-trip charges at the stop, as a Decimal."""
    return (D(entry) - D(stop)) * D(qty) + D(round_trip_costs(entry, stop, qty, costs)["total"])


def breakeven(entry, qty, tick, costs=None):
    """The lowest sell price on the grid at which the round trip stops losing money."""
    e, q, t = D(entry), D(qty), D(tick)

    def net(p):
        return (p - e) * q - D(round_trip_costs(e, p, q, costs)["total"])

    p = snap(e + D(round_trip_costs(e, e, q, costs)["total"]) / q, t, "up")
    for _ in range(100000):
        if net(p) >= 0:
            break
        p += t
    while p - t > e and net(p - t) >= 0:
        p -= t
    return p


# ---------------------------------------------------------------- inputs

def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SizeError("no %s at %s" % (what, path))
    except json.JSONDecodeError as e:
        raise SizeError("%s is not valid JSON: %s" % (path, e))


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        cfg.update(read_json(path, "config"))
    return cfg


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def nse_symbol(text):
    """" m&m.ns " -> "M&M". NSE symbols are A-Z, 0-9, & and -, up to 20 characters."""
    s = str(text or "").strip().upper()
    if s.endswith(".NS"):
        s = s[:-3]
    return s


def price_time(doc, sym):
    """When this symbol's price is from: the quote's own time (detail[sym].time), else when it
    was fetched (detail[sym].fetched), else the file's as_of."""
    det = ((doc or {}).get("detail") or {}).get(sym) or {}
    for key in ("time", "fetched"):
        t = parse_utc(det.get(key))
        if t:
            return t
    return parse_utc((doc or {}).get("as_of"))


def fresh_price(doc, sym, now_utc, minutes=FRESH_MINUTES):
    """(price, time_utc, fresh) for sym from a prices.json doc, or (None, None, False)."""
    if not isinstance(doc, dict):
        return None, None, False
    prices = doc.get("prices") or {}
    p = _f(prices.get(sym, prices.get(sym + ".NS")))
    if p is None or p <= 0:
        return None, None, False
    t = price_time(doc, sym)
    if t is None:
        return p, None, False
    age = (now_utc - t).total_seconds()
    return p, t, -60 <= age <= minutes * 60


# ---------------------------------------------------------------- sizing

def shares_for(rupees, per_share):
    """Whole shares that fit in rupees at per_share."""
    r, p = D(rupees), D(per_share)
    if p <= 0 or r <= 0:
        return 0
    return int((r / p).to_integral_value(rounding=ROUND_FLOOR))


def max_loss_shares(max_loss, entry, stop, costs):
    """The most whole shares whose loss at the stop, charges included, fits max_loss."""
    q = shares_for(max_loss, D(entry) - D(stop))
    while q > 0 and loss_at_stop(entry, stop, q, costs) > D(max_loss):
        q -= 1
    return q


def size_trade(plan, cfg, budget=None, limit=None, price=None, cash=None, today=None):
    """Pure function: one plan + your rules -> the sizing (a dict). qty may be 0.

    limit  the limit price for a limit order (None = market)
    price  a FRESH latest price; a market order is sized at the higher of it and the plan's entry
    cash   the cash in trader/portfolio.json, if there is one
    today  IST date (YYYY-MM-DD); a plan for another day is refused"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    costs = c.get("costs") or COSTS
    if not isinstance(plan, dict):
        raise SizeError("the plan is not a JSON object")
    sym = nse_symbol(plan.get("symbol"))
    if not sym:
        raise SizeError("the plan has no symbol (run nse-exit)")
    side = str(plan.get("side") or "buy").strip().lower()
    if side != "buy":
        raise SizeError("this pack sizes long (buy) intraday trades only; the plan says %r" % side)
    if str(c.get("product") or "MIS").upper() != "MIS":
        raise SizeError("\"product\" is %r in the config: this pack sizes intraday (MIS) trades only"
                        % c.get("product"))
    if str(c.get("exchange") or "NSE").upper() != "NSE":
        raise SizeError("\"exchange\" is %r in the config: this pack trades NSE only" % c.get("exchange"))
    if today and plan.get("date") and str(plan["date"]) != today:
        raise SizeError("the plan is for %s and today is %s in India: make a new plan with nse-exit"
                        % (plan["date"], today))
    entry_plan, stop_in, target_in = _f(plan.get("entry")), _f(plan.get("stop")), _f(plan.get("target"))
    for name, v in (("entry", entry_plan), ("stop", stop_in), ("target", target_in)):
        if v is None or v <= 0:
            raise SizeError("the plan needs a positive %s (run nse-exit); the order needs entry, stop "
                            "and target" % name)
    blocked = {nse_symbol(b) for b in (c.get("blocked") or [])}
    if sym in blocked:
        raise SizeError("%s is on your blocked list" % sym)

    budget = D(budget if budget is not None else (c.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json")
    buf_pct = D(c.get("cash_buffer_pct") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    if _f(c.get("mis_leverage", 1)) is None or _f(c.get("mis_leverage", 1)) < 1:
        raise SizeError("mis_leverage must be 1 (no leverage) or more, got %r" % (c.get("mis_leverage"),))
    lev = D(c.get("mis_leverage", 1))

    plan_tick = plan.get("tick")
    if plan_tick is not None:
        if _f(plan_tick) is None or D(plan_tick) not in NSE_TICKS:
            raise SizeError("the plan's tick %r is not an NSE tick size (0.01, 0.05, 0.10, 0.50, 1.00 or 5.00)"
                            % (plan_tick,))
        tick, tick_from = D(plan_tick).quantize(PAISA), "plan"
    if limit is not None:
        if plan_tick is None:
            tick, tick_from = tick_for(limit), "band"
        if not on_grid(limit, tick):
            raise SizeError("the limit %s is off %s's %s tick (%s): use %s or %s"
                            % (px(limit), sym, px(tick), TICK_WORDS[tick_from], px(snap(limit, tick, "down")),
                               px(snap(limit, tick, "up"))))
        ref, ref_from = q2(limit), "limit"
    else:
        if plan_tick is None:
            tick, tick_from = tick_for(entry_plan), "band"
        plan_ref = snap(entry_plan, tick)
        if price is not None and snap(price, tick) > plan_ref:
            ref, ref_from = snap(price, tick), "price"
        else:
            ref, ref_from = plan_ref, "plan"
    if plan_tick is None:
        tick = tick_for(ref)
    stop, target = snap(stop_in, tick, "down"), snap(target_in, tick)
    if stop >= ref:
        raise SizeError("the stop %s is not below the entry %s; a long trade needs its stop underneath"
                        % (px(stop), px(ref)))
    if target <= ref:
        raise SizeError("the target %s is not above the entry %s: the move has already happened. "
                        "Skip it or make a new plan with nse-exit" % (px(target), px(ref)))
    risk = ref - stop
    reward = target - ref

    cash_buffer = q2(budget * buf_pct / 100)
    available = budget - cash_buffer
    cash_note = "budget %s minus the %s cash buffer" % (inr(budget), inr(cash_buffer))
    if cash is not None and D(cash) - cash_buffer < available:
        available = D(cash) - cash_buffer
        cash_note = "cash %s in trader/portfolio.json minus the %s buffer" % (inr(cash), inr(cash_buffer))
    available = max(q2(available), Decimal("0"))
    buying_power = available * lev

    limits = []
    max_loss = _f(c.get("max_loss_per_trade_inr"))
    if max_loss:
        n = max_loss_shares(max_loss, ref, stop, costs)
        if n:
            how = "%s incl. charges: %d x %s + %s charges = %s" % (
                inr(max_loss), n, inr(risk), inr(round_trip_costs(ref, stop, n, costs)["total"]),
                inr(loss_at_stop(ref, stop, n, costs)))
        else:
            how = "%s incl. charges: 1 share loses %s at the stop" % (inr(max_loss), inr(loss_at_stop(ref, stop, 1, costs)))
        limits.append({"name": "max loss", "inr": max_loss, "per": float(risk), "shares": n, "how": how})
    cap_pct = _f(c.get("max_position_pct"))
    if cap_pct:
        cap = q2(budget * D(cap_pct) / 100)
        limits.append({"name": "position cap", "inr": float(cap), "per": float(ref), "shares": shares_for(cap, ref),
                       "how": "%s%% of %s = %s / %s" % (_num(cap_pct), inr(budget), inr(cap), inr(ref))})
    max_order = _f(c.get("max_order_inr"))
    if max_order:
        limits.append({"name": "max order", "inr": max_order, "per": float(ref),
                       "shares": shares_for(max_order, ref), "how": "%s / %s" % (inr(max_order), inr(ref))})
    if lev > 1:
        how = "%s = %s x mis_leverage %s = %s / %s" % (cash_note, inr(available), _num(lev), inr(buying_power), inr(ref))
    else:
        how = "%s = %s / %s" % (cash_note, inr(available), inr(ref))
    limits.append({"name": "cash", "inr": float(buying_power), "per": float(ref),
                   "shares": shares_for(buying_power, ref), "how": how})

    qty = max(0, min(l["shares"] for l in limits))
    bound = [l["name"] for l in limits if l["shares"] == qty]
    for l in limits:
        l["binds"] = l["name"] == bound[0]
    cost = q2(ref * qty)
    at_stop = round_trip_costs(ref, stop, qty, costs) if qty else None
    at_target = round_trip_costs(ref, target, qty, costs) if qty else None
    loss = q2(loss_at_stop(ref, stop, qty, costs)) if qty else Decimal("0")
    win = q2(reward * qty - D(at_target["total"])) if qty else Decimal("0")
    be = breakeven(ref, qty, tick, costs) if qty else None
    refusal = None
    if qty == 0:
        if bound[0] == "max loss":
            refusal = ("one share loses %s at the stop (entry %s minus stop %s, plus Groww's charges), more than "
                       "your %s max loss per trade. No order. Skip this trade, or plan a closer stop with nse-exit."
                       % (inr(loss_at_stop(ref, stop, 1, costs)), px(ref), px(stop), inr(max_loss)))
        else:
            refusal = ("one share of %s costs %s, more than your %s limit (%s). No order."
                       % (sym, inr(ref), bound[0], limits[[l["name"] for l in limits].index(bound[0])]["how"]))
    elif _f(c.get("min_reward_risk")) and reward < D(c["min_reward_risk"]) * risk:
        refusal = ("reward:risk %.2f (target %s, entry %s, stop %s) is under your minimum %s, so nse-risk "
                   "would block it. No order. Skip it, or make a new plan with nse-exit."
                   % (float(reward / risk), px(target), px(ref), px(stop), _num(c["min_reward_risk"])))
    elif cost < D(c.get("min_order_inr") or 0):
        refusal = ("%d share%s of %s is %s, under your %s minimum order. No order."
                   % (qty, "" if qty == 1 else "s", sym, inr(cost), inr(c["min_order_inr"])))
    return {
        "symbol": sym, "side": "buy", "setup": plan.get("setup", ""), "date": plan.get("date"),
        "entry_time": plan.get("entry_time", ""), "entry_plan": float(q2(entry_plan)),
        "entry": float(ref), "entry_from": ref_from, "stop": float(stop), "target": float(target),
        "tick": float(tick), "tick_from": tick_from,
        "tick_basis": plan.get("tick_basis") if isinstance(plan.get("tick_basis"), dict) else None,
        "risk_per_share": float(risk), "reward_risk": round(float(reward / risk), 2),
        "stop_label": plan.get("stop_label", ""), "target_label": plan.get("target_label", ""),
        "budget": float(budget), "cash_buffer_pct": float(buf_pct), "cash_buffer": float(cash_buffer),
        "cash_available": float(available), "mis_leverage": float(lev),
        "max_loss_per_trade_inr": max_loss, "limits": limits, "qty": qty, "bound_by": bound[0],
        "cost": float(cost), "margin": float(q2(cost / lev)), "max_loss": float(loss),
        "charges_at_stop": at_stop, "charges_at_target": at_target, "win_at_target": float(win),
        "breakeven": float(be) if be is not None else None,
        "flat_by": plan.get("flat_by") or c.get("flat_by"),
        "order_type": "limit" if limit is not None else "market", "refusal": refusal,
    }


def _num(x):
    x = float(x)
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    return ("%.4f" % x).rstrip("0").rstrip(".")


def proposed_order(s):
    """The one proposed order for orders.json: an NSE intraday (MIS) buy with its stoploss and
    target, the way Groww's "Add SL/TGT" takes them; "why" and "plan" are notes for the next tools."""
    if s["refusal"] or s["qty"] <= 0:
        return None
    o = {"symbol": s["symbol"], "exchange": "NSE", "product": "MIS", "side": "buy",
         "qty": str(int(s["qty"])), "type": s["order_type"], "validity": "day"}
    if s["order_type"] == "limit":
        o["limit_price"] = px(s["entry"])
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "intraday trade")
    bound = {"max loss": "%s max loss per trade" % inr(s["max_loss_per_trade_inr"] or 0),
             "position cap": "position cap", "max order": "max order",
             "cash": "cash after the buffer"}[s["bound_by"]]
    why = ("%s on %s: %d shares, bound by your %s (%d x %s = %s at the stop + %s Groww charges = %s)"
           % (setup, s["symbol"], s["qty"], bound, s["qty"], inr(s["risk_per_share"]),
              inr(s["risk_per_share"] * s["qty"]), inr(s["charges_at_stop"]["total"]), inr(s["max_loss"])))
    o.update({
        "stop_loss_price": px(s["stop"]),
        "target_price": px(s["target"]),
        "tick": px(s["tick"]),
        "why": why,
        "plan": {"entry": s["entry"], "stop": s["stop"], "target": s["target"], "tick": s["tick"],
                 "tick_from": s["tick_from"], "risk_per_share": s["risk_per_share"], "max_loss": s["max_loss"],
                 "charges_at_stop": s["charges_at_stop"]["total"],
                 "charges_at_target": s["charges_at_target"]["total"], "breakeven": s["breakeven"],
                 "setup": s["setup"], "entry_time": s["entry_time"], "flat_by": s["flat_by"]},
    })
    if s.get("tick_basis"):
        o["plan"]["tick_basis"] = s["tick_basis"]
    return o


def charges_table(s):
    a, b = s["charges_at_stop"], s["charges_at_target"]
    lines = ["Groww charges, estimated (two orders: buy %d at %s, then sell %d):"
             % (s["qty"], px(s["entry"]), s["qty"]),
             "  %-18s %22s %24s" % ("", "at the stop " + px(s["stop"]), "at the target " + px(s["target"]))]
    for key, name in CHARGE_NAMES:
        lines.append("  %-18s %22s %24s" % (name, inr(a[key]), inr(b[key])))
    lines.append("  %-18s %22s %24s" % ("total", inr(a["total"]), inr(b["total"])))
    return lines


def plan_report(s, order, notes=()):
    lines = []
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "setup")
    lines.append("%s buy, %s, entry %s%s" % (s["symbol"], setup, inr(s["entry_plan"]),
                                            (" at " + s["entry_time"]) if s["entry_time"] else ""))
    lines.append("Stop %s%s. Target %s%s." % (inr(s["stop"]), (": " + s["stop_label"]) if s["stop_label"] else "",
                                             inr(s["target"]),
                                             (": " + s["target_label"]) if s["target_label"] else ""))
    for n in notes:
        lines.append(n)
    src = {"plan": "the plan's entry", "price": "the latest price, above the plan's entry",
           "limit": "your limit price"}[s["entry_from"]]
    lines.append("Sized at %s (%s). Risk %s a share (entry %s minus stop %s). Reward:risk %.2f."
                 % (inr(s["entry"]), src, inr(s["risk_per_share"]), px(s["entry"]), px(s["stop"]), s["reward_risk"]))
    lines.append("NSE tick %s (%s): every price above sits on it." % (inr(s["tick"]), TICK_WORDS[s["tick_from"]]))
    lines.append("")
    lines.append("Limits (whole shares; the smallest one wins):")
    for l in s["limits"]:
        lines.append("  %-13s %-74s %3d shares%s" % (l["name"], l["how"], l["shares"],
                                                      "  <- binds" if l["binds"] else ""))
    lines.append("")
    if order is None:
        lines.append("No order: " + s["refusal"])
        lines.append("Education and tooling, not financial advice.")
        return "\n".join(lines)
    lev = s["mis_leverage"]
    lines.append("Size: %d shares of %s = %s at %s, intraday (MIS)%s."
                 % (s["qty"], s["symbol"], inr(s["cost"]), inr(s["entry"]),
                    "" if lev <= 1 else ", Groww blocks about %s at mis_leverage %s" % (inr(s["margin"]), _num(lev))))
    lines.append("")
    lines.extend(charges_table(s))
    lines.append("")
    lines.append("Max loss at the stop: %d x %s = %s + %s charges = %s (your limit %s)."
                 % (s["qty"], inr(s["risk_per_share"]), inr(s["risk_per_share"] * s["qty"]),
                    inr(s["charges_at_stop"]["total"]), inr(s["max_loss"]),
                    inr(s["max_loss_per_trade_inr"] or 0)))
    lines.append("At the target: %d x %s = %s - %s charges = %s."
                 % (s["qty"], inr(s["target"] - s["entry"]), inr((s["target"] - s["entry"]) * s["qty"]),
                    inr(s["charges_at_target"]["total"]), inr(s["win_at_target"])))
    move = s["breakeven"] - s["entry"]
    lines.append("Breakeven: %s has to reach %s (+%s a share, %.2f%%) before this trade makes a rupee."
                 % (s["symbol"], inr(s["breakeven"]), inr(move), 100.0 * move / s["entry"]))
    lines.append("A stop is an order, not a promise: in a fast market it can fill below %s." % px(s["stop"]))
    lines.append("")
    lines.append("Proposed order (nothing has been placed):")
    how = "limit %s" % order["limit_price"] if order["type"] == "limit" else "market"
    lines.append("  BUY %s %s, NSE, intraday (MIS), %s, day, stoploss %s, target %s, tick %s"
                 % (order["qty"], order["symbol"], how, order["stop_loss_price"], order["target_price"],
                    order["tick"]))
    if s["flat_by"]:
        lines.append("Flat by %s IST: anything still open then gets closed (nse-alerts rings, nse-groww flatten). "
                     "Groww stops new intraday orders at 15:05 for F&O stocks and squares off open intraday "
                     "positions from 15:20 for ₹50 + GST each." % s["flat_by"])
    lines.append("")
    lines.append("Next: nse-risk checks it, then nse-groww shows the ticket you place in the Groww app yourself.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- orders file

def write_orders(path, orders, source="", now_utc=None):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    doc = {
        "created_by": "nse-budget",
        "created_at": utc_iso(now_utc),
        "status": "proposed",
        "note": ("Proposals only. Nothing is placed: nse-risk checks each order, then you place it "
                 "yourself in the Groww app and say yes in the chat."),
        "source": source,
        "orders": orders,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- main

def run_plan(a, cfg):
    now = parse_now(a.now)
    today = to_ist(now).date().isoformat()
    plan = read_json(a.plan, "plan (run nse-exit first)")
    limit = None
    if a.limit is not None:
        if a.limit == "plan":
            limit = _f((plan or {}).get("entry"))
            if limit:
                limit = float(snap(limit, tick_for(limit)))
        else:
            limit = _f(a.limit)
            if limit is None or limit <= 0:
                raise SizeError("--limit wants a price, like --limit 1105.70")
    sym = nse_symbol((plan or {}).get("symbol"))
    notes, price = [], None
    ppath = a.prices or ("trader/prices.json" if os.path.exists("trader/prices.json") else None)
    if ppath and limit is None and sym:
        pdoc = read_json(ppath, "prices")
        p, t, fresh = fresh_price(pdoc, sym, now)
        entry = _f(plan.get("entry")) or 0
        if p is not None and fresh:
            price = p
            if entry and snap(p, tick_for(entry)) > snap(entry, tick_for(entry)):
                notes.append("%s last %s (as of %s IST) is above the plan's entry %s, so the risk is bigger: "
                             "sized at %s." % (sym, px(snap(p, tick_for(entry))), to_ist(t).strftime("%H:%M"),
                                               px(entry), px(snap(p, tick_for(entry)))))
        elif p is not None:
            notes.append("%s in %s is %s: not used (older than %d minutes). Sized at the plan's entry."
                         % (sym, ppath, ("from " + to_ist(t).strftime("%Y-%m-%d %H:%M") + " IST")
                            if t else "undated", FRESH_MINUTES))
    cash = None
    if a.portfolio and os.path.exists(a.portfolio):
        pf = read_json(a.portfolio, "portfolio")
        cash = _f(pf.get("cash")) if isinstance(pf, dict) else None
    s = size_trade(plan, cfg, a.budget, limit, price, cash, today)
    order = proposed_order(s)
    if order and a.write:
        src = "trader/plan.json (%s %s %s)" % (s["symbol"], s["setup"] or "plan", s["date"] or today)
        write_orders(a.write, [order], src, now)
    if a.json:
        print(json.dumps({"sizing": s, "orders": [order] if order else [], "notes": notes}, indent=2,
                         ensure_ascii=False))
    else:
        print(plan_report(s, order, notes))
        if order and a.write:
            print("Wrote 1 proposed order to %s" % a.write)
    return 0 if order else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Size an NSE intraday trade to your budget and max loss (nse-budget).")
    ap.add_argument("--plan", default="trader/plan.json", help="the trade plan (default trader/plan.json)")
    ap.add_argument("--config", default="trader/config.json", help="rules (default trader/config.json)")
    ap.add_argument("--budget", type=float, help="override the budget in the config (rupees)")
    ap.add_argument("--limit", nargs="?", const="plan", metavar="PRICE",
                    help="a limit order instead of market, at the plan's entry or at PRICE")
    ap.add_argument("--prices", help="prices JSON (default trader/prices.json if it exists)")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; its cash caps the size if it exists")
    ap.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-11-17 09:45\"")
    ap.add_argument("--write", nargs="?", const="trader/orders.json", metavar="PATH",
                    help="write the proposed order (default trader/orders.json)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the report")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config)
        return run_plan(a, cfg)
    except SizeError as e:
        print("size.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
