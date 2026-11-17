---
name: nse-budget
description: >-
  Budget Manager for NSE intraday trades on Groww. Sizes one intraday (MIS)
  trade in a Nifty 500 stock to your budget and your max loss per trade,
  with Groww's brokerage, STT, exchange, SEBI, IPFT, stamp duty and GST
  counted in the loss. Reads the plan from nse-exit (entry, stop, target),
  finds the whole shares that fit inside four limits (max loss incl.
  charges, position cap, max order, cash after your buffer and MIS
  leverage), says which limit decided it, prints the charges and the
  breakeven move, puts every price on the NSE tick grid, and writes ONE
  proposed buy with its stoploss and target to trader/orders.json. A stop
  too far away for your max loss is a plain refusal. Nothing is placed. Use
  when the user says "how many shares", "size this trade", "position size",
  "how much should I buy", "I have ₹50,000", "I only want to risk ₹500 a
  trade", "kitne shares lun", "what will Groww charge", "brokerage on this
  trade", "breakeven", "set my rules", "keep 10% in cash", or after
  /nse-exit writes a plan.
argument-hint: "[budget in rupees]"
---

# nse-budget

The Budget Manager turns a plan ("buy SBIN at 1,105.70, stop 1,096.70") into
an exact order ("buy 33 shares, intraday"), inside rules the user sets once.
The stop decides the size: the further away the stop, the fewer shares. On
NSE the charges matter too: Groww's ₹20 brokerage per order is a large slice
of a small trade, so the max loss counts them. It writes a proposal only.
nse-risk checks it, and nse-groww turns it into a ticket the user types into
the Groww app themselves.

Most intraday traders lose money. SEBI's study (press release 13/2024, July
24 2024) found that 71% of individual intraday traders in the equity cash
segment lost money in FY23, and 80% of those who made more than 500 trades a
year. Sizing to a max loss keeps each loss small; it does not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py"                          # trader/plan.json + trader/config.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --write                  # write trader/orders.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit --write          # a limit order at the plan's entry
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit 1105.70 --json   # a limit order at 1105.70
python3 "${CLAUDE_SKILL_DIR}/size.py" --budget 50000           # override the budget (rupees)
```

`--now "2026-11-17 09:45"` pins the clock (IST). A plan for another day (IST
date) is refused: make a new one with nse-exit.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | the rules (start from `config.example.json` in this folder) |
| `trader/plan.json` | the trade: entry, stop, target, setup, flat-by time (nse-exit writes it) |
| `trader/prices.json` | optional: a fresh price above the plan's entry means a bigger risk (nse-alerts writes it) |
| `trader/portfolio.json` | optional: its cash caps the size (nse-groww `fill` keeps it) |
| `trader/orders.json` | the one proposed order it writes with `--write` |

## Step 1: set the rules once

If `trader/config.json` does not exist, copy `config.example.json` into
`trader/config.json` and ask the user for each value in plain words. The
defaults suit a small retail intraday account; change them to the user's
own numbers, never on your own:

| key | default | why this default |
| --- | --- | --- |
| `budget` | 50000 | ₹50,000: money they can afford to lose, set aside for this |
| `max_loss_per_trade_inr` | 500 | 1% of the budget at the stop, charges included |
| `max_daily_loss_inr` | 1500 | three full losses (3% of the budget) end the day; nse-risk enforces it |
| `max_trades_per_day` | 3 | fewer trades, fewer charges; SEBI found losses worst among the busiest traders |
| `cash_buffer_pct` | 10 | ₹5,000 always stays in cash |
| `max_position_pct` | 75 | no single position over ₹37,500 |
| `max_order_inr` | 40000 | 80% of the budget in one order at most |
| `min_order_inr` | 5000 | under ₹5,000 Groww's ₹5 minimum brokerage is more than 0.1% of the order |
| `mis_leverage` | 1 | no leverage. Groww's intraday margin can let you buy several times your cash; raise this only on purpose, knowing the loss at the stop is still yours in full |
| `min_reward_risk` | 1.0 | the target must be at least as far as the stop (price only; charges come on top) |
| `no_entries_before` / `no_entries_after` | "09:30" / "14:30" | after the 15-minute opening range; half an hour before the flat-by |
| `flat_by` | "15:00" | out before Groww's 15:05 cut-off: its risk policy stops NEW intraday orders at 15:05 for F&O-enabled stocks (most of the Nifty 500; 15:20 for the rest), and a sell to exit is a new order. Anything still open is squared off by Groww from 15:20 for ₹50 + GST per position |
| `product` / `exchange` | "MIS" / "NSE" | intraday on NSE only; this pack sizes nothing else |
| `costs` | Groww's intraday charges | see below; update them if Groww changes its pricing |
| `holidays` | [] | extra NSE holidays (nse-risk adds them to its 2026 list) |
| `blocked` | [] | symbols they never want to trade |
| `allow_short` | false | stays false: this pack is long-only |

## Step 2: size the trade

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py" --write
```

