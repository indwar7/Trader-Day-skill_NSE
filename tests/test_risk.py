import copy
import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

risk = load("nse-risk", "check")
ticket = load("nse-groww", "ticket")

CFG_PATH = os.path.join(ROOT, "nse-budget", "config.example.json")
SBIN_PATH = os.path.join(FIX, "nse-risk", "orders-sbin.json")
NOW = "2026-11-17 09:45"          # IST, Tuesday
TODAY = "2026-11-17"


def at(text):
    """An IST wall-clock time -> naive UTC, the way --now reads it."""
    return risk.parse_now(text)


def cfg(**over):
    with open(CFG_PATH) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def sbin(**over):
    with open(SBIN_PATH, encoding="utf-8") as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def untick(o):
    """The order without a tick of its own, as an older nse-budget wrote it."""
    o = dict(o)
    o.pop("tick", None)
    o["plan"] = {k: v for k, v in (o.get("plan") or {}).items() if k not in ("tick", "tick_from", "tick_basis")}
    return o


def order(sym, qty, entry, stop, target, **over):
    o = {"symbol": sym, "exchange": "NSE", "product": "MIS", "side": "buy", "qty": str(qty), "type": "market",
         "validity": "day", "stop_loss_price": stop, "target_price": target,
         "plan": {"entry": float(entry), "stop": float(stop), "target": float(target)}}
    o.update(over)
    return o


def sell(sym, qty, **over):
    o = {"symbol": sym, "exchange": "NSE", "product": "MIS", "side": "sell", "qty": str(qty), "type": "market",
         "validity": "day"}
    o.update(over)
    return o


def placed(sym, side="buy", day=TODAY, max_loss=357.14, kind=None, status="placed", fp=None):
    r = {"at": day + "T09:46:00+05:30", "date": day, "symbol": sym, "side": side, "status": status,
         "max_loss": max_loss, "fingerprint": fp or ("fp-%s-%s" % (sym, side))}
    if kind:
        r["kind"] = kind
    return r


def holding(sym, shares, cost):
    return {"cash": 13511.90, "positions": [{"ticker": sym, "shares": shares,
                                            "lots": [{"date": TODAY, "shares": shares, "cost": cost}]}]}


def verdicts(res):
    return [r["verdict"] for r in res["results"]]


def reasons(res, i=0):
    return " | ".join(res["results"][i]["reasons"])


def warnings(res, i=0):
    return " | ".join(res["results"][i]["warnings"])


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = risk.main(argv)
    return code, out.getvalue(), err.getvalue()


