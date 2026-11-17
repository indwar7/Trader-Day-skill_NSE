import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

ticket = load("nse-groww", "ticket")
risk = load("nse-risk", "check")
size = load("nse-budget", "size")

CFG_PATH = os.path.join(ROOT, "nse-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "nse-risk", "orders-sbin.json")
PLAN_PATH = os.path.join(FIX, "nse-budget", "plan-sbin.json")
NOW = "2026-11-17 09:45"          # IST
TODAY = "2026-11-17"
BATCH = ["SBIN", "M&M", "BAJAJ-AUTO", "20MICRONS"]


def sbin(**over):
    with open(ORDERS_PATH, encoding="utf-8") as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def config():
    with open(CFG_PATH) as fh:
        return json.load(fh)


def mm():
    return {"symbol": "M&M", "exchange": "NSE", "product": "MIS", "side": "buy", "qty": "11", "type": "market",
            "validity": "day", "stop_loss_price": "3372.20", "target_price": "3411.80",
            "plan": {"entry": 3392.0, "stop": 3372.2, "target": 3411.8}}


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


class ExplicitYes(unittest.TestCase):
    """Yes now means "I placed it in Groww", for one order, and nothing else counts."""

    def test_plain_yes_counts(self):
        for reply in ("yes", "Yes", "y", "yes, placed it", "YES placed SBIN"):
            ok, why = ticket.parse_yes(reply, sbin(), BATCH)
            self.assertTrue(ok, (reply, why))
        self.assertIn("placed in Groww", ticket.parse_yes("yes", sbin(), BATCH)[1])

    def test_anything_else_does_not(self):
        for reply in ("", "sure", "ok", "done", "placed", "yes?", "yes to all", "yes both", "yes but wait",
                      "yes, not yet", "yes if it dips", "no", "nope"):
            ok, _ = ticket.parse_yes(reply, sbin(), BATCH)
            self.assertFalse(ok, reply)

    def test_numbers_must_match_this_order(self):
        for reply in ("yes 33", "yes 33 shares at market", "yes stoploss 1096.70", "yes, max loss 357.14",
                      "yes 1,114.70"):
            self.assertTrue(ticket.parse_yes(reply, sbin(), BATCH)[0], reply)
        ok, why = ticket.parse_yes("yes 34 shares", sbin(), BATCH)
        self.assertFalse(ok)
        self.assertIn("the reply says 34", why)

    def test_symbols_with_and_digits_and_dashes(self):
        self.assertTrue(ticket.parse_yes("yes M&M", mm(), BATCH)[0])
        ok, why = ticket.parse_yes("yes SBIN", mm(), BATCH)
        self.assertFalse(ok)
        self.assertIn("names SBIN, but this order is for M&M", why)
        ok, why = ticket.parse_yes("yes m&m.ns", sbin(), BATCH)
        self.assertFalse(ok)
        self.assertIn("names M&M", why)
        tiny = {"symbol": "20MICRONS", "exchange": "NSE", "product": "MIS", "side": "buy", "qty": "40",
                "type": "market", "validity": "day"}
        self.assertTrue(ticket.parse_yes("yes 20MICRONS", tiny, BATCH)[0])     # the 20 is the symbol, not a qty
        self.assertTrue(ticket.parse_yes("yes 40 20MICRONS", tiny, BATCH)[0])
        self.assertFalse(ticket.parse_yes("yes 20", tiny, BATCH)[0])
        bajaj = {"symbol": "BAJAJ-AUTO", "exchange": "NSE", "product": "MIS", "side": "sell", "qty": "3",
                 "type": "market", "validity": "day"}
        self.assertTrue(ticket.parse_yes("yes BAJAJ-AUTO 3", bajaj, BATCH)[0])


class TicketText(unittest.TestCase):
    def setUp(self):
        self.risk = risk.check([sbin()], config(), now=risk.parse_now(NOW))
        self.cfg = config()

    def render(self, order=None, separate=False):
        return ticket.render(order or sbin(), 1, 1, self.cfg, self.risk, {"orders": []}, TODAY, separate)

    def test_the_sbin_ticket(self):
        text, ready = self.render()
        self.assertTrue(ready)
        for want in ("NSE intraday, real money. You place it yourself in the Groww app.",
                     "BUY 33 SBIN, market, NSE intraday (MIS), validity Day",
                     "stoploss 1096.70, target 1114.70, NSE tick ₹0.05",
                     "max loss about ₹357.14 at the stop: ₹297.00 + ₹60.14 Groww charges",
                     "₹60.14 if the stop fills, ₹60.31 at the target; breakeven 1107.55 (₹1.85 a share above 1105.70)",
                     "READY: nse-risk PASS",
                     "1. Search SBIN and tap BUY. Choose Intraday (not Delivery) and NSE.",
                     "2. Qty 33. Order type: Market.",
                     "3. Tap \"Add SL/TGT\": Stoploss 1096.70, Target 1114.70 (Groww takes prices in steps of "
                     "₹0.05, this stock's NSE tick). Leave Trail off. When one of them fills, Groww cancels the other.",
                     "Do NOT also place a separate stop-loss sell or a target sell",
                     "--separate-stop",
                     "Out by 15:00 IST, win or lose",
                     "Groww stops new intraday orders at 15:05 for F&O stocks",
                     "15:20 for ₹50 + GST",
                     "Placed it in Groww? Reply \"yes\""):
            self.assertIn(want, text)

    def test_the_separate_stop_ticket_warns_about_a_short(self):
        text, _ = self.render(separate=True)
        self.assertNotIn("Add SL/TGT\":", text)
        self.assertIn("stop-loss order with trigger price 1096.70 and no limit price (it sells at market once "
                      "triggered; prices in steps of ₹0.05)", text)
        self.assertIn("Do NOT place a target sell at 1114.70 as well", text)
        self.assertIn("can BOTH fill", text)
        self.assertIn("unintended short", text)
        self.assertIn("nse-alerts watches the target", text)
        self.assertIn("\"Sell at market price\"", text)

    def test_a_sell_ticket_cancels_the_legs_first(self):
        o = {"symbol": "SBIN", "exchange": "NSE", "product": "MIS", "side": "sell", "qty": "33", "type": "market",
             "validity": "day"}
        steps = ticket.groww_steps(o)
        self.assertIn("cancel it first", steps[0])
        self.assertIn("Positions tab: SBIN (Intraday) > Exit", steps[1])

    def test_a_limit_ticket(self):
        steps = ticket.groww_steps(sbin(type="limit", limit_price="1105.65"))
        self.assertEqual(steps[1], "Qty 33. Order type: Limit, price 1105.65 (Groww takes prices in steps of "
                                   "₹0.05, this stock's NSE tick).")

    def test_the_ticket_prints_the_orders_tick(self):
        # SBIN above 1,000 trades on 0.05 this month; the band would say 0.10
        o = sbin()
        self.assertEqual((o["tick"], ticket.tick_of(o)), ("0.05", 0.05))
        self.assertIn("NSE tick ₹0.05", ticket.plain(o))
        self.assertEqual(ticket.breakeven(1105.70, 33, None, 0.05), 1107.55)
        self.assertEqual(ticket.breakeven(1105.70, 33), 1107.6)          # no tick: the price band's 0.10
        bare = {k: v for k, v in o.items() if k not in ("tick", "plan")}
        self.assertIsNone(ticket.tick_of(bare))
        self.assertNotIn("NSE tick", ticket.plain(bare))
        self.assertNotEqual(ticket.fingerprint(bare), ticket.fingerprint(o))

    def test_json_ticket(self):
        t = ticket.ticket_json(sbin(), 1, self.cfg, self.risk, {"orders": []}, TODAY)
        self.assertEqual((t["ready"], t["max_loss"], t["charges_at_stop"], t["breakeven"], t["tick"]),
                         (True, 357.14, 60.14, 1107.55, 0.05))
        self.assertEqual(t["fingerprint"], risk.fingerprint(sbin()))
        self.assertEqual((t["exchange"], t["product"]), ("NSE", "MIS"))
        self.assertTrue(any("Add SL/TGT" in s for s in t["steps"]))

    def test_not_ready_tickets_show_no_steps(self):
        text, ready = ticket.render(sbin(), 1, 1, self.cfg, None, {"orders": []}, TODAY)
        self.assertFalse(ready)
        self.assertIn("NOT READY: nse-risk has not checked these orders", text)
        self.assertNotIn("In the Groww app", text)


class NoBrokerHere(unittest.TestCase):
    def test_the_folder_is_the_skill_and_the_ticket_only(self):
        files = sorted(f for f in os.listdir(os.path.join(ROOT, "nse-groww")) if not f.startswith("__"))
        self.assertEqual(files, ["SKILL.md", "ticket.py"])

    def test_charges_match_the_budget(self):
        for args in ((1338.90, 1330.00, 100), (1105.70, 1096.70, 33), (100, 101, 30)):
            self.assertEqual(ticket.round_trip_costs(*args), size.round_trip_costs(*args))
            self.assertEqual(ticket.round_trip_costs(*args), risk.round_trip_costs(*args))
        self.assertEqual(ticket.inr(123456.5), "₹1,23,456.50")
        self.assertEqual(ticket.ist_iso(ticket.parse_now(NOW)), "2026-11-17T09:45:00+05:30")


class Flow(unittest.TestCase):
    """plan -> nse-budget -> nse-risk -> ticket -> yes -> fill -> flatten -> nse-risk -> yes -> fill."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = lambda *x: os.path.join(self.d, "trader", *x)
        os.makedirs(self.p("state"))
        shutil.copy(PLAN_PATH, self.p("plan.json"))
        shutil.copy(CFG_PATH, self.p("config.json"))
        code, _, err = run(size, ["--plan", self.p("plan.json"), "--config", self.p("config.json"),
                                  "--portfolio", self.p("portfolio.json"), "--now", NOW,
                                  "--write", self.p("orders.json")])
        self.assertEqual(code, 0, err)

    def tearDown(self):
        shutil.rmtree(self.d)

    def risk_check(self, now=NOW):
        return run(risk, ["--orders", self.p("orders.json"), "--config", self.p("config.json"),
                          "--portfolio", self.p("portfolio.json"), "--prices", self.p("prices.json"),
                          "--state", self.p("state", "broker.json"), "--out", self.p("state", "risk.json"),
                          "--now", now])

    def t(self, *argv, **kw):
        cmd = list(argv)
        now = kw.get("now", "2026-11-17 09:46")
        if cmd[0] in ("show", "placed", "approve", "decline", "result"):
            cmd += ["--orders", self.p("orders.json"), "--config", self.p("config.json"),
                    "--risk", self.p("state", "risk.json"), "--state", self.p("state", "broker.json"), "--now", now]
        elif cmd[0] == "fill":
            cmd += ["--orders", self.p("orders.json"), "--config", self.p("config.json"),
                    "--portfolio", self.p("portfolio.json"), "--state", self.p("state", "broker.json"), "--now", now]
        elif cmd[0] == "flatten":
            cmd += ["--orders", self.p("orders.json"), "--config", self.p("config.json"),
                    "--portfolio", self.p("portfolio.json"), "--plan", self.p("plan.json"), "--now", now]
        elif cmd[0] == "log":
            cmd += ["--state", self.p("state", "broker.json"), "--now", now]
        return run(ticket, cmd)

    def state(self):
        with open(self.p("state", "broker.json"), encoding="utf-8") as fh:
            return json.load(fh)["orders"]

    def portfolio(self):
        with open(self.p("portfolio.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_nothing_is_ready_before_the_risk_check(self):
        code, text, _ = self.t("show")
        self.assertEqual(code, 1)
        self.assertIn("NOT READY", text)
        code, _, err = self.t("placed", "--index", "1", "--reply", "yes")
        self.assertEqual(code, 1)
        self.assertIn("not ready", err)

    def test_the_whole_day(self):
        code, text, _ = self.risk_check()
        self.assertEqual(code, 0, text)
        code, text, _ = self.t("show")
        self.assertEqual(code, 0)
        self.assertIn("READY: nse-risk PASS", text)
        # "sure" is not a yes; nothing is recorded
        code, text, _ = self.t("placed", "--index", "1", "--reply", "sure")
        self.assertEqual(code, 1)
        self.assertIn("Not recorded", text)
        self.assertFalse(os.path.exists(self.p("state", "broker.json")))
        # the yes: placed in Groww
        code, text, _ = self.t("placed", "--index", "1", "--reply", "yes, placed")
        self.assertEqual(code, 0, text)
        self.assertIn("Recorded: you placed order 1 in Groww", text)
        rec = self.state()[-1]
        self.assertEqual((rec["status"], rec["kind"], rec["max_loss"], rec["stop"], rec["reply"]),
                         ("placed", "entry", 357.14, 1096.7, "yes, placed"))
        self.assertEqual(rec["fingerprint"], risk.fingerprint(sbin()))
        self.assertEqual(rec["at"], "2026-11-17T09:46:00+05:30")
        code, text, _ = self.t("show")
        self.assertIn("DONE: placed", text)
        code, _, err = self.t("placed", "--index", "1", "--reply", "yes")
        self.assertIn("not ready", err)
        # nse-risk now counts it: one trade, 357.14 of the daily loss, and no second SBIN buy
        code, text, _ = self.risk_check("2026-11-17 10:00")
        self.assertIn("duplicate: a buy SBIN was already placed today", text)
        self.assertIn("today before this: 1 of 3 trades, ₹357.14 of ₹1,500.00 daily loss", text)
        # the fill, from Groww's order details
        code, text, _ = self.t("fill", "--index", "1", "--price", "1105.90", now="2026-11-17 09:47")
        self.assertEqual(code, 0, text)
        self.assertIn("Groww charges on it, estimated: ₹26.11. Cash now ₹13,479.19.", text)
        pf = self.portfolio()
        self.assertEqual(pf["cash"], 13479.19)        # 50,000 - 36,494.70 - 26.11
        self.assertEqual(pf["positions"][0]["ticker"], "SBIN")
        self.assertEqual(pf["positions"][0]["shares"], 33)
        self.assertEqual(self.state()[-1]["status"], "filled")
        code, _, err = self.t("fill", "--index", "1", "--price", "1105.90")
        self.assertIn("already recorded as filled", err)
        code, _, err = self.t("result", "--index", "1", "--status", "cancelled")
        self.assertIn("already has a fill", err)
        # flat by 15:00: the sell ticket
        code, text, _ = self.t("flatten", "--symbol", "SBIN", now="2026-11-17 15:00")
        self.assertEqual(code, 0)
        self.assertIn("SELL 33 SBIN, market, NSE intraday (MIS)", text)
        code, text, _ = self.risk_check("2026-11-17 15:01")
        self.assertEqual(code, 0, text)
        self.assertIn("PASS  #1 sell SBIN 33 shares, exit", text)
        code, text, _ = self.t("show", now="2026-11-17 15:01")
        self.assertIn("cancel it first", text)
        code, text, _ = self.t("placed", "--index", "1", "--reply", "yes", now="2026-11-17 15:02")
        self.assertEqual(code, 0, text)
        self.assertEqual(self.state()[-1]["kind"], "exit")
        code, text, _ = self.t("fill", "--index", "1", "--price", "1114.70", now="2026-11-17 15:03")
        self.assertEqual(code, 0, text)
        pf = self.portfolio()
        # 13,479.19 + 36,785.10 - 34.23 sell charges (brokerage 20, STT 9.20, exchange 1.13, SEBI 0.04,
        # IPFT 0.04, GST 18% of 21.21 = 3.82)
        self.assertEqual(pf["cash"], 50230.06)
        self.assertEqual(pf["positions"], [])
        code, text, _ = self.t("log", now="2026-11-17 15:20")
        self.assertIn("2 placed today.", text)
        self.assertIn("filled", text)

    def test_an_edited_order_is_not_ready(self):
        self.risk_check()
        with open(self.p("orders.json"), encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["orders"][0]["qty"] = "34"
        with open(self.p("orders.json"), "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        code, text, _ = self.t("show")
        self.assertIn("this exact order was not in the last risk check", text)

    def test_yesterdays_check_does_not_count(self):
        self.risk_check()
        code, text, _ = self.t("show", now="2026-11-18 09:46")
        self.assertIn("the risk check is from 2026-11-17, not today", text)

    def test_blocked_orders_are_never_ready(self):
        self.risk_check("2026-11-17 09:20")
        code, text, _ = self.t("show", now="2026-11-17 09:20")
        self.assertEqual(code, 1)
        self.assertIn("BLOCKED by nse-risk: window: no new entries before 09:30 IST", text)

    def test_decline_rejected_and_cancelled(self):
        self.risk_check()
        code, _, err = self.t("result", "--index", "1", "--status", "rejected")
        self.assertIn("not recorded as placed today", err)
        code, text, _ = self.t("decline", "--index", "1", "--reply", "no")
        self.assertIn("you did not place order 1", text)
        self.assertEqual(self.state()[-1]["status"], "declined")
        code, text, _ = self.t("show")
        self.assertIn("DONE: declined", text)

    def test_a_rejected_order_frees_the_day(self):
        self.risk_check()
        self.t("placed", "--index", "1", "--reply", "yes")
        code, text, _ = self.t("result", "--index", "1", "--status", "rejected", "--note", "insufficient margin")
        self.assertEqual(code, 0)
        self.assertIn("no longer counts", text)
        res = risk.check([mm()], config(), now=risk.parse_now("2026-11-17 10:00"),
                         state={"orders": self.state()})
        self.assertEqual(res["today"]["entries_placed"], 0)

    def test_fills_need_a_yes_and_cannot_sell_what_you_do_not_hold(self):
        self.risk_check()
        code, _, err = self.t("fill", "--index", "1", "--price", "1105.90")
        self.assertIn("say yes first", err)
        code, _, err = self.t("fill", "--symbol", "SBIN", "--side", "sell", "--qty", "33", "--price", "1100")
        self.assertIn("cannot record selling 33 SBIN", err)
        code, _, err = self.t("fill", "--symbol", "SBIN", "--side", "buy", "--qty", "2.5", "--price", "1100")
        self.assertIn("whole numbers", err)
        code, text, _ = self.t("fill", "--symbol", "m&m.ns", "--side", "buy", "--qty", "11", "--price", "3392",
                               "--no-charges")
        self.assertEqual(code, 0, text)
        self.assertEqual(self.portfolio()["positions"][0]["ticker"], "M&M")
        self.assertEqual(self.portfolio()["cash"], 50000 - 11 * 3392)

    def test_flatten_with_the_qty_from_the_positions_tab(self):
        code, text, _ = self.t("flatten", "--symbol", "BAJAJ-AUTO", "--qty", "3", now="2026-11-17 15:00")
        self.assertEqual(code, 0, text)
        with open(self.p("orders.json"), encoding="utf-8") as fh:
            o = json.load(fh)["orders"][0]
        self.assertEqual(o, {"symbol": "BAJAJ-AUTO", "exchange": "NSE", "product": "MIS", "side": "sell",
                             "qty": "3", "type": "market", "validity": "day", "why": o["why"]})
        code, _, err = self.t("flatten", "--symbol", "INFY")
        self.assertIn("nothing to flatten", err)
        code, _, err = self.t("flatten", "--symbol", "NIFTY 50")
        self.assertIn("not an NSE symbol", err)


if __name__ == "__main__":
    unittest.main()
