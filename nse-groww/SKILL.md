---
name: nse-groww
description: >-
  Groww order tickets for NSE intraday trades, placed by hand. Turns each
  order that nse-risk passed today into a plain ticket to type into the
  Groww app: BUY qty, Intraday (MIS), NSE, Market or Limit, then Groww's
  "Add SL/TGT" with the stoploss and target (one cancels the other), the max
  loss at the stop with Groww's charges, the estimated brokerage, STT and
  fees, and the breakeven. Records the user's explicit "yes" (meaning "I
  placed it in Groww") word for word, records the fill price into
  trader/portfolio.json, builds the sell ticket that squares off a position
  by 15:00 (before Groww's 15:05 order cut-off), and logs
  each trade's max loss so nse-risk can hold the daily limit. Never connects
  to Groww or any broker. Use when the user says "place the order", "how do
  I enter this in Groww", "Groww ticket", "buy it", "I placed it", "order
  ho gaya", "it filled at", "record my fill", "close my position", "square
  off", "flatten", "get me out", "exit SBIN", or after /nse-risk passes.
argument-hint: "[order number]"
---

# nse-groww

Nothing in this pack can place an order. Groww's app is where the user
places every order, by hand, so this skill is about getting that one moment
right: the ticket says exactly what to tap, and the log says exactly what was
done. An order reaches the market only through three gates, every time:

1. **nse-risk passed this exact order today** (matched by fingerprint).
2. **The user places it themselves in the Groww app**, from the ticket.
3. **The user tells Claude "yes"** for that one order, meaning "I placed it",
   and `ticket.py placed` records the reply word for word.

No PASS, no ticket. No yes, nothing recorded, and nse-risk does not count it.

Most intraday traders lose money: SEBI's study (press release 13/2024, July
24 2024) found 71% of individual intraday traders in the equity cash segment
lost money in FY23, and 80% of those with more than 500 trades a year.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                     # every ticket, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show --index 1 --separate-stop
python3 "${CLAUDE_SKILL_DIR}/ticket.py" placed --index 1 --reply "yes"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --index 1 --price 1105.90
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol SBIN --side sell --qty 33 --price 1114.70
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status rejected --note "insufficient margin"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" flatten --symbol SBIN --qty 33
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log
```

`ticket.py` renders tickets, checks that a reply is a real yes for that
order, records fills, builds the flatten ticket and keeps the log in
`trader/state/broker.json`. `--now "2026-11-17 09:45"` pins the clock (IST).
`approve` is another name for `placed`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (nse-budget writes it; `flatten` replaces it) |
| `trader/state/risk.json` | nse-risk's verdicts; only a PASS from today is READY |
| `trader/state/broker.json` | every yes (placed), no, rejected, cancelled and fill, with each entry's max loss |
| `trader/portfolio.json` | cash and shares, updated by `fill` (Groww's estimated charges come off the cash) |
| `trader/config.json` | `costs` for the charges, `flat_by`, the budget for a first fill |
| `trader/plan.json` | the flat-by time for `flatten` |

## What Groww offers for intraday stocks (checked 2026-11-17)

- **Market and Limit**, with Intraday chosen instead of Delivery on the order
  screen. Groww's help: "You can switch from Delivery to Intraday mode ... while
  placing the order", and intraday allows short selling and leverage.
  (groww.in/help/stocks,-f&o-&-ipo/searchable/is-intraday-trading-available-on-groww)
- **Stoploss/Target (Add SL/TGT)** for intraday, on a new order or an open
  position: "When any one of SL and Target is hit, the position is squared-off
  and the other leg (SL or Target) is automatically cancelled." No extra
  margin; full position size by default; valid for the day. (Groww update,
  May 27 2025: groww.in/updates/introducing-stoploss-target-orders-on-groww-for-intraday;
  "Intraday OCO", May 6 2025: groww.in/updates/updates-from-groww-tax-loss-harvesting-intraday-oco-bonds-and-lots-more)
  This is the OCO the ticket uses.
- **Trail** option under Stoploss (May 8 2026: groww.in/updates/trailing-stop-loss-intraday).
  The ticket says leave it off: nse-risk measured the loss from a fixed stop.
- **Stop-loss orders**: a trigger price, plus a limit price for SL or none for a
  market stop (SL-M). (groww.in/blog/how-to-place-stop-loss-order, Oct 31 2025)
  A pending stop-loss can be turned into a market order from the Orders tab
  with "Sell at market price". (groww.in/help/stocks,-f&o,-ipo-&-mtf/order/how-can-i-modify-my-stoploss-order-to-market--17)
- **Bracket orders** are "available for F&O trading on Groww" (Jan 29 2026:
  groww.in/updates/bracket-orders-on-groww), not for intraday stocks. Not used.
- **Square-off**: "all open positions should be squared off by 3:20 PM", else
  Groww closes them at market for ₹50 + 18% GST per position (₹59).
  (groww.in/help/stocks,-f&o,-ipo-&-mtf/sx-margin/what-are-intraday-square-off-charges-1)
  Groww's risk policy also says new intraday orders stop at 3:05 PM for
  F&O-enabled stocks and 3:20 PM for the rest. (groww.in/p/risk-policy) An exit
  sell is a new order, so the pack's flat-by is 15:00, before that cut-off.
- **Prices in ticks**: Groww takes prices only on the stock's NSE tick, which
  NSE sets each month from the previous month-end close. The ticket prints the
  order's tick ("NSE tick ₹0.05") next to the stoploss and target.
- **Charges**: groww.in/pricing/stocks (nse-budget lists them).

App screens change. If the user's screen does not match a ticket step, stop
and ask; do not guess a button.

## Steps: the entry

1. `ticket.py show`. Only READY tickets go to the user. A ticket that is NOT
   READY or BLOCKED goes back to `/nse-risk`.
2. Show ONE ticket and its numbered Groww steps. The user does them in the
   app: search the stock, BUY, Intraday, NSE, the qty, Market (or the limit
   price), "Add SL/TGT" with the stoploss and target, check, place.
3. Ask: "Did you place it in Groww? Reply yes or no." One order per question.
   Never offer "yes to all".
4. Run `ticket.py placed --index N --reply "<the user's reply, word for word>"`.
   If it prints "Not recorded", the reply was not an explicit yes for this
   order (for example "done", "ok", "yes to all", or a yes that names a
   different stock or qty). Ask again. Never turn a reply into a yes yourself.
5. When Groww shows the order executed, record the average price from its
   order details: `ticket.py fill --index N --price P`. If Groww rejected it,
   or the user cancelled it before it filled: `ticket.py result --index N
   --status rejected|cancelled`; it then stops counting for today. A new try
   is a new order: nse-budget, nse-risk, a new ticket and a new yes.

**No "Add SL/TGT" on the screen?** `ticket.py show --index N --separate-stop`
gives the fallback: place the buy alone; once it executes, place ONE
stop-loss sell (trigger at the stop, no limit price); do NOT place a target
sell as well. A resting target sell AND a resting stop-loss sell for the same
shares can both fill: the second sells shares the user no longer holds, which
on Groww intraday is a short sale they did not mean to make. nse-alerts
watches the target instead (`alerts.py add --from-plan`); when it rings, the
user opens the pending stop-loss in the Orders tab and taps "Sell at market
price", so the stop itself becomes the exit and there is never a second sell.

## Steps: while it is open, and out by the close

- nse-alerts watches the stop, the target and the flat-by time. Groww's SL/TGT
  does the stopping; the alert is a second pair of eyes.
- When the stop or the target fills, record it:
  `ticket.py fill --symbol SBIN --side sell --qty 33 --price 1096.70`.
- By `flat_by` (15:00 IST), anything still open gets closed. Why 15:00: Groww
  stops new intraday orders at 15:05 for F&O-enabled stocks (most of the Nifty
  500), and the sell that gets the user out is a new order; after that only
  Groww's own square-off from 15:20 (₹50 + GST a position) closes it:
  1. Check the Positions tab in Groww for the qty.
  2. `ticket.py flatten --symbol SBIN --qty 33` writes a market sell (Intraday,
     NSE) to `trader/orders.json`.
  3. `/nse-risk`. A sell that only closes what the user holds is never blocked
     by the daily limits or the entry window.
  4. `ticket.py show`: cancel any pending stoploss/target for the stock in the
     Orders tab FIRST (left open, it could fill after the exit and open a
     short), then Exit from the Positions tab. Then the yes, then `fill`.
- Past 15:20 Groww squares off whatever is left, for ₹50 + GST a position.

## Example: SBIN, Tuesday 2026-11-17

The 33-share order nse-budget sized and nse-risk passed at 09:45 IST. The tool printed:

```
ORDER 1 of 1: NSE intraday, real money. You place it yourself in the Groww app.
  BUY 33 SBIN, market, NSE intraday (MIS), validity Day
  stoploss 1096.70, target 1114.70, NSE tick ₹0.05
  max loss about ₹357.14 at the stop: ₹297.00 + ₹60.14 Groww charges (a stop can fill worse in a fast market)
  Groww charges, estimated, buy + sell: ₹60.14 if the stop fills, ₹60.31 at the target; breakeven 1107.55 (₹1.85 a share above 1105.70)
  READY: nse-risk PASS, waiting for you to place it in Groww
  In the Groww app:
    1. Search SBIN and tap BUY. Choose Intraday (not Delivery) and NSE.
    2. Qty 33. Order type: Market.
    3. Tap "Add SL/TGT": Stoploss 1096.70, Target 1114.70 (Groww takes prices in steps of ₹0.05, this stock's NSE tick). Leave Trail off. When one of them fills, Groww cancels the other.
    4. Check the order screen says Intraday, NSE, qty 33, then place it.
    5. Do NOT also place a separate stop-loss sell or a target sell for these shares: two resting sells for the same 33 shares can both fill and leave you short.
  Out by 15:00 IST, win or lose (nse-alerts rings, then ticket.py flatten): Groww stops new intraday orders at 15:05 for F&O stocks (15:20 for the rest) and squares off open intraday positions from 15:20 for ₹50 + GST each.
  Placed it in Groww? Reply "yes". Did not place it? Reply "no".
```

The user replies "yes". `placed` records it with the ₹357.14 max loss. Say
it filled at 1,105.90: `fill --index 1 --price 1105.90` printed "Groww charges
on it, estimated: ₹26.11. Cash now ₹13,479.19." At 15:00, `flatten`, nse-risk
and the sell ticket; the user cancels the SL/TGT, exits, says yes, and `fill
--index 1 --price 1114.70` printed "estimated: ₹34.23. Cash now ₹50,230.06":
₹290.40 on the price, ₹230.06 after both sides' charges. (An illustration of
the bookkeeping with chosen fill prices, not a result.)

## What it never does

- Never connects to Groww, places, modifies or cancels an order. The user does, in the app.
- Never records a yes that is not an explicit yes for that one order, and never two orders on one yes.
- Never runs from a scheduled or background task: nobody is there to place it or say yes.
- Never asks for, reads, stores or repeats the user's Groww login, PIN, OTP, TPIN or any API key.
- Never promises a fill price: a market order and a stop fill at whatever the market gives.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Every ticket is a real-money order; this pack has no practice mode. Rehearse with /nse-timemachine first.
- Nothing in this pack places an order. The user places every order by hand in the Groww app, only
  after nse-risk passed that exact order today and this skill showed its ticket.
- No short selling, F&O or extra leverage. Groww's MIS allows shorts and leverage; this pack does not use them.
