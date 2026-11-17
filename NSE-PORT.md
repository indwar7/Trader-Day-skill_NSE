# NSE port: conventions

This repo is a port of [Jakeschincariol/day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)
(MIT, US markets, Alpaca/IBKR, commit 3d0f4ef) to **NSE cash-segment intraday trading for a
Groww user**. The user places orders by hand in the Groww app. Nothing in this pack talks to a
broker.

Every tool follows this file. If you change a convention here, change every tool that
depends on it, and its tests, in the same commit.

## Layout and names

| upstream | here | notes |
| --- | --- | --- |
| trader-scanner/scan.py | nse-scanner/scan.py | |
| trader-news/news.py | nse-news/news.py | SEC parts removed |
| trader-entry/entry.py | nse-entry/entry.py | |
| trader-exit/exit.py | nse-exit/exit.py | |
| trader-timemachine/timemachine.py | nse-timemachine/timemachine.py | |
| trader-budget/size.py | nse-budget/size.py | plan mode only; weights mode (13F) removed; owns `config.example.json` |
| trader-risk/check.py | nse-risk/check.py | PDT guard removed |
| trader-broker/ticket.py | nse-groww/ticket.py | manual Groww order ticket, no broker API |
| trader-alerts/alerts.py | nse-alerts/alerts.py | Stooq removed (no NSE coverage) |
| trader-analyst, trader-pelosi | removed | |

- SKILL.md `name:` equals the folder name. Every cross-reference in prose and code changes
  `/trader-x` -> `/nse-x` and `trader-broker` -> `nse-groww`.
- The project-local state folder stays `trader/` with the same file names (config.json,
  watchlist.json, universe.txt, entry.json, plan.json, timemachine.json, orders.json,
  portfolio.json, prices.json, alerts.json, state/). Keep JSON schemas identical to upstream
  except the renames listed under Money, so the tools still chain.
- Each script stays self-contained, standard-library only, Python 3.8+ syntax (no `match`,
  no `X | Y` type unions). Shared helpers are copied into each script, as upstream does.
- `${CLAUDE_SKILL_DIR}` usage in SKILL.md stays as upstream.

## Time and calendar

- All times are IST, fixed UTC+05:30, no daylight saving. Replace the eastern_* helpers with
  IST equivalents (e.g. `to_ist(utc)`, `ist_to_utc(local)`); remove `_nth_sunday`.
  `--now "YYYY-MM-DD HH:MM"` flags are IST. Print times as IST ("10:05 AM IST" or "10:05").
- Normal session: open 09:15, close 15:30. 5-minute bars per full session: 75
  (first bar starts 09:15, last bar starts 15:25). 1-minute: 375.
- No early closes. Remove `NYSE_EARLY_CLOSE` logic. Special sessions (Diwali Muhurat,
  Budget-day Sunday) are not supported: treat them as non-trading days and say so if asked.
- `NSE_HOLIDAYS` (capital-market segment, weekdays, 2026; NSE circulars CMTR/71775 and
  CMTR/72260):
  2026-01-15 (Maharashtra municipal elections), 2026-01-26, 2026-03-03, 2026-03-26,
  2026-03-31, 2026-04-03, 2026-04-14, 2026-05-01, 2026-05-28, 2026-06-26, 2026-09-14,
  2026-10-02, 2026-10-20, 2026-11-10, 2026-11-24, 2026-12-25.
  `CALENDAR_YEARS = (2026,)`. For a date in a year outside CALENDAR_YEARS, do NOT raise:
  treat weekdays as trading days and add a one-line warning to the output
  ("NSE holiday list for 2027 not loaded: check nseindia.com"). A tool that reads
  trader/config.json also adds any dates in its optional `"holidays": ["YYYY-MM-DD", ...]`.
- Groww intraday: open MIS positions not squared off by 15:20 are squared off by Groww for
  Rs 50 + GST per position (groww.in help, read 2026-11-17). Groww's risk policy
  (groww.in/p/risk-policy, read 2026-11-17) also stops NEW intraday orders at 15:05 for
  F&O-enabled stocks (most of the Nifty 500) and 15:20 for the rest, and does not say whether
  a closing sell counts, so the pack gets out before 15:05. Defaults everywhere:
  `no_entries_before` "09:30" (after the 15-minute opening range), `no_entries_after` "14:30",
  `flat_by` "15:00". The opening range defaults to 15 minutes (09:15-09:30, three 5m bars).

