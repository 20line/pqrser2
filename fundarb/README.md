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

A second pass closed four more gaps, all in `LiveRunner`:

- **Position-level stop-loss.** `risk.max_unrealized_loss_usd` is checked
  every cycle against funding PnL *and* basis drag combined
  (`_pnl_breakdown`), independent of whichever exit rule the deployment
  picked — a `rate_reversal` deployment sitting on a positive rate but a
  blown-out basis would otherwise never exit. A breach forces an immediate
  close (`IntentReason.STOP_LOSS_CLOSE`), which can itself cascade into
  `daily_loss_limit_usd` tripping the kill switch, same as any other
  realized loss.
- **`margin_low` and `rate_reversal` alerts are wired**, not just declared
  on `TelegramAlerter`. Margin ratio (`get_perp_margin_balance` / position
  notional) is checked every cycle against `monitor.margin_alert_ratio`;
  a negative funding print fires `rate_reversal` regardless of
  `exit_rule.mode` — a `fixed_profit` deployment still gets told the edge
  went negative even though it won't exit on that alone.
- **`MetricsRegistry` is actually populated.** It existed with nothing
  calling `update_position()`; now every cycle with an open position
  writes accumulated funding, basis, delta, and margin ratio into it.
- **`execution/ledger.py` — an append-only trade ledger.** One parquet row
  per closed position (entry/exit time and basis, funding PnL, basis PnL,
  realized PnL, exit reason), written by the same `_execute_close` helper
  every closing path (normal exit, stop-loss, kill switch) shares. This is
  the spec's "full journal from day one, in a format fit for reporting" —
  previously the only record of a closed trade was a structlog line.
  Fixed a real bug along the way: realized PnL recorded to the daily-loss
  tracker only ever counted funding, never basis drag, understating losses
  from basis moves.
- **Basis is checked at the moment of entry, not only in the scanner.**
  `CarryStrategy.decide_entry` used to accept `basis_bps` and silently
  ignore it — the scanner's basis snapshot can be stale by the time an
  entry actually fires. `CarryStrategy` now takes an optional
  `max_spread_bps` (wired from `config.universe.max_spread_bps`) and
  rejects entries whose basis is too wide, directly implementing the
  spec's risk-table row for basis-convergence risk.

A third pass (a `code-review`-skill audit of the diff above) found four
smaller but real issues in `_execute_close`, now fixed:

- **A trade-ledger write failure used to leave `self.position` stale and
  crash the whole process** — the exchange legs had already closed, but
  the exception from `ledger.append()` propagated before `journal.clear()`
  / `self.position = None` ran, and `cli.py`'s loop had no try/except at
  all. Fixed by reordering `_execute_close` so the safety-critical state
  (risk's realized-PnL tracker, the journal, `self.position`, metrics)
  updates immediately after the exchange confirms the close, with the
  ledger write isolated in its own try/except afterward — a parquet
  failure now logs and alerts instead of corrupting position tracking.
- **`MetricsRegistry.positions` was never cleared on close**, so a closed
  position's last snapshot (margin ratio, basis, accumulated funding) sat
  there indefinitely, reporting a flat symbol as still open and at risk.
- **A kill switch triggered by the `daily_loss_limit_usd` cascade (via
  `record_realized_pnl`) never actually alerted** — the operator got the
  triggering event's own message (e.g. "stop-loss triggered") but nothing
  telling them new entries are now blocked pending a manual reset.
- **The `-(exit_basis - entry_basis) * notional` basis-PnL formula was
  hand-rolled identically in three places** (`backtest/strategy.py`,
  `backtest/engine.py`, `execution/live_runner.py`) — extracted to
  `research/yield_calc.py::basis_pnl`, the single place a future change to
  the PnL convention now needs to happen.

The same pass added a last-resort backstop: `LiveRunner.run_once()` now
catches and alerts on any exception a cycle didn't already handle inline,
rather than relying solely on `cli.py`'s loop (deliberately bare) or
process supervision to recover.

A fourth pass (same skill, `--level max`) caught one more gap in that
ledger isolation: `TradeLedger.append`'s `except StorageError` only wrapped
the *write* (`OSError` from `write_parquet`/`os.replace`), while the read
of the existing file to merge the new row into ran unguarded — a corrupt
or locked `trades.parquet` raised a raw `polars.exceptions.PolarsError`
that `_execute_close`'s `except StorageError` didn't match, so it fell
through to the generic unhandled-exception backstop instead of the
specific "trade closed, ledger write failed, backfill manually" alert.
Fixed by wrapping the read+merge+write sequence as one unit and catching
`(OSError, pl.exceptions.PolarsError)`.

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
just spot), the trade ledger in isolation (round-trip, multiple appends,
and a corrupt-file read failure normalizing to `StorageError`), and the
live runner end to end against a fake adapter — entry (including the
basis filter and leverage check), both exit rules, the position-level
stop-loss (and its cascade into the daily-loss kill switch, including the
kill-switch alert itself), auto rebalance, kill-switch auto-close,
margin/rate-reversal alerting, isolation from a failing ledger write,
metrics cleanup on close, the unhandled-exception backstop, and position
persistence across a simulated restart. CI runs the suite on every push/PR
touching `fundarb/**` (`.github/workflows/fundarb-tests.yml`).

## Known limitations (carried over from the spec)

- **Testnet doesn't validate economics.** Funding rates on testnets are
  synthetic and book depth is nominal. Testnet is for mechanics (orders,
  reconnects, reconciliation, restart behavior); yield numbers only mean
  anything computed from mainnet history via the public endpoints.
- **No automatic margin top-up.** `risk.max_leverage` is enforced as a
  ceiling before every entry, `monitor.margin_alert_ratio` now alerts when
  margin runs low, and `risk.max_unrealized_loss_usd` forces a close before
  losses compound — but nothing adds margin automatically as price moves
  against the perp leg. The spec's risk table lists auto-top-up as one
  mitigation for liquidation risk; today the response is stop-loss-then-
  alert, not top-up.
- **Single symbol per `LiveRunner`.** Fine for Phase 5 ("one pair, minimal
  size"), but risk limits (`max_total_exposure_usd`, order-frequency) are
  only accurate as long as nothing else trades against the same
  `RiskGuard`/account concurrently — running multiple symbols would need
  either one `RiskGuard` shared correctly across runners or explicit
  cross-symbol exposure aggregation, neither of which exists yet.
- **API keys**: trading-only permission, no withdrawal, IP-bound, testnet
  and mainnet keys kept separate — see `.env.example`. Run the kill-switch
  test (`risk.trigger_kill_switch` closing both legs) before any live run.
