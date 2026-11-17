---
name: nse-exit
description: >-
  Exit Planner for NSE intraday trades. Sets your stop and your target before
  you enter, so you know when to get out. Reads the entry from nse-entry (or a
  price you give), puts the stop at the opening-range midpoint by default (or
  the range low, your price, a percent, or a multiple of the ATR), rounds it
  down onto the stock's NSE tick (reviewed monthly from the previous
  month-end close), and sets the target at 2R (or 1R, or your price). Refuses
  a stop at or above the entry, a target at or below it, or a stop too tight
  to survive the spread, and says why. Flat by 15:00 IST, ahead of Groww's
  15:05 cut-off for new intraday orders and its 15:20 intraday (MIS) auto
  square-off. Prices in rupees. Writes
  trader/plan.json for the Time Machine, the Budget Manager and Price Alerts.
  Long side only. Use when the user says "where's my stop", "set my stop and
  target", "stop loss for SBIN", "plan the exit", "when do I get out",
  "what's my R", "risk reward", "intraday stop loss and target for my Groww
  trade", or right after /nse-entry triggers, before /nse-timemachine.
argument-hint: "[--stop or-mid|or-low|price:X|pct:X|atr:K] [--target 2R|1R|price:X]"
---

# nse-exit

The Exit Planner decides how an intraday trade ends before it starts. The
stop is where the setup has failed. The target is where you take what you
came for. Both are set BEFORE the entry, written down and handed on, so
nobody has to decide anything in the middle of a fast move. A stop is a plan,
not a guarantee: a gap or a fast market can fill past it. It never places an
order: you place the buy, the stop and the target yourself in the Groww app.

Day trading loses most people money, and a neat plan does not change that.
SEBI's study of intraday traders (press release 13/2024, July 24 2024) found
that 71% of individual intraday traders in the equity cash segment lost money
in FY23, and 80% of those who made more than 500 trades a year.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan                          # trader/entry.json: midpoint stop, 2R target
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop or-mid --target 1R --write
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop atr:1.5           # needs the session's bars (Yahoo or --offline)
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol SBIN --entry 1105.70 --or-high 1101.40 --or-low 1092.00
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol "M&M" --entry 3392 --setup level --stop pct:1
python3 "${CLAUDE_SKILL_DIR}/exit.py" show                          # the current trader/plan.json
```

Other flags: `--tick 0.05` (the stock's tick, when you know it from the Groww
price ladder), `--flat-by 15:00` (IST), `--json`, `--now "2026-11-17 09:46"`
(IST), `--entry PRICE` with a waiting entry.json to plan ahead, `--setup
level`, `--offline FILE` (a saved Yahoo chart JSON for an ATR stop; it also
skips the daily chart, so the tick falls back to the entry price's band).
Quote `"M&M"` in the shell.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/entry.json` | the trigger from nse-entry: symbol, entry price, time, bar, opening range |
| `trader/config.json` | `flat_by` (15:00 IST), `min_reward_risk` (1.0: a note here, nse-risk enforces it), `holidays` (extra closed days) |
| Yahoo daily chart | `range=3mo&interval=1d`: the previous month's last close, for the tick (free, unofficial) |
| Yahoo chart API | only for an `atr:K` stop: the session's bars up to the entry bar, free and unofficial |
| `trader/plan.json` | with `--write`: entry, stop and its rule, target and its R, risk a share, reward:risk, flat by, `tick` and `tick_basis`. nse-timemachine, nse-budget, nse-risk, nse-groww and nse-alerts read it |

## Stops and targets

| stop | where it goes |
| --- | --- |
| `or-mid` | the opening-range midpoint (the default for an opening-range breakout) |
| `or-low` | the opening-range low: wider, so fewer shares for the same max loss |
| `price:X` | your price, in rupees (`price:1096.70` or `price:₹1,096.70`) |
| `pct:X` | X percent under the entry |
| `atr:K` | K times the 14-bar ATR under the entry, on the session's bars up to the entry bar. With fewer than 14 bars by then it averages all of them and says so |

A level setup has no opening range to lean on, so it needs a stop you name.

- **The tick grid.** NSE prices move in ticks by price band: under ₹250,
  ₹0.01; under ₹1,000, ₹0.05; under ₹5,000, ₹0.10; under ₹10,000, ₹0.50;
  under ₹20,000, ₹1.00; above that, ₹5.00. NSE reviews each stock's tick
  monthly, from its close on the last trading day of the previous month
  (NSE/CMTR/62174, amended by CMTR/67133), not from today's price. So SBIN,
  which closed October at ₹961.85, trades on ₹0.05 ticks above ₹1,000 in
  November. The planner reads Yahoo's daily chart, takes the last close in
  the month before the plan's date and picks the band from that close. It
  writes `"tick"` and `"tick_basis"` (`{"rule": "NSE monthly review",
  "close": 961.85, "date": "2026-10-30"}`) into plan.json. `--tick X`
  overrides it (basis `flag`). With `--offline`, or when the daily chart
  cannot be read, it falls back to the band of the entry price, says so, and
  writes `{"rule": "entry-price band (fallback)"}`: then check the price
  ladder in Groww before you type a stop.
