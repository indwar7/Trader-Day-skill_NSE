---
name: nse-timemachine
description: >-
  Time Machine for NSE intraday trades. Replays every possible outcome of a
  planned trade before you risk a rupee: 10,000 simulated sessions plus every
  past time the setup fired. Takes the plan from nse-exit (entry, stop,
  target, flat-by), rebuilds the rest of the day 10,000 times from the
  stock's own 5-minute NSE bars over the last 60 days, and re-runs the same
  rule on every past session. Prints the odds of target first, stop first, or
  neither by your flat-by time (default 15:00 IST, before Groww stops new
  intraday orders on F&O stocks at 15:05 and squares off open MIS positions
  at 15:20), where the price sits then, the average R,
  the history, and one honest verdict, then hands off to /nse-budget. Use
  when the user says "run the time machine", "replay this trade", "what are
  the odds", "simulate this intraday trade", "monte carlo", "backtest this
  setup", "how often does this hit the target", "is this a coin flip", "will
  this SBIN / RELIANCE trade work", or after /nse-exit writes a plan, before
  /nse-budget.
argument-hint: "[path to plan.json, or NSE SYMBOL]"
---

# nse-timemachine

The Time Machine sits between the plan and the money. nse-exit has set the
entry, the stop and the target. Before anything is sized or typed into
Groww, this skill asks one question: if the rest of today plays out like the
stock's own last 60 sessions on NSE, how often does this plan hit the target
first, the stop first, or neither by the flat-by time? It answers with
10,000 replays and with the real record of the same rule on every past
session. It is a simulation built from the stock's own recent bars, not a
prediction. Most intraday traders lose money: SEBI's study (press release
13/2024, July 24 2024) found that 71 % of individual intraday traders in the
equity cash segment lost money in FY23, and 80 % of those who made more than
500 trades a year. A good-looking replay does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                    # trader/plan.json, 10,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --write            # also save trader/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 14:30      # end each replay before the plan's flat-by
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --symbol SBIN --date 2026-11-17 \
    --entry 1105.7 --stop 1096.7 --target 1114.7 --entry-time "9:45 AM"
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from           # ASCII fan chart of the saved run
```

Other flags: `--replays N`, `--seed N` (default 19), `--window N` (default 2
bars), `--offline FILE` (a saved Yahoo chart JSON, `.json` or `.json.gz`),
`--now "2026-11-17 11:04"` (IST) to pin the clock. Symbols are NSE symbols
(RELIANCE, SBIN, M&M, BAJAJ-AUTO); a trailing `.NS` is fine. All times are
IST.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the plan from nse-exit: symbol, date, entry, entry_time / entry_bar, stop, stop_rule, target, target_r, opening range, flat_by |
| Yahoo chart API | one request: 60 days of 5-minute bars for `<SYMBOL>.NS` (`range=60d&interval=5m`, `M&M` sent as `M%26M.NS`), free and unofficial, can lag |
| `trader/timemachine.json` | with `--write`: `{plan, mc, history}` plus the verdict. `mc` holds the odds, percentiles, the fan-chart bands, 160 sample paths and a 24-bin histogram of the price at the flat-by time |

## How it works

1. **The session.** NSE's regular session is 09:15 to 15:30 IST: 75
   five-minute bars. Each replay ends at the plan's `flat_by` (15:00 when the
   plan has none), or at `--until`. Groww's risk policy takes no new intraday
   orders on F&O stocks (most of the Nifty 500) after 15:05, and Groww
   squares off intraday (MIS) positions still open at 15:20 for ₹50 + GST
   per position; a replay that runs past either time says so.
2. **The pool.** It keeps only past sessions before the plan's date that have
   every bar from 09:15 to that end time. A session with a gap is left out
   and named. Yahoo's NSE 5-minute data has no usable bars after 15:15: on
   2026-11-17 the 15:20 bar was empty in all 60 sessions, and the 15:25 bar
   was empty or a single closing-price print. So a replay can end at 15:15
   at the latest; asking for later is refused in one plain line. Every bar
   becomes four ratios to the bar before it: open, high, low and close over
   the previous close.
3. **One replay.** Start at the entry price. For each bar left until the
   flat-by time, pick a past bar from the same time of day (plus or minus 2
   bars) at random, and scale it to the current price. Bar by bar, it checks
   the stop first, then the target. A bar that touches both counts as the
   stop.
4. **10,000 replays** give four odds. Target first. Stop first. Neither,
   which means the trade was still open at the flat-by time. Green, which
   means target first or above the entry at the flat-by time. R is +1 at a
   1R target, -1 at the stop, and otherwise the distance from the entry at
   the flat-by time divided by the risk. A gap through the stop still counts
   as -1R here. A real fill can be worse.
5. **The history.** The plan's own rule (the same opening range, stop rule
   and target R) runs again on every one of those past sessions. That is
   what really happened, not a simulation. Its stops sit on the NSE tick
   grid, rounded away from the entry, with entries and targets on the
   nearest tick, the way nse-exit sets them. The tick is the plan's `tick`:
   NSE sets each stock's tick monthly from its close on the last trading day
   of the previous month, which is why SBIN traded on ₹0.05 ticks at ₹1,100
   in November 2026. Without one, the tick comes from the entry price's band
   (₹0.01 under ₹250, ₹0.05 to ₹1,000, ₹0.10 to ₹5,000, ₹0.50 to ₹10,000,
   ₹1 to ₹20,000, ₹5 above). The history line names the tick it used; one
   tick serves every past session, an approximation.
6. **The verdict** is one line from the numbers. It looks only at the
   replays that hit one side, and asks how often the target came first. A
   bigger target needs to win less often to break even: 1 in (1 + R). That
   is 50 % at 1R, 33 % at 2R, 25 % at 3R. Less than 5 points from break-even:
   "Close to a coin flip" at 1R, "Close to break-even" at any other R. 5 to
   15 points: "Leans toward the target" or "Leans toward the stop". 15 or
   more: "Strongly toward" one of them.
7. **Below the verdict**, when it applies: a line when more than 40 % of the
   replays hit neither (the price at the flat-by time decides those), and
   the average R when its sign goes against the lean. Those can only point
   the other way because of the replays that hit neither.

**The seed.** The draws come from Python's `random.Random(seed)`, seed 19 by
default. Same plan, same data, same seed: the same numbers, on any machine.
Say the seed with every result so the user can repeat it.

**5-minute bars only.** Yahoo keeps 60 days of 5-min bars but only about 7
days of 1-min bars, too few sessions for a fair pool. It refuses, in one
plain line, when there are fewer than 20 complete sessions, when the entry
bar is not in the data (wrong date, an NSE holiday or a weekend, not closed
yet, a future day), or when the plan does not make sense (a stop above a
buy's entry, an entry off the 5-minute grid). A live in-progress bar is
never used.

**Charges are not in it.** Brokerage, STT, exchange charges, stamp duty and
GST are not in the R numbers. On a small risk per share they can eat a 1R
win; nse-budget counts them.

## Steps

1. Make sure `trader/plan.json` is today's plan from `/nse-exit`. If there
   is none, run that first, or pass the plan as flags.
2. Run `timemachine.py run --write`. The replays end at the plan's flat-by
   (15:00 unless the plan says otherwise); `--until` ends them earlier.
3. Tell the user, in this order: the four odds, the median and the 5 to 95
   range of the price at the flat-by time, the average R, the history line,
   then the verdict line and any line under it exactly as printed. Say the
   seed.
4. If the verdict is a coin flip or close to break-even, say so plainly. Do
   not dress it up. If the history and the replays disagree, say that too.
5. Say once that this is built from the last 60 sessions and is not a
   prediction. Results days, news, circuit limits and halts can do things
   those 60 sessions never did. Check `/nse-news` for the stock before going
   on.
6. If the user still wants the trade, hand off to `/nse-budget` to size it.

## Example: SBIN, Tuesday 2026-11-17 (data saved live at 11:04 IST)

The 15-minute opening range is ₹1,092.00 to ₹1,101.40. The 09:40 bar closes
above it: entry ₹1,105.70 at 9:45 AM (bar 5), stop ₹1,096.70 (the range
midpoint), target ₹1,114.70 (1R, risk ₹9.00 a share), flat by 15:00, and
the plan carries SBIN's November tick, ₹0.05.

```
10,000 replays of the rest of the session, 9:45 AM to 3:00 PM IST (the plan's flat-by; 63 five-minute bars), seed 19.
  target first   26.4 %
  stop first     43.8 %
  neither        29.8 %   still open at 3:00 PM
  green          40.6 %   target first, or above the entry at 3:00 PM
  Price at 3:00 PM: median ₹1,103.40, 90 % of replays between ₹1,087.78 and ₹1,119.50.
  Average R: -0.179 (R = the ₹9.00 risk: +1R at the target, -1R at the stop).
History: the same rule (15-min opening range, stop at the opening-range midpoint, 1R target, prices on
the ₹0.05 tick from the plan) on each of those 58 real sessions fired 32 times: 12 target, 15 stop,
5 neither. Average R -0.045.
Leans toward the stop: target first 26.4 % vs stop first 43.8 %.
```

At 1R the target has to come first in half of the replays that hit one
side. It did in 38 %. The replays lean toward the stop, and the real history
of this rule on SBIN was slightly worse than even. That is the honest answer
for this setup, and it is what the user should hear before `/nse-budget`
sizes anything. The data and the plan of this run are in
`tests/fixtures/nse-timemachine/`, so it can be repeated offline.

## Rules

- Every number comes from the run. Never round a coin flip up into an edge.
- The verdict line and the lines under it are printed by the tool. Repeat
  them, do not rewrite them. Never judge a 2R or 3R plan by target first vs
  stop first alone: the verdict already weighs the target's R.
- Simulated odds are not predictions and not promises. Say it once, plainly.
- Free Yahoo data is unofficial and can lag. Bars are 5-minute bars, checked
  bar by bar, never tick by tick and never real time.
- Always say the seed, so any run can be repeated.
- One plan per run. A changed entry, stop, target, flat-by or date is a new
  run.

## What it never does

It never places an order, never sizes a position, never changes the plan,
and never calls a result a sure thing. It never prints a P&L or a profit.
It never uses a bar that has not closed yet, and never mixes today's bars
into the pool.

## Safety

- Education and tooling, not financial advice. A replay is a simulation, not
  a recommendation.
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
