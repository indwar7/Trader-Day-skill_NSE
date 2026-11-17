# NSE Day Trader: Claude skills for intraday trading on NSE with Groww

Nine Claude skills that turn Claude into a disciplined intraday trading
assistant for the Indian stock market. Claude scans the Nifty 500, checks the
news, times the entry, plans the stop and target, replays the rest of the day
10,000 times, sizes the position to your budget with Groww's real charges,
checks every rule you set, and hands you a ticket to place in the Groww app.

**Free. Open source (MIT). No API key. No broker login. Claude never places
an order: you do, in Groww.**

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> groww   (one trade)
alerts: stop, target, flat by 15:00                                         (while it is open)
```

---

## The problem

An intraday trader on NSE has about fifteen minutes after the 9:15 open to do
a lot of work by hand:

- find which of 500 stocks are actually breaking out, not just moving;
- check whether a result, a board meeting, a block deal or a SEBI order is
  behind the move;
- decide the exact entry, the stop and the target before buying;
- work out how many shares fit the money you can afford to lose;
- remember that brokerage, STT, exchange charges, stamp duty and GST come out
  of every trade;
- remember Groww's clock: no new intraday orders in F&O stocks after 15:05,
  and anything still open at 15:20 is squared off for ₹50 + GST.

Done in a hurry, this goes wrong in the same few ways: no stop, a position
too big for the account, charges that eat the profit, a trade taken on news
you never saw, a position forgotten past 15:20.

The numbers are harsh. SEBI's study of July 2024 found that **71% of
individual intraday traders in the equity cash segment lost money in FY23**,
and 80% of those who traded more than 500 times a year. Loss-makers paid
trading costs equal to another 57% of their losses.

There is already a popular open-source "Claude day trader" skill pack, but it
is built for the US market: New York hours, dollars, Alpaca, SEC filings. It
cannot scan an NSE stock, does not know the NSE holiday list, and knows
nothing about Groww.

## The solution

This repo ports that pack to the NSE and Groww, and keeps its best idea:
**Claude runs real code on real data and never guesses a price.** Every step
is a small, tested Python tool. Claude runs it, reads the result and explains
it to you. Each step writes a file the next step reads, so the plan you made
at 9:45 is the plan the risk check enforces.

| command | job | what it does |
| --- | --- | --- |
| `/nse-scanner` | Chart Scanner | Scans the Nifty 500 (or every EQ-series NSE stock) for setups: opening-range breakouts, bull flags, 20-day breakouts, oversold bounces. |
| `/nse-news` | News Monitor | Reads NSE's own announcement feeds (filings, ex-dates, board meetings, results) and Google News India for every stock you watch, tags what matters and flags big moves. |
| `/nse-entry` | Entry Timer | Watches a setup bar by bar and tells you the moment the entry triggers (first 5-minute close above the 9:15 to 9:30 range). |
| `/nse-exit` | Exit Planner | Sets the stop and target before you buy, on the stock's real NSE tick size (NSE reviews ticks monthly). |
| `/nse-timemachine` | Time Machine | Replays the rest of the session 10,000 times from the stock's own last 60 days of 5-minute bars, plus every past day the same setup fired, and gives honest odds. |
| `/nse-budget` | Budget Manager | Sizes the position in whole shares so the loss at the stop, Groww charges included, fits your max loss per trade. |
| `/nse-risk` | Risk Manager | Checks every order against your limits (loss per trade, daily loss, trades per day, time windows, holidays, tick sizes) and blocks anything over them. |
| `/nse-groww` | Groww Ticket | Turns a passed order into step-by-step instructions for the Groww app (Intraday, quantity, Add SL/TGT) and records what you placed and filled. |
| `/nse-alerts` | Price Alerts | Rings when your stop, your target, a price or the 15:00 flat-by time hits, and tells you if NSE is open today. |

## Why use it

- **A plan before every trade.** Entry, stop, target and size are written down
  before you buy. Nobody decides anything in the middle of a fast move.
- **Hard limits that say no.** If an order breaks your max loss, daily loss,
  trade count or time window, `/nse-risk` blocks it and says why.
- **Charges counted up front.** Every size and every breakeven includes
  Groww's brokerage, STT, exchange charges, SEBI fee, IPFT, stamp duty and GST.
- **Groww's rules built in.** Intraday (MIS) only, the 15:05 order cut-off,
  the 15:20 auto square-off, and Groww's Add SL/TGT so the stop and target
  cancel each other.
- **Honest odds.** The Time Machine often says "skip this one". That is the
  point: it stops bad trades before they cost money.
- **You stay in control.** No broker login, no API key, no automatic orders.
  Nothing can run away with your money.
- **Free data, no signup.** Yahoo Finance charts for `.NS` symbols, the Nifty
  500 list, NSE's public announcement feeds and Google News.

## What the NSE port adds

The NSE port is by [indwar7](https://github.com/indwar7). Changes from the US
original:

- **Market clock:** IST, the NSE session (9:15 to 15:30, 75 five-minute bars)
  and the 2026 NSE holiday list. A year that is not loaded yet gets a warning,
  and you can add dates in the config.
- **Stock list:** the Nifty 500 (or all EQ-series NSE stocks) replaces
  Nasdaq's list. Symbols like `M&M` and `BAJAJ-AUTO` work (the US version
  would fetch the US stock `M`).
- **Prices:** in rupees with Indian grouping (₹1,23,456.50), on NSE tick sizes
  with the monthly review rule. Yahoo's float noise is snapped to the paisa
  before any rounding, so stops never land one tick off.
- **Costs:** Groww's intraday charges, its 15:05 cut-off, its 15:20 square-off
  penalty and its Add SL/TGT order flow.
- **News:** NSE's corporate announcement, corporate action, board meeting and
  results feeds, plus Google News India, replace SEC filings.
- **Broker:** a manual Groww ticket replaces the Alpaca order executor.
- **Mac Python fix:** works on a python.org install with no CA certificate
  bundle, without ever switching off certificate checks.
- **Tests:** 378 offline tests on real NSE data captured on 17 November 2026.

---

## Requirements

- **Claude Code** (the terminal app, the VS Code or JetBrains extension, or
  the Claude desktop app). It needs a Claude Pro or Max plan, or an Anthropic
  API key. [Install Claude Code](https://code.claude.com/docs/en/setup).
- **Python 3.8 or newer.** Nothing to `pip install`; the tools use only the
  standard library. macOS ships `/usr/bin/python3`; on Windows install Python
  from python.org.
- **Internet access** for the free data sources.
- **A Groww account** with intraday enabled, for placing the trades yourself.

## Connect it to Claude

### Option 1: install as a plugin (easiest)

Inside Claude Code, run:

```
/plugin marketplace add indwar7/Trader-Day-skill_NSE
/plugin install nse-day-trader@nse-day-trader
```

On Claude Code 2.1.275 or later this also works in one line:

```
/plugin install nse-day-trader --marketplace indwar7/Trader-Day-skill_NSE
```

Installed as a plugin, the skills are named `/nse-day-trader:nse-scanner`,
`/nse-day-trader:nse-entry` and so on.

### Option 2: copy the skills (short names like `/nse-scanner`)

```bash
git clone https://github.com/indwar7/Trader-Day-skill_NSE.git
cp -r Trader-Day-skill_NSE/nse-* ~/.claude/skills/
```

That makes them available in every project. To keep them in one project
only, copy the folders into that project's `.claude/skills/` instead. Restart
Claude Code after copying.

### Option 3: the Claude desktop app

Local Claude Code sessions in the desktop app read `~/.claude/skills/`, so
Option 2 works there too.

### Option 4: claude.ai in the browser (not tested)

Zip one skill folder (for example `nse-alerts/`) and upload it under
**Customize > Skills > + > Create skill > Upload a skill**. Code execution
must be on (Settings > Capabilities). The scripts also need internet access
to `finance.yahoo.com`, `niftyindices.com`, `nsearchives.nseindia.com` and
`news.google.com`, which the claude.ai sandbox may block. For live data,
Claude Code (Options 1 to 3) is the reliable way.

### Check that it works

Open Claude Code in any folder and ask:

```
is NSE open today?
```

Claude should run `/nse-alerts` and answer with the session times, the next
open and Groww's 15:05 and 15:20 rules.

---

## First-time setup (2 minutes)

Make a folder for your trading. Everything the skills write goes into a
`trader/` folder inside it, and each skill reads what the last one wrote.

```bash
mkdir -p ~/trading/trader && cd ~/trading
cp ~/.claude/skills/nse-budget/config.example.json trader/config.json
claude
```

(If you installed as a plugin, copy `nse-budget/config.example.json` from
the cloned repo instead.)

Then open `trader/config.json` and set your own numbers. The example is for
a small account:

| key | example | meaning |
| --- | --- | --- |
| `budget` | 50000 | money for intraday trading, in rupees |
| `max_loss_per_trade_inr` | 500 | the most one trade may lose at its stop, charges included |
| `max_daily_loss_inr` | 1500 | stop trading for the day after this much loss |
| `max_trades_per_day` | 3 | new trades allowed per day |
| `max_position_pct` | 75 | largest single position, as % of the budget |
| `cash_buffer_pct` | 10 | budget kept aside, never used |
| `min_reward_risk` | 1.0 | the target must be at least this many times the risk |
| `no_entries_before` | "09:30" | no buys before the opening range is set |
| `no_entries_after` | "14:30" | no new buys after this time |
| `flat_by` | "15:00" | everything closed by this time |
| `mis_leverage` | 1 | 1 = no leverage; raise it only on purpose |
| `holidays` | [] | extra NSE holidays, as "YYYY-MM-DD" |
| `costs` | Groww rates | brokerage, STT, exchange, SEBI, IPFT, stamp duty, GST |

## How to use it: one trade, step by step

You talk to Claude in plain English (or Hinglish). Claude picks the right
skill, runs it and explains the result. Below is a real replay of
**17 November 2026** on live data; the numbers are what the tools printed.

**1. Scan the market (after 9:30).**

> "Scan Nifty 500 for opening range breakouts" / "aaj kaunse stocks breakout de rahe hain?"

`/nse-scanner` checked all 495 Nifty 500 stocks. By 10:00, 188 of them had
closed a 5-minute bar above their 9:15 to 9:30 range. SBIN was one: range
₹1,092.00 to ₹1,101.40.

**2. Check the news.**

> "Any news on SBIN?"

`/nse-news` showed SBIN +1.66% on the day, NSE filings about an analyst meet,
and headlines tagged `risk`, `macro` or `deal`, newest first in IST. A results
date or a SEBI order here would change the picture.

**3. Time the entry.**

> "Watch SBIN for the breakout"

`/nse-entry`: **TRIGGERED at 9:45 AM at ₹1,105.70** (the 9:40 bar closed
0.39% above the range high). If you ask later, it warns that a late entry is a
different trade.

**4. Plan the exit, before buying.**

> "Set my stop and target"

`/nse-exit`: stop **₹1,096.70** (the range midpoint), target **₹1,123.70**
(2R), risk ₹9.00 a share, tick ₹0.05 (from SBIN's ₹961.85 close on 30 Oct,
because NSE reviews tick sizes monthly), flat by 15:00.

**5. Replay the rest of the day.**

> "What are the odds on this trade?"

`/nse-timemachine` ran 10,000 replays from 9:45 to 15:00: **target first
4.8%, stop first 44.8%, neither 50.5%.** On the 58 real past sessions, the
same rule fired 32 times: 4 targets, 18 stops, 10 neither. Verdict:
**"Strongly toward the stop."** This is the skill doing its job: a textbook
breakout that, for SBIN at 2R, has usually failed. You can skip it, or try a
1R target and replay again.

**6. Size the position.**

> "How many shares can I buy?"

`/nse-budget`: **33 shares = ₹36,488.10** (your 75% position cap binds).
Groww charges ₹60.14 if the stop fills. Max loss at the stop: ₹297.00 +
₹60.14 = **₹357.14** (limit ₹500). At the target: +₹533.61 after charges.
Breakeven: ₹1,107.55.

**7. Check the rules.**

> "Check my risk"

`/nse-risk`: **PASS.** 0 of 3 trades today, ₹0 of ₹1,500 daily loss used.

**8. Get the Groww ticket and place it yourself.**

> "Show me the ticket"

`/nse-groww` prints:

```
BUY 33 SBIN, market, NSE intraday (MIS), validity Day
stoploss 1096.70, target 1123.70, NSE tick ₹0.05
In the Groww app:
  1. Search SBIN and tap BUY. Choose Intraday (not Delivery) and NSE.
  2. Qty 33. Order type: Market.
  3. Tap "Add SL/TGT": Stoploss 1096.70, Target 1123.70. Leave Trail off.
     When one of them fills, Groww cancels the other.
  4. Check the order screen says Intraday, NSE, qty 33, then place it.
  5. Do NOT also place a separate stop-loss sell or target sell.
