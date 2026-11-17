---
name: nse-scanner
description: >-
  Chart Scanner for NSE intraday trading. Scans the Nifty 500 (or every
  EQ-series stock on NSE) for setups: opening-range breakouts (ORB), bull
  flags, 20-day breakouts, oversold bounces. Builds the symbol list from
  niftyindices.com's Nifty 500 file or NSE's EQUITY_L.csv, runs a pre-market
  scan of daily closes and an intraday opening-range breakout scan on
  5-minute bars (IST, 9:15 AM open), then writes trader/watchlist.json for
  nse-news and nse-entry. Free Yahoo data, bar by bar, prices in ₹. Use when
  the user says "scan the market", "scan Nifty 500 stocks", "find intraday
  setups", "what is breaking out on NSE", "opening range breakout scan", "ORB
  stocks today", "scan for bull flags", "oversold stocks", "pre-market scan",
  "build my watchlist", "which NSE stocks to watch today", "intraday
  watchlist for Groww", or at the start of an NSE trading day before
  /nse-news.
argument-hint: "[universe | daily | orb] [--at HH:MM]"
---

# nse-scanner

The Chart Scanner is the first step of an intraday trade on NSE. It reads
hundreds of free charts and lists the ones that match a rule. That is all it
does. A scan is a list of charts that match a rule, not a list of stocks to
buy, and a setup on a chart is not a prediction. Most intraday traders lose
money: SEBI's study of July 24 2024 (PR 13/2024) found that 71% of individual
intraday traders in the equity cash segment lost money in FY23, and 80% of
those with more than 500 trades a year. The scanner does not change that. It
only saves you from flipping through 500 charts by hand.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe                             # Nifty 500 -> trader/universe.txt
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe --list all                  # every EQ-series NSE stock instead
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily                                # before 9:15 AM -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --symbols RELIANCE,SBIN,M&M    # or --watchlist FILE, --limit 200
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb                                  # after 9:35 AM IST -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --at 10:30                       # cut the clock at 10:30 AM IST
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --watchlist trader/watchlist.json --json
```

Flags: `--min-price 10` (₹), `--min-value` (₹ traded, price x volume: daily
₹10 crore, orb 0), `--min-volume` (shares, 0), `--or-minutes 15`,
`--max-confirm 500` (orb stage 2), `--now "2026-11-16 10:30"` (IST) to pin
the clock, `--offline PATH` to read saved Yahoo files, `--no-write`,
`--show N`, `--json`. Symbols are NSE symbols (`RELIANCE`, `M&M`,
`BAJAJ-AUTO`); a trailing `.NS` is accepted and dropped. Quote `M&M` in a
shell (`--symbols "M&M,SBIN"`). Exit code 0 when the scan found something, 1
when it found nothing or could not run (the reason is one plain line).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/universe.txt` | written by `universe`: one `SYMBOL\|Name` a line, read by `daily` and `orb` |
| `trader/watchlist.json` | written by `daily` and `orb`: `mode`, `as_of` (IST), `universe`, `scanned`, `failed`, `filters`, `hits[]`. nse-news and nse-entry read `hits[].symbol` |
| `trader/config.json` | read by `orb`: `no_entries_after` (default 14:30) and `holidays` (extra closed dates, `["YYYY-MM-DD", ...]`) |

## The universe

`universe` downloads one list and keeps a row when, in this order:

1. Its series is `EQ`. `BE` and `BZ` are trade-for-trade (no intraday), `RR`
   is a REIT.
2. It is not a placeholder row: a "Dummy" name or an ISIN starting `DUM`
   (the Nifty 500 file of 2026-11-17 had one, "Dummy HEG Ltd.").
3. It is a plain NSE symbol: capitals, digits, `&` and `-` (`M&M`,
   `BAJAJ-AUTO`, `20MICRONS`).

| `--list` | source | 2026-11-17 |
| --- | --- | --- |
| `nifty500` (default) | niftyindices.com `ind_nifty500list.csv` | 501 rows, 495 kept: 3 REITs (RR), HFCL and STLTECH (BE), one "Dummy HEG" placeholder dropped |
| `all` | NSE `EQUITY_L.csv` (nsearchives.nseindia.com) | 2,633 rows, 2,404 kept: 195 BE and 34 BZ dropped |

Every one of the 495 is in the 2,404. Run `universe` again after an index
change or a new listing you care about.

## Price and liquidity floors (₹)

| flag | default | why |
| --- | --- | --- |
| `--min-price` | ₹10 | Under ₹250 the NSE tick is 1 paisa; at ₹10 that is 0.1 % of the price, and below it a single tick eats a real part of an intraday move. ₹10 keeps the cheap but heavily traded Nifty 500 names (IDEA at ₹12.57 and YESBANK at ₹17.98 on 2026-11-17). |
| `--min-value` | daily ₹10 crore, orb 0 | Price x Yahoo's `regularMarketVolume`. Share counts do not compare across a ₹12 stock and a ₹1,00,000 one, so the traded value is the liquidity test and `--min-volume` (shares) defaults to 0. At ₹10 crore a day, a ₹1 lakh order is 0.1 % of the day's turnover. |

