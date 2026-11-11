---
name: crypto-trader
description: >-
  Crypto day trading on Binance.com spot, the crypto sibling of the trader-* stock skills. Scans liquid USDT
  pairs for daily-close setups (20-day closing-high breakout, bull flag, reclaim of the 20-day average,
  oversold, oversold bounce), watches a range breakout or a price level bar by bar on closed 5-minute
  candles inside a UTC trading window you pick, plans the stop and target, replays the rest of the window
  10,000 times (Time Machine) plus the same rule on past days, sizes to your max loss and budget with
  Binance's tick, step and minimum-notional filters, risk-checks every order, and builds one OTOCO bracket
  (limit buy, then a take-profit LIMIT_MAKER and a STOP_LOSS_LIMIT). Binance Spot Testnet by default;
  nothing is placed without the user's explicit yes. Use when the user says "scan crypto", "crypto setups",
  "watch BTC", "trade ETH", "Binance", "size this crypto trade", "run the time machine on SOL", "place it
  on testnet", "flatten my BTC", or "crypto day trade".
argument-hint: "[scan | entry | plan | timemachine | size | risk | ticket] [SYMBOL]"
---

# crypto-trader

The same day-trading pass as the stock pack (scan, entry, exit, Time Machine,
budget, risk, broker), rebuilt for Binance.com spot. One script, standard
library only. Most people who day trade lose money, and crypto moves faster
than stocks. Sizing to a max loss keeps each loss small; it does not make a
trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
Every file lives under `trader/crypto/` in the current directory. Every time
is **UTC**.

```bash
C="${CLAUDE_SKILL_DIR}/crypto.py"
python3 "$C" init                                   # trader/crypto/config.json (copies money rules from trader/config.json)
python3 "$C" scan                                   # liquid USDT pairs -> watchlist.json
python3 "$C" scan --symbols BTC,ETH,SOL --show 30
python3 "$C" price BTC
python3 "$C" entry check BTCUSDT --write            # range breakout on closed 5-minute bars
python3 "$C" entry check BTCUSDT --level 76000 --write
python3 "$C" entry watch BTCUSDT                    # one line per closed bar, exits 0 on the trigger
python3 "$C" plan --target 1R --write               # from entry.json: range-mid stop by default
python3 "$C" plan --stop atr:1.5 --target 2R --write
python3 "$C" timemachine                            # 10,000 replays to flat_by, seed 19
python3 "$C" size --write                           # one OTOCO bracket -> orders.json
python3 "$C" risk                                   # PASS / BLOCK -> state/risk.json
python3 "$C" ticket show                            # the plain ticket + the exact request
python3 "$C" ticket approve --index 1 --reply "yes" # the user's words, verbatim
python3 "$C" ticket place --index 1                 # testnet; live needs "paper": false AND --live
python3 "$C" ticket flatten --symbol BTCUSDT --qty 0.004   # cancel open orders, market sell
python3 "$C" balance                                # signed account read -> portfolio.json
```

Global flags go before the command: `--now "2026-11-12 15:40"` (UTC) pins the
clock, `--offline DIR` reads saved Binance JSON, `--config PATH`.

## The window (crypto never closes)

Stocks have a bell; crypto does not. The config sets one window a day, in UTC:

| key | default | meaning |
| --- | --- | --- |
| `session_start` | `14:30` | the window opens (9:30 AM New York in winter) |
| `range_minutes` | `60` | the first hour is the range; its high is the level to beat |
| `no_entries_after` | `20:30` | no new entries after this |
| `flat_by` | `20:55` | out of every trade, win or lose |

No entries before the range is done. A trigger is the first closed 5-minute
candle that CLOSES above the range high (or `--level`). A wick is not a
trigger.

## Money rules

