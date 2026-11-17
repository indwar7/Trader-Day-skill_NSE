import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

from _load import load, ROOT, FIX

size = load("nse-budget", "size")

CFG = os.path.join(ROOT, "nse-budget", "config.example.json")
PLAN = os.path.join(FIX, "nse-budget", "plan-sbin.json")
PLAN_MM = os.path.join(FIX, "nse-budget", "plan-mm.json")
ORDERS_FIXTURE = os.path.join(FIX, "nse-risk", "orders-sbin.json")
NOW = "2026-11-17 09:45"          # IST
TODAY = "2026-11-17"
# the shipped config, key for key: a small retail intraday account on Groww
CONTRACT_CFG = {
    "budget": 50000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_inr": 40000,
    "min_order_inr": 5000, "max_loss_per_trade_inr": 500, "max_daily_loss_inr": 1500,
    "max_trades_per_day": 3, "max_orders_per_day": 10, "min_reward_risk": 1.0, "require_stop": True,
    "no_entries_before": "09:30", "no_entries_after": "14:30", "flat_by": "15:00",
    "product": "MIS", "exchange": "NSE", "mis_leverage": 1, "blocked": [], "allow_short": False,
    "holidays": [],
    "costs": {"brokerage_pct": 0.1, "brokerage_cap": 20, "brokerage_min": 5, "brokerage_min_pct": 2.5,
              "stt_sell_pct": 0.025, "exchange_pct": 0.00307, "sebi_pct": 0.0001, "ipft_pct": 0.0001,
              "stamp_buy_pct": 0.003, "gst_pct": 18},
}


def cfg(**over):
    with open(CFG) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def plan(path=PLAN, **over):
    with open(path) as fh:
        p = json.load(fh)
    p.update(over)
    return p


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = size.main(argv)
    return code, out.getvalue(), err.getvalue()


def prices_doc(price, quote_time="2026-11-17T04:16:00Z", sym="SBIN"):
    return {"as_of": quote_time, "source": "yahoo", "prices": {sym: price},
            "detail": {sym: {"prev_close": 1090.5, "time": quote_time, "source": "yahoo"}}}


class Charges(unittest.TestCase):
    """Groww intraday charges, checked by hand."""

    def test_reliance_100_shares_by_hand(self):
        # buy 100 RELIANCE at 1,338.90 (value 1,33,890.00), sell at 1,330.00 (value 1,33,000.00)
        #   brokerage  min(20, 0.1% of 1,33,890) + min(20, 0.1% of 1,33,000) = 20 + 20        = 40.00
        #   STT        0.025% of 1,33,000 (sell only) = 33.25                                 = 33.25
        #   exchange   0.00307% of 2,66,890 = 8.193523                                        =  8.19
        #   SEBI       0.0001% of 2,66,890 = 0.26689                                          =  0.27
        #   IPFT       0.0001% of 2,66,890 = 0.26689                                          =  0.27
        #   stamp      0.003% of 1,33,890 (buy only) = 4.0167                                 =  4.02
        #   GST        18% of (40.00 + 8.19 + 0.27 + 0.27 = 48.73) = 8.7714                   =  8.77
        #   total                                                                             = 94.77
        c = size.round_trip_costs(1338.90, 1330.00, 100, CONTRACT_CFG["costs"])
        self.assertEqual(c, {"brokerage": 40.0, "stt": 33.25, "exchange": 8.19, "sebi": 0.27, "ipft": 0.27,
                             "stamp": 4.02, "gst": 8.77, "total": 94.77})
        # the loss at that exit: 100 x 8.90 = 890.00 on the price, 984.77 with the charges
        self.assertEqual(size.loss_at_stop(1338.90, 1330.00, 100), Decimal("984.77"))

    def test_the_default_costs_are_the_config_costs(self):
        self.assertEqual(size.COSTS, CONTRACT_CFG["costs"])
        self.assertEqual(size.round_trip_costs(1338.90, 1330.00, 100), size.round_trip_costs(
            1338.90, 1330.00, 100, CONTRACT_CFG["costs"]))

    def test_brokerage_floor_and_cap(self):
        c = size._costs(None)
        b = lambda v: float(size.brokerage(Decimal(v), c))
        self.assertEqual(b("100000"), 20.0)      # 0.1% = 100, capped at 20
        self.assertEqual(b("10000"), 10.0)       # 0.1%
        self.assertEqual(b("3000"), 5.0)         # 0.1% = 3 is under 5: min(5, 2.5% = 75) = 5
        self.assertEqual(b("100"), 2.5)          # 0.1% = 0.10 is under 5: min(5, 2.5% = 2.50) = 2.50
        self.assertEqual(b("0"), 0.0)

    def test_one_order_and_a_round_trip(self):
        buy = size.order_costs(1105.90, 33, "buy")
        # 36,494.70: brokerage 20, exchange 1.12, SEBI 0.04, IPFT 0.04, stamp 1.09, GST 18% of 21.20 = 3.82
        self.assertEqual(buy, {"brokerage": 20.0, "stt": 0.0, "exchange": 1.12, "sebi": 0.04, "ipft": 0.04,
                               "stamp": 1.09, "gst": 3.82, "total": 26.11})
        sell = size.order_costs(1105.90, 33, "sell")
        self.assertEqual((sell["stamp"], sell["stt"]), (0.0, 9.12))

    def test_breakeven_is_the_first_tick_that_covers_the_charges(self):
        be = size.breakeven(1105.70, 33, Decimal("0.10"))
        self.assertEqual(be, Decimal("1107.60"))
        self.assertEqual(size.breakeven(1105.70, 33, Decimal("0.05")), Decimal("1107.55"))
        net = lambda p: (Decimal(str(p)) - Decimal("1105.70")) * 33 - Decimal(str(
            size.round_trip_costs(1105.70, p, 33)["total"]))
        self.assertGreaterEqual(net(1107.60), 0)
        self.assertLess(net(1107.50), 0)


