---
name: nse-entry
description: >-
  Entry Timer for NSE intraday trades. Watches a setup bar by bar and tells
  you the moment to get in. Reads free Yahoo 5-minute or 1-minute bars for an
  NSE cash-segment stock (RELIANCE, SBIN, M&M, BAJAJ-AUTO, any Nifty 500
  stock), builds the 15-minute opening range (9:15 to 9:30 IST) and calls the
  entry on the first completed bar that CLOSES above the range high, or above
  a level you give it (like a 20-day high from the scanner), with an optional
  volume check. Says waiting and how far in rupees, triggered with the time and
  the price, expired (no new entries after 14:30 IST by default), or not open
  yet, knows the NSE 2026 holidays, and writes trader/entry.json for /nse-exit.
  Long side only. It never places an order: you place it yourself in Groww.
  Use when the user says "tell me when to get in", "watch SBIN", "has
  RELIANCE broken out", "did it break the opening range", "ORB", "intraday
  entry signal", "time my entry", "check my watchlist", or after /nse-scanner
  and /nse-news, before /nse-exit.
argument-hint: "[SYMBOL] [--setup orb|level] [--interval 5m|1m]"
---

# nse-entry

The Entry Timer turns an intraday setup on the NSE into one moment: the bar
that confirms it. It reads bars, 1-minute or 5-minute, and waits for each one
to finish. A price that pokes above the high in the middle of a bar is not an
entry. A bar that CLOSES above it is. Then it says the time and the price,
writes them down for the Exit Planner, and stops. It never buys anything: you
place every order yourself, by hand, in the Groww app.

Day trading loses most people money, and a clean signal does not change that.
SEBI's study of intraday traders (press release 13/2024, July 24 2024) found
that 71% of individual intraday traders in the equity cash segment lost money
in FY23, and 80% of those who made more than 500 trades a year.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/entry.py" check SBIN                  # 15-minute opening range, 5-minute bars
python3 "${CLAUDE_SKILL_DIR}/entry.py" check SBIN --write          # also save trader/entry.json
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --setup level --level 1344.00
python3 "${CLAUDE_SKILL_DIR}/entry.py" check "M&M" --interval 1m --min-volume-x 1.5
python3 "${CLAUDE_SKILL_DIR}/entry.py" watch SBIN --until 14:30    # one line per closed bar, stops on the trigger
python3 "${CLAUDE_SKILL_DIR}/entry.py" watchlist                   # every hit in trader/watchlist.json
```

Symbols are NSE symbols: `RELIANCE`, `SBIN`, `M&M`, `BAJAJ-AUTO`. A trailing
`.NS` is fine and dropped. Always quote `"M&M"` in the shell: an unquoted `&`
sends the command to the background. Indices (`^NSEI` for NIFTY 50,
`^NSEBANK`) are context only, never a setup.

Other flags: `--or-minutes N` (default 15), `--json`, `--offline FILE` (a
saved Yahoo chart JSON, `.json` or `.json.gz`, or a folder of
`<SYMBOL>.json`), `--now "2026-11-17 09:45"` (IST) to replay a day. `watch`
with `--now` or `--offline` runs on a simulated clock and replays the day bar
by bar without waiting.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/watchlist.json` | the scanner's hits (`hits[].symbol`), for `watchlist` |
| `trader/config.json` | optional: `no_entries_before` (09:30) and `no_entries_after` (14:30), IST, and `holidays` (extra closed days, `["YYYY-MM-DD", ...]`) |
| Yahoo chart API | today's bars for `SYMBOL.NS`, `range=1d&interval=5m` (or `1m`): free, unofficial, can lag |
| `trader/entry.json` | with `check --write`, or when `watch` triggers: symbol, setup, status, time, bar, price, level, opening range |

## The rule

- **Times are IST.** The NSE cash session runs 9:15 to 15:30 IST, with no
  daylight saving and no early closes.
- **Opening range (orb).** The high and low of the first 15 minutes: the
  9:15, 9:20 and 9:25 five-minute bars, or fifteen 1-minute bars. The level
  to beat is the range high.
- **Level.** A price you give, like a 20-day high from `/nse-scanner`.
- **The trigger** is the first completed bar after the range that CLOSES
  above the level. A wick above is not a trigger. A close exactly at the
  level is not above it (prices are compared to the paisa).
- **The time** is that bar's close time (the 9:40 bar closes at 9:45 AM).
  The price is its close, to the paisa.