## Symbols and data

- NSE symbol: `^[A-Z0-9][A-Z0-9&-]{0,19}$` (e.g. RELIANCE, M&M, BAJAJ-AUTO, 20MICRONS).
  Accept user input with a trailing `.NS` and strip it. Uppercase it.
- Yahoo symbol: `urllib.parse.quote(sym + ".NS")` (so `M&M` -> `M%26M.NS`). Yahoo's response
  `meta.symbol` is the unquoted form (`M&M.NS`). Indices for context only: NIFTY 50 = `^NSEI`,
  NIFTY BANK = `^NSEBANK`; never tradable here.
- Yahoo v8 chart and v7 spark work for `.NS` (verified 2026-11-17). Yahoo's NSE bars were a
  few seconds behind live at 09:44 IST that day. `meta.exchangeTimezoneName` is
  `Asia/Kolkata`, `gmtoffset` 19800. 60d of 5m bars is available (60 sessions x 75 bars).
- Universe: default `nifty500` from
  `https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv`
  (columns: Company Name, Industry, Symbol, Series, ISIN Code). Option `all`: NSE's
  `https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv`, rows whose SERIES is `EQ`
  only (BE/BZ are trade-for-trade: no intraday).
- News: NSE announcements RSS `https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml`
  (item description starts `SYMBOL : ...` and contains `|SUBJECT: ...`; pubDate like
  `17-Nov-2026 09:31:00`, IST) and Google News RSS
  `https://news.google.com/rss/search?q=<query>&hl=en-IN&gl=IN&ceid=IN:en`. Yahoo's per-ticker
  RSS for `.NS` returned no items; do not rely on it.
- HTTP: User-Agent exactly `Mozilla/5.0 (compatible; nse-day-trader/1.0)`. Never spoof a
  browser, never warm cookies or call nseindia.com's `/api/` endpoints (bot-protected; off
  limits). Polite pacing as upstream.
- SSL: python.org builds on this Mac have no CA bundle. Every urlopen gets a context built by
  a helper like:
  ```python
  def _ssl_context():
      ctx = ssl.create_default_context()
      if not ctx.get_ca_certs() and not ctx.cert_store_stats().get("x509_ca"):
          for path in ("/etc/ssl/cert.pem", "/private/etc/ssl/cert.pem"):
              if os.path.exists(path):
                  ctx.load_verify_locations(cafile=path)
                  break
      return ctx
  ```
  Never disable verification.

## Money

- Currency is INR. Display with a rupee sign and Indian digit grouping, 2 decimals:
  `inr(123456.5)` -> `₹1,23,456.50`, negatives `-₹1,250.00`. Copy one helper into each script
  that prints money.
- Config key renames (all tools): `max_order_usd`->`max_order_inr`, `min_order_usd`->`min_order_inr`,
  `max_loss_per_trade_usd`->`max_loss_per_trade_inr`, `max_daily_loss_usd`->`max_daily_loss_inr`.
  JSON fields `usd`->`inr`, `value_usd`->`value_inr`, `dollars`->`rupees`.
- Remove `broker`, `paper`, `pdt_guard`, `sec_user_agent`, `fractional`, `allow_options`,
  `allow_margin` from config. Add `"product": "MIS"`, `"exchange": "NSE"`,
  `"mis_leverage": 1` (1 = no leverage; Groww's per-stock intraday margin may allow more; the
  user must set it deliberately), and `"costs"` (below). Long-only stays (`allow_short` false;
  nothing in this pack plans a short).
- Prices sit on the NSE tick grid (NSE/CMTR/62174 of May 2024 as amended by CMTR/67133 of
  March 2025, effective 2025-04-15), by price band:
  `< 250: 0.01; < 1000: 0.05; < 5000: 0.10; < 10000: 0.50; < 20000: 1.00; else 5.00`.
  NSE reviews each stock's tick MONTHLY from its close on the last trading day of the previous
  month, so the band of today's price is only an approximation (on 2026-11-17 SBIN traded on
  0.05 above 1,000; 28 Nifty 500 stocks sat on a finer tick than their price band). nse-exit
  therefore fetches the symbol's daily closes (`range=3mo&interval=1d`), takes the last close
  dated in the previous month, picks the tick from that, and writes `"tick"` plus
  `"tick_basis": {"rule": "NSE monthly review", "close": ..., "date": ...}` into plan.json;
  `--tick X` overrides; offline or on a failed fetch it falls back to the entry-price band and
  says so (`"rule": "entry-price band (fallback)"`). Every downstream tool uses plan["tick"]
  when present and the price band only when absent. Use Decimal; snap Yahoo's float32 prices
  to the paisa before comparing or rounding. Keep upstream's rounding directions (stop rounds
  away from the entry, i.e. down for a long; target as upstream) but onto the tick grid
  instead of cents. Whole shares only.