Whole shares only. The size is the smallest of four limits:

| limit | shares |
| --- | --- |
| max loss | the most shares whose (entry minus stop) x shares + Groww's round-trip charges at the stop fit `max_loss_per_trade_inr` |
| position cap | budget x `max_position_pct` / entry |
| max order | `max_order_inr` / entry |
| cash | (budget minus the cash buffer) x `mis_leverage` / entry, never more than the cash in `trader/portfolio.json` allows |

The entry it sizes at: the limit price with `--limit`. For a market order, the
plan's entry, or the price in `trader/prices.json` if it is fresh (under 15
minutes old) and higher. A price that ran up since the plan means a bigger
loss at the same stop, so fewer shares. A lower price never shrinks the risk.

**Groww's charges** (groww.in/pricing/stocks, read 2026-11-17), per executed
order, two orders per trade: brokerage ₹20 or 0.1% of the order, whichever is
lower (under ₹5, then ₹5 or 2.5%, whichever is lower); STT 0.025% on the sell;
NSE exchange charge 0.00307%; SEBI 0.0001%; IPFT 0.0001%; stamp duty 0.003% on
the buy; GST 18% on brokerage + exchange + SEBI + IPFT; no DP charge for
intraday. Each is rounded to the paisa. They are an estimate: the contract
note is the real figure.

**The tick.** Each NSE stock has one tick size for the month, set from its
close on the last trading day of the previous month (NSE/CMTR/62174 as
amended by CMTR/67133), by band: under ₹250 0.01, under ₹1,000 0.05, under
₹5,000 0.10, under ₹10,000 0.50, under ₹20,000 1.00, else 5.00. So today's
price band can be wrong: SBIN trades above ₹1,000 in November on 0.05,
because it closed October at 961.85. nse-exit writes the month's tick into
the plan (`"tick"`, with `"tick_basis"`); this tool uses it for every price it
puts on the grid and copies it into the order (`"tick": "0.05"`), so nse-risk
and the Groww ticket use the same one. Only a plan without a tick falls back
to the entry price's band, and the report says it is an approximation. The
stop rounds down onto the grid (away from the entry), the target and a fresh
price to the nearest tick. A `--limit` price off the tick is refused with the
two nearest valid prices.

It refuses, and writes no order, when: the size is zero shares, the order is
under the minimum, reward:risk is under `min_reward_risk`, the stop is not
below the entry, the target is not above it (the move already happened), the
plan is for another day, the symbol is blocked, the plan is a short, or the
config's product is not MIS. Say the reason to the user in one line.

## Step 3: show the user

Show the four limits, which one decided the size, the shares, the rupees,
the charges at the stop and at the target, the max loss at the stop and the
breakeven. Say plainly that a stop is an order, not a promise: in a fast
market it can fill below the stop. Then hand over to `/nse-risk`.

## Example: SBIN, Tuesday 2026-11-17

