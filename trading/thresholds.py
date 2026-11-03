"""All thresholds. Aligned with risk_rules.md and questions.md (owned by Chefus Maximus; never edited here).
Week 1 = calibration (may change). Week 2 = evaluation: thresholds frozen (hash checked, see collector.phase())."""
import datetime as _dt
EXPERIMENT_START = _dt.date(2026, 10, 29)

# Hard filter (questions.md section 1)
MIN_LIQUIDITY_USD = 12_000
MIN_VOLUME_24H_USD = 48_000
MAX_SINGLE_WALLET_PCT = 5.0      # excl. pool vaults and burn
MAX_TOP10_PCT = 60.0
REQUIRE_MINT_AUTH_REVOKED = True
REQUIRE_FREEZE_AUTH_REVOKED = True
BANNED_TOKEN2022_EXT = {"transferHook", "permanentDelegate", "transferFeeConfig", "defaultAccountState", "nonTransferable", "pausableConfig"}
LP_BURNED_DEXES = {"pumpswap"}   # protocols that burn LP at migration; any other dex = LP unverifiable -> reject
# nodata1: dexes whose LP burn/lock is verified ON-CHAIN per pool (collector.lp_lock_onchain). Supported programs only:
# Raydium AMM v4 + CPMM (LP mint burned: 1 - mint supply / pool lp counter, + LP held by the incinerator) and Meteora DAMM v2
# (permanent_lock_liquidity / liquidity). Any other program on these dexes (Raydium CLMM, Meteora DLMM, DAMM v1...) or any missing /
# inconsistent value = unknown = reject. Missing data NEVER counts as a pass.
LP_ONCHAIN_DEXES = {"raydium", "meteora"}
# r2: NO value until 069 approves one directly. None -> every raydium/meteora candidate is rejected in the LP check
# ('lp threshold not approved'), before any RPC call. Setting a number here changes thresholds.py's sha -> new activation.
LP_MIN_LOCKED_PCT = 98.0   # % of LP burned/permanently locked required -- approved by 069 on 2026-10-30 (DAMM v2 effective 99.0 = +DAMM_V2_MARGIN_PP)
# r2: the LP check shares the scanloop thread with monitor() (stops/exits) -> hard bounds per call and per tick
LP_CALL_TIMEOUT_SEC = 2.0       # wall-clock per RPC call (thread join); timeout -> reject
LP_TICK_MAX_CALLS = 8           # LP RPC calls per scan tick (one cycle()); exhausted -> reject
LP_TICK_MAX_SEC = 4.0           # LP wall time per scan tick; exhausted -> reject

# Week-1 calibration change (approved by 069 2026-10-29 20:35 CDMX; pending Trading Risk Auditor review)
# Tier B: 24h volume >= 25k (tier A stays >= 48k) at 0.5x size. ONLY allowed when the cluster filter (<=20%)
# AND the sniper filter (<=10%) exist in code and are active. Hard-gated in collector.tier_b_allowed():
# neither filter is implemented yet, so tier B is OFF regardless of this flag.
TIER_B_ENABLED = False           # explicitly OFF until cluster + sniper filters are implemented AND audited (auditor cond. 6)
TIER_B_MIN_VOLUME_24H_USD = 25_000
TIER_B_SIZE_FACTOR = 0.5
TIER_B_MAX_CLUSTER_PCT = 20.0    # required filter (not implemented)
TIER_B_MAX_SNIPER_PCT = 10.0     # required filter (not implemented)
# Scan universe: pools already migrated to PumpSwap + trending pools; never pump.fun / DBC bonding curves
_NEW = [f"/networks/solana/new_pools?page={p}" for p in range(1, 11)]
_TREND = [f"/networks/solana/trending_pools?page={p}" for p in range(1, 6)]
_PSWAP = [f"/networks/solana/dexes/pumpswap/pools?page={p}&sort=h24_tx_count_desc" for p in range(1, 6)]
# Full-cycle mode (`cycle`/`loop`): page 1 of each feed. Continuous mode uses SCAN_ROTATION.
SCAN_SOURCES = (_NEW[0], _TREND[0], _PSWAP[0])
# Continuous rotation: every other tick is a "hot" page 1 (where unseen pools appear first); the rest walk deep pages.
_HOT = [_NEW[0], _PSWAP[0], _NEW[0], _TREND[0]]
_DEEP = _NEW[1:] + _TREND[1:] + _PSWAP[1:]          # 18 deep pages
SCAN_ROTATION = tuple(x for i, d in enumerate(_DEEP) for x in (_HOT[i % len(_HOT)], d))   # 36 ticks ~ 7.2 min
# DexScreener supplement, one call per tick, rotating (limits: profiles 60/min, search/pairs 300/min)
DS_SUPPLEMENT = ("ds:/token-profiles/latest/v1", "ds:/latest/dex/search?q=pumpswap", "ds:/token-boosts/latest/v1")
BONDING_CURVE_DEXES = {"pumpfun", "pump-fun", "meteoradbc", "meteora-dbc", "raydium-launchlab", "launchlab", "stonkfun", "moonshot"}
# Continuous scan mode (`collector.py scanloop`): one GeckoTerminal source per tick, rotating
SCAN_TICK_SEC = 12               # 5 list calls/min + <=5 gecko_pool fallbacks/min (cap below) <= GECKO_MAX_CALLS_PER_MIN
SCAN_MAX_POOL_FALLBACKS_PER_TICK = 1
SCAN_DEDUPE_TTL_SEC = 1800       # complete evaluations: re-evaluated at most every 30 min
SCAN_DEDUPE_INCOMPLETE_TTL_SEC = 60   # missing data / source_error in tick: retry after <= 60 s (auditor cond. 1)
MONITOR_EVERY_SEC = 60
DEXSCREENER_MAX_CALLS_PER_MIN = 60   # public limit is 300/min for /tokens; stay well below
JEV_MAX_EVALS_PER_HOUR = 20      # guard: each eval = up to 3 OpenRouter calls (Q1,Q2,Q4); only hard-filter survivors

