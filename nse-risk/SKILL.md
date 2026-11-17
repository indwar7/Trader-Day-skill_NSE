---
name: nse-risk
description: >-
  Risk Manager for NSE intraday trading on Groww. Checks every proposed
  intraday (MIS) order against your limits before it can become a Groww
  ticket: a stop is required, the loss at the stop including Groww's
  brokerage, STT and fees must fit your max loss per trade, reward:risk
  must meet your minimum, the day's total risk must fit your daily loss
  limit, trades per day, and no new entries before 09:30, after 14:30 or
  after your 15:00 flat-by IST, on weekends or NSE holidays. Also checks NSE
  symbols (M&M, BAJAJ-AUTO), exchange NSE, product MIS, whole shares, the
  NSE tick grid, order size, position cap, cash buffer, MIS leverage,
  long-only (no accidental shorts), no indices or F&O, and duplicates.
  Getting out is never blocked by a day limit. Prints PASS or BLOCK and
  records the verdicts so nse-groww only shows a ticket that passed. Use
  when the user says "check my order", "is this safe", "risk check", "can I
  take this trade", "how much have I risked today", "kitna risk hai", or
  before /nse-groww, always.
argument-hint: "[path to orders.json]"
---

# nse-risk

The Risk Manager is the gate between a proposal and the Groww app. Every
order written by nse-budget or by nse-groww's flatten passes through it. A
BLOCK is final for that order: nse-groww shows no ticket for it, so the user
has nothing to type into Groww. Most intraday traders lose money: SEBI's study
(press release 13/2024, July 24 2024) found 71% of individual intraday traders
in the equity cash segment lost money in FY23, and 80% of those with more
than 500 trades a year. These limits keep each loss and each day's losses
small. They do not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/check.py"                           # trader/ files; writes trader/state/risk.json
python3 "${CLAUDE_SKILL_DIR}/check.py" --json
python3 "${CLAUDE_SKILL_DIR}/check.py" --no-write                # look without recording
python3 "${CLAUDE_SKILL_DIR}/check.py" --now "2026-11-17 09:45"  # pin the clock (IST)
```

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (nse-budget, or nse-groww's flatten) |
| `trader/config.json` | the limits (nse-budget ships `config.example.json`) |
| `trader/portfolio.json` | cash and holdings; if missing, a fresh account funded with the budget |
| `trader/prices.json` | a fresh price raises the entry it measures risk from; also values holdings |
| `trader/state/broker.json` | what the user placed in Groww today (nse-groww): duplicates, trades, daily loss |
| `trader/state/risk.json` | the verdicts it writes, with a fingerprint of each exact order |

## Entries and exits

An **entry** opens or adds to a position: a buy, or a sell of shares the user
does not hold. An **exit** only sells what they already hold. Getting out must
always be possible, so an exit is checked for format, tick grid, asset and
short sales only. Every other rule (day limits, window, loss, reward:risk,
blocked list, size, duplicates) turns into a warning for an exit, never a
BLOCK.

## The checks

Orders are checked in file order, as if each passed order had filled, so a
batch that is fine order by order but breaks a limit together is caught.

| check | blocks when |
| --- | --- |
| format | no symbol, side not buy or sell, qty not a whole number of shares, an amount instead of a qty, exchange not NSE, product not MIS (CNC is delivery), type not market / limit / sl / sl-m, validity not day / ioc, a limit or SL order without its prices, a stoploss/target on a sell |
| tick | a limit, trigger, stoploss or target price off the order's `tick` (nse-budget copies it from the plan; nse-exit takes it from the month-end close, the way NSE sets each stock's tick for the month), or a `tick` that is not an NSE tick size. Only an order with no tick falls back to the entry price's band (under ₹250 0.01, under ₹1,000 0.05, under ₹5,000 0.10, under ₹10,000 0.50, under ₹20,000 1.00, else 5.00), an approximation: SBIN above ₹1,000 trades on 0.05 in November, not the band's 0.10. It names the two nearest valid prices |
| asset | an index (NIFTY, BANKNIFTY ...), an F&O contract (RELIANCE26NOVFUT, ...CE / ...PE), or anything that is not an NSE symbol: A-Z, 0-9, & and -, up to 20 characters (RELIANCE, M&M, BAJAJ-AUTO, 20MICRONS). A trailing `.NS` is stripped |
| short | a sell of more than the user holds. Groww intraday would let it through as a short sale; this pack is long-only, whatever `allow_short` says |
| blocked | (entries) the symbol is on the `blocked` list |
| size | (entries) under `min_order_inr` or over `max_order_inr` |
| position | (entries) the stock would be over `max_position_pct` of the account |
| cash | (entries) the buy's margin (value / `mis_leverage`) would leave less than the cash buffer, or need more cash than there is |
| stop | (entries) no stop while `require_stop` is true (the order's `stop_loss_price`, or the plan's stop); a stop not below the entry |
| loss | (entries) qty x (entry minus stop) + Groww's round-trip charges at the stop over `max_loss_per_trade_inr` |
| reward:risk | (entries) (target minus entry) / (entry minus stop) under `min_reward_risk` |
| daily loss | (entries) the max losses at the stops of today's entries, plus this one, over `max_daily_loss_inr` |
| trades | (entries) `max_trades_per_day` entries already placed today |
| window | (entries) before `no_entries_before` (09:30), after `no_entries_after` (14:30), at or after `flat_by` (15:00), or the market is closed |
| duplicate | (entries) the same symbol and side twice in one batch, or already placed today |
| daily limit | (entries) `max_orders_per_day` orders already placed today |

How the numbers work:

- **The entry it measures from**: the limit price for a limit or SL order, the
  trigger for an SL-M order. For a market order, the HIGHER of the plan's
  entry and a fresh price from `trader/prices.json` (under 15 minutes old). A
  plan written at 09:45 cannot understate the risk at 10:20 after the price
  ran up. An older price is not used, and it says so.
- **Groww's charges** are the same function as nse-budget's (`round_trip_costs`:
  brokerage, STT, exchange, SEBI, IPFT, stamp duty, GST), from `"costs"` in the config.
- **Daily loss** counts the full max loss of every entry placed today, win or
  lose, as nse-groww logged it. An order the user marked rejected or cancelled
  in Groww (`ticket.py result`) stops counting.
- **The window** is IST (UTC+05:30, no daylight saving), worked out in the
  tool, never from the computer's clock zone. The NSE session is 09:15 to
  15:30. Weekends and the NSE capital-market holidays for 2026 are closed:
  Jan 15 and 26, Mar 3, 26 and 31, Apr 3 and 14, May 1 and 28, Jun 26, Sep 14,
  Oct 2 and 20, Nov 10 and 24, Dec 25. Dates in `"holidays"` in the config are
  added. For a year it has no list for, it treats weekdays as trading days and
  warns ("NSE holiday list for 2027 not loaded: check nseindia.com"). Special
  sessions (Diwali Muhurat, a Budget-day weekend session) are not supported:
  they count as closed. Set `no_entries_before`, `no_entries_after` and
  `flat_by` all to null to switch the window off.
- **Groww's cut-offs**: Groww's risk policy (groww.in/p/risk-policy) stops NEW
  intraday orders at 15:05 for F&O-enabled stocks (most of the Nifty 500) and
  15:20 for the rest, and a sell to get out is a new order. That is why the
  defaults are no entries after 14:30 and flat by 15:00. Open intraday
  positions are squared off by Groww from 15:20 for ₹50 + GST each. With
  `no_entries_after` or `flat_by` later than 15:05 the check warns.

Warnings do not block, but say them to the user: a stop that lives only in
the plan and not on the order, a price too old to use, MIS leverage above 1,
a 2027 date, an exit after the flat-by time.

## Steps

1. If the price may have moved since the plan, refresh it first
   (nse-alerts: `alerts.py quote SBIN --write`).
2. Run `check.py`. Exit code 0 means every order passed, 1 means at least one
   was blocked.
3. Tell the user the result in plain words, order by order: the max loss at
   the stop with the charges, reward:risk, and how much of today's limits it
   uses. For a BLOCK, give the reason and the fix (fewer shares, a closer stop
   from nse-exit, wait for the window, or skip the trade). Never change a
   limit yourself.
4. If the order changes in any way, run the check again. nse-groww matches
   each order to its verdict by fingerprint, so an edited order is not ready
   until it passes again, and a verdict from yesterday does not count today.

## Example: SBIN, Tuesday 2026-11-17

The 33-share SBIN order from nse-budget at 09:45 IST, on a fresh ₹50,000
account with the example config. The tool printed:

```
PASS  #1 buy SBIN 33 shares, entry
          entry 1105.70 (the plan's entry), stop 1096.70, target 1114.70: risk ₹9.00 a share, NSE tick 0.05
          max loss ₹357.14 = ₹297.00 + ₹60.14 Groww charges (limit ₹500.00), reward:risk 1.00 (min 1)
          today before this: 0 of 3 trades, ₹0.00 of ₹1,500.00 daily loss

1 passed, 0 blocked at 2026-11-17 09:45 IST. Cash after the passed orders: ₹13,511.90.
```

With a ₹2,00,000 budget, 49 shares fit every other limit and 49 x ₹9.00 =
₹441.00 is under ₹500 on price alone. The charges tip it over:

```
BLOCK #1 buy SBIN 49 shares, entry
        - loss: 49 shares x ₹9.00 risk a share = ₹441.00 + ₹66.43 Groww charges = ₹507.43 at the stop, over your ₹500.00 max loss per trade
```

The same order at 09:20 is blocked by the window (no new entries before
09:30 IST); on Tuesday 2026-11-24 it is blocked because NSE is closed for
Prabhu Shri Guru Nanak Dev Jayanti. The 15:02 sell that closes the 33 shares
passes, with a warning to get out now (before Groww's 15:05 cut-off), even
though it is after the 14:30 entry cutoff: it is an exit. A stoploss of
1096.73 on this order is blocked: it is off SBIN's 0.05 tick.

## Rules

- A PASS only means the ticket may be shown to the user. It is not a yes, and not advice.
- Every number comes from the config, the order, the portfolio, the prices or the Groww order log.
- Free Yahoo prices are unofficial and can lag; Groww's price is the one that fills, and a
  stop can fill worse than its price in a fast market.

## What it never does

It never places an order, never edits an order to make it pass, never
loosens a limit, never blocks a sell that only gets the user out, and never
treats a PASS as the user's yes.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Nothing in this pack places an order. The user places every order by hand in the Groww app,
  only after nse-risk passed that exact order today and nse-groww showed its ticket.
- Never ask for, store or repeat the user's Groww login, PIN, OTP or TPIN.
- No short selling, F&O or extra leverage. Groww's MIS allows shorts and leverage; this pack does not use them.