- **Yahoo's float prices.** Yahoo sends float32 prices (₹1,214.60 arrives as
  1214.5999755859375). Every price is snapped to the paisa before it is
  compared or rounded, so a range low never floors one tick too low.
- **Rounding.** The entry rounds half up to its tick (an off-grid entry gets
  a note). The stop rounds AWAY from the entry onto the tick (down, for a
  long). Risk = entry minus stop. Target = entry + R x risk, rounded half up
  onto the tick. `2R` by default; `1R`, `1.5R` or `price:X` also work.
- **Refused, with the reason.** A stop at or above the entry. A target at or
  below it. A risk under 0.05% of the price: too tight to survive the
  spread, normal wiggle would stop you out. An entry.json that has not
  triggered (unless you give `--entry`). A short: this pack plans longs only.
- **Flat by.** 15:00 IST by default (or `flat_by` in the config). Out by
  then, win or lose, no overnight hold. Groww stops new intraday orders in
  F&O stocks (most of the Nifty 500) at 15:05, and squares off intraday (MIS)
  positions still open at 15:20, charging ₹50 + GST per position for it. A
  flat-by at or after 15:20 gets a note, and one at or after the 15:30 close
  is refused. An entry at or after the flat-by time is refused.
- **Calendar.** A plan dated on a weekend or an NSE holiday gets a note. For
  a year outside the loaded list (2026) it says "NSE holiday list for 2027 not
  loaded: check nseindia.com" and still plans.

## Steps

1. Run it right after `/nse-entry` triggers. Before that, `--entry PRICE`
   plans ahead at the level you expect.
2. Use the default stop for an opening-range breakout unless the user names
   another. A level setup needs one: ask.
3. Run `exit.py plan --write`. Read back the entry, the stop and its rule,
   the target and its rule, the risk a share, reward:risk and flat by.
4. If it refuses, say the reason and the fix (a wider stop, a farther
   target). Never loosen anything on your own.
5. Say once that the stop is a plan, not a guarantee, and that Groww stops
   new intraday orders in F&O stocks at 15:05 and squares off open MIS
   positions at 15:20 for ₹50 + GST per position. If the tick fell back to
   the entry price's band, say so and ask the user to check the price ladder
   in Groww (or give `--tick`).
6. Hand off to `/nse-timemachine` to replay the plan, then `/nse-budget` to
   size it to the max loss per trade, Groww's charges included. Offer
   `/nse-alerts` for the stop, the target and the flat-by time. Claude can
   keep `entry.py watch` running in the background or on a `/loop` until a
   setup triggers, then come here.

## Example: the reel

SBIN, Tuesday 2026-11-17. The entry is ₹1,105.70 at 9:45 AM (the 9:40 bar,
bar 5) and the opening range is ₹1,092.00 to ₹1,101.40. `exit.py plan --stop
or-mid --target 1R --write`:

```
Exit plan: SBIN long, intraday (MIS), Tue 2026-11-17, opening-range breakout on 5-minute bars
  Entry         ₹1,105.70 at 9:45 AM (bar 5)
  Stop          ₹1,096.70  opening-range midpoint: (₹1,101.40 + ₹1,092.00) / 2 = ₹1,096.70
  Target        ₹1,114.70  1R (one times the risk)
  Risk a share  ₹9.00 (0.81% of the entry)
  Reward:risk   1.0
  Tick          ₹0.05, from the ₹961.85 close on 30 Oct (NSE reviews ticks monthly)
  Flat by       15:00 IST, win or lose: no overnight hold
```

The default 2R target would be 1,105.70 + 2 x 9.00 = ₹1,123.70. A midpoint
off the grid rounds down: RELIANCE's range that morning was ₹1,337.70 to
₹1,343.60, so a plan from ₹1,343.70 puts the stop at ₹1,340.60 (the midpoint
₹1,340.65, rounded down to the ₹0.10 tick from RELIANCE's ₹1,164.90 close on
30 Oct). `/nse-budget` then sizes the
trade to your max loss per trade.

## Rules

- The stop and the target are set before the entry, never after.
- Never move a stop further from the entry once the trade is on.
- Plain numbers only: entry, stop, target, risk a share, reward:risk. It
  never prints a P&L or a return.
- Nothing here promises winning trades or money. A 1R target is not a
  prediction that the price gets there.
- Long side only: this pack plans longs only.

## What it never does

It never places an order, never sizes a position, never widens a stop to
make a plan pass, and never calls a stop a guarantee.

## Safety

- Education and tooling, not financial advice. A signal, a plan or a PASS is a rules check, not a
  recommendation.
- Nothing in this pack places an order or talks to a broker. The user places every order by hand in the
  Groww app. Claude prepares an order ticket (`/nse-groww`) only after `/nse-risk` passed that exact order
  today and the user typed an explicit yes for that one order in the chat.
- Intraday (MIS) longs in the NSE cash segment only. No short selling, F&O or leverage: `mis_leverage`
  stays 1 unless the user changes it in their own config, deliberately.
- Groww stops new intraday orders in F&O stocks (most of the Nifty 500) at 15:05 and squares off MIS positions
  still open at 15:20 for ₹50 + GST per position. Plan to be flat by 15:00.
- Never ask for or accept Groww login details, PINs or OTPs in the chat.
