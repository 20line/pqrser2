# Hosting

Nothing in `fundarb` depends on where the process runs — no local file
paths outside `data/` and `config/`, no assumptions about the host's clock
beyond correct UTC/NTP sync. That's what makes the two-stage plan below
possible without a rewrite.

## Stage 1 (now): local machine in Moscow

Fine for Phases 1-4:

- **Collect / scan / backtest** are not latency-sensitive at all — they're
  reading history and computing, not racing an order book.
- **Testnet execution** (Phase 4) only needs to validate mechanics (fills,
  reconnects, reconciliation, restart behavior), not fill quality, so the
  extra ~100-150ms round trip from Moscow to Binance/Bybit's matching
  engines doesn't invalidate the test.

Run it as a systemd user service (or equivalent) so it survives SSH
disconnects and restarts on crash:

```ini
# ~/.config/systemd/user/fundarb.service
[Unit]
Description=fundarb live run
After=network-online.target

[Service]
WorkingDirectory=%h/fundarb
ExecStart=%h/fundarb/.venv/bin/fundarb run --venue binance --symbol BTC/USDT
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now fundarb.service
journalctl --user -u fundarb -f
```

A restart under this setup is exactly the case `execution/reconcile.py`
exists for: `fundarb run` always reconciles real exchange state before
resuming, so an unclean restart doesn't trust stale local state.

## Stage 2 (before scaling live size): VPS in the exchange's region

Move once Phase 5 is past "minimal size, one pair" and fill quality on the
perp leg starts to matter — a wider effective spread from execution
latency eats directly into the net-APR edge computed in `research/`.
Practical notes for the move:

- Pick a region close to the exchange's matching engine (AWS ap-northeast-1
  / ap-southeast-1 are common choices for Binance/Bybit; verify current
  guidance, it changes).
- Copy `config/`, `.env` (regenerate keys bound to the new server's IP —
  see the root README's key-handling notes), and `data/` (or let `collect`
  rebuild it; parquet reads are cheap to replay).
- IP-allowlist the new server's address on both exchanges' API key
  settings **before** cutting over, and confirm the old key is revoked
  once traffic has moved — don't run both simultaneously against the same
  account.
- This is a config/ops change, not a code change: the `ExchangeAdapter`
  interface and everything above it (`risk`, `execution`, `monitor`)
  target ccxt-through-unified-API, not a specific host.
