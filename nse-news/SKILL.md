---
name: nse-news
description: >-
  News Monitor for NSE intraday trading. Tracks exchange filings and breaking
  news on every NSE stock you watch or hold (Nifty 500 stocks and the rest of
  the NSE cash market) and flags what moves it: NSE's own announcement feeds
  (corporate filings with their SUBJECT, upcoming ex-dates, board meeting
  notices, results filed), Google News India headlines searched by company
  name, and how far each stock is from yesterday's close. Shows only what is
  new since the last check, newest first, in IST, tags each item (results,
  board meeting, dividend, bonus, split, buyback, rights issue, QIP, order
  win, SEBI/penalty, block/bulk deal, pledge, rating, deal, management,
  resolution plan...) and never repeats one. Use when the user says "any news
  on RELIANCE", "why is SBIN moving", "news on my watchlist", "what did the
  company tell NSE", "any announcements today", "results today?", "ex-date",
  "board meeting", "morning briefing before the open", "pre-market news for
  my Groww intraday trades", "monitor the news", "did anything break", or
  after /nse-scanner finds setups.
argument-hint: "[NSE symbols]"
---

# nse-news

A chart can look perfect and still be about to jump on a results filing, an
ex-date, a SEBI order or a block deal. Before a setup becomes an intraday
trade, this skill reads what the company told the exchange and what the
Indian press is writing, for every stock on the watchlist, and tells you only
what is new. It also prints how far each stock has moved today, so a headline
and a move sit side by side.

Tool in this folder (standard library Python, no keys, no login):

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py"                               # watchlist + plan + portfolio
python3 "${CLAUDE_SKILL_DIR}/news.py" --tickers RELIANCE SBIN "M&M" --hours 12
python3 "${CLAUDE_SKILL_DIR}/news.py" --no-google --json            # NSE feeds only, machine readable
```

Symbols are NSE symbols (RELIANCE, SBIN, M&M, BAJAJ-AUTO); a trailing `.NS`
is dropped. Quote `"M&M"` in the shell.

## What it reads and writes

| file / feed | what |
| --- | --- |
| `--tickers`, else `trader/watchlist.json` | the scanner's hits (nse-scanner) |
| `trader/plan.json` | the trade you are planning (nse-exit) |
| `trader/portfolio.json` | what you hold (nse-groww records your fills there) |
| NSE announcements RSS | `https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml`: every company filing, with its SUBJECT. One request per check for the whole market; only items about your stocks are kept |
| NSE corporate actions RSS | `.../RSS/Corporate_action.xml`: upcoming ex-dates (dividend, bonus, split, buyback, rights) with the record date |
| NSE board meetings RSS | `.../RSS/Board_Meetings.xml`: the day's board meeting notices and their purpose (often results) |
| NSE financial results RSS | `.../RSS/Financial_Results.xml`: results filed (NSE updates this one irregularly) |
| Google News RSS, India edition | one search per stock for the company name, limited to `--hours`: `https://news.google.com/rss/search?q="Reliance Industries" when:24h&hl=en-IN&gl=IN&ceid=IN:en` |
| Nifty 500 list and NSE's `EQUITY_L.csv` | company names for the searches and for matching filings to symbols; cached a week in `trader/state/nse-names.json` |
| Yahoo Finance chart endpoint | the last price of `RELIANCE.NS` against yesterday's close (unofficial, can lag a little) |
| `trader/state/news.json` | what has been shown (kept 30 days) and the last check time |

Every request carries an honest User-Agent
(`Mozilla/5.0 (compatible; nse-day-trader/1.0)`), never a browser's, and the
tool never calls nseindia.com's `/api/` endpoints (they are bot-protected and
off limits). Requests are paced: Google News gets 3 seconds between searches,
and when it answers HTTP 429 (too many requests) the tool waits once, then
stops asking Google for that check and says which stocks it skipped.

Limits worth knowing:

- **NSE's announcements feed is short.** It holds only the latest few hundred
  filings, most of them mutual-fund NAV declarations (the tool drops those),
  so it often reaches back only a few hours. When filings since your last
  check may have rolled off, the tool prints a note with how far back the
  feed reached. Check during the session (`/loop 15m /nse-news`) and once in
  the evening, when results and board outcomes are usually filed, so nothing
  rolls off unseen. For anything older, open the company's page on
  nseindia.com in a browser.
