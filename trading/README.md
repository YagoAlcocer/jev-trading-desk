# JEV paper trading system

Code for the paper trading system behind the JEV Trading Desk dashboard (published from the `gh-pages` branch).

- **Paper trading only.** No real orders and no funds.
- **No keys are included.** This repo has no databases, logs, backups or env files.
- Keys are set through environment variables: `OPENROUTER_API_KEY`, `HELIUS_API_KEY`, `JEV_API_KEY`, `JUPITER_API_KEY` (and `DASHBOARD_GH_TOKEN` for publishing).

Main files: `collector.py` (scan and paper trading loop), `dashboard_data.py`, `thresholds.py`, `judge.py`, `risk_rules.md`, `brier1/` (scoring). See `OVERVIEW.md` for the original notes.