class Formatting(unittest.TestCase):
    def test_inr_uses_indian_grouping(self):
        self.assertEqual(size.inr(123456.5), "₹1,23,456.50")
        self.assertEqual(size.inr(-1250), "-₹1,250.00")
        self.assertEqual(size.inr(12345678.9), "₹1,23,45,678.90")
        self.assertEqual(size.inr(999.999), "₹1,000.00")
        self.assertEqual(size.inr(100), "₹100.00")
        self.assertEqual(size.inr(0), "₹0.00")

    def test_tick_bands_at_the_edges(self):
        for price, tick in ((249.99, "0.01"), (250, "0.05"), (999.95, "0.05"), (1000, "0.10"),
                            (4999.9, "0.10"), (5000, "0.50"), (9999.5, "0.50"), (10000, "1.00"),
                            (19999, "1.00"), (20000, "5.00"), (54321, "5.00")):
            self.assertEqual(size.tick_for(price), Decimal(tick), price)

    def test_snapping_onto_the_grid(self):
        t = Decimal("0.10")
        self.assertEqual(size.snap(1096.79, t, "down"), Decimal("1096.70"))    # a long's stop: down
        self.assertEqual(size.snap(1096.6999999999998, t, "down"), Decimal("1096.70"))  # float noise is not a tick
        self.assertEqual(size.snap(1114.75, t), Decimal("1114.80"))            # nearest, half up
        self.assertEqual(size.snap(1105.699951171875, t), Decimal("1105.70"))  # a Yahoo price
        self.assertEqual(size.snap(10736.5, Decimal("1.00"), "down"), Decimal("10736.00"))
        self.assertEqual(size.snap(512.33, Decimal("0.05"), "down"), Decimal("512.30"))
        self.assertTrue(size.on_grid(1105.70, t))
        self.assertFalse(size.on_grid(1105.73, t))

    def test_ist_clock(self):
        self.assertEqual(size.parse_now("2026-11-17 09:45").isoformat(), "2026-11-17T04:15:00")
        self.assertEqual(size.parse_now("2026-11-17T04:15:00Z").isoformat(), "2026-11-17T04:15:00")
        self.assertEqual(size.to_ist(size.parse_now("2026-11-16T18:45:00Z")).isoformat(), "2026-11-17T00:15:00")

    def test_symbols(self):
        self.assertEqual(size.nse_symbol(" m&m.ns "), "M&M")
        self.assertEqual(size.nse_symbol("bajaj-auto"), "BAJAJ-AUTO")


