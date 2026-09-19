# fundarb

Delta-neutral funding-rate arbitrage system for Binance and Bybit: long spot
+ short perpetual, harvesting the funding payment while the price risk
cancels out. Not HFT — the holding horizon is hours to days, and execution
latency in the hundreds of milliseconds is acceptable.

Full design spec: see the project's "Спецификация: fundarb" doc (economics,
architecture, phased rollout, risk table). This README covers what's
implementation-specific: how the five open decisions from that spec were
resolved, and how to run the thing.

## The five decisions

The spec's "Что предстоит решить" list is resolved as follows (all encoded
in `config/config.yaml`, with commentary inline there):

1. **Instrument universe** — broad coverage cut by a liquidity filter
   (`universe.mode: volume_filter`), not a hand-picked shortlist of
   large-cap pairs. Any pair on Binance or Bybit that clears
   `min_24h_quote_volume_usd` and `min_contract_age_days` is eligible; the
   scanner decides what's liquid enough, not a static list.

2. **Entry threshold (net APR)** — there's no universally "correct" number,
   so it's a config knob (`entry.min_net_apr_pct`), not a hardcoded
   constant. It ships at a conservative 15%; the real value should come
   from looking at the actual net-APR distribution the scanner produces
   over your collected history (Phase 2's go/no-go step) and adjusting
   from there.

3. **Exit rule** — both variants from the spec are implemented in
   `backtest/strategy.py::CarryStrategy`, selectable via `exit_rule.mode`:
   - `fixed_profit` — close once accrued funding + basis PnL clears
     `target_pct_of_notional` (mirrors the Hummingbot funding-arb script's
     logic).
   - `rate_reversal` — hold through the edge, exit once the rate has been
     negative for `consecutive_negative_periods` in a row.
   Backtest both on your own data before picking one for a live deployment.

4. **Hosting** — no code depends on where the process runs. Planned
   rollout: a local machine in Moscow for now (fine for Phase 1-4: data
   collection, research, backtest, testnet execution all tolerate the
   extra latency), moving to a VPS in the exchange's region before sizing
   up live trading, once latency to the matching engine starts to matter
   for fill quality. See `deploy/` for hosting notes as they're added.

5. **Delta rebalancing** — automatic (`rebalance.mode: auto`), not
   alert-only. `risk/guard.py::RiskGuard.check_delta` computes the
   spot/perp notional deviation on every cycle; past
   `max_delta_deviation_pct` it sizes and returns a rebalancing order
   (capped by `max_rebalance_notional_pct`, rate-limited by
   `min_rebalance_interval_sec`), and the live run loop (`cli.py run`)
   executes it through the same risk-checked path as everything else.
   Setting `rebalance.mode: alert_only` falls back to alert-without-act if
   you'd rather rebalance by hand.

## Architecture

```
Binance / Bybit API -> ExchangeAdapter -> collector -> storage (parquet)
                                                          |         |
                                                     research    scanner
                                                          |         |
                                                       backtest <---'
                                                          |
                                                      strategy -> OrderIntent
                                                                     |
                                                                risk guard
                                                                     |
                                                              (approved only)
                                                                     |
                                                                 executor -> adapter -> monitor
```

The risk guard sits on every path from a strategy decision to the
exchange — nothing places an order without going through
`RiskGuard.check()` first. See `src/fundarb/` for the module breakdown
(`core`, `exchanges`, `collect`, `research`, `scanner`, `backtest`,
`execution`, `risk`, `monitor`, `cli.py`).

The live trading loop itself is `execution/live_runner.py::LiveRunner` —
`cli.py run` just wires its dependencies and calls it. It's a class rather
than a script so each of the following is independently testable against a
fake adapter (`tests/test_live_runner.py`):

- **Funding collection happens inline, every cycle.** Before making any
  decision, `run_once()` backfills a short recent window into storage
  itself — `cumulative_funding_pnl` no longer silently stalls if a separate
  `collect` process isn't also running.
- **The kill switch actually closes positions.** It used to only block new
  entries; an already-open position stayed open indefinitely. Now
  kill-switch-active is checked first every cycle, and closes both legs
  immediately (bypassing the normal risk gate, the same way the executor's
  own leg-1 rollback does) before anything else runs — it still blocks new
  entries afterward, pending a manual `reset_kill_switch()`.
- **Leverage is set and checked, not assumed.** `set_leverage` runs before
  the first entry, and `RiskGuard.check_leverage` is checked against the
  account's real perp margin balance (`get_perp_margin_balance`) rather
  than trusting the exchange account's default leverage.
- **Connection loss is tracked and alerted.** A failed quote fetch marks
  the venue disconnected in `MetricsRegistry` and fires
  `TelegramAlerter.connection_lost` once `monitor.connection_loss_alert_sec`
  has elapsed since the last successful contact.
- **Position metadata survives restarts.** `execution/journal.py` persists
  entry_time, entry_basis, accumulated funding PnL, and the exit-streak
  counters after every cycle that touches them. `reconcile()` still owns
  spot/perp *quantities* (the exchange is always ground truth for how much
  is held); the journal only supplies *since when* and *at what basis* —
  the one thing the exchange doesn't track for us.

Two correctness bugs in `execution/reconcile.py` surfaced while wiring the
above and are fixed: `get_positions()` is a derivatives-only endpoint on
every real exchange (spot holdings live in `get_balances()`, never in
positions), and `is_single_legged`/`delta_notional_ratio` compared raw
signed quantities — since a correct hedge is *short* perp (negative
signed quantity), the old check flagged every healthy position as
single-legged. Both are covered by
`test_live_runner.py::test_position_metadata_survives_restart`.

## Setup

```bash
cd fundarb
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in testnet keys before running `run`
```

## Usage

```bash
# Phase 1: backfill funding + price history for the configured universe
fundarb collect --days 365

# Phase 2: rank the universe by net APR after costs (the go/no-go signal)
fundarb scan --top 20

# Phase 3: backtest the carry strategy over stored history
fundarb backtest --venue binance --symbol BTC/USDT --start 2025-01-01

# Phase 4/5: run live (testnet by default; --mainnet requires
# FUNDARB_CONFIRM_MAINNET=1 as a last-line guard against trading real
# money by accident)
fundarb run --venue binance --symbol BTC/USDT
```

## Testing

```bash
pytest
```

Coverage focuses on the modules where a bug has real consequences: the net
APR formula (interval changes mid-history, four-fill costs, basis drag),
the risk guard (position/exposure limits, order-frequency limiting, kill
switch, auto delta-rebalance), the two-leg executor (partial fills,
rollback on leg-2 failure, rollback-failure escalating to the kill switch),
idempotent parquet storage, the scanner's liquidity filter (both legs, not
just spot), and the live runner end to end against a fake adapter (entry,
both exit rules, auto rebalance, kill-switch auto-close, connection-loss
alerting, and position persistence across a simulated restart). CI runs the
suite on every push/PR touching `fundarb/**`
(`.github/workflows/fundarb-tests.yml`).

## Known limitations (carried over from the spec)

- **Testnet doesn't validate economics.** Funding rates on testnets are
  synthetic and book depth is nominal. Testnet is for mechanics (orders,
  reconnects, reconciliation, restart behavior); yield numbers only mean
  anything computed from mainnet history via the public endpoints.
- **No automatic margin top-up.** The risk guard enforces a leverage
  *ceiling* (`risk.max_leverage`, checked before every entry) but doesn't
  add margin automatically as price moves against the perp leg — the
  spec's risk table lists auto-top-up as one mitigation for liquidation
  risk; today that's still a manual/alert-driven response.
- **Single symbol per `LiveRunner`.** Fine for Phase 5 ("one pair, minimal
  size"), but risk limits (`max_total_exposure_usd`, order-frequency) are
  only accurate as long as nothing else trades against the same
  `RiskGuard`/account concurrently — running multiple symbols would need
  either one `RiskGuard` shared correctly across runners or explicit
  cross-symbol exposure aggregation, neither of which exists yet.
- **API keys**: trading-only permission, no withdrawal, IP-bound, testnet
  and mainnet keys kept separate — see `.env.example`. Run the kill-switch
  test (`risk.trigger_kill_switch` closing both legs) before any live run.