From the 5-minute bars that morning: the opening range (09:15 to 09:30) ran
1,092.00 to 1,101.40, and the 09:40 bar closed at 1,105.70. A breakout plan:
entry 1,105.70 at 09:45, stop 1,096.70 (the opening-range midpoint), target
1,114.70 (1R). Risk ₹9.00 a share. SBIN closed October at 961.85, so its
November tick is 0.05 and the plan says so. The example config: ₹50,000, 10%
buffer, ₹500 max loss. The tool printed:

```
NSE tick ₹0.05 (from the plan: NSE's tick for this month): every price above sits on it.

Limits (whole shares; the smallest one wins):
  max loss      ₹500.00 incl. charges: 48 x ₹9.00 + ₹66.04 charges = ₹498.04    48 shares
  position cap  75% of ₹50,000.00 = ₹37,500.00 / ₹1,105.70                      33 shares  <- binds
  max order     ₹40,000.00 / ₹1,105.70                                          36 shares
  cash          ... = ₹45,000.00 / ₹1,105.70                                    40 shares

Size: 33 shares of SBIN = ₹36,488.10 at ₹1,105.70, intraday (MIS).
Groww charges, estimated: ₹60.14 at the stop 1096.70, ₹60.31 at the target 1114.70
Max loss at the stop: 33 x ₹9.00 = ₹297.00 + ₹60.14 charges = ₹357.14 (your limit ₹500.00).
At the target: 33 x ₹9.00 = ₹297.00 - ₹60.31 charges = ₹236.69.
Breakeven: SBIN has to reach ₹1,107.55 (+₹1.85 a share, 0.17%) before this trade makes a rupee.
```

Worth saying out loud: the charges are about a fifth of the price risk here,
so a "1R" target wins ₹236.69 while the stop loses ₹357.14. On price alone
₹500 / ₹9.00 would allow 55 shares; with the charges only 48 fit. It writes:

```json
{"symbol": "SBIN", "exchange": "NSE", "product": "MIS", "side": "buy", "qty": "33",
 "type": "market", "validity": "day", "stop_loss_price": "1096.70", "target_price": "1114.70", "tick": "0.05",
 "why": "opening-range breakout on SBIN: 33 shares, bound by your position cap (...)",
 "plan": {"entry": 1105.7, "stop": 1096.7, "target": 1114.7, "tick": 0.05, "tick_from": "plan", "max_loss": 357.14,
          "charges_at_stop": 60.14, "charges_at_target": 60.31, "breakeven": 1107.55, "...": "..."}}
```

`stop_loss_price` and `target_price` are what the user types into Groww's
"Add SL/TGT". `why` and `plan` are notes for nse-risk and nse-groww. With the
stop at 600.00 instead, one share would lose more than ₹500: no order. If a
fresh quote of 1,110.00 had arrived, reward:risk falls to 0.35 and it refuses:
the move it was waiting for has mostly happened.

## Rules

- Proposals only. This skill never contacts Groww or any broker.
- Never size money the user has not said is for this. Ask for the budget and the max loss.
- Every number comes from the config, the plan or the prices. Nothing is guessed.
- No projections: never say what a trade will make. A target is a plan, not a forecast.
- Free Yahoo prices are unofficial and can lag a few seconds or more. Groww's price is the one that fills.

## What it never does

It never places, approves or schedules an order, never raises a limit on its
own, never shrinks a stop to fit more shares, never sizes delivery (CNC),
F&O, margin beyond `mis_leverage`, or a short.

## Safety

- Education and tooling, not financial advice. A size is arithmetic, not a recommendation.
- Nothing in this pack places an order. The user places every order by hand in the Groww app,
  after nse-risk passed that exact order today and nse-groww showed its ticket.
- Never ask for, store or repeat the user's Groww login, PIN, OTP or TPIN.
- No short selling, F&O or extra leverage. Groww's MIS allows both shorts and leverage; this pack does not use them.