## Groww intraday equity charges (groww.in/pricing/stocks and help page, read 2026-11-17)

Per executed order (one buy, one sell = two orders), value = price x qty:

| charge | rate |
| --- | --- |
| brokerage | min(₹20, 0.1% of value); if that is under ₹5, then min(₹5, 2.5% of value) |
| STT | 0.025% of value, sell only |
| exchange transaction (NSE) | 0.00307% both sides |
| SEBI turnover | 0.0001% both sides |
| IPFT (NSE) | 0.0001% both sides |
| stamp duty | 0.003% buy only |
| GST | 18% of (brokerage + exchange + SEBI + IPFT) |
| DP | ₹0 for intraday |

Defaults in config `"costs"`: `{"brokerage_pct": 0.1, "brokerage_cap": 20, "brokerage_min": 5,
"brokerage_min_pct": 2.5, "stt_sell_pct": 0.025, "exchange_pct": 0.00307, "sebi_pct": 0.0001,
"ipft_pct": 0.0001, "stamp_buy_pct": 0.003, "gst_pct": 18}`. A round-trip cost function
`round_trip_costs(entry, exit, qty, costs)` returns a dict of each charge and the total. Max
loss at the stop is (entry - stop) x qty + round-trip costs at the stop price, and sizing
must keep that within `max_loss_per_trade_inr`.

## Tests

- Offline `unittest`, stdlib only. Shared live fixtures captured 2026-11-17 09:48 IST are in
  `tests/fixtures/nse/`; each tool's own real-data slices are under `tests/fixtures/nse-<skill>/`.
  The shared set:
  nifty500list.csv, EQUITY_L.csv, {RELIANCE,HDFCBANK,SBIN,INFY,M&M,BAJAJ-AUTO}-1d-5m.json
  (only 09:15-09:45 of 2026-11-17), {RELIANCE,SBIN}-1d-1m.json, {RELIANCE,SBIN}-60d-5m.json.gz
  (60 full sessions 2026-08-20..2026-11-16 plus a partial 2026-11-17), NSEI-1d-5m.json,
  spark-1d-5m.json and spark-3mo-1d.json (8 symbols), gnews-reliance.xml,
  yahoo-rss-RELIANCE.xml (empty feed), nse-ann-rss.xml.
- The reference full session for deterministic tests is **2026-11-16** (Monday) from the 60d
  files. New fixtures must be real captures (honest UA, polite), never hand-made prices.
- `tests/_load.py` loads a tool from its folder: `load("nse-exit", "exit")`. Run everything with
  `python3 -m unittest discover -s tests -v`, or one file with `-p "test_exit.py"`.
- Tests cover IST conversion, holiday handling (including the warning for a year outside the
  loaded calendar), tick rounding at band edges and the monthly tick rule, `M&M`/`BAJAJ-AUTO`
  symbol encoding, Groww charges and INR formatting.

## SKILL.md

Rewrite each for NSE + Groww: Indian examples (RELIANCE, SBIN...), ₹, IST, the defaults
above, the Groww 15:20 square-off note where relevant. Keep the frontmatter `description`
specific and trigger-rich (add natural Indian phrasing: "intraday", "NSE", "Nifty 500 stocks",
"Groww"). Keep upstream's honesty: not financial advice; most intraday traders lose money
(SEBI's study of July 24 2024, PR 13/2024: 71% of individual intraday traders in the equity
cash segment lost money in FY23, 80% of those with more than 500 trades a year).
Never claim a number you did not get from running the tool.

## Yearly upkeep

- Each January, add the new year's NSE holiday list (NSE publishes it in December) to
  `NSE_HOLIDAYS` and `CALENDAR_YEARS` in every script that carries them
  (`grep -rl NSE_HOLIDAYS nse-*`). Until then the tools warn and treat weekdays as trading days;
  users can add dates under `"holidays"` in trader/config.json.
- Re-check Groww's charges (groww.in/pricing/stocks) and intraday timings
  (groww.in/p/risk-policy) and update the defaults if they changed.
