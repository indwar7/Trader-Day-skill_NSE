---
name: nse-alerts
description: >-
  Price Alerts and the NSE clock for intraday trading on Groww. Rings when a
  price, a stop, a target, a percent move, the 09:15 opening bell or your
  flat-by time hits, each once so you are not spammed. Turns the trade plan
  into three alerts in one go (the stop, the target and "flat by" at the
  plan's flat-by time, default 15:00 IST, before Groww stops new intraday
  orders on F&O stocks at 15:05 and squares off open MIS positions at 15:20
  for ₹50 + GST each). Knows NSE hours (09:15 to 15:30
  IST) and the 2026 NSE trading holidays, works out IST itself, fetches free
  delayed Yahoo quotes in ₹ for NSE symbols (RELIANCE, SBIN, M&M,
  BAJAJ-AUTO) one at a time, and writes trader/prices.json for sizing and
  the risk check. Use when the user says "alert me when", "tell me if
  RELIANCE hits 1,350", "watch my stop", "watch my trade", "remind me to
  square off by 3", "is the market open", "is NSE open today", "is today a
  market holiday", "when does the market open", "get me a price", "what is
  SBIN trading at", or right after /nse-exit writes a plan.
argument-hint: "[SYMBOL] [price]"
---

# nse-alerts