Before 9:15 AM it should be the last full session's volume (not yet checked
on NSE: this port's live runs were during the session). During the session it
is today's so far, and the daily scan says so ("on today's volume so far"):
at 11:00 AM on 2026-11-17, 347 of 495 Nifty 500 charts had traded ₹10 crore
yet. Run `daily` before the open or after the close.

## The daily scan (before the open)

Yahoo's spark endpoint returns closes for 20 symbols a request (21 gets HTTP
400, checked for `.NS` symbols on 2026-11-17), so the Nifty 500 takes 25
requests: 20 seconds live on 2026-11-17 with the polite pause (0.3 s).
Today's daily bar is left out until 3:30 PM.

The setups are computed from daily closes only (spark has no highs or lows):

| setup | the rule on closes |
| --- | --- |
| `breakout_20d` | the last close is above every close of the 20 sessions before it |
| `bull_flag` | up more than 10 % over the 10 sessions before the last 5, then the last 5 closes inside a 5 % range |
| `reclaim_ma20` | the close before was under its 20-day average, the last close is above it |
| `oversold` | RSI 14 (Wilder) under 30 |
| `oversold_bounce` | RSI 14 was under 30 yesterday and today closed up |

A closing-high breakout fires more often than an intraday-high one. Hits are
listed most traded first (price times volume).

Yahoo leaves a recent daily close of many NSE stocks null for a while, and
spark drops a null bar. On 2026-11-17 (11:00 AM) 201 of the 495 Nifty 500
daily charts had no close for Monday 2026-11-16 yet, so their last close was
Friday's. The scan does not invent the missing close: it says how many of the
scanned charts end early ("121 of the 347 end before 2026-11-16: Yahoo has no
close for that session yet"), each hit keeps its own `close_date`, and a hit
row whose closes stop early says "(closes through 2026-11-13)". Tell the user
when that count is large; a later run may have the close.

## The opening-range breakout scan (during the session)

The session is 9:15 AM to 3:30 PM IST, 75 five-minute bars. The opening range
is the first `--or-minutes` (15) of it: the 9:15, 9:20 and 9:25 bars. The
first bar after the range closes at 9:35 AM. A hit is the first completed
5-minute bar to CLOSE above the range high, at that bar's close time.

1. Stage 1, every symbol: spark closes of today's 5-minute bars, 20 a
   request. A chart goes on only if a completed bar after the range closed at
   or above the highest close of the range. That is a necessary condition
   (the range high is at least every range close), so no breakout is lost.
2. Stage 2, each candidate: the v8 chart with real highs and lows, one
   request each. Range high = the highest high of the range bars, range low =
   the lowest low. Stage 2 checks at most `--max-confirm` (500, so all of a
   Nifty 500 scan) candidates, the most traded first, and prints PARTIAL with
   the count it skipped. `--max-confirm 0` checks them all.

A chart is usable only when every 5-minute bar from 9:15 to the cutoff is
there. Yahoo's spark answer often skips one 5-minute bar of an NSE stock that
its v8 chart has (61 of 495 Nifty 500 charts at 10:24 AM on 2026-11-17,
including IDEA, YESBANK, SUNPHARMA and ETERNAL; 100 at 11:00 AM), so a spark
chart with a gap goes to stage 2 too and the v8 chart decides. A bar missing
from the v8 chart as well is a halt or no trades, and the chart is out. Null
closes are dropped, a bar off the 5-minute grid (the live tick, like 9:48:39)
is dropped, and a bar counts only once it is complete (start + 5 minutes).

It prints one plain line and writes nothing before 9:35 AM, on a weekend or an
NSE holiday, after the close, or after `no_entries_after` (an expired setup;
14:30 by default, because Groww squares off open MIS positions at 3:20 PM for
₹50 + GST a position, and the pack's default `flat_by` is 15:00).

Timing, live on Tue 2026-11-17: the whole Nifty 500 at 11:00 AM took 4
minutes 17 seconds: 25 spark requests, then 362 v8 charts (100 of the
candidates were spark gaps), 387 requests in all at the 0.3-second pause,
every one HTTP 200 (no 429). 494 of 495 charts were usable (RAMCOCEM had no
10:45 AM bar anywhere) and 254 had closed above their opening-range high. The
hits are already minutes old when the scan ends: for a fast answer, scan the
daily watchlist or the names you trade (`--watchlist`, `--symbols`).

In Yahoo's stored 5-minute history the 3:15 and 3:20 PM bars are often null
(20 of 40 charts on 2026-11-16), so a replay with a cutoff after 3:15 PM loses
those charts. The default `no_entries_after` of 14:30 stops well before.

## Calendar

- All times are IST (UTC+05:30, no daylight saving). `--now` and `--at` are
  IST.
- NSE capital-market holidays for 2026 are built in (NSE circulars
  CMTR/71775 and CMTR/72260): 15 Jan (Maharashtra municipal elections),
  26 Jan, 3 Mar, 26 Mar, 31 Mar, 3 Apr, 14 Apr, 1 May, 28 May, 26 Jun,
  14 Sep, 2 Oct, 20 Oct, 10 Nov, 24 Nov, 25 Dec. Add any other closed day to
  `"holidays"` in trader/config.json.
- For a year with no list loaded (2027 on), weekdays count as trading days
  and the output says "NSE holiday list for 2027 not loaded: check
  nseindia.com".
- NSE has no early closes. Special sessions (the Diwali Muhurat session, a
  Budget-day Sunday session) are not supported: the scanner treats those days
  as closed. Say so if the user asks.

## Data and limits

- Yahoo's chart and spark endpoints are free, unofficial and can lag (Yahoo's
  NSE bars were a few seconds behind live on 2026-11-17). Bars are 5-minute
  bars, bar by bar, never tick by tick, never real time. query2 first, query1
  on a 429 or 5xx, one retry after a 5-second wait on a 429.
- NSE symbols go to Yahoo with `.NS` and URL-encoded: `M&M` is sent as
  `M%26M.NS`. Unencoded, the `&` cuts the request and Yahoo answers for a
  different, US-listed `M`. Answers come back as `M&M.NS` and map to `M&M`.
- Prices are Yahoo's closes, which sit on NSE's tick grid; the scanner rounds
  nothing to a tick because it plans no order.
- Partial results are said as partial, like "Fetched 8 of 495 charts (487
  failed or had no data)" (the daily scan over this skill's test fixture).
  Never round a partial scan up to "the whole market".