Placed it in Groww? Reply "yes".
```

Place the order in Groww, reply "yes", then tell Claude your fill price
("filled at 1105.90") so your record matches your account.

**9. Let the alerts watch it.**

> "Set alerts from my plan"

`/nse-alerts` adds the stop, the target and a 15:00 "flat by" alert. To have
Claude check every few minutes while the trade is open:

```
/loop 3m /nse-alerts check
```

## Things you can ask

| you say | Claude runs |
| --- | --- |
| "is the market open today?" / "kal market khulega?" | `/nse-alerts` clock |
| "pre-market scan" / "build my watchlist" | `/nse-scanner` daily |
| "ORB stocks today" / "scan at 10:30" | `/nse-scanner` orb |
| "why is RELIANCE moving?" / "results today?" | `/nse-news` |
| "watch M&M for the breakout" | `/nse-entry` |
| "set a 1% stop instead" / "use a 1R target" | `/nse-exit` |
| "is this trade worth it?" | `/nse-timemachine` |
| "size it for ₹1 lakh" | `/nse-budget` |
| "check my limits" | `/nse-risk` |
| "show the Groww ticket" / "I sold at 1114.70" | `/nse-groww` |
| "alert me if SBIN falls below 1,096" | `/nse-alerts` |

Each skill's `SKILL.md` lists every command and flag.

## What it does not do

- **No automatic orders.** It never connects to Groww or any broker.
- **No F&O, no options, no short selling, no MTF.** Long intraday (MIS) trades
  in NSE cash stocks only.
- **No tick-by-tick data.** It reads free 1-minute and 5-minute bars; they
  were seconds behind live in testing but are unofficial.
- **No guarantees.** The Time Machine is built from 60 days of history and
  cannot see tomorrow's results, circuits or headlines.

## Fine print

**Not financial advice.** This is software that reads public data and does
arithmetic. It does not know your situation and it cannot see the future.
Most intraday traders lose money (SEBI, July 2024: 71% in FY23). Decide for
yourself, and talk to a SEBI-registered adviser if you need advice.

**No order goes out on its own.** `/nse-groww` prints a ticket; your hands
place the order. Say "yes" only after you have placed it.

**Groww's timings.** Groww stops new intraday orders at 15:05 for
F&O-enabled stocks (most of the Nifty 500) and 15:20 for the rest, and
squares off open intraday positions at 15:20 for ₹50 + GST each (Groww risk
policy and help pages, read November 2026). The pack's defaults are no new
entries after 14:30 and flat by 15:00. Groww can change its rules: check the
app.

**Use Add SL/TGT.** If you place the stop and the target as two separate sell
orders instead, both can fill and leave you short. `/nse-groww show
--separate-stop` gives the safe sequence for that case.

**A stop is a plan, not a guarantee.** In a fast market or across a gap, a
stop can fill well past its price, and a stock locked at its lower circuit
may not fill at all.

**The data is free and unofficial.** Yahoo Finance's chart endpoint is not an
official API: it can lag, change or rate-limit you. Check the price in Groww
before you act.

**Tick sizes.** NSE reviews each stock's tick every month from the previous
month's last close. `/nse-exit` follows that rule and falls back to the
price band, with a warning, when the history is not available.

**Taxes.** Intraday equity profit is speculative business income in India,
taxed at your slab rate. Nothing here is tax advice.

**Upkeep.** The 2027 NSE holiday list must be added when NSE publishes it
(see `NSE-PORT.md`). Until then the tools warn and you can add holidays in
`trader/config.json`.

## For developers

```bash
python3 -m unittest discover -s tests -v    # 378 tests, offline, about 10 seconds
```

All tools are standard-library Python 3.8+, one script per skill, with an
honest User-Agent and polite pacing for the free endpoints. `NSE-PORT.md`
records every convention the port follows (calendar, symbols, ticks, charges,
data sources).

```
nse-scanner/      SKILL.md, scan.py
nse-news/         SKILL.md, news.py
nse-entry/        SKILL.md, entry.py
nse-exit/         SKILL.md, exit.py
nse-timemachine/  SKILL.md, timemachine.py
nse-budget/       SKILL.md, size.py, config.example.json
nse-risk/         SKILL.md, check.py
nse-groww/        SKILL.md, ticket.py
nse-alerts/       SKILL.md, alerts.py, alerts.example.json
tests/            unittest suite + real NSE fixtures
.claude-plugin/   plugin and marketplace manifests
```

## Credits and licence

MIT. NSE port by [indwar7](https://github.com/indwar7). Based on
[Jakeschincariol/day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)
by Jake Schincariol (the US version); his licence notice is kept in
`LICENSE`.