# Data / execution (risk_rules.md section 2)
MAX_DATA_AGE_SEC = 60
MAX_SLIPPAGE_PCT = {"meme": 1.5, "liquid": 0.3}
QUOTE_RETRY_DELAY_SEC = 30
PRIORITY_FEE_USD = 0.01
NETWORK_FEE_USD = 0.001

# Sizing (section 1)
PAPER_BANKROLL_USD = 1000.0
MAX_TICKET_PCT_BANK = 6.0
MAX_TICKET_PCT_LIQ = 2.0
FACTOR_DARK_DATA = 0.4
FACTOR_NO_X = 0.6
FIXED_TICKET_PCT_BANK = 1.0      # risk_rules 1.1: fixed 1% per trade until auditor-verified calibration
KELLY_ENABLED = False            # flip only after auditor verifies calibration (then cap 6%)
# edge_est (risk_rules.md "edge_est")
EDGE_BASE_RATE_DEFAULT = 0.35    # until MIN_TRADES_FOR_STATS closed simple-rule trades
EDGE_AVG_WIN = 0.20
EDGE_AVG_LOSS = 0.15
EDGE_MIN = 0.02
MIN_TRADES_FOR_STATS = 20

# Exits (section 3)
STOP_LOSS_PCT = -15.0
TAKE_PROFIT = [(15.0, 0.25), (30.0, 0.25)]   # (gain %, fraction of ORIGINAL qty)
TRAILING_STOP_PCT = 10.0
SIMPLE_MAX_HOLD_HOURS = 24
VOL_DECAY_MIN_RATIO = 0.20       # volume.h6/(volume.h24/4)
MAX_OPEN_POSITIONS = 40          # week1d v2 FINAL (069 delegated to auditor): per strategy (simple 40 + jev 40). None = off
MAX_POSITIONS_PER_TOKEN = 1      # first entry per mint per strategy
# week1e pyramiding (069: "si una moneda salta una segunda vez de señal pues tal vez vale la pena incrementar la posición" / "Hagámoslo"):
# ONE add-on when the signal fires again on an open mint, only if that position is in profit at a FRESH real Jupiter mark;
# same ticket (1% x 0.6) and all caps (80% exposure, cash, bank, liq, 40 open, 1 entry/cycle, risk halts) apply.
PYRAMID_ENABLED = True
MAX_ENTRIES_PER_MINT = 2
PYRAMID_MIN_GAIN_PCT = 0.0

# Hard limits (section 4)
DAILY_LOSS_CAP_PCT = 5.0
MAX_DRAWDOWN_PCT = 30.0          # week1d v2: 069 "Súbelo a 30%" (was 25%); from peak equity incl. unrealized -> close all + stop strategy
KILL_SWITCH_FILE = "/workspace/trading/KILL"