class TheExample(unittest.TestCase):
    def test_the_sbin_order_passes_every_check(self):
        res = risk.check([sbin()], cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        r = res["results"][0]
        self.assertEqual((r["kind"], r["entry_ref"], r["entry_ref_from"], r["tick"], r["tick_from"]),
                         ("entry", 1105.7, "plan", 0.05, "order"))
        self.assertEqual((r["risk_per_share"], r["max_loss"], r["reward_risk"]), (9.0, 357.14, 1.0))
        self.assertEqual(r["charges_at_stop"]["total"], 60.14)
        self.assertEqual(r["value_inr"], 36488.10)
        self.assertEqual(res["cash_after"], 13511.90)
        self.assertEqual(res["now_ist"], "2026-11-17 09:45")
        self.assertIn("max loss ₹357.14 = ₹297.00 + ₹60.14 Groww charges (limit ₹500.00)", " ".join(r["info"]))
        self.assertEqual(res["warnings"], [])

    def test_the_charges_tip_it_over_the_max_loss(self):
        # 49 x 9.00 = 441.00 is under 500 on the price alone; with 66.15 of Groww charges it is not
        c = cfg(budget=200000, max_order_inr=100000)
        res = risk.check([sbin(qty="49")], c, now=at(NOW))
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertEqual(len(res["results"][0]["reasons"]), 1, reasons(res))
        self.assertIn("loss: 49 shares x ₹9.00 risk a share = ₹441.00 + ", reasons(res))
        self.assertIn("Groww charges = ₹507.", reasons(res))
        self.assertIn("over your ₹500.00 max loss per trade", reasons(res))
        self.assertEqual(verdicts(risk.check([sbin(qty="48")], c, now=at(NOW))), ["PASS"])

    def test_fingerprint_is_shared_with_the_ticket(self):
        for o in (sbin(), order("M&M", 11, 3392.0, "3372.20", "3411.80"), sell("BAJAJ-AUTO", 3)):
            self.assertEqual(risk.fingerprint(o), ticket.fingerprint(o))
        self.assertEqual(risk.ORDER_FIELDS, ticket.ORDER_FIELDS)
        base = risk.fingerprint(sbin())
        self.assertEqual(risk.fingerprint(sbin(why="other words", plan={})), base)   # notes are not the order
        for k, v in (("qty", "34"), ("stop_loss_price", "1096.60"), ("product", "CNC"), ("exchange", "BSE"),
                     ("tick", "0.10"),
                     ("target_price", "1114.80"), ("type", "limit")):
            self.assertNotEqual(risk.fingerprint(sbin(**{k: v})), base, k)
        res = risk.check([sbin()], cfg(), now=at(NOW))
        self.assertEqual(res["results"][0]["fingerprint"], ticket.fingerprint(sbin()))

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "risk.json")
            code, text, _ = run(["--orders", SBIN_PATH, "--config", CFG_PATH, "--portfolio", os.path.join(d, "x"),
                                 "--prices", os.path.join(d, "x"), "--state", os.path.join(d, "x"),
                                 "--now", NOW, "--out", out])
            self.assertEqual(code, 0)
            self.assertIn("PASS  #1 buy SBIN 33 shares, entry", text)
            self.assertIn("1 passed, 0 blocked at 2026-11-17 09:45 IST. Cash after the passed orders: ₹13,511.90.",
                          text)
            self.assertIn("not financial advice", text)
            with open(out, encoding="utf-8") as fh:
                rec = json.load(fh)
            self.assertEqual((rec["date"], rec["pass"], rec["block"]), (TODAY, 1, 0))
            self.assertEqual(rec["results"][0]["fingerprint"], ticket.fingerprint(sbin()))
            code, text, _ = run(["--orders", SBIN_PATH, "--config", CFG_PATH, "--portfolio", os.path.join(d, "x"),
                                 "--prices", os.path.join(d, "x"), "--state", os.path.join(d, "x"),
                                 "--now", "2026-11-17 09:20", "--no-write", "--json"])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(text)["block"], 1)


class TheWindowIsIST(unittest.TestCase):
    def verdict(self, when, c=None, orders=None):
        res = risk.check(orders or [sbin()], c or cfg(), now=at(when))
        return res["results"][0]["verdict"], reasons(res), res

    def test_entry_window(self):
        cases = [
            ("2026-11-17 09:10", "BLOCK", "the market is closed (Tue 2026-11-17 09:10 IST; the NSE session is 09:15 to 15:30)"),
            ("2026-11-17 09:15", "BLOCK", "no new entries before 09:30 IST (it is 09:15)"),
            ("2026-11-17 09:29", "BLOCK", "no new entries before 09:30 IST"),
            ("2026-11-17 09:30", "PASS", ""),
            ("2026-11-17 14:30", "PASS", ""),
            ("2026-11-17 14:31", "BLOCK", "no new entries after 14:30 IST (it is 14:31)"),
            ("2026-11-17 14:59", "BLOCK", "no new entries after 14:30 IST"),
            ("2026-11-17 15:00", "BLOCK", "at or past your flat-by 15:00 IST: exits only"),
            ("2026-11-17 15:30", "BLOCK", "the market is closed"),
        ]
        for when, want, why in cases:
            v, r, _ = self.verdict(when)
            self.assertEqual(v, want, "%s: %s" % (when, r))
            self.assertIn(why, r, when)

    def test_the_clock_is_ist_not_the_machine(self):
        v, _, res = self.verdict("2026-11-17T04:15:00Z")            # 09:45 IST
        self.assertEqual((v, res["now_ist"]), ("PASS", "2026-11-17 09:45"))
        v, r, _ = self.verdict("2026-11-17T03:55:00+00:00")         # 09:25 IST
        self.assertIn("before 09:30 IST", r)
        self.assertEqual(risk.to_ist(datetime.datetime(2026, 11, 16, 19, 0)).isoformat(), "2026-11-17T00:30:00")
        self.assertEqual(risk.ist_to_utc(datetime.datetime(2026, 11, 17, 9, 15)).isoformat(), "2026-11-17T03:45:00")

    def test_weekends_and_nse_holidays(self):
        v, r, _ = self.verdict("2026-11-21 10:00")                  # Saturday
        self.assertIn("closed today (weekend", r)
        v, r, _ = self.verdict("2026-11-24 10:00")                  # Guru Nanak Jayanti
        self.assertEqual(v, "BLOCK")
        self.assertIn("NSE holiday: Prabhu Shri Guru Nanak Dev Jayanti", r)
        v, r, _ = self.verdict("2026-01-15 10:00")
        self.assertIn("Maharashtra municipal elections", r)
        self.assertEqual(len(risk.NSE_HOLIDAYS), 16)
        self.assertTrue(all(datetime.date.fromisoformat(d).weekday() < 5 for d in risk.NSE_HOLIDAYS))
        self.assertEqual(risk.CALENDAR_YEARS, (2026,))

    def test_config_holidays_are_added(self):
        v, r, _ = self.verdict("2026-11-18 10:00", cfg(holidays=["2026-11-18"]))
        self.assertEqual(v, "BLOCK")
        self.assertIn("a holiday in your config", r)
        with self.assertRaises(risk.RiskError):
            risk.check([sbin()], cfg(holidays=["18 Nov"]), now=at("2026-11-18 10:00"))

    def test_2027_is_not_loaded_but_weekdays_trade(self):
        v, r, res = self.verdict("2027-01-04 10:00")                # Monday
        self.assertEqual(v, "PASS", r)
        self.assertIn("NSE holiday list for 2027 not loaded: check nseindia.com", res["warnings"])
        v, r, res = self.verdict("2027-01-09 10:00")                # Saturday
        self.assertIn("weekend", r)

    def test_flat_by_wins_over_a_later_entry_cutoff(self):
        c = cfg(no_entries_after="15:15")
        v, r, res = self.verdict("2026-11-17 15:02", c)
        self.assertIn("past your flat-by 15:00", r)
        self.assertTrue(any("15:05 for F&O stocks" in w for w in res["warnings"]))
        v, r, res = self.verdict("2026-11-17 14:50", c)
        self.assertEqual(v, "PASS", r)

    def test_the_window_can_be_switched_off(self):
        c = cfg(no_entries_before=None, no_entries_after=None, flat_by=None)
        v, r, res = self.verdict("2026-11-21 10:00", c)
        self.assertEqual(v, "PASS", r)
        self.assertTrue(any("window is off" in w for w in res["warnings"]))
        with self.assertRaises(risk.RiskError):
            risk.check([sbin()], cfg(flat_by="3:00 pm"), now=at(NOW))