- Downloads use the User-Agent `Mozilla/5.0 (compatible; nse-day-trader/1.0)`
  and verified TLS (the system CA file when Python ships none). Never spoof a
  browser and never call nseindia.com's `/api/` endpoints.

## Steps

1. Once a week or so: `scan.py universe`.
2. Before 9:15 AM: `scan.py daily`. Tell the user the counts (fetched,
   liquid, scanned, hits) and the top few by setup.
3. After 9:35 AM IST (10:00 to 10:30 AM is common): `scan.py orb`. Tell the
   user how many usable charts broke their range and when, and that a later
   hit is closer to the price now.
4. The user picks one chart. Hand it on: `/nse-news SYMBOL` (what is moving
   it), then `/nse-entry SYMBOL` (the entry bar by bar). Never pick for them.

## Example: a live scan

Tue 2026-11-17, the Nifty 500, `scan.py orb --at 11:00` (real output; the hit
list is 254 lines long, four shown):

```
Opening-range breakout scan, Tue 2026-11-17 11:00 AM IST: 15-minute range (9:15 to 9:30 AM), 5-minute bars, Yahoo.
Fetched 495 of 495 charts; 494 usable; 362 candidates (100 of them for a missing bar in Yahoo's spark closes), 361 confirmed with real highs and lows.
254 of 494 charts closed above their opening-range high by 11:00 AM:
   9:35 AM  M&M           ₹3,392.00  range 3,355.30 to 3,389.10  +0.09% over
   9:45 AM  BAJAJ-AUTO   ₹10,799.00  range 10,709.00 to 10,795.00  +0.04% over
   9:45 AM  SBIN          ₹1,105.70  range 1,092.00 to 1,101.40  +0.39% over
  10:15 AM  HDFCBANK        ₹818.90  range 811.00 to 816.20  +0.33% over
```

SBIN's range is the 9:15 to 9:25 bars, high 1,101.40, low 1,092.00. The 9:35
bar closed exactly at 1,101.40, which is not above it; the 9:40 bar closed at
1,105.70, 0.39 % over, at 9:45 AM. If the user picks SBIN, hand it to
`/nse-news SBIN`, then `/nse-entry SBIN`.

The daily scan that morning (closes through Mon 2026-11-16, Yahoo's 11:00 AM
answers) tagged 135 of 347 liquid charts, among them HDFCBANK (20-day
breakout and bull flag), SBIN (20-day breakout), ICICIBANK (bull flag) and
TATACHEM (reclaimed the 20-day average).

## Rules

- Say what was scanned: how many symbols, how many charts came back, how many
  were usable, and the time of the data (IST).
- Say "matched a rule", never "a buy", "a winner" or "a good trade".
- Never print a P&L, a return or a profit figure.
- A hit is stale once the price has moved on. Re-run before acting on it.

## What it never does

It never places, sizes or checks an order, never says which stock to buy,
never calls data real time, and never promises that a setup works.

## Safety

- Education and tooling, not financial advice. A match is a rules check, not a
  recommendation.
- Nothing in this pack talks to a broker or places an order. The user places
  every order by hand in the Groww app; `/nse-groww` only writes a ticket to
  copy from.
- Long only: nothing in this pack plans a short. Intraday (MIS) only, with no
  leverage unless the user sets `mis_leverage` in trader/config.json
  deliberately.
- Groww squares off open MIS positions at 3:20 PM for ₹50 + GST a position.
- Never paste a Groww password, PIN or OTP into the chat. Nothing here needs
  one.