# Jev (questions.md section 2) — reject/act thresholds
JEV_Q1_WHALE_REJECT_P = 0.60
JEV_Q2_EXHAUSTED_REJECT_P = 0.55
JEV_Q3_FAKE_X_REJECT_P = 0.50
JEV_Q4_MIN_SCORE = 6
JEV_Q4_MIN_CONF = 0.50
JEV_Q6_MIN_CONF = 0.60

GECKO_MAX_CALLS_PER_MIN = 10

# Marks (week1b): last valid Jupiter sell-quote mark is used for at most STALE_MARK_TTL_SEC, then $0 (no_route).
# Never-marked positions with no route = $0. mark_failed logged on first failure of a streak + every MARK_FAILED_LOG_EVERY_SEC.
STALE_MARK_TTL_SEC = 300
MARK_FAILED_LOG_EVERY_SEC = 300
# week1c: exit_pending record when an exit is skipped for a stale/no_route mark (auditor C1), rate-limited per position
EXIT_PENDING_AGE_HOURS = 24          # log when the position is older than this ...
EXIT_PENDING_LOG_EVERY_SEC = 300     # ... or its last valid mark is already below the stop; at most once per 5 min per position
# week1c sizing base: total strategy equity = cash + open positions at mark_valued value (stale <= TTL, else $0; never cost)
SIZE_BASE = "total_equity"
# week1d (requested by 069 2026-10-29): exposure cap replaces the 3-open-positions limit. Per strategy (simple and jev alike):
# remaining cost basis of open positions + new ticket <= MAX_EXPOSURE_PCT x total strategy equity (size base above).
MAX_EXPOSURE_PCT = 0.80
# week1d API guard: shared limiter for ALL read-only Jupiter quotes (entries, round-trip costs, marks, exit sells)
# Jupiter docs (developers.jup.ag/docs/portal/rate-limits, read 2026-10-29): keyless 30/min (0.5 RPS), free key 60/min,
# 60 s sliding window, one bucket for quote/price/token. Collector is keyless (lite-api, being reduced) -> 27/min (10% headroom).
# With JUPITER_API_KEY the collector uses the *_KEYED values below (40 marks/min fit). Entries (buy quote, jev round-trip) are capped separately
# so marks + exit sells always keep JUP_MAX - JUP_ENTRY slots/min.
JUP_MAX_CALLS_PER_MIN = 27            # keyless (lite-api) fallback
JUP_ENTRY_MAX_CALLS_PER_MIN = 7
# week1e: JUPITER_API_KEY present -> api.jup.ag with x-api-key header (free tier 60/min) -> 54 total / 14 entries
JUP_MAX_CALLS_PER_MIN_KEYED = 54
JUP_ENTRY_MAX_CALLS_PER_MIN_KEYED = 14
MONITOR_MAX_QUOTES_PER_PASS = 80    # v2 FINAL: every open position (2 x MAX_OPEN_POSITIONS) is quoted each pass; pass length = limiter
# v2 (auditor C1) quote priority: 1) urgent = cached mark <= URGENT_MARK_CHG_PCT or age >= EXIT_PENDING_AGE_HOURS,
# 2) healthy, oldest quote attempt first, 3) no_route (failing streak), at most NO_ROUTE_MAX_QUOTES_PER_PASS per pass
URGENT_MARK_CHG_PCT = -10.0
NO_ROUTE_MAX_QUOTES_PER_PASS = 5
# v2 (C2): any open position not quoted for > STALE_MARK_TTL_SEC (300 s), or a backlog that cannot be cleared within it,
# blocks NEW entries (rule monitor_backlog). Exits are never blocked.
# v2 FINAL (C4, two levels), evaluated in monitor() right after that pass's real quotes (no_route = $0):
#  daily loss >= DAILY_LOSS_CAP_PCT -> block new entries; stops/exits of open positions stay active
#  drawdown >= MAX_DRAWDOWN_PCT     -> close ALL of that strategy with real Jupiter sell quotes (full qty, impact included),
#                                       HALT_<strategy> file = strategy KILL until 069 reviews; close without route -> exit_pending
DAILY_LOSS_ACTION = "block_entries"
DRAWDOWN_ACTION = "close_all_and_stop"
# v2 (C5): max new entries per strategy per cycle, same for simple and jev (None = no limit)
MAX_ENTRIES_PER_CYCLE = 1