class IntradayChecks(unittest.TestCase):
    def test_a_stop_is_required(self):
        o = sbin()
        del o["stop_loss_price"]
        o["plan"] = {}
        res = risk.check([o], cfg(), now=at(NOW), prices={"SBIN": 1105.7})
        self.assertIn("stop: no stop", reasons(res))
        res = risk.check([o], cfg(require_stop=False, min_reward_risk=None), now=at(NOW), prices={"SBIN": 1105.7})
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertIn("no stop", warnings(res))

    def test_a_plan_stop_counts_but_warns(self):
        o = sbin()
        del o["stop_loss_price"]
        res = risk.check([o], cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertIn("in the plan only", warnings(res))
        self.assertIn("Add SL/TGT", warnings(res))

    def test_stop_and_target_on_the_right_side(self):
        res = risk.check([sbin(stop_loss_price="1110.00")], cfg(), now=at(NOW))
        self.assertIn("stop: the stop 1110.00 must be below the entry 1105.70", reasons(res))
        res = risk.check([sbin(target_price="1100.00")], cfg(), now=at(NOW))
        self.assertIn("target: the target 1100.00 must be above the entry 1105.70", reasons(res))

    def test_reward_risk(self):
        res = risk.check([sbin(target_price="1110.00")], cfg(), now=at(NOW))
        self.assertIn("reward:risk: 0.48 (target 1110.00, entry 1105.70, stop 1096.70) is under your minimum 1",
                      reasons(res))

    def test_a_fresh_higher_price_is_the_entry(self):
        times = {"SBIN": at("2026-11-17 09:44")}
        res = risk.check([sbin()], cfg(), now=at(NOW), prices={"SBIN": 1109.0}, price_times=times)
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["entry_ref_from"], r["risk_per_share"]), (1109.0, "price", 12.3))
        self.assertIn("reward:risk: 0.46", reasons(res))
        # a lower price never shrinks the risk
        res = risk.check([sbin()], cfg(), now=at(NOW), prices={"SBIN": 1101.0}, price_times=times)
        self.assertEqual(res["results"][0]["entry_ref"], 1105.7)

    def test_a_stale_price_is_not_used(self):
        times = {"SBIN": at("2026-11-17 09:20")}
        res = risk.check([sbin()], cfg(), now=at(NOW), prices={"SBIN": 1109.0}, price_times=times)
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertIn("older than 15 minutes", warnings(res))

    def test_a_limit_order_uses_its_limit_price(self):
        o = sbin(type="limit", limit_price="1104.00")
        res = risk.check([o], cfg(), now=at(NOW))
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["entry_ref_from"], r["risk_per_share"]), (1104.0, "limit", 7.3))

    def test_daily_loss_and_trades_per_day(self):
        state = {"orders": [placed("INFY", max_loss=480.0), placed("HDFCBANK", max_loss=490.0)]}
        res = risk.check([sbin()], cfg(), now=at(NOW), state=state)
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        state["orders"].append(placed("RELIANCE", max_loss=400.0))
        res = risk.check([sbin()], cfg(), now=at(NOW), state=state)
        self.assertIn("daily loss: ₹1,370.00 already at risk today plus ₹357.14 for this one = ₹1,727.14, over "
                      "your ₹1,500.00 daily loss limit", reasons(res))
        self.assertIn("trades: 3 entries placed today", reasons(res))

    def test_rejected_and_cancelled_orders_stop_counting(self):
        state = {"orders": [placed("INFY", max_loss=700.0), placed("HDFCBANK", max_loss=700.0)]}
        self.assertIn("daily loss", reasons(risk.check([sbin()], cfg(), now=at(NOW), state=state)))
        state["orders"].append(placed("HDFCBANK", status="rejected", fp="fp-HDFCBANK-buy"))
        res = risk.check([sbin()], cfg(), now=at(NOW), state=state)
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertEqual(res["today"]["entries_placed"], 1)

    def test_exits_declines_and_other_days_do_not_count_as_trades(self):
        state = {"orders": [placed("INFY", side="sell", kind="exit"), placed("HDFCBANK", status="declined"),
                            placed("RELIANCE", day="2026-11-16"), placed("ITC")]}
        res = risk.check([sbin()], cfg(), now=at(NOW), state=state)
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertEqual(res["today"]["entries_placed"], 1)
        self.assertEqual(res["today"]["orders_placed"], 2)

    def test_the_batch_counts_toward_the_daily_limits(self):
        mm = order("M&M", 11, 3392.0, "3372.20", "3411.80")
        res = risk.check([sbin(), mm], cfg(budget=200000), now=at(NOW),
                         state={"orders": [placed("INFY", max_loss=900.0)]})
        self.assertEqual(verdicts(res), ["PASS", "BLOCK"])
        self.assertIn("daily loss: ₹1,257.14 already at risk today", reasons(res, 1))

    def test_duplicates(self):
        res = risk.check([sbin()], cfg(), now=at(NOW), state={"orders": [placed("SBIN")]})
        self.assertIn("duplicate: a buy SBIN was already placed today", reasons(res))
        res = risk.check([sbin(), sbin(qty="5")], cfg(budget=200000), now=at(NOW))
        self.assertIn("duplicate: a second buy SBIN in this batch", reasons(res, 1))

    def test_cash_buffer_position_cap_and_leverage(self):
        res = risk.check([sbin(qty="40")], cfg(max_order_inr=None), now=at(NOW))
        self.assertIn("position: SBIN would be ₹44,228.00, 88.5% of the ₹50,000.00 account, over your 75% cap",
                      reasons(res))
        res = risk.check([sbin(qty="44")], cfg(max_order_inr=None, max_position_pct=100,
                                               max_loss_per_trade_inr=1000), now=at(NOW))
        self.assertIn("cash: leaves ₹1,349.20 cash, under your 10% buffer of ₹5,000.00", reasons(res))
        res = risk.check([sbin(qty="44")], cfg(max_order_inr=None, max_position_pct=100, mis_leverage=5,
                                               max_loss_per_trade_inr=1000), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertTrue(any("mis_leverage 5" in w for w in res["warnings"]))
        self.assertEqual(res["cash_after"], round(50000 - 44 * 1105.7 / 5, 2))
        with self.assertRaises(risk.RiskError):
            risk.check([sbin()], cfg(mis_leverage=0), now=at(NOW))


class Exits(unittest.TestCase):
    def test_getting_out_is_never_blocked_by_the_day_limits(self):
        state = {"orders": [placed("SBIN"), placed("INFY", max_loss=900.0), placed("ITC", max_loss=500.0)]}
        pf = holding("SBIN", 33, 36488.10)
        for when in ("2026-11-17 14:45", "2026-11-17 15:02"):
            res = risk.check([sell("SBIN", 33)], cfg(), portfolio=pf, now=at(when), state=state)
            self.assertEqual(verdicts(res), ["PASS"], reasons(res))
            self.assertEqual(res["results"][0]["kind"], "exit")
        self.assertIn("past your flat-by 15:00 IST, get out now", warnings(res))
        self.assertIn("15:20 for ₹50 + GST", warnings(res))
        self.assertIn("15:05 for F&O stocks", warnings(res))
        res = risk.check([sell("SBIN", 33)], cfg(), portfolio=pf, now=at("2026-11-17 15:45"), state=state)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("the market is closed", warnings(res))

    def test_long_only(self):
        res = risk.check([sell("SBIN", 10)], cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertIn("short: selling 10 SBIN but you hold 0; this pack is long-only", reasons(res))
        res = risk.check([sell("SBIN", 40)], cfg(allow_short=True), portfolio=holding("SBIN", 33, 36488.10),
                         now=at(NOW))
        self.assertIn("short:", reasons(res))
        self.assertIn("\"allow_short\" is ignored: this pack is long-only", res["warnings"])

    def test_buy_then_sell_in_one_batch(self):
        res = risk.check([sbin(), sell("SBIN", 33)], cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        self.assertEqual(res["results"][1]["kind"], "exit")

    def test_format_and_tick_still_apply_to_a_sell(self):
        pf = holding("SBIN", 33, 36488.10)
        res = risk.check([sell("SBIN", 33, product="CNC")], cfg(), portfolio=pf, now=at(NOW))
        self.assertIn("product must be MIS", reasons(res))
        res = risk.check([sell("SBIN", 33, type="limit", limit_price="1105.73")], cfg(), portfolio=pf, now=at(NOW))
        self.assertIn("tick: limit_price 1105.73 is not on the 0.10 tick for this price band", reasons(res))


class FormatTickAsset(unittest.TestCase):
    def test_format_rules(self):
        cases = [
            (dict(exchange="BSE"), "exchange must be NSE"),
            (dict(exchange=None), "exchange must be NSE"),
            (dict(product="CNC"), "product must be MIS (intraday), got 'CNC'; CNC is delivery"),
            (dict(qty="33.5"), "qty must be a whole number of shares"),
            (dict(qty=None), "no qty"),
            (dict(notional="5000"), "give qty, not an amount"),
            (dict(type="stop"), "unknown order type 'stop'"),
            (dict(validity="gtc"), "unknown validity 'gtc'"),
            (dict(type="limit"), "a limit order needs a limit_price"),
            (dict(type="sl", limit_price="1106.00"), "an SL order needs a trigger_price"),
            (dict(limit_price="1105.70"), "a market order has no limit_price"),
            (dict(side="short"), "side must be buy or sell"),
        ]
        for over, msg in cases:
            res = risk.check([sbin(**over)], cfg(), now=at(NOW))
            self.assertEqual(verdicts(res), ["BLOCK"], over)
            self.assertIn(msg, reasons(res), over)
        o = sell("SBIN", 33, stop_loss_price="1096.70")
        res = risk.check([o], cfg(), portfolio=holding("SBIN", 33, 36488.10), now=at(NOW))
        self.assertIn("a stoploss/target belongs on the buy", reasons(res))

    def test_the_orders_tick_wins_over_the_price_band(self):
        # SBIN above 1,000 trades on 0.05 this month (October close 961.85); the order carries it
        self.assertEqual(sbin()["tick"], "0.05")
        res = risk.check([sbin(stop_loss_price="1096.73")], cfg(), now=at(NOW))
        self.assertIn("tick: stop_loss_price 1096.73 is off SBIN's 0.05 tick this month (NSE sets it from the "
                      "month-end close; Groww will not take it): use 1096.70 or 1096.75", reasons(res))
        res = risk.check([sbin(stop_loss_price="1096.75")], cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertIn("NSE tick 0.05", " ".join(res["results"][0]["info"]))
        res = risk.check([sbin(type="limit", limit_price="1105.65")], cfg(), now=at(NOW))
        self.assertNotIn("tick", reasons(res))
        # without a tick on the order (or its plan), the entry's band: 0.10 above 1,000
        res = risk.check([untick(sbin(stop_loss_price="1096.75"))], cfg(), now=at(NOW))
        self.assertIn("tick: stop_loss_price 1096.75 is not on the 0.10 tick for this price band", reasons(res))
        self.assertIn("(from the price band: approximate)", " ".join(res["results"][0]["info"]))
        res = risk.check([sbin(tick="0.03")], cfg(), now=at(NOW))
        self.assertIn("format: tick '0.03' is not an NSE tick size", reasons(res))

    def test_prices_must_sit_on_the_tick_grid(self):
        res = risk.check([untick(sbin(type="limit", limit_price="1105.75"))], cfg(), now=at(NOW))
        self.assertIn("tick: limit_price 1105.75", reasons(res))
        bajaj = order("BAJAJ-AUTO", 3, 10799.0, "10752.00", "10846.50")
        res = risk.check([bajaj], cfg(), now=at(NOW))
        self.assertIn("tick: target_price 10846.50 is not on the 1.00 tick for this price band", reasons(res))
        bajaj["target_price"] = "10846.00"
        self.assertEqual(verdicts(risk.check([bajaj], cfg(), now=at(NOW))), ["PASS"])
        cheap = order("IDEA", 3000, 9.85, "9.70", "10.01", stop_loss_price="9.70", target_price="10.01")
        res = risk.check([cheap], cfg(), now=at(NOW))
        self.assertNotIn("tick", reasons(res))                      # 0.01 below 250

    def test_symbols(self):
        ok = [("M&M", 11, 3392.0, "3372.20", "3411.80"), ("BAJAJ-AUTO", 3, 10799.0, "10752.00", "10846.00"),
              ("sbin.ns", 33, 1105.7, "1096.70", "1114.70")]
        for args in ok:
            res = risk.check([order(*args)], cfg(), now=at(NOW))
            self.assertEqual(verdicts(res), ["PASS"], (args, reasons(res)))
        self.assertEqual(risk.check([order(*ok[2])], cfg(), now=at(NOW))["results"][0]["symbol"], "SBIN")
        for sym, msg in (("NIFTY", "is an index"), ("BANKNIFTY", "is an index"),
                         ("RELIANCE26NOVFUT", "looks like an F&O contract"),
                         ("NIFTY2611724000CE", "looks like an F&O contract"),
                         ("BTC/INR", "is not an NSE symbol"), ("^NSEI", "is not an NSE symbol")):
            res = risk.check([order(sym, 10, 1000.0, "990.00", "1010.00")], cfg(), now=at(NOW))
            self.assertIn("asset: ", reasons(res), sym)
            self.assertIn(msg, reasons(res), sym)

    def test_every_eq_symbol_in_the_nse_list_is_accepted(self):
        import csv
        with open(os.path.join(FIX, "nse", "EQUITY_L.csv"), encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        syms = [r[0].strip() for r in rows[1:] if r and r[2].strip() == "EQ"]
        self.assertGreater(len(syms), 1000)
        bad = [s for s in syms if not risk.SYMBOL.match(s) or risk.FNO.search(s) or s in risk.INDEX_NAMES]
        self.assertEqual(bad, [])


class Cli(unittest.TestCase):
    def test_prices_file_freshness(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "prices.json")
            with open(p, "w") as fh:
                json.dump({"as_of": "2026-11-17T04:14:00Z", "prices": {"SBIN": 1109.0},
                           "detail": {"SBIN": {"time": "2026-11-17T04:14:00Z"}}}, fh)
            args = ["--orders", SBIN_PATH, "--config", CFG_PATH, "--portfolio", os.path.join(d, "x"),
                    "--prices", p, "--state", os.path.join(d, "x"), "--no-write", "--json"]
            code, text, _ = run(args + ["--now", NOW])
            self.assertEqual(json.loads(text)["results"][0]["entry_ref_from"], "price")
            code, text, _ = run(args + ["--now", "2026-11-17 10:30"])
            r = json.loads(text)["results"][0]
            self.assertEqual(r["entry_ref_from"], "plan")
            self.assertIn("older than 15 minutes", " ".join(r["warnings"]))

    def test_errors_are_one_line(self):
        code, _, err = run(["--orders", "/nonexistent.json"])
        self.assertEqual(code, 1)
        self.assertIn("run nse-budget first", err)
        self.assertEqual(len(err.strip().splitlines()), 1)
        code, _, err = run(["--orders", SBIN_PATH, "--now", "half past nine"])
        self.assertIn("IST", err)


if __name__ == "__main__":
    unittest.main()