`init` copies budget, cash buffer, position cap, max loss per trade, daily
loss limit, trades per day, reward:risk and paper from the stock pack's
`trader/config.json`, and caps `max_order_usd` at the budget. Crypto only:
`quote` (`USDT`), `stop_limit_offset_pct` (0.2: the stop-limit sits 0.2 %
under the stop so it can still fill in a fast drop), `min_quote_volume_usd`
(the scan's 24h volume floor, $20M), `blocked` (symbols never traded).

## Sizing

Units are decimals, floored to the pair's `stepSize`; prices to `tickSize`.
The size is the smallest of max loss / (entry minus the stop-LIMIT price),
the position cap, the max order and the cash after the buffer (and the free
USDT in `portfolio.json` when `balance` has run). It refuses, with one line,
on zero units, under Binance's `minNotional` or `min_order_usd`, reward:risk
under the minimum, a price already through the stop or the target, or a plan
for another day. A fresh price above the plan's entry sizes at the higher
price.

## The order (OTOCO)

`POST /api/v3/orderList/otoco`: a LIMIT BUY (`workingPrice`), and once it
fills an OCO SELL: `LIMIT_MAKER` at the target and `STOP_LOSS_LIMIT` with
`pendingBelowStopPrice` (the stop) and `pendingBelowPrice` (the stop-limit).
`why` and `plan` in orders.json are notes; only the fields shown by
`ticket show` are sent. Signed with HMAC-SHA256, `recvWindow` 5000.

## Risk check (every order)

Format, Binance filters (tick, step, min quantity), spot only and the quote
asset, OTO/OCO allowed, blocked list, a stop required, max loss at the
stop-limit, reward:risk, max order, position cap, cash buffer, the day's total
risk against `max_daily_loss_usd`, trades per day, orders per day, one entry
per symbol per day, the entry window, and the plan's date. An exit (flatten)
is only checked for format: getting out is never blocked by a day limit.

## Three gates before anything is placed

1. `risk` passed this exact order today (matched by fingerprint).
2. The user typed an explicit yes for this one order in the chat. Record it
   verbatim with `ticket approve --reply "..."`. A reply that hedges ("yes but
   wait", "not yet") is not a yes. The yes is good for 15 minutes and must come
   after the risk check.
3. Claude's own permission card for the `ticket place` command. Never
   pre-approve that command.

## Keys

Never in the chat. Testnet keys come from https://testnet.binance.vision
(log in with GitHub, "Generate HMAC_SHA256 Key"). The user puts them in their
own shell as `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET`, or in
`~/.config/crypto-trader/testnet.json` (`{"api_key": ..., "api_secret": ...}`,
chmod 600). Live keys: `BINANCE_API_KEY` / `BINANCE_API_SECRET` or `live.json`,
created with trading enabled, withdrawals OFF, and an IP allowlist.

## Where it can run

- Market data: `data-api.binance.vision` (public, no key).
- Orders: `testnet.binance.vision` (paper) or `api.binance.com` (live).
- Binance blocks some regions (HTTP 451), including some cloud data centres
  and the US for binance.com. If `place` gets a 451, the ticket is still
  valid: place it from a computer where Binance is available to the user.
  The account must be one the user may legally hold where they live.
- Testnet has its own order book. Its prices can differ from the real market,
  so paper fills are a mechanics check, not a performance record. `place`
  warns when testnet's price is more than 1 % from the order.

## Steps

1. `init` once, then confirm the window and the money rules with the user.
2. `scan`. Say the counts and the top few by setup. A scan is a list of charts
   that match a rule, not a list of coins to buy.
3. The user picks. `entry check` (or `entry watch` in the background from the
   end of the range).
4. On a trigger: `plan --write`, then `timemachine`. Report the four odds,
   the history line, the verdict exactly as printed, and the seed. Drop a plan
   that leans toward the stop unless the user says otherwise.
5. `size --write`, `risk`, `ticket show`. Then wait for the user's yes.
6. On a clear yes: `ticket approve --reply "<their words>"`, then
   `ticket place`. Report the order ids.
7. Before `flat_by`: `ticket flatten` for anything still open (it cancels the
   OCO first), `risk`, show it, and wait for the yes again.

## Rules

- Say what was scanned and when. Never call a match "a buy" or "a winner".
- Never print a projected P&L. A target is a plan, not a forecast.
- Never raise a limit, widen a stop or shrink it to fit more units on your own.
- Spot only: no margin, futures, leverage or shorting. Long only.
- Paper (testnet) by default. Going live is the user's own decision, in their
  own config, and every live order still needs all three gates plus `--live`.
- Education and tooling, not financial advice.