- **Google News searches by company name** ("State Bank of India", "Mahindra &
  Mahindra") and matches the article text, not just the headline. A search
  also returns market wraps and stories about related companies: on
  2026-11-17 the M&M search returned SML Mahindra's rights issue and M&M
  Financial's NCDs, and the SBI search returned SBI Clerk exam news. Every
  such item still "touches" the stock it was searched for, so check that a
  headline is about the stock itself before you report it. Each check shows
  at most 20 headlines per stock, newest first (`--max-headlines`), and says
  how many older ones it left out.
- Google News sometimes answers with HTTP 429 and a "Sorry..." page when this
  connection has asked too often. The check then says which stocks got no
  headlines: say "not checked", never "no news", for those.
- Yahoo's per-symbol headline RSS has no items for NSE stocks, so it is not
  used.

## The rules

- **Headlines are leads, not facts.** Before telling the user something
  happened, open the story or the filing and read it. Quote the source and
  link it. Never summarise a paywalled story you could not read: say so.
- **Filings beat headlines.** An NSE announcement is what the company told
  the exchange. If a headline and a filing disagree, the filing wins.
- **Say what it touches.** Every item lists the stocks it touches and its
  tags: results, board meeting, dividend, bonus, split, buyback, rights
  issue, QIP, fund raising, order win, SEBI/penalty, block/bulk deal, pledge,
  rating, analyst, deal (acquisition, merger, JV), management, resolution plan
  (insolvency, NCLT), legal, halt (suspension, circuit, F&O ban), risk,
  regulatory, macro. Tags come from keywords, so treat them as hints.
- **`!` marks the filings most likely to move the stock today**: an NSE
  announcement whose SUBJECT is results or a board meeting outcome; a
  dividend, bonus, split or buyback; a rights issue, QIP or preferential
  issue; an acquisition, merger, scheme of arrangement or open offer; a change
  in management, directors or auditors, or a resignation; a credit rating; an
  order or contract win; action taken or orders passed by a regulator, a
  penalty; a pledge or its invocation; insolvency, a resolution plan, a
  default or fraud; a clarification or news verification the exchange asked
  for, or a price or volume spurt; a fire, strike, shutdown or cyber
  incident; a suspension or delisting. Also every results filing, an ex-date
  that is today (from today the stock trades without the dividend, bonus or
  split, and the price reflects it), and a board meeting held today.
- **MOVING** marks a stock 3 % or more from yesterday's close (`--move-pct`).
  A move with no headline and no filing is worth saying out loud too.
- **No predictions.** Report what was published and how far the price is from
  yesterday's close. Never say what the stock will do next.

## Steps

1. Run the check: `python3 "${CLAUDE_SKILL_DIR}/news.py"`. Exit code 1 means
   nothing new. `--all` shows items already seen, `--json` gives everything
   machine readable, `--dry-run` leaves the state alone.
   On a Monday morning use `--hours 72` to cover the weekend.
2. Read the items that matter: every `!` filing, anything tagged results,
   SEBI/penalty, halt, pledge, block/bulk deal, resolution plan or risk, and
   every MOVING stock first. Open the NSE PDF or the story.
3. Write a short brief, per stock: what happened, the source and link, the
   move, and one line on why it matters to the setup (for example "the board
   meets on results after the close today: an intraday (MIS) trade is closed
   before then, and the filing lands on tomorrow's open" or "ex-date today:
   part of the drop from yesterday's close is the dividend leaving the price,
   not news").
4. If a story changes the picture, say so before `/nse-entry` and
   `/nse-exit`: a results date, a board meeting, an ex-date, a SEBI order, a
   block deal, a suspension or a circuit. Never act on a headline alone.

During the session: `/loop 15m /nse-news`, or a scheduled task each morning
before 9:15 IST and each evening after the close. An intraday (MIS) position
on Groww is squared off by Groww from 15:20 IST if you have not closed it
(₹50 + GST per position), so a story that breaks after the close matters for
tomorrow's setup, not for today's trade.

## Example: a live check

Tuesday 2026-11-17, during the session. The check at 10:39 IST for
RELIANCE, SBIN, HDFCBANK and M&M (`--hours 24`) found no NSE filing and no
ex-date for any of the four; NSE's announcements feed reached back only to
01:27 IST, and the tool said so in a note. Google News led SBIN with "SBI has
told the stock exchanges about its data breach, but not the customers whose
data was leaked" (09:11 IST, tagged risk) and "State Bank of India completes
acquisition of 9.90% stake in CareEdge Global IFSC" (16 Nov, 18:41 IST,
deal), beside the price line
`SBIN ₹1,111.60 +1.93% vs yesterday's close ₹1,090.50 at 10:39 IST`.
"Lcc Projects wins Rs 62.32 Cr order from Reliance Industries" (16 Nov,
21:19 IST, order win) touches RELIANCE only as the customer: say that,
rather than calling it RELIANCE news. Google answered the M&M search with
HTTP 429, so the brief says M&M's headlines were not checked.

An NSE-only check at 11:03 IST (`--no-google`) for stocks that did file
shows the `!` marks:

```
! ex-date 2026-11-17    PFC         NSE action  Power Finance Corporation Limited Ex-date 17-Nov-2026: INTERIM DIVIDEND - RS 4.10 PER SHARE (record date 17-Nov-2026)
! 2026-11-17 10:15 IST  YESBANK     NSE filing  News Verification: The Exchange has sought clarification from Yes Bank Limited with respect to recent news item captioned YES Bank denies 32 crore cyber fraud report, says no government accounts affected. The response...
! 2026-11-17 10:13 IST  CARYSIL     NSE filing  Resignation: CARYSIL LIMITED has informed the Exchange regarding Resignation of Mr. Anand Kumar Sharma as Executive Director and Group CFO of the company w.e.f. November 17, 2026.
```

The brief: PFC goes ex-dividend today (₹4.10 a share), so part of any drop
from yesterday's close is the dividend leaving the price, not news. YESBANK:
the exchange asked about a press report and the company's reply is attached;
open the PDF before reading anything into the price. CARYSIL: its executive director and group CFO
resigned with effect from today. When a stock has no news, say so rather
than padding the brief.

## What it never does

- Never invents or embellishes a story, a quote or a number.
- Never trades on news, and never tells the user to buy or sell because of a
  headline or a filing.
- Never hammers a server: one request per NSE feed per check, one Google News
  search per stock with a pause between them, a stop on repeated HTTP 429,
  an honest User-Agent, and no nseindia.com `/api/` calls.

## Safety

Education and tooling, not financial advice. Most intraday traders lose
money: SEBI's study of July 24 2024 (PR 13/2024) found that 71% of individual
intraday traders in the equity cash segment lost money in FY23, and 80% of
those with more than 500 trades a year. This skill only reads and reports.
Nothing in this pack places an order: `/nse-groww` prints a ticket after
`/nse-risk` passes, and you place the order yourself in the Groww app.
