# Read-only Solana collector + paper-trade log
No wallets, keys, signing or real orders. Free keyless sources only: GeckoTerminal new pools (≤10 calls/min), DexScreener,
public Solana RPC (api.mainnet-beta, fallback solana-rpc.publicnode.com), Jupiter lite **quote** API (no swap), Coinbase spot (baselines).
Rules come from `risk_rules.md` / `questions.md` (owned by Chefus Maximus; never edited by this code) -> `thresholds.py`.

## Commands
- One cycle: `python3 /workspace/trading/collector.py cycle`
- Positions only: `python3 /workspace/trading/collector.py monitor`
- Loop: `python3 /workspace/trading/collector.py loop` (monitor every 60 s, scan every 5 min)
- Verify the log: `python3 /workspace/trading/collector.py verify`
- Auditor test: `python3 /workspace/trading/test_jev_off.py`
- Kill switch: `touch /workspace/trading/KILL` (closes all positions, blocks entries)

## Flow
1. Freshness: RPC block ≤ 60 s old, and the two RPCs must agree within about 60 s of slots. Otherwise the cycle is skipped.
2. Hard filter. Any failure or missing data means reject, with the rule logged:
   - liquidity ≥ $12k and 24h volume ≥ $48k
   - LP burned (pumpswap only; other DEXes count as unverifiable)
   - mint and freeze authority revoked
   - no dangerous Token-2022 extensions
   - top holder ≤ 5% and top 10 ≤ 60%, excluding pool vaults and burn
   - copycat: no older token with the same symbol or name
3. Two paper strategies, each with its own trade ids:
   - **simple**: filter, then a -15% stop and a 24h exit, no Jev.
   - **jev**: Q1/Q2 (p ≥ 0.60 / 0.55 rejects) and Q4 (score ≥ 6, conf ≥ 0.5). An abstain, error or unconfigured Jev means NO entry.
4. Fills use Jupiter quotes only, never the DexScreener price:
   - The buy quote includes price impact. Impact over 1.5% (meme) or 0.3% (liquid) means no fill.
   - No route: retry once after 30 s, then reject.
   - Exits and marks use the sell quote.
5. Exits (jev): 25% at +15%, 25% at +30%, a 10% trailing stop on the rest, a -15% stop, and a close when volume.h6/(h24/4) < 0.2.
6. Limits: max 3 open positions per strategy and 1 per token. Equity includes unrealized PnL (sell-quote marks).
   - A 5% daily loss halts the strategy for the day.
   - A 25% drawdown from peak writes HALT_<strategy>, which needs a manual restart.
7. Week 1 is calibration. From day 7 the hash of thresholds.py is pinned in thresholds.frozen.sha256, and any change aborts cycles.

## Storage
- `paper.db`: scans, rejections, positions, fills, equity, baselines (BTC and SOL hold).
- `decisions.log.jsonl`: append-only, sha256 hash-chained.
  - Every decision is logged, including rejects and "none", with state, Jev answers, rule, size, quote and thresholds hash.
  - Outcomes are appended as new records; nothing is edited.