class TheExample(unittest.TestCase):
    """SBIN on 2026-11-17: the 09:40 bar closed at 1,105.70 above the 15-minute opening range
    (1,092.00 to 1,101.40). Stop at the midpoint 1,096.70, target 1R 1,114.70. Risk 9.00 a share.
    SBIN closed October at 961.85, so NSE's tick for it in November is 0.05 (the plan says so)."""

    def test_config_example_is_the_contract(self):
        with open(CFG) as fh:
            self.assertEqual(json.load(fh), CONTRACT_CFG)
        self.assertEqual(size.DEFAULTS["costs"], CONTRACT_CFG["costs"])
        for k, v in CONTRACT_CFG.items():
            if k != "budget":
                self.assertEqual(size.DEFAULTS[k], v, k)

    def test_sbin_is_33_shares_bound_by_the_position_cap(self):
        s = size.size_trade(plan(), cfg(), today=TODAY)
        self.assertEqual(s["qty"], 33)
        self.assertEqual(s["bound_by"], "position cap")
        self.assertEqual(s["risk_per_share"], 9.0)
        self.assertEqual((s["tick"], s["tick_from"]), (0.05, "plan"))
        self.assertEqual(s["cost"], 36488.10)
        self.assertEqual(s["charges_at_stop"]["total"], 60.14)
        self.assertEqual(s["charges_at_target"]["total"], 60.31)
        self.assertEqual(s["max_loss"], 357.14)          # 33 x 9.00 = 297.00 + 60.14
        self.assertEqual(s["win_at_target"], 236.69)     # 297.00 - 60.31
        self.assertEqual(s["breakeven"], 1107.55)          # on the 0.05 tick; 1107.60 on the 0.10 band
        self.assertEqual(s["cash_buffer"], 5000)
        self.assertEqual([(l["name"], l["shares"]) for l in s["limits"]],
                         [("max loss", 48), ("position cap", 33), ("max order", 36), ("cash", 40)])
        self.assertEqual([l["name"] for l in s["limits"] if l["binds"]], ["position cap"])
        self.assertIsNone(s["refusal"])

    def test_the_max_loss_counts_the_charges(self):
        # without charges 500 / 9.00 would allow 55 shares; with them only 48 fit
        self.assertEqual(size.shares_for(500, 9), 55)
        self.assertEqual(size.max_loss_shares(500, 1105.70, 1096.70, None), 48)
        self.assertEqual(size.loss_at_stop(1105.70, 1096.70, 48), Decimal("498.04"))
        self.assertGreater(size.loss_at_stop(1105.70, 1096.70, 49), 500)
        s = size.size_trade(plan(), cfg(budget=200000, max_order_inr=100000), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"], s["max_loss"]), (48, "max loss", 498.04))

    def test_the_proposed_order(self):
        o = size.proposed_order(size.size_trade(plan(), cfg(), today=TODAY))
        why = o.pop("why")
        self.assertEqual(o, {
            "symbol": "SBIN", "exchange": "NSE", "product": "MIS", "side": "buy", "qty": "33", "type": "market",
            "validity": "day", "stop_loss_price": "1096.70", "target_price": "1114.70", "tick": "0.05",
            "plan": {"entry": 1105.7, "stop": 1096.7, "target": 1114.7, "tick": 0.05, "tick_from": "plan",
                     "risk_per_share": 9.0, "max_loss": 357.14, "charges_at_stop": 60.14, "charges_at_target": 60.31,
                     "breakeven": 1107.55, "setup": "orb", "entry_time": "09:45", "flat_by": "15:00",
                     "tick_basis": {"rule": "month-end close", "date": "2026-10-30", "close": 961.85}}})
        self.assertIn("bound by your position cap", why)
        self.assertIn("₹60.14 Groww charges = ₹357.14", why)

    def test_the_fixture_orders_file_is_what_it_writes(self):
        with open(ORDERS_FIXTURE, encoding="utf-8") as fh:
            fixture = json.load(fh)["orders"][0]
        self.assertEqual(size.proposed_order(size.size_trade(plan(), cfg(), today=TODAY)), fixture)

    def test_a_limit_order(self):
        o = size.proposed_order(size.size_trade(plan(), cfg(), limit=1105.70, today=TODAY))
        self.assertEqual((o["type"], o["limit_price"], o["qty"]), ("limit", "1105.70", "33"))
        s = size.size_trade(plan(), cfg(), limit=1104.00, today=TODAY)
        self.assertEqual((s["risk_per_share"], s["qty"]), (7.3, 33))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(), limit=1105.73, today=TODAY)
        self.assertIn("off SBIN's 0.05 tick (from the plan: NSE's tick for this month)", str(e.exception))
        self.assertIn("1105.70 or 1105.75", str(e.exception))

    def test_cli_writes_one_order(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "trader", "orders.json")
            code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(d, "none"),
                                 "--now", NOW, "--write", out])
            self.assertEqual(code, 0)
            self.assertIn("Size: 33 shares of SBIN = ₹36,488.10 at ₹1,105.70, intraday (MIS)", text)
            self.assertIn("33 x ₹9.00 = ₹297.00 + ₹60.14 charges = ₹357.14 (your limit ₹500.00)", text)
            self.assertIn("Breakeven: SBIN has to reach ₹1,107.55 (+₹1.85 a share, 0.17%)", text)
            self.assertIn("NSE tick ₹0.05 (from the plan: NSE's tick for this month)", text)
            self.assertIn("stoploss 1096.70, target 1114.70, tick 0.05", text)
            self.assertIn("15:05 for F&O stocks", text)
            self.assertIn("STT (sell)", text)
            self.assertIn("<- binds", text)
            self.assertIn("15:20", text)
            self.assertIn("not financial advice", text)
            with open(out, encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertEqual(doc["status"], "proposed")
            self.assertEqual(doc["created_by"], "nse-budget")
            self.assertEqual(doc["created_at"], "2026-11-17T04:15:00Z")    # 09:45 IST
            self.assertEqual(len(doc["orders"]), 1)
            self.assertEqual(doc["orders"][0]["plan"]["max_loss"], 357.14)

    def test_cli_json_and_limit(self):
        code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/nonexistent", "--now", NOW,
                             "--limit", "--json"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual(out["sizing"]["qty"], 33)
        self.assertEqual(out["orders"][0]["limit_price"], "1105.70")


class PlanRules(unittest.TestCase):
    def test_zero_shares_is_a_plain_refusal(self):
        s = size.size_trade(plan(stop=600.0), cfg(), today=TODAY)
        self.assertEqual(s["qty"], 0)
        self.assertIn("more than your ₹500.00 max loss per trade", s["refusal"])
        self.assertIsNone(size.proposed_order(s))
        with tempfile.TemporaryDirectory() as d:
            far, out = os.path.join(d, "plan.json"), os.path.join(d, "orders.json")
            with open(far, "w") as fh:
                json.dump(plan(stop=600.0), fh)
            code, text, _ = run(["--plan", far, "--config", CFG, "--portfolio", "/x", "--now", NOW, "--write", out])
            self.assertEqual(code, 1)
            self.assertIn("No order: one share loses", text)
            self.assertFalse(os.path.exists(out))

    def test_each_limit_can_bind(self):
        self.assertEqual(size.size_trade(plan(), cfg(max_order_inr=20000), today=TODAY)["bound_by"], "max order")
        s = size.size_trade(plan(), cfg(max_position_pct=100, max_order_inr=None), today=TODAY)
        self.assertEqual((s["bound_by"], s["qty"]), ("cash", 40))
        s = size.size_trade(plan(), cfg(max_loss_per_trade_inr=250), today=TODAY)
        # 21 x 9.00 = 189.00 + 55.45 charges = 244.45 fits; 22 shares would lose 253.84
        self.assertEqual((s["bound_by"], s["qty"], s["max_loss"]), ("max loss", 21, 244.45))
        self.assertGreater(size.loss_at_stop(1105.70, 1096.70, 22), 250)

    def test_mis_leverage_raises_the_cash_limit_only(self):
        s = size.size_trade(plan(), cfg(mis_leverage=5, max_position_pct=1000, max_order_inr=None,
                                        max_loss_per_trade_inr=5000), today=TODAY)
        self.assertEqual([(l["name"], l["shares"]) for l in s["limits"] if l["name"] == "cash"], [("cash", 203)])
        self.assertEqual(s["margin"], round(s["cost"] / 5, 2))
        s1 = size.size_trade(plan(), cfg(), today=TODAY)
        s5 = size.size_trade(plan(), cfg(mis_leverage=5), today=TODAY)
        self.assertEqual(s1["qty"], s5["qty"])            # the cap and the max loss do not move
        with self.assertRaises(size.SizeError):
            size.size_trade(plan(), cfg(mis_leverage=0.5), today=TODAY)

    def test_a_fresh_higher_price_means_a_bigger_risk(self):
        s = size.size_trade(plan(), cfg(), price=1107.0, today=TODAY)
        self.assertEqual((s["entry"], s["entry_from"], s["risk_per_share"]), (1107.0, "price", 10.3))
        s = size.size_trade(plan(), cfg(), price=1103.0, today=TODAY)       # lower: not used
        self.assertEqual((s["entry"], s["entry_from"]), (1105.7, "plan"))
        s = size.size_trade(plan(), cfg(), price=1110.0, today=TODAY)       # ran toward the target
        self.assertEqual(s["reward_risk"], 0.35)
        self.assertIn("reward:risk 0.35", s["refusal"])

    def test_prices_file_is_used_only_when_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "prices.json")
            with open(p, "w") as fh:
                json.dump(prices_doc(1107.0), fh)
            code, text, _ = run(["--plan", PLAN, "--config", CFG, "--prices", p, "--portfolio", "/x",
                                 "--now", "2026-11-17 09:50", "--json"])
            out = json.loads(text)
            self.assertEqual(out["sizing"]["entry"], 1107.0)
            self.assertIn("above the plan's entry", out["notes"][0])
            self.assertIn("09:46 IST", out["notes"][0])
            code, text, _ = run(["--plan", PLAN, "--config", CFG, "--prices", p, "--portfolio", "/x",
                                 "--now", "2026-11-17 10:30", "--json"])
            out = json.loads(text)
            self.assertEqual(out["sizing"]["entry"], 1105.7)
            self.assertIn("older than 15 minutes", out["notes"][0])

    def test_the_portfolio_cash_caps_the_size(self):
        s = size.size_trade(plan(), cfg(), cash=20000, today=TODAY)
        self.assertEqual((s["bound_by"], s["qty"]), ("cash", 13))           # (20,000 - 5,000) / 1,105.70
        with tempfile.TemporaryDirectory() as d:
            pf = os.path.join(d, "portfolio.json")
            with open(pf, "w") as fh:
                json.dump({"cash": 20000, "positions": []}, fh)
            code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", pf, "--now", NOW, "--json"])
            self.assertEqual(json.loads(text)["sizing"]["qty"], 13)

    def test_the_plans_tick_wins_over_the_price_band(self):
        # SBIN above 1,000 trades on 0.05 this month (October close 961.85); the band would say 0.10
        s = size.size_trade(plan(stop=1096.79, target=1114.77), cfg(), today=TODAY)
        self.assertEqual((s["tick"], s["stop"], s["target"]), (0.05, 1096.75, 1114.75))
        o = size.proposed_order(s)
        self.assertEqual((o["tick"], o["stop_loss_price"], o["target_price"]), ("0.05", "1096.75", "1114.75"))
        s = size.size_trade(plan(), cfg(), limit=1105.65, today=TODAY)      # on 0.05, off the 0.10 band
        self.assertEqual(size.proposed_order(s)["limit_price"], "1105.65")
        # no tick in the plan: the entry's price band, an approximation
        s = size.size_trade(plan(tick=None, stop=1096.79, target=1114.77), cfg(), today=TODAY)
        self.assertEqual((s["tick"], s["tick_from"], s["stop"], s["target"]), (0.1, "band", 1096.7, 1114.8))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(tick=None), cfg(), limit=1105.75, today=TODAY)
        self.assertIn("from the entry's price band, an approximation", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(tick=0.03), cfg(), today=TODAY)
        self.assertIn("not an NSE tick size", str(e.exception))

    def test_prices_snap_onto_the_tick_grid(self):
        s = size.size_trade(plan(tick=None, stop=1096.79, target=1114.75), cfg(), today=TODAY)
        self.assertEqual((s["stop"], s["target"]), (1096.7, 1114.8))       # stop down, target nearest
        p = plan(symbol="BAJAJ-AUTO", entry=10799.0, stop=10752.5, target=10846.4, tick=None)
        s = size.size_trade(p, cfg(), today=TODAY)
        self.assertEqual((s["tick"], s["stop"], s["target"], s["qty"]), (1.0, 10752.0, 10846.0, 3))
        o = size.proposed_order(s)
        self.assertEqual((o["symbol"], o["stop_loss_price"], o["target_price"]), ("BAJAJ-AUTO", "10752.00", "10846.00"))

    def test_m_and_m(self):
        s = size.size_trade(plan(PLAN_MM, symbol="m&m.ns"), cfg(), today=TODAY)
        self.assertEqual((s["symbol"], s["qty"], s["bound_by"]), ("M&M", 11, "position cap"))
        self.assertEqual([(l["name"], l["shares"]) for l in s["limits"]][0], ("max loss", 21))
        o = size.proposed_order(s)
        self.assertEqual((o["symbol"], o["stop_loss_price"], o["target_price"]), ("M&M", "3372.20", "3411.80"))

    def test_bad_plans(self):
        bad = [
            (dict(side="sell"), "long (buy)"),
            (dict(stop=None), "positive stop"),
            (dict(stop=1110.0), "not below the entry"),
            (dict(target=1100.0), "not above the entry"),
            (dict(symbol=""), "no symbol"),
            (dict(date="2026-11-16"), "the plan is for 2026-11-16"),
        ]
        for over, msg in bad:
            with self.assertRaises(size.SizeError) as e:
                size.size_trade(plan(**over), cfg(), today=TODAY)
            self.assertIn(msg, str(e.exception), over)
        for c, msg in ((cfg(blocked=["sbin"]), "blocked"), (cfg(product="CNC"), "intraday (MIS)"),
                       (cfg(exchange="BSE"), "NSE only"), (cfg(budget=0), "no budget")):
            with self.assertRaises(size.SizeError) as e:
                size.size_trade(plan(), c, today=TODAY)
            self.assertIn(msg, str(e.exception))

    def test_the_plan_day_is_the_ist_day(self):
        # 00:15 IST on the 17th is still the 16th in UTC: the plan for the 17th is today's
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/x", "--now", "2026-11-16T18:45:00Z"])
        self.assertEqual((code, err), (0, ""))
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/x", "--now", "2026-11-17T18:31:00Z"])
        self.assertEqual(code, 1)
        self.assertIn("today is 2026-11-18 in India", err)

    def test_under_the_minimum_order(self):
        s = size.size_trade(plan(), cfg(min_order_inr=40000), today=TODAY)
        self.assertIn("under your ₹40,000.00 minimum order", s["refusal"])

    def test_missing_plan_is_one_line(self):
        code, _, err = run(["--plan", "/nonexistent/plan.json", "--config", CFG])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)
        self.assertEqual(len(err.strip().splitlines()), 1)
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--now", "tomorrow"])
        self.assertIn("IST", err)

    def test_weights_mode_is_gone(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                size.main(["--weights", "SBIN=50,INFY=50"])
        self.assertFalse(hasattr(size, "parse_weights"))


if __name__ == "__main__":
    unittest.main()