Price Alerts watches the numbers so the user does not have to stare at a
chart. An alert is information, never an order: when a stop alert rings,
nothing is sold by this skill. Nothing in this pack talks to Groww. If the
user placed a stop-loss or target order in the Groww app, that order does
the work; the alerts tell the user what happened and when it is time to be
flat, before Groww's 15:05 order cut-off and 15:20 square-off.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/alerts.py" clock                          # NSE open or closed, next open, Groww's 15:05 / 15:20
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan                # stop, target, flat by from trader/plan.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --opening-bell
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --stop 1337.7
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --target 1345.7
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --at 15:00 --label "flat by" --symbol RELIANCE
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol SBIN --pct 1.5
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check                          # exit 0 = something rang
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote RELIANCE "M&M" --write   # -> trader/prices.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote --holdings --write       # portfolio + plan symbols
python3 "${CLAUDE_SKILL_DIR}/alerts.py" list
python3 "${CLAUDE_SKILL_DIR}/alerts.py" reset reliance-plan-stop
```

`--now "2026-11-17 15:00"` pins the clock (IST); `--offline DIR` reads saved
quote files (`yahoo-<SYMBOL>.json`) instead of the network. Symbols are NSE
symbols; a trailing `.NS` is fine, and quote `M&M` in the shell.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the trade from nse-exit, for `add --from-plan` |
| `trader/config.json` | `flat_by` when the plan has none; optional `"holidays": ["YYYY-MM-DD", ...]`, extra closed days |
| `trader/alerts.json` | the rules (`alerts.example.json` in this folder) |
| `trader/state/alerts.json` | which rules rang and when, so each rings once |
| `trader/prices.json` | the latest quotes in ₹, merged in, for nse-budget and nse-risk: `prices` plus `detail` with each quote's time |
| `trader/portfolio.json` | the symbols held, for `quote --holdings` |

## Rule types

| type | rings when | rings again |
| --- | --- | --- |
| `above` / `target` | price at or above `price` | after `reset`, or on its own if `"rearm": true` and the price fell back |
| `below` / `stop` | price at or below `price` | same |
| `pct_move` | moved `pct` percent from the previous close (or `ref`), optional `direction` up or down | once per day at most |
| `time` | the IST clock passed `at` ("15:00") on a trading day | once per trading day |
| `opening_bell` | NSE is open (from 09:15 IST) | once per trading day |

A rule with `"date"` is watched on that IST date only; `--from-plan` rules
carry the plan's date, so yesterday's stop never rings today.

## Quotes

- Yahoo's chart JSON for `<SYMBOL>.NS`
  (`query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=1d`,
  `M&M` sent as `M%26M.NS`), an unofficial free endpoint, one request per
  symbol, half a second apart. On a 429 it waits and tries once more; on a
  429 or a 5xx after that it tries `query1`. It identifies itself as
  `nse-day-trader/1.0` and always verifies TLS. Stooq is gone: it has no NSE
  coverage.
- Free quotes are unofficial, delayed and can lag. They are fine for alerts
  and for checking a size, not for timing an order. The price in Groww is
  the one that fills. Each price in `trader/prices.json` keeps its quote
  time, so nse-risk can tell a stale one.
- If Yahoo is down, read the last traded price in the Groww app and write it
  into `trader/prices.json` as
  `{"as_of": "2026-11-17T05:33:35Z", "prices": {"RELIANCE": 1341.8}}`.

## The NSE clock

Regular session 09:15 to 15:30 IST, Monday to Friday. The pre-open call
auction before 09:15 counts as "before the open". Closed on these 2026
capital-market holidays (NSE circulars CMTR/71775 and CMTR/72260): Jan 15
(Maharashtra municipal elections), Jan 26, Mar 3, Mar 26, Mar 31, Apr 3,
Apr 14, May 1, May 28, Jun 26, Sep 14, Oct 2, Oct 20, Nov 10, Nov 24 and
Dec 25.

- NSE has no early closes. Special sessions (Diwali Muhurat trading, a
  Budget-day Sunday session) are not supported: the clock calls those days
  closed.
- Add any other closed day to `trader/config.json` as
  `"holidays": ["2026-12-31"]`.
- For 2027 and later the holiday list is not loaded: weekdays count as
  trading days, and every clock and check says so
  ("NSE holiday list for 2027 not loaded: check nseindia.com").
- IST is a fixed UTC+05:30 with no daylight saving, worked out in the tool:
  it needs no time zone files and ignores the computer's own zone.

## Groww's 15:05 cut-off and 15:20 square-off

Groww's risk policy takes no new intraday orders on F&O stocks (most of the
Nifty 500) after 15:05, and Groww squares off intraday (MIS) positions still
open at 15:20, for ₹50 + GST per position (groww.in help, read 2026-11-17).
The pack's default flat-by is 15:00, so the "flat by" alert rings five
minutes before the cut-off: close the position then. A plan whose flat-by
is at or after 15:20 gets a note when its alerts are added, and a flat-by
alert that rings after 15:20 (a missed check) says the square-off time has
passed and to check the Groww app's positions.

## Steps

1. Right after nse-exit writes the plan, run `add --from-plan`. Confirm the
   three alerts back to the user: the stop, the target and the flat-by
   time, in ₹ and IST.
2. Run `check` while the trade is open: every few minutes from a Claude
   scheduled task or `/loop`. Each run fetches only the symbols in today's
   rules and rings each alert once. A check reads the latest delayed quote;
   it is not a live feed, and a price can pass a level between two checks.
3. Tell the user what rang in one line each. A stop or target alert does not
   mean anything was sold: if the user placed a stop-loss or target order in
   Groww, check the app for the fill and record it with nse-groww's
   `ticket.py fill`. "Flat by" means close anything still open: nse-groww's
   `ticket.py flatten`, then nse-risk, then the user places the order in
   Groww. Never act for them.

## Example: RELIANCE, Tuesday 2026-11-17

The plan: in at ₹1,341.70 at 9:50 AM, stop ₹1,337.70 (the opening-range
low), target ₹1,345.70 (1R), flat by 15:00. `add --from-plan` writes
`reliance-plan-stop`, `reliance-plan-target` and `reliance-flat-by` for that
day. A `check` at 10:50 reads the real 10:49 quote, ₹1,340.50, between the
stop and the target:

```
NSE OPEN (2026-11-17 10:50 IST, regular session).
Nothing hit. 3 rule(s) watched today, RELIANCE ₹1,340.50.
```

At 3:00 PM:

```
ALERT Flat by: it is 15:00 IST (set for 15:00), the market closes at 15:30. If RELIANCE is still open,
close it now: Groww takes no new intraday orders on F&O stocks after 15:05 and squares off intraday (MIS)
positions still open at 15:20 (₹50 + GST per position). nse-groww's ticket.py flatten --symbol RELIANCE
builds the sell ticket, then nse-risk, then you place it in the Groww app.
```

It rings once. The next morning the plan's three alerts are no longer
watched; with `add --opening-bell`, the bell rings from 09:15. A live check
the same morning at 11:03, with SBIN above ₹1,105 and a 1 % move on M&M
set:

```
ALERT SBIN is ₹1,109.65: at or above your alert level ₹1,105.00.
ALERT M&M moved +1.26% to ₹3,381.00 (from ₹3,339.00).
```

## Rules

- An alert is a fact about a price or the clock. Say it as a fact.
- Never present a quote as live. Free quotes are delayed and unofficial.
- Confirm every new rule back to the user: symbol, type, level in ₹, day.
- Say times in IST.

## What it never does

It never places, cancels or changes an order, never talks to Groww, never
claims a quote is live, and never predicts where a price is going.

## Safety

- Education and tooling, not financial advice. An alert is a fact, not a
  recommendation. Most intraday traders lose money: SEBI's study (press
  release 13/2024, July 24 2024) found that 71 % of individual intraday
  traders in the equity cash segment lost money in FY23, and 80 % of those
  who made more than 500 trades a year.
- Nothing in this pack talks to a broker or places an order. The user places
  every order by hand in the Groww app; `/nse-groww` only writes a ticket to
  copy from.
- Long only: nothing in this pack plans a short. Intraday (MIS) only, with no
  leverage unless the user sets `mis_leverage` in trader/config.json
  deliberately.
- Groww takes no new intraday orders on F&O stocks after 3:05 PM and squares
  off open MIS positions at 3:20 PM for ₹50 + GST a position.
- Never paste a Groww password, PIN or OTP into the chat. Nothing here needs
  one.