- **Only closed bars count.** A bar is closed when the clock passes its start
  plus 5 (or 1) minutes and Yahoo has caught up to that moment (it waits a
  minute at most). Yahoo's live tick, like 09:48:32, is never a bar.
- **Volume, if you want it.** `--min-volume-x 1.5` also needs the trigger
  bar's volume at 1.5 times the range bars' average. Range bars that show no
  volume are left out of that average: Yahoo often shows 0 on the 9:15 bar of
  an NSE session.
- **Time limits.** No entries before 09:30 or after 14:30 IST (config). That
  leaves room to be flat by 15:00. Groww stops new intraday orders in F&O
  stocks (most of the Nifty 500) at 15:05, and squares off intraday (MIS)
  positions still open at 15:20 for ₹50 + GST per position.
- **Calendar.** Weekends and the NSE trading holidays for 2026 are closed
  days. For a date in another year it treats weekdays as trading days and says
  "NSE holiday list for 2027 not loaded: check nseindia.com". Special sessions
  (Diwali Muhurat trading, a Budget-day Sunday) are not supported: it treats
  them as closed.
- **What it says.** Waiting (and how far the last close is from the level),
  triggered, expired, or no data. Before 9:15: not open yet. Before 9:30: the
  range is still forming. Weekends and NSE holidays: the market is closed,
  with the next open.

## Steps

1. Get the symbol from the user, or run `entry.py watchlist` after
   `/nse-scanner`. Check `/nse-news` for it first.
2. Run `entry.py check SYMBOL`. Tell the user the status in one line:
   waiting and how far, triggered at what time and price, or expired.
3. To wait for it, Claude can run `entry.py watch SYMBOL --until 14:30` in
   the background, or `entry.py check SYMBOL --write` on a `/loop` every 5
   minutes. `watch` wakes just after each bar closes, prints one line per
   bar, and exits 0 on the trigger (it writes `trader/entry.json`) or 1 when
   the setup expires. Keep the computer awake while it watches: a sleeping
   laptop pauses it, and it catches up, late, when it wakes.
4. When it triggers, say the time and the price, and that nothing was placed.
   If the trigger is more than a bar old, say that too: a late entry is a
   different trade.
5. Hand off to `/nse-exit` to set the stop and the target BEFORE anything is
   bought in Groww.

## Example: the reel

SBIN, Tuesday 2026-11-17, 5-minute bars. The opening range is ₹1,092.00 to
₹1,101.40. The 9:35 bar closes at ₹1,101.40, AT the high, which is not above
it. At 09:44 the 9:40 bar is still open, so it waits:

```
WAITING  last close ₹1,101.40 (9:35 bar) is AT the range high ₹1,101.40, not above it.
The 9:40 bar is still forming (₹1,105.70 so far, closes 9:45 AM); it does not count until it closes.
```

At 09:45 the bar has closed:

```
SBIN  Tue 2026-11-17 09:45 IST  opening-range breakout (15-minute range) on 5-minute bars (Yahoo, free, can lag)
Opening range 9:15-9:30: high ₹1,101.40, low ₹1,092.00 (3 of 3 bars).
TRIGGERED 9:45 AM at ₹1,105.70: the 9:40 bar (bar 5) closed above the range high ₹1,101.40 (+0.39%).
This is the moment to get in, if you take the trade. Nothing was placed.
Next: set the stop and target BEFORE you buy: /nse-exit (exit.py plan --from trader/entry.json).
```

With `--min-volume-x 1` the same bar passes too: 1.48x the range's volume.
The same morning `"M&M"` triggered at 9:35 AM at ₹3,392.00 (the 9:30 bar,
over a ₹3,389.10 range high).

## Rules

- Bar by bar, on free Yahoo 1-minute or 5-minute bars. They are unofficial
  and can lag. Say so when a minute matters.
- A signal is information, not an order, and not a promise. Nothing here
  promises winning trades or money.
- Long side only: this pack plans longs only. If the user wants a short,
  say that.
- One trigger per setup per day: the first close above. Later closes do not
  reset it.
- Never call an entry early because a bar looks like it will close above.

## What it never does

It never places an order, never sizes a position, never sets the stop (that
is `/nse-exit`), never moves the level after the fact, and never counts a bar
that has not closed. It never prepares a Groww order ticket (`/nse-groww`)
from a background watch: nobody is there to say yes.

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
