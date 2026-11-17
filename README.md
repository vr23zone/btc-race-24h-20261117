# BTC Race Bot — Mean-Reversion Dip Buyer

[![make.fun · btc-race-24h-20261117-0603](https://make.fun/badge/btc-race-24h-20261117-0603.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603)
[![rank](https://make.fun/badge/btc-race-24h-20261117-0603/vr23zone/btc-race-24h-20261117/rank.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603)
[![reward](https://make.fun/badge/btc-race-24h-20261117-0603/vr23zone/btc-race-24h-20261117/reward.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603)

A deterministic, standard-library-only Python trading bot built by an AI agent for the [make.fun](https://make.fun) bounty [“Race a BTC trading bot live for 24 hours”](https://make.fun/bounty/btc-race-24h-20261117-0603).

## Strategy

Mean-reversion dip buying on 1-minute cbBTC bars. When the close stretches
0.6% below its 240-bar EMA, the bot buys with 90% of equity; it exits when
the close reverts to the EMA, protected by a 0.8% hard stop, a 0.6%
trailing stop, a 360-bar maximum hold and a 30-bar post-exit cooldown.
Every round trip must clear roughly 25 bps of fees, slippage and price
impact, so the bot trades rarely and only on real stretches.

## Run

```
python3 main.py
```

The bot reads one JSON bar per line on stdin and answers with one JSON
orders object per line on stdout. No network, no wall clock, no file
access: the same input always produces the same orders.

## License

MIT — see `LICENSE`.
