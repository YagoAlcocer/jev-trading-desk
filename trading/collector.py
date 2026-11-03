"""READ-ONLY Solana collector + paper trader. No wallets, keys, signing or orders.
Two paper strategies run side by side: 'simple' (hard filter + stop/24h, no Jev) and 'jev' (hard filter + Jev).
exits1 r2: third paper strategy 'simple_tp' = PAIRED copy of every simple entry (same quote/price/tick/ticket) with Jev's exits
(stop, TP 25%/25%, 10% trailing) + simple's 24h expiry. A detect-only fast thread queues exit checks that the main thread executes.
Usage:  python3 -B collector.py cycle | monitor | loop | scanloop | verify   (ALWAYS -B)"""
import json, os, sys, time, sqlite3, hashlib, datetime as dt
sys.dont_write_bytecode = True   # pycache_fix2: also covers a manual `python3 collector.py verify` without -B (imports thresholds/hashlog/control/judge)
import requests
import thresholds as T, hashlog, control
from judge import Judge, JevUnavailable, Noul, Score, Choice

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "paper.db")
_HK = os.environ.get("HELIUS_API_KEY", "")
HELIUS = f"https://mainnet.helius-rpc.com/?api-key={_HK}" if _HK else None
_ENV_RPC = os.environ.get("SOLANA_RPC_URL", "")
DEFAULT_RPC = "https://api.mainnet-beta.solana.com"
# primary Helius (if HELIUS_API_KEY), then SOLANA_RPC_URL (if set), then mainnet-beta
RPCS = list(dict.fromkeys(u for u in (HELIUS, _ENV_RPC, DEFAULT_RPC) if u))

_JK = os.environ.get("JUPITER_API_KEY", "")   # week1e: sent ONLY as x-api-key header to api.jup.ag; never in URLs/logs
JUP_QUOTE_URL = "https://api.jup.ag/swap/v1/quote" if _JK else "https://lite-api.jup.ag/swap/v1/quote"
def _jup_limits():
    """(total, entry) per minute: keyed (free key 60/min) or keyless (30/min), both with 10% headroom."""
    tot, ent = (T.JUP_MAX_CALLS_PER_MIN_KEYED, T.JUP_ENTRY_MAX_CALLS_PER_MIN_KEYED)   # budget54 MEASUREMENT: keyed tier (54/14) even without a key
    return min(tot, MARKS1_JUP_MAX_PER_MIN), min(ent, MARKS1_JUP_ENTRY_MAX_PER_MIN)   # marks1-r1: hard ceiling (auditor: 27/min)

def mask(s):
    """Never expose the Helius / Jupiter keys: strip them from any string (errors, URLs, logs)."""
    s = str(s)
    if _JK: s = s.replace(_JK, "***")
    if _ENV_RPC and _ENV_RPC != DEFAULT_RPC: s = s.replace(_ENV_RPC, "<SOLANA_RPC_URL>")
    return s.replace(_HK, "***") if _HK else s

import builtins
_print = builtins.print
def print(*a, **k): _print(*(mask(x) for x in a), **k)
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
INCINERATOR = "1nc1nerator11111111111111111111111111111111"
AMM_AUTHORITIES = {"5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium AMM v4
                   "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",  # Raydium CPMM
                   "WLHv2UAZm6z4KyaaELi5pjdbJh6RESMva1Rnn8pJVVh"}   # Meteora vault authority
TOKEN2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
STRATS = ("simple", "jev", "simple_tp")   # exits1: + simple_tp (paired with simple, Jev exits); simple unchanged (reference)
S = requests.Session(); S.headers["User-Agent"] = "readonly-collector/0.2"
now = lambda: dt.datetime.now(dt.timezone.utc)
_gecko_calls = []
_jup_calls = []
_jup_entry_calls = []
_jup_purpose = ["exit"]   # set to "entry" by try_entry/roundtrip_cost (signature of jup_quote unchanged)

# ---------------- data sources (all read-only, keyless) ----------------
_source_errors = [0]   # bumped on every logged source_error (cycle uses it to decide dedupe)

def _get_json(url, params=None, tries=3, timeout=15, limiter=None, retry_429=True):
    """GET with timeout + retries (backoff 2s/4s) on network errors, 429 and 5xx. `limiter` is called before EVERY
    attempt (so retries count against the rate limit). retry_429=False: a 429 is logged and raised at once (no retry in tick)."""
    err = None
    for i in range(tries):
        if limiter: limiter()
        try:
            r = S.get(url, params=params, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500: raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status(); return r.json()
        except Exception as e:
            err = e; _source_errors[0] += 1
            hashlog.append("source_error", url=url.split("?")[0], attempt=i + 1, error=mask(f"{type(e).__name__}: {e}")[:300])
            if "HTTP 429" in str(e) and not retry_429: break
            if i < tries - 1: time.sleep(2 * (i + 1))
    raise RuntimeError(mask(f"GET {url.split('?')[0]} failed: {type(err).__name__}: {err}"))

# gecko429-r1: rate limit + smoothing + global 429 backoff + short TTL cache. GeckoTerminal public API ~30 calls/min (free);
# we never exceed min(T.GECKO_MAX_CALLS_PER_MIN, GECKO_FREE_LIMIT_PER_MIN) in any 60 s window and space calls >= 60/rate s apart.
GECKO_FREE_LIMIT_PER_MIN = 30
GECKO_BACKOFF_BASE_SEC = 15.0     # after a 429: 15, 30, 60, 120, 240 s (cap) during which gecko() does not call out
GECKO_BACKOFF_MAX_SEC = 240.0
# gecko429-r2: NO response cache (auditor): list pages and pool pages both carry reserve_in_usd/liquidity, and a cached value would be
# older than t_data / data_as_of_ts, hiding its age from MAX_DATA_AGE_SEC. Every filter input is a fresh response.
LIQ_UNKNOWN_RETRY_SEC = 60.0      # liquidity_unknown -> requeued, retried after max(60 s, remaining backoff)
LIQ_UNKNOWN_MAX_RETRIES = 6       # then a hard 'liquidity' reject ("undeterminable after N retries")
class GeckoUnavailable(RuntimeError):
    def __init__(self, kind, msg): super().__init__(msg); self.kind = kind
_gecko_backoff = {"until": 0.0, "n": 0}
_gecko_stats = {"calls": 0, "backoff_skips": 0, "http_429": 0}
def _gecko_rate(): return max(1, min(int(T.GECKO_MAX_CALLS_PER_MIN), GECKO_FREE_LIMIT_PER_MIN))

def _gecko_wait():
    rate = _gecko_rate(); gap = 60.0 / rate
    while True:
        t = time.time(); win = [x for x in _gecko_calls if t - x < 60]; _gecko_calls[:] = win
        w = max((win[-1] + gap - t) if win else 0.0, (win[0] + 60 - t) if len(win) >= rate else 0.0)
        if w <= 0: break
        time.sleep(min(w, 1.0))
    _gecko_calls.append(time.time()); _gecko_stats["calls"] += 1

def _gecko_kind(e):
    m = str(e)
    if "HTTP 429" in m: return "rate_limited"
    if "Timeout" in m: return "timeout"
    if "HTTP 5" in m: return "server_error"
    return "error"

def gecko(path):
    t = time.time()
    if t < _gecko_backoff["until"]:
        _gecko_stats["backoff_skips"] += 1
        raise GeckoUnavailable("rate_limited", f"GeckoTerminal backoff {(_gecko_backoff['until'] - t):.0f}s left (after HTTP 429)")
    try: j = _get_json("https://api.geckoterminal.com/api/v2" + path, limiter=_gecko_wait, retry_429=False)
    except hashlog.HashlogCorrupt: raise
    except Exception as e:
        k = _gecko_kind(e)
        if k == "rate_limited":
            _gecko_stats["http_429"] += 1; _gecko_backoff["n"] += 1
            d = min(GECKO_BACKOFF_MAX_SEC, GECKO_BACKOFF_BASE_SEC * 2 ** (_gecko_backoff["n"] - 1)); _gecko_backoff["until"] = time.time() + d
            hashlog.append("gecko_backoff", seconds=d, consecutive_429=_gecko_backoff["n"], path=path.split("?")[0])
        raise GeckoUnavailable(k, str(e)) from None
    _gecko_backoff["n"] = 0
    return j

def dexscreener_get(url, params=None):
    return _get_json(url, params=params, limiter=_ds_wait, retry_429=False)

_ds_calls = []
def _ds_wait():
    while len([t for t in _ds_calls if time.time() - t < 60]) >= T.DEXSCREENER_MAX_CALLS_PER_MIN: time.sleep(1)
    _ds_calls.append(time.time())

def dexscreener(mints):
    out = {}
    for i in range(0, len(mints), 30):
        try: batch = dexscreener_get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(mints[i:i+30])) or []
        except hashlog.HashlogCorrupt: raise
        except Exception as e: print("dexscreener batch failed:", e); batch = []
        for p in batch:
            m = p["baseToken"]["address"]
            if (p.get("liquidity") or {}).get("usd", 0) >= (out.get(m, {}).get("liquidity") or {}).get("usd", 0): out[m] = p
    return out

class RPC:
    url = RPCS[0]
    def _call(self, url, m, p):
        time.sleep(0.12 if url == HELIUS else 0.4)   # Helius free tier ~10 req/s; public RPC slower
        try:
            j = S.post(url, json={"jsonrpc": "2.0", "id": 1, "method": m, "params": p}, timeout=15).json()
        except Exception as e:
            raise RuntimeError(mask(f"{m} via {'helius' if url == HELIUS else url}: {type(e).__name__}: {e}")) from None
        if "error" in j: raise RuntimeError(mask(f"{m}: {j['error'].get('message')}"))
        return j["result"]
    def call_once(self, m, p, timeout):
        """r2 (LP check only): one POST to the primary, connect/read timeout <= timeout, no sleep/retry/fallback."""
        try: j = S.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": m, "params": p}, timeout=(timeout, timeout)).json()
        except Exception as e: raise RuntimeError(mask(f"{m}: {type(e).__name__}")) from None
        if "error" in j: raise RuntimeError(mask(f"{m}: {(j['error'] or {}).get('code')}"))
        return j["result"]
    def call(self, m, p):
        err = None
        for i in range(3):
            try: return self._call(self.url, m, p)
            except Exception as e:
                err = e
                if "429" not in str(e) and "oo many" not in str(e): break
                time.sleep(2 * (i + 1))
        for u in RPCS:
            if u != self.url:
                try: return self._call(u, m, p)
                except Exception as e: err = e
        raise err
    def freshness(self):
        """Pick a primary whose latest block is <= MAX_DATA_AGE_SEC old; check slot discrepancy vs the other."""
        res = {}
        for u in RPCS:
            try:
                slot = self._call(u, "getSlot", [{"commitment": "confirmed"}])
                res[u] = (slot, time.time() - (self._call(u, "getBlockTime", [slot]) or 0))
            except Exception: res[u] = None
        res = {("helius" if u == HELIUS else u): v for u, v in res.items()}
        name = lambda u: "helius" if u == HELIUS else u
        fresh = [u for u in RPCS if res.get(name(u)) and res[name(u)][1] <= T.MAX_DATA_AGE_SEC]
        if not fresh: return None, f"stale/no RPC data: {res}"
        slots = [v[0] for v in res.values() if v]
        if len(slots) == 2 and abs(slots[0] - slots[1]) > T.MAX_DATA_AGE_SEC / 0.4: return None, f"RPC slot discrepancy {slots}"
        self.url = fresh[0]; return name(self.url), "ok"

GECKO_DEX_MAP = {"pumpswap": "pumpswap", "pump-swap": "pumpswap"}

def merge_sources(ds_pair, gk_attrs, gk_dex):
    """DexScreener pair dict, filled from GeckoTerminal pool attrs where DS lacks liquidity/volume (and vice versa:
    if DS has data, it wins; if only Gecko has it, Gecko is used). Adds per-field source tags."""
    d = dict(ds_pair or {}); src = {}
    liq = (d.get("liquidity") or {}).get("usd"); vol = (d.get("volume") or {}).get("h24")
    g = gk_attrs or {}
    def f(x):
        try: return float(x)
        except (TypeError, ValueError): return None
    g_liq = f(g.get("reserve_in_usd")); g_vol = f((g.get("volume_usd") or {}).get("h24")); g_h6 = f((g.get("volume_usd") or {}).get("h6"))
    if liq is not None: src["liquidity"] = "dexscreener"
    elif g_liq is not None: d["liquidity"] = {**(d.get("liquidity") or {}), "usd": g_liq}; src["liquidity"] = "geckoterminal"
    if vol is not None: src["volume_h24"] = "dexscreener"
    elif g_vol is not None:
        d["volume"] = {**(d.get("volume") or {}), "h24": g_vol, "h6": g_h6}; src["volume_h24"] = "geckoterminal"
    if not d.get("dexId") and gk_dex: d["dexId"] = GECKO_DEX_MAP.get(gk_dex, gk_dex); src["dex"] = "geckoterminal"
    if not d.get("priceUsd") and g.get("base_token_price_usd"): d["priceUsd"] = g["base_token_price_usd"]
    if not d.get("pairCreatedAt") and g.get("pool_created_at"):
        try: d["pairCreatedAt"] = int(dt.datetime.fromisoformat(g["pool_created_at"].replace("Z", "+00:00")).timestamp() * 1000)
        except Exception: pass
    d["_sources"] = src
    return d

def ds_source(path):
    """DexScreener supplement: returns [(pool, mint, name)] for Solana tokens/pairs from one DS list endpoint (1 call)."""
    j = dexscreener_get("https://api.dexscreener.com" + path)
    if isinstance(j, dict) and "pairs" in j:
        return [(p["pairAddress"], p["baseToken"]["address"], f'{p["baseToken"].get("symbol")} / {p["quoteToken"].get("symbol")}')
                for p in j["pairs"] or [] if p.get("chainId") == "solana" and p.get("dexId") == "pumpswap"]
    return [(None, t["tokenAddress"], t.get("description", "")[:40] or t["tokenAddress"]) for t in (j or []) if t.get("chainId") == "solana"]

def gecko_pool(pool):
    """Single-pool GeckoTerminal fetch (used when DexScreener has the mint but not this pool's numbers).
    gecko429-r1: returns (attrs, dex, fail_kind); fail_kind None on success, else rate_limited/timeout/server_error/error."""
    try:
        j = gecko(f"/networks/solana/pools/{pool}")["data"]
        return j["attributes"], ((j.get("relationships") or {}).get("dex") or {}).get("data", {}).get("id"), None
    except hashlog.HashlogCorrupt: raise
    except GeckoUnavailable as e: print("gecko pool fetch failed:", e); return {}, None, e.kind
    except Exception as e: print("gecko pool fetch failed:", e); return {}, None, "error"

# gecko429-r2: liquidity_unknown requeue persisted in paper.db (new table, created on demand) -> a restart keeps due times and retry counters
RQ_SCHEMA = """CREATE TABLE IF NOT EXISTS liquidity_requeue(pool TEXT PRIMARY KEY, mint TEXT, name TEXT, due REAL, tries INTEGER NOT NULL DEFAULT 0,
  fail_kind TEXT, first_ts REAL, last_ts REAL)"""
def rq_get(c, pool):
    c.execute(RQ_SCHEMA); r = c.execute("SELECT mint,name,due,tries,fail_kind FROM liquidity_requeue WHERE pool=?", (pool,)).fetchone()
    return dict(mint=r[0], name=r[1], due=r[2], tries=r[3], kind=r[4]) if r else None
def rq_due(c, t):
    c.execute(RQ_SCHEMA); return c.execute("SELECT pool,mint,name FROM liquidity_requeue WHERE due<=? ORDER BY tries DESC, due", (t,)).fetchall()
def rq_put(c, pool, mint, name, due, tries, kind):
    c.execute(RQ_SCHEMA); t = time.time()
    c.execute("""INSERT INTO liquidity_requeue(pool,mint,name,due,tries,fail_kind,first_ts,last_ts) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(pool) DO UPDATE SET due=excluded.due, tries=excluded.tries, fail_kind=excluded.fail_kind, last_ts=excluded.last_ts""",
              (pool, mint, name, due, tries, kind, t, t)); c.commit()   # commit at once: survives a crash/restart mid-cycle
def rq_pop(c, pool):
    c.execute(RQ_SCHEMA); c.execute("DELETE FROM liquidity_requeue WHERE pool=?", (pool,))
UNKNOWN_KINDS = ("rate_limited", "timeout", "server_error", "budget_skipped")   # liquidity could not be determined (not a real "missing")

# marks1-r1 (constants here; thresholds.py untouched). STALE_MARK_TTL_SEC (300 s, $0 after) and DAILY_LOSS_CAP_PCT unchanged.
MARKS1_JUP_MAX_PER_MIN = 54            # r11 (069 approved): hard ceiling 54/min with a key (Free: 60/min org bucket, 54 + 2 price = 56); keyless -> T tier 27
MARKS1_JUP_ENTRY_MAX_PER_MIN = 14      # r11: entry quotes inside the ceiling (keyed 14; keyless -> T tier 7)
MARKS2_JUP_BURST = 20                  # r2: token bucket (replaces r1's fixed 2.22 s gap): up to 20 back-to-back, refill 27/60 per s;
                                       # the 60 s sliding window (<= 27) still applies on top
MARKS2_PASS_DEADLINE_SEC = 45.0        # r2: hard wall-clock (monotonic) deadline per monitor pass; later legs -> cached mark + exit_pending
MARKS4_CLOSE_SHARE_PER_MIN = 13       # r4: kill/close_all sells per 60 s while a healthy strategy has open legs (marks keep >= 14 of 27)
MARKS4_CLOSE_BUDGET_SEC = 15.0         # r4: close time budget per pass when healthy legs exist (checked before each sell; worst case + one 15 s timeout)
MARKS4_LEG_BACKOFF_BASE_SEC = 60.0     # r4: per-leg close backoff after no_route / 400 / timeout: 60, 120, 240, 480, 900 s (cap)
MARKS4_LEG_BACKOFF_MAX_SEC = 900.0
MARKS4_LEG_BACKOFF_KINDS = ("no_route", "quote_error", "timeout")
_close_ctx = {"share": 27, "budget": 45.0, "spent": 0.0, "leg_backoff_skips": 0, "budget_skips": 0}
_close_calls = []
_close_leg_backoff = {}                # position_id -> {"until": t, "n": k} (in memory: a restart retries each leg once, then backs off again)
MARKS5_RECOVERY_BONUS_SEC = 60.0      # r5: quote_plan score = sec since last attempt (+60 if in recovery, +60 if urgent); highest first.
MARKS5_URGENT_BONUS_SEC = 60.0         #     Bounded bonus -> round-robin over time: a leg not tried for > 120 s longer than another goes first.
MARKS5_MISSED_PASS_SEC = 90.0         # r5: a leg whose last attempt is >= 90 s old (it missed the last pass) is quoted before every other leg
MARKS6_AGE_CAP_SEC = 300.0            # r6: last-good age counts at most 300 s in the score (r5: uncapped -> dead legs won forever)
MARKS7_DEAD_MIN_NOROUTES = 2          # r7: dead lane only after >= 2 consecutive no-route answers (fails streak, last kind no_route) ...
MARKS7_DEAD_MIN_AGE_SEC = 300.0        #     ... AND last good mark older than 300 s (= STALE_MARK_TTL_SEC) or never; before that: normal (live) lane
MARKS7_DEAD_MIN_OUTAGE_SEC = 300.0     #     ... AND (r7 extra guard) the no-route streak itself started > 300 s ago (fail_since_ts)
MARKS8_DEAD_MIN_NOROUTES = 2          # r8: dead lane IMMEDIATELY after >= 2 CONSECUTIVE no_route answers (marks8_dead.noroutes: +1 on no_route,
                                       #     reset to 0 by an ok quote AND by any transient failure); r7's 300 s age/streak guards (MARKS7_*) no longer used
MARKS10_PER_MINT = True                # r10: ONE executable quote per mint per pass, sized for the mint's TOTAL open qty, split pro rata to its legs
                                       #     (sell() still quotes the leg's own qty when a sale executes)
MARKS10_TRIAGE = True                  # r10: one batched Jupiter Price API call for the suspicious legs (dead + suspects); a mint whose price blockId ADVANCED
                                       #     since the last look (new on-chain swap) is quoted first. Used ONLY to pick what to quote; stop/sale use executable quotes.
MARKS10_PRICE_URL = "https://api.jup.ag/price/v3"   # GET ?ids=<comma mints>, <= 50 ids/request, unpriced mints omitted; same key header as quotes
MARKS10_PRICE_IDS_PER_CALL = 50
MARKS10_PRICE_MAX_PER_MIN = 2          # r10: own limiter, NOT in the 27/60 s quote window. Jupiter counts Price + Quote in ONE bucket per org:
                                       #     keyless 30/min -> 27 quotes + 2 price = 29; Free key 60/min -> 54 + 2 = 56
MARKS11_PRICE_TIMEOUT_SEC = 2.0        # r11: /price/v3 timeout = min(2 s, time left before the 45 s pass deadline); no call if < 0.2 s left
MARKS11_PRICE_MIN_LEFT_SEC = 0.2
MARKS11_URGENT_MARK_AGE_SEC = 180.0    # r11: "urgent" = stop zone (URGENT_MARK_CHG_PCT) / EXIT_PENDING_AGE_HOURS, or last good mark >= 180 s (2 passes from the TTL)
MARKS11_PRICE_CB_SEC = 300.0           # r11: circuit breaker: any error / timeout / non-200 / malformed body -> no Price call for 5 min (r9 lane only)
MARKS10_TRIAGE_MAX_PER_PASS = 8        # r10: at most 8 triaged legs pulled ahead of the live lane per pass (at least MARKS8_WAKE_MIN)
MARKS9_PROBE_YOUNG_SEC = 900.0         # r9: 2 probe slots among dead legs with a streak < 15 min (backoff ignored): A = oldest attempt,
                                       #     B = most recent streak (youngest fail_since_ts; ties: oldest attempt), B != A
MARKS9_EXTRA_PROBE_SLOTS = 1           # r9: the 2nd probe gets its own lane slot (r8: 3 / 2 under HALT -> r9: 4 / 3)
MARKS9_CLUSTER_GAP_SEC = 15.0          # r9: streak starts > 15 s apart = different clusters (a pass quotes for <= 45 s, passes are 60 s apart)
MARKS9_WAKE_GOOD_SEC = 900.0           # r9: cohort wake order: 1) streak start closest to the event's (in passes), 2) last good mark < 15 min, 3) fewer consecutive no_route, 4) oldest attempt
MARKS9_BORROW_MAX_AGE_SEC = 300.0      # r9 borrowed mark: a no_route leg is valued from a SIBLING leg (same mint) only if its good mark is < 5 min old
MARKS8_DEAD_SLOTS = 3                  # r8: dead-lane slots per pass, ahead of live legs (r6/r7: 1) ...
MARKS8_DEAD_SLOTS_MIN = 2              #     ... 2 while a HALT close share is active (lane quotes then compete with marks in the 27/60 s window)
MARKS8_PASS_SEC = 60.0                 # r8: nominal pass length for the backoff
MARKS8_BACKOFF_MAX_SEC = 900.0         # r8: per-leg retry interval 60 * 2**level = 1, 2, 4, 8 passes, then capped at 15 min
MARKS8_DEAD_MAX_GAP_SEC = 900.0        # r8: a dead leg not tried for >= 15 min (minus 1 pass of slack) is overdue: next in the dead lane after the probe
MARKS8_PROBE_YOUNG_SEC = 900.0         # r8: probe slot (first of the 3): oldest-attempt dead leg whose no-route streak is < 15 min, backoff ignored
MARKS8_COHORT_SEC = 180.0              # r8: route-back wake: when a dead/no-route leg gets an ok quote, dead legs whose streak started within +-180 s ...
MARKS8_WAKE_TTL_SEC = 600.0            #     ... of its streak start are quoted in the LIVE lane (once per event and leg; event valid 600 s), at most
                                       #     the spare mark capacity per pass (cap - ceil(n_live/2) - dead slots, min 1), so healthy legs keep <= 2 passes
MARKS8_WAKE_MIN = 4                    #     ... but at least 4 woken legs per pass (a returning leg may need its stop sold)
_r8_events = []                        # r8: (ts, fail_since_ts) of route-back events
_r8_woken = {}                         # r8: position_id -> event ts already used for a wake
_r8_lane = set()                       # r8: this pass's dead-lane legs: their quotes count in the 27/60 s window but not in the 20-token bucket
_r8_lane_call = [False]                #     (set by monitor around a lane leg's mark quote; see _jup_avail / _jup_take)
_r8_tail = set()                       # r8: due dead legs placed after every live leg (quote_plan) -> skipped once a live leg was skipped
MARKS6_DEAD_PER_PASS = 1              # r6: dead (no-route clock) legs: a fixed 1-slot lane per pass; the rest only after every live leg
MARKS5_AGE_WEIGHT = 1.0               # r5: quote_plan score also counts the age of the last good mark (oldest marks retried every pass)
MARKS5_CLOSE_MIN_PER_MIN = 8          # r6: floor 8 (r5: 5); close share shrinks (13 -> floor 8) so healthy marks get ceil(n_healthy/2) slots per pass: each leg tried every <= 2 passes
MARKS5_ENTRY_YIELD_AGE_SEC = 180.0     # r5: entries yield (no entry quote) while any healthy leg's last good mark is older than this, or never marked
MARKS5_HTTP_TIMEOUT_SEC = 15.0         # r5: HTTP timeout for monitor/close quotes = min(15, time left in the 45 s pass), floor 1 s
MARKS5_HTTP_TIMEOUT_MIN_SEC = 1.0
_entry_yield = [False]
_pass_t0 = [None]                      # monotonic start of the running monitor pass (None outside monitor)
MARKS2_ENTRY_MAX_WAIT_SEC = 10.0       # r2: entry quotes (cycle, not monitor) wait at most this for a slot, never during a 429 backoff
MARKS1_BACKOFF_BASE_SEC = 5.0          # after an HTTP 429: 5, 10, 20, 40, 60 s (cap); no Jupiter call during backoff
MARKS1_BACKOFF_MAX_SEC = 60.0
MARKS1_STALE_AFTER_SEC = 120           # last good mark older than this (2 passes) -> leg is "in recovery" (quoted first)
_jup_backoff = {"until": 0.0, "n": 0}
_jup_stats = {"http_429": 0, "backoff_sec": 0.0, "entry_deferred": 0, "local_skips": 0, "deadline_sell_skips": 0}
_jup_bucket = {"tok": float(MARKS2_JUP_BURST), "t": None}
def _jup_refill():
    t = time.time(); _jup_calls[:] = [x for x in _jup_calls if t - x < 60]; _jup_entry_calls[:] = [x for x in _jup_entry_calls if t - x < 60]
    if _jup_bucket["t"] is not None:
        _jup_bucket["tok"] = min(float(MARKS2_JUP_BURST * _jup_limits()[0] // 27), _jup_bucket["tok"] + (t - _jup_bucket["t"]) * _jup_limits()[0] / 60.0)   # r11: refill at the EFFECTIVE ceiling (54 keyed / 27 keyless)
    _jup_bucket["t"] = t; return t
def _jup_avail(purpose="exit", need=1):
    """r2: NON-BLOCKING check. None = `need` Jupiter calls may go now; else reason (backoff / window / bucket / entry_cap / reserve)."""
    t = _jup_refill(); tot, ent = _jup_limits()
    if t < _jup_backoff["until"]: return "backoff"
    if purpose == "entry" and _entry_yield[0]: return "yield"   # r5: marks are behind (a healthy leg's last good mark > 180 s or never) -> entries yield first
    if len(_jup_calls) + need > tot: return "window"
    if purpose != "close" and not _r8_lane_call[0] and _jup_bucket["tok"] < need: return "bucket"   # r3: kill/close_all sells do not wait for bucket tokens
    if purpose == "close":   # r4: close share of the window (13/60 s while healthy legs exist)
        _close_calls[:] = [x for x in _close_calls if t - x < 60]
        if len(_close_calls) + need > _close_ctx["share"]: return "close_share"
    if purpose == "entry":
        if len(_jup_entry_calls) + need > ent: return "entry_cap"
        if len(_jup_calls) + need + min(_recovery_reserve[0], tot - 1) > tot: return "reserve"
    return None
def _jup_take(purpose="exit"):
    """r2: non-blocking: consume one slot (window + bucket) or return why not. Never sleeps."""
    why = _jup_avail(purpose)
    if why: return why
    if (purpose != "close" or _close_ctx["share"] >= _jup_limits()[0]) and not _r8_lane_call[0]: _jup_bucket["tok"] = max(0.0, _jup_bucket["tok"] - 1.0)   # r4: shared close never drains marks' bucket
    _jup_calls.append(time.time())
    if purpose == "close": _close_calls.append(time.time())
    if purpose == "entry": _jup_entry_calls.append(time.time())
    return None
_recovery_reserve = [0]                # legs still unmarked after the last pass: their slots are kept free from entry quotes
MARKS111_MINT_SKIP_SEC = 2.0          # r11.1: after an HTTP 429 inside a monitor pass: only THAT mint is skipped for 2 s (no global backoff)
MARKS111_RETRIES = 3                  # r11.1: a leg whose quote got a 429 is re-queued at the END of the same pass at most 3 times
MARKS111_DEFERS = 3                   # r11.1: a leg reached while its mint is skipped moves to the end at most 3 times, then is tried anyway
MARKS111_PASS_429_STREAK = 6          # r11.1: safety valve: 6 CONSECUTIVE in-pass 429s -> the old r1 global backoff (gateway really closed)
MARKS111_INPASS_WAIT_SEC = 0.0        # r11.1: opt-in bounded wait (<= 1 s) -- NOT used (0 = off); the monitor still never sleeps
_r111 = {"mint_skip": {}, "streak": 0, "inpass_429": 0, "requeued": 0, "deferred": 0}
def _jup_note(kind):
    """marks1-r1: 429 -> exponential global backoff; any non-429 answer resets the streak.
    r11.1: inside a monitor pass a 429 does NOT start the global backoff (that skipped the whole rest of the pass); monitor
    re-queues the leg and skips its mint for 2 s. Only MARKS111_PASS_429_STREAK consecutive 429s fall back to the global backoff."""
    if kind == "rate_limited" and _pass_t0[0] is not None:
        _jup_stats["http_429"] += 1; _r111["inpass_429"] += 1; _r111["streak"] += 1
        if _r111["streak"] < MARKS111_PASS_429_STREAK: return
    if kind != "rate_limited": _r111["streak"] = 0
    if kind == "rate_limited":
        _jup_stats["http_429"] += 0 if _pass_t0[0] is not None else 1; _jup_backoff["n"] += 1
        d = min(MARKS1_BACKOFF_BASE_SEC * 2 ** (_jup_backoff["n"] - 1), MARKS1_BACKOFF_MAX_SEC)
        _jup_backoff["until"] = max(_jup_backoff["until"], time.time() + d)
    else: _jup_backoff["n"] = 0
def _jup_wait(purpose="entry"):
    """r2: ENTRY quotes only (cycle; monitor never calls this). Waits at most MARKS2_ENTRY_MAX_WAIT_SEC for window/bucket/reserve room;
    returns at once with a reason during a 429 backoff. None = slot taken."""
    t0 = time.time(); deferred = False
    while True:
        why = _jup_take(purpose)
        if why is None: return None
        if why in ("backoff", "yield") or time.time() - t0 >= MARKS2_ENTRY_MAX_WAIT_SEC: return why   # r5: yield -> no wait
        if why == "reserve" and not deferred: deferred = True; _jup_stats["entry_deferred"] += 1
        time.sleep(1)
def _jup_wait_r0(purpose="exit"):   # pre-marks1 limiter, kept for diff readability (unused)
    """week1d v2: 60 s sliding-window limiter for every Jupiter quote (JUP_MAX_CALLS_PER_MIN total). purpose='entry' is
    additionally capped at JUP_ENTRY_MAX_CALLS_PER_MIN so marks/exit sells always keep the rest of the minute."""
    while True:
        t = time.time(); _jup_calls[:] = [x for x in _jup_calls if t - x < 60]; _jup_entry_calls[:] = [x for x in _jup_entry_calls if t - x < 60]
        tot, ent = _jup_limits()
        if len(_jup_calls) < tot and (purpose != "entry" or len(_jup_entry_calls) < ent): break
        time.sleep(1)
    _jup_calls.append(time.time())
    if purpose == "entry": _jup_entry_calls.append(time.time())

_jup_last = [None]   # noroute1-r2 diag: (kind, http_status, error) of the LAST quote attempt; reset before every quote
MONITOR_QUOTE_MIN_GAP_SEC = 0.35   # noroute1-r2 diag: min spacing between monitor-pass quotes (no burst; 11 quotes ~3.9 s)
TRANSIENT_QUOTE_FAILURES = ("rate_limited", "timeout", "server_error", "quote_error")   # r3: NOT no_route -> keep last good mark, flagged
NOROUTE_ENABLED = False   # r3: noroute1 wiring present but OFF; flipping it is an activation step (069 + auditor)
_last_live_quote = [0.0]
def _space_live_quote():
    """r3: called ONLY right before a live monitor quote (never for mark_cached): >= MONITOR_QUOTE_MIN_GAP_SEC between live quotes."""
    w = MONITOR_QUOTE_MIN_GAP_SEC - (time.monotonic() - _last_live_quote[0])
    if w > 0: time.sleep(w)
    _last_live_quote[0] = time.monotonic()
def _classify_quote(status, j):
    """noroute1-r2: 'no_route' ONLY for an explicit no-route answer (HTTP 200/400 + route error text);
    429 -> rate_limited; 5xx -> server_error; anything else -> quote_error. Never conflates a rate-limit with no route."""
    err = ""
    if isinstance(j, dict): err = str(j.get("error") or j.get("errorCode") or j.get("message") or "")[:120]
    if status == 429: return "rate_limited", err
    if status is not None and 500 <= status < 600: return "server_error", err
    if status in (200, 400) and "route" in err.lower(): return "no_route", err
    return "quote_error", err
def _jup_timeout():
    """r5: HTTP timeout for one Jupiter quote: 15 s, but inside a monitor pass never more than the time left before the 45 s deadline (min 1 s)."""
    if _pass_t0[0] is None: return MARKS5_HTTP_TIMEOUT_SEC
    return max(MARKS5_HTTP_TIMEOUT_MIN_SEC, min(MARKS5_HTTP_TIMEOUT_SEC, MARKS2_PASS_DEADLINE_SEC - (time.monotonic() - _pass_t0[0])))
# combo1 r2: quote staleness. ~15 min without ONE ok Jupiter quote -> QUOTE_STALE flag file + quote_stale log event (once per
# episode); the next ok quote removes the flag and logs quote_stale_cleared. Clock starts at process start (a restart resets it).
# r2.1: a flag left by the previous process keeps its 'since' as last_ok and clears only after an OK quote of THIS process.
QUOTE_STALE_SEC = 900.0
def _qstale_init():
    st = {"last_ok": time.time(), "flagged": False, "last_fail": None, "ok_seen": False}
    try: since = json.load(open(os.path.join(HERE, "QUOTE_STALE"))).get("since")
    except FileNotFoundError: return st
    except (OSError, ValueError, AttributeError): since = None   # unreadable flag: keep it, clock from now
    st["flagged"] = True
    try: st["last_ok"] = min(st["last_ok"], dt.datetime.fromisoformat(since).timestamp())
    except (TypeError, ValueError): pass
    return st
_qstale = _qstale_init()
def _qs_log(kind, **kw):   # r2.1: bypass the monitor-pass deferral so a rollback can't turn the alert into a db_rollback entry
    (_defer["real"] or hashlog.append)(kind, **kw)
def _quote_stale_check():
    """Called after every quote and at the end of every monitor pass (so it also fires when no quote is attempted)."""
    f = os.path.join(HERE, "QUOTE_STALE"); age = time.time() - _qstale["last_ok"]
    if age < QUOTE_STALE_SEC:
        if _qstale["flagged"] and _qstale["ok_seen"]:
            _qstale["flagged"] = False
            try: os.remove(f)
            except FileNotFoundError: pass
            _qs_log("quote_stale_cleared", url=JUP_QUOTE_URL, keyed=bool(_JK))
        return
    if _qstale["flagged"]: return
    _qstale["flagged"] = True; lf = _qstale["last_fail"]
    info = dict(since=dt.datetime.fromtimestamp(_qstale["last_ok"], dt.timezone.utc).isoformat(), age_sec=round(age), url=JUP_QUOTE_URL,
                keyed=bool(_JK), last_fail_kind=lf and lf[0], last_fail_status=lf and lf[1])
    tmp = f + ".tmp"
    with open(tmp, "w") as fh: json.dump(info, fh)
    os.replace(tmp, f); _qs_log("quote_stale", **info)

def jup_quote(inp, out, amount_raw):
    q = _jup_quote(inp, out, amount_raw); k = _jup_last[0]
    if k and k[0] == "ok": _qstale["last_ok"] = time.time(); _qstale["ok_seen"] = True
    elif k: _qstale["last_fail"] = k[:2]
    try: _quote_stale_check()
    except OSError as e: print("quote_stale check failed:", type(e).__name__)
    return q

def _jup_quote(inp, out, amount_raw):
    """Read-only Jupiter quote (no swap). Returns dict or None (see _jup_last for why). Rate-limited (week1d)."""
    _jup_last[0] = None   # r2: never carry a previous quote's result into this one
    why = _jup_wait("entry") if _jup_purpose[0] == "entry" else _jup_take(_jup_purpose[0])   # r2: marks/exits/kill/drawdown never sleep here
    if why:   # local limiter said no: no HTTP call, fail fast as rate_limited (transient: keeps last good mark, no no-route clock)
        _jup_stats["local_skips"] += 1; _jup_last[0] = ("rate_limited", None, "local_limiter:" + why); return None
    try:
        r = S.get(JUP_QUOTE_URL, params={"inputMint": inp, "outputMint": out, "amount": int(amount_raw), "slippageBps": 150},
                  headers={"x-api-key": _JK} if _JK else None, timeout=_jup_timeout())   # key only in header, never in params/URL
        try: j = r.json() if r.content else {}
        except ValueError: j = {}
        if r.status_code == 200 and isinstance(j, dict) and "outAmount" in j: _jup_last[0] = ("ok", 200, None); _jup_note("ok"); return j
        kind, err = _classify_quote(r.status_code, j); _jup_last[0] = (kind, r.status_code, err); _jup_note(kind); return None
    except Exception as e:
        _jup_last[0] = ("timeout" if "Timeout" in type(e).__name__ else "quote_error", None, type(e).__name__); return None

def spot(sym):
    return float(S.get(f"https://api.coinbase.com/v2/prices/{sym}-USD/spot", timeout=15).json()["data"]["amount"])

# ---------------- db ----------------
# ---- dblock1 r2 ----
# Lock wait 8 s for every connection (monitor and cycle): <= 1/5 of the 45 s monitor pass deadline (marks1 r11.1), so one
# lock wait can't eat the pass; > the 5 s default that failed on 15:07; < the 12 s scan tick, so a cycle never overlaps the next.
DB_BUSY_TIMEOUT_SEC = 8.0
_defer = {"buf": None, "real": None}   # monitor pass: hash-log events buffered until the DB commit that makes them true
def _defer_append(kind, **fields):
    _defer["buf"].append((kind, fields)); return None   # monitor path never uses append()'s return value
_hl_unwritten = []   # exits1 r4 (aud52 F1): committed events whose hash-log write AND its flush-failed record both failed -> retried next write
# exits1 r5 (aud53 G2): _hl_unwritten is main-thread only (fm_tick/_fastmon_run never touch it). Bounded: when it reaches
# HL_UNWRITTEN_MAX entries or its oldest entry is older than HL_UNWRITTEN_MAX_AGE_SEC (10 min = 50 scan ticks of 12 s), it is
# appended to HL_UNWRITTEN_FILE (JSONL, flush+fsync of file and dir) and the alert flag HL_UNWRITTEN_FLAG is written; then cleared.
HL_UNWRITTEN_MAX = 1000
HL_UNWRITTEN_MAX_AGE_SEC = 600.0
HL_UNWRITTEN_FILE = os.path.join(HERE, "HASHLOG_UNWRITTEN.jsonl")
HL_UNWRITTEN_FLAG = os.path.join(HERE, "HASHLOG_UNWRITTEN_ALERT")
_hl_unwritten_since = [None]   # time.time() when the oldest held entry was first held
HL_SPILL_RETRY_SEC = 60.0   # r6 (aud54): after a failed dump, retry only after 60 s or another HL_UNWRITTEN_MAX entries (no O(n^2) on a bad disk)
_hl_spill_fail = {"t": None, "n": 0}
HL_ALERT_FALLBACK = os.path.join("/tmp", "exits1_HASHLOG_UNWRITTEN_ALERT")
def _fsync_write(path, text, mode="a"):
    with open(path, mode) as fh:
        fh.write(text); fh.flush(); os.fsync(fh.fileno())
    dfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
    try: os.fsync(dfd)
    finally: os.close(dfd)
def _hl_alert(info):
    """r6 (H2b): alert flag; if the primary flag can't be written, fallback flag in /tmp + stderr line. Never raises."""
    txt = json.dumps(info, default=str) + "\n"
    for path in (HL_UNWRITTEN_FLAG, HL_ALERT_FALLBACK):
        try: _fsync_write(path, txt, "w"); return path
        except Exception as e: print("hashlog alert flag write failed:", path, type(e).__name__, file=sys.stderr)
    print("ALERT (no flag file could be written):", txt.strip(), file=sys.stderr); return None
def _hl_spill(reason):
    """exits1 r5 (G2) / r6 (H2, H2b): append held events to HL_UNWRITTEN_FILE (fsync). Memory is cleared as soon as the JSONL is
    durable (so a later flag failure never re-writes the same lines); the alert flag is handled separately (_hl_alert, with
    fallback). If the JSONL dump fails, events stay in memory, an alert is still raised and retries are throttled. Never raises."""
    if not _hl_unwritten: return False
    ts = dt.datetime.now(dt.timezone.utc).isoformat(); n = len(_hl_unwritten)
    try:
        lines = "".join(json.dumps({"ts": ts, "reason": reason, "kind": k, "fields": f}, default=str) + "\n" for k, f in _hl_unwritten)
        _fsync_write(HL_UNWRITTEN_FILE, lines)
    except Exception as e:
        _hl_spill_fail.update(t=time.time(), n=n)
        _hl_alert({"ts": ts, "reason": reason, "n": n, "file": HL_UNWRITTEN_FILE, "dump_failed": f"{type(e).__name__}: {str(e)[:200]}",
                   "note": "events still held in memory only"})
        return False
    _hl_unwritten.clear(); _hl_unwritten_since[0] = None; _hl_spill_fail.update(t=None, n=0)
    _hl_alert({"ts": ts, "reason": reason, "n": n, "file": HL_UNWRITTEN_FILE})
    print(f"ALERT: {n} unwritten hash-log events dumped to {HL_UNWRITTEN_FILE} ({reason})", file=sys.stderr)
    return True
def _hl_check(reason_age="age"):
    """r6 (H3): cap/age check; called after holding events and every scanloop tick from _fm_watch (MAIN thread)."""
    if not _hl_unwritten: return False
    t = time.time(); f = _hl_spill_fail
    if f["t"] is not None and t - f["t"] < HL_SPILL_RETRY_SEC and len(_hl_unwritten) < f["n"] + HL_UNWRITTEN_MAX: return False
    if len(_hl_unwritten) >= HL_UNWRITTEN_MAX: return _hl_spill("cap")
    if t - (_hl_unwritten_since[0] or t) >= HL_UNWRITTEN_MAX_AGE_SEC: return _hl_spill(reason_age)
    return False
def _hl_hold(rest):
    if _hl_unwritten_since[0] is None: _hl_unwritten_since[0] = time.time()   # since = oldest held entry
    _hl_unwritten.extend(rest); _hl_check()
def _hl_exit_dump():
    """r6 (H3) / r7: atexit (normal exit or SystemExit, e.g. the HashlogCorrupt SystemExit(2)): dump whatever is still held.
    r7 (aud55 S1-S4): no TERM signal handler -> TERM/KILL behave exactly as in 4ded3fcd and lose in-memory held events (<= 10 min / 1000)."""
    if _hl_unwritten: _hl_spill("exit")
import atexit; atexit.register(_hl_exit_dump)
def _hl_write(items, write, source):
    """exits1 r4 (aud52 F1): write committed events one by one; an event leaves the list only once written. On the first failure
    the unwritten rest goes into ONE hashlog_flush_failed record (kind + fields under 'missing'); if that also fails they are
    kept in _hl_unwritten (bounded, r5 G2) and retried before the next write. Never raises EXCEPT hashlog.HashlogCorrupt
    (r5 G1, same convention as 4ded3fcd): the events are held first, then it propagates (loop -> SystemExit(2))."""
    since = _hl_unwritten_since[0]
    items = _hl_unwritten + list(items); _hl_unwritten.clear(); _hl_unwritten_since[0] = None
    for i, (k, f) in enumerate(items):
        try: write(k, **f)
        except hashlog.HashlogCorrupt:
            _hl_unwritten.extend(items[i:]); _hl_unwritten_since[0] = since or time.time(); _hl_spill("corrupt"); raise   # r6 H1
        except Exception as e:
            rest = items[i:]
            try: write("hashlog_flush_failed", source=source, error=mask(f"{type(e).__name__}: {e}")[:300], n_missing=len(rest),
                       missing=[{"kind": k2, **f2} for k2, f2 in rest])
            except hashlog.HashlogCorrupt:
                _hl_unwritten.extend(rest); _hl_unwritten_since[0] = since or time.time(); _hl_spill("corrupt"); raise   # r6 H1
            except Exception:
                _hl_unwritten_since[0] = since; _hl_hold(rest)
            return False
    return True
def _defer_flush():
    buf = _defer["buf"]
    if not buf and not _hl_unwritten: return
    items = list(buf or []); buf is not None and buf.clear()
    _hl_write(items, _defer["real"] or hashlog.append, "monitor_defer")
def _defer_discard(why):
    """Rollback: the buffered events describe DB writes that no longer exist -> NOT logged as such; one db_rollback record
    keeps them (kind + fields) under 'discarded' so nothing is silently lost and the next pass may redo the sells."""
    buf = _defer["buf"]
    if not buf: return
    items = list(buf); buf.clear()
    _defer["real"]("db_rollback", reason=mask(str(why))[:300], n_discarded=len(items),
                   discarded_position_ids=sorted({f["position_id"] for _, f in items if isinstance(f.get("position_id"), int)}),
                   discarded=[{"kind": k, **f} for k, f in items])
class _Conn(sqlite3.Connection):
    """commit(): flush deferred events only AFTER the commit succeeded. rollback(): discard them (db_rollback)."""
    _why = "rollback"
    def commit(self):
        super().commit(); pend = _fm_cyc_pending.pop(id(self), None)   # exits1 r4 (F1): detach right after the commit -> never logged as rolled back
        try: _defer_flush()
        finally: _fm_cyc_commit(self, pend)   # exits1 r3: in-cycle fast-exec events only after the cycle commit
    def rollback(self):
        super().rollback(); _defer_discard(self._why); _fm_cyc_rollback(self, self._why)   # exits1 r3: compensation + re-queue
def _close_db(c, why=None):
    """Roll back any open transaction (a failed COMMIT stays open and holds a PENDING/RESERVED lock) and close.
    Python 3.13 sqlite3 connections are only freed by the cyclic GC, so an unclosed one can hold its lock for minutes."""
    if c is None: return
    try:
        if c.in_transaction:
            if why: c._why = why
            c.rollback()
    except sqlite3.Error as e: print("db rollback failed:", mask(e))
    try: c.close()
    except sqlite3.Error as e: print("db close failed:", mask(e))
def db():
    c = sqlite3.connect(DB, timeout=DB_BUSY_TIMEOUT_SEC, factory=_Conn)
    try:
        c.execute(f"PRAGMA busy_timeout={int(DB_BUSY_TIMEOUT_SEC * 1000)}")
        _db_init(c)
    except BaseException:
        _close_db(c, "db() init failed"); raise   # r2 (3): never leave a half-initialised connection open
    return c

def _db_init(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS scans(ts, pool, mint, name, dex, liquidity REAL, volume_h24 REAL, txns_h1 INTEGER, passed INTEGER, state TEXT);
    CREATE TABLE IF NOT EXISTS rejections(ts, strategy, pool, mint, name, rule, reason);
    CREATE TABLE IF NOT EXISTS positions(id INTEGER PRIMARY KEY AUTOINCREMENT, strategy, mint, pool, symbol, opened_ts,
        qty_raw INTEGER, qty_left_raw INTEGER, cost_usd REAL, entry_fee_usd REAL, entry_impact_pct REAL,
        peak_mark REAL, tp_done INTEGER DEFAULT 0, status DEFAULT 'open', closed_ts, decision_hash);
    CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY AUTOINCREMENT, position_id INTEGER, ts, side, qty_raw INTEGER,
        usd REAL, price_impact_pct REAL, fee_usd REAL, reason);
    CREATE TABLE IF NOT EXISTS equity(ts, strategy, realized REAL, unrealized REAL, equity REAL);
    CREATE TABLE IF NOT EXISTS holder_snapshots(ts_epoch REAL, ts TEXT, mint TEXT, holders_count INTEGER, holders_source TEXT, top10_pct REAL, top_holder_pct REAL);
    CREATE INDEX IF NOT EXISTS holder_snapshots_mint_ts ON holder_snapshots(mint, ts_epoch);
    CREATE TABLE IF NOT EXISTS baselines(ts, btc_usd REAL, sol_usd REAL, btc_value_of_bank REAL, sol_value_of_bank REAL);
    """)
    migrate(c)

# exits1 r2 part 2: price history rows are buffered in memory (deque, no IO in the caller) and written by the fast thread to
# price_history.db (NOT paper.db: the watchdog's paper.db-mtime stall check stays driven by the main thread only).
import collections
_PH_BUF = collections.deque(maxlen=50000)
_hist_src = ["quote_monitor"]
def _ph_push(p, price, upnl, source, peak_upnl=None, est=0):
    if len(_PH_BUF) == _PH_BUF.maxlen: _fm_stats["hist_dropped"] += 1   # exits1 r3 (C4): count silent drops
    _PH_BUF.append((p["id"], now().isoformat(), price, None if upnl is None else round(upnl, 4), source, p.get("strategy"), p.get("qty_left_raw"),
                    None if peak_upnl is None else round(peak_upnl, 4), est))

def migrate(c):
    """Add immutable `lane` ('meme'|'liquid') to positions and fills. Idempotent. Triggers:
    lane must be valid on insert and can never be reassigned; fills stay append-only."""
    for t in ("positions", "fills"):
        if "lane" not in [r[1] for r in c.execute(f"PRAGMA table_info({t})")]:
            c.execute(f"ALTER TABLE {t} ADD COLUMN lane TEXT")
            c.execute(f"UPDATE {t} SET lane='meme' WHERE lane IS NULL")   # only liquid lane is unbuilt, so legacy rows are meme
        c.executescript(f"""
        CREATE TRIGGER IF NOT EXISTS {t}_lane_valid BEFORE INSERT ON {t}
          WHEN NEW.lane IS NULL OR NEW.lane NOT IN ('meme','liquid') BEGIN SELECT RAISE(ABORT,'lane must be meme or liquid'); END;
        CREATE TRIGGER IF NOT EXISTS {t}_lane_immutable BEFORE UPDATE OF lane ON {t}
          WHEN NEW.lane IS NOT OLD.lane BEGIN SELECT RAISE(ABORT,'lane is immutable'); END;""")
    c.execute("""CREATE TABLE IF NOT EXISTS position_marks(position_id INTEGER PRIMARY KEY, last_ok_ts TEXT, last_ok_usd REAL,
        last_ok_impact REAL, last_ok_qty INTEGER, fail_since_ts TEXT, last_fail_logged_ts TEXT, fails INTEGER DEFAULT 0)""")
    for col, typ in (("marks_missing", "INTEGER"), ("marks_stale", "INTEGER")):
        if col not in [r[1] for r in c.execute("PRAGMA table_info(equity)")]: c.execute(f"ALTER TABLE equity ADD COLUMN {col} {typ}")
    if "last_attempt_ts" not in [r[1] for r in c.execute("PRAGMA table_info(position_marks)")]:   # v2: last real quote attempt
        c.execute("ALTER TABLE position_marks ADD COLUMN last_attempt_ts TEXT")
    if "last_fail_kind" not in [r[1] for r in c.execute("PRAGMA table_info(position_marks)")]:   # r3 diag: kind of the last failed quote
        c.execute("ALTER TABLE position_marks ADD COLUMN last_fail_kind TEXT")
    if "last_ttl_alert_ts" not in [r[1] for r in c.execute("PRAGMA table_info(position_marks)")]:   # r4: TTL-crossing alert cadence
        c.execute("ALTER TABLE position_marks ADD COLUMN last_ttl_alert_ts TEXT")
    if "decimals" not in [r[1] for r in c.execute("PRAGMA table_info(positions)")]: c.execute("ALTER TABLE positions ADD COLUMN decimals INTEGER")
    if "pyramid_of" not in [r[1] for r in c.execute("PRAGMA table_info(positions)")]: c.execute("ALTER TABLE positions ADD COLUMN pyramid_of INTEGER")   # week1e add-on leg -> base leg id
    if "peak_ts" not in [r[1] for r in c.execute("PRAGMA table_info(positions)")]: c.execute("ALTER TABLE positions ADD COLUMN peak_ts TEXT")   # exits1: time of peak_mark (logging only)
    if "paired_with" not in [r[1] for r in c.execute("PRAGMA table_info(positions)")]: c.execute("ALTER TABLE positions ADD COLUMN paired_with INTEGER")   # exits1 r2: simple_tp leg -> simple leg id
    c.executescript("""
    CREATE TRIGGER IF NOT EXISTS fills_no_update BEFORE UPDATE ON fills BEGIN SELECT RAISE(ABORT,'fills are append-only'); END;
    CREATE TRIGGER IF NOT EXISTS fills_no_delete BEFORE DELETE ON fills BEGIN SELECT RAISE(ABORT,'fills are append-only'); END;""")
    c.commit()

def reject(c, strat, pool, mint, name, rule, reason, state=None):
    _pyramid.pop((strat, mint), None)   # week1e: a rejected candidate never leaves a pending add-on behind
    reason = mask(reason)
    c.execute("INSERT INTO rejections VALUES(?,?,?,?,?,?,?)", (now().isoformat(), strat, pool, mint, name, rule, reason))
    hashlog.append("decision", strategy=strat, mint=mint, pool=pool, action="reject", rule=rule, reason=reason,
                   state=state, thresholds_sha=thresholds_sha())

def thresholds_sha(): return hashlib.sha256(open(os.path.join(HERE, "thresholds.py"), "rb").read()).hexdigest()

def phase():
    """Week 1 calibrate; week 2+ evaluate with frozen thresholds (hash pinned at first week-2 run)."""
    day = (dt.date.today() - T.EXPERIMENT_START).days
    if day < 7: return "calibration", None
    f = os.path.join(HERE, "thresholds.frozen.sha256")
    if not os.path.exists(f): open(f, "w").write(thresholds_sha())
    if open(f).read().strip() != thresholds_sha(): return "evaluation", "thresholds.py changed after freeze"
    return "evaluation", None

# ---------------- hard filter ----------------
# Filters that exist in code AND run in hard_filter(). Tier B requires both "cluster" and "sniper".
# Add a name here only in the same change that implements and wires the filter (auditor-reviewed).
IMPLEMENTED_FILTERS = frozenset({"liquidity", "volume_24h", "lp_lock", "mint_authority", "freeze_authority", "token2022",
                                 "top_holder", "top10", "copycat", "bonding_curve"})

def tier_b_allowed():
    """Hard gate (code, not config): tier B only if enabled AND cluster(<=20%) AND sniper(<=10%) filters exist + active."""
    return bool(getattr(T, "TIER_B_ENABLED", False)) and {"cluster", "sniper"} <= IMPLEMENTED_FILTERS \
        and getattr(T, "TIER_B_MAX_CLUSTER_PCT", None) == 20.0 and getattr(T, "TIER_B_MAX_SNIPER_PCT", None) == 10.0

# ---- nodata1: on-chain LP burn/lock for Raydium / Meteora (offsets checked against mainnet pools, see nodata1/README.md) ----
RAY_AMM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"   # LiquidityStateV4: lpMint @464, lpReserve u64 @720 (752 bytes)
RAY_CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"     # PoolState: lp_mint @136, lp_supply u64 @333 (637 bytes)
MET_DAMM_V2 = "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG"    # Pool: liquidity u128 @360, permanent_lock_liquidity u128 @552 (1112 bytes)
DAMM_V2_MARGIN_PP = 1.0   # r2.1 (auditor): DAMM v2 effective threshold = LP_MIN_LOCKED_PCT + 1 pp (Raydium unchanged)
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
def _b58(b):
    n = int.from_bytes(b, "big"); out = ""
    while n: n, r = divmod(n, 58); out = _B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out
class LPCheckAbort(Exception):
    """r2: LP check could not finish within its bounds (timeout / tick budget / monitor due / rpc error) -> caller rejects."""
_lp_budget = {"calls": 0, "deadline": 0.0}
_last_monitor_done = [0.0]   # set at the end of every monitor() pass
def lp_budget_reset(t=None):
    """Called once per cycle() right after the (optional) monitor() pass: LP_TICK_MAX_CALLS calls / LP_TICK_MAX_SEC seconds."""
    t = time.time() if t is None else t
    _lp_budget.update(calls=int(getattr(T, "LP_TICK_MAX_CALLS", 8)), deadline=t + float(getattr(T, "LP_TICK_MAX_SEC", 4.0)))
def _lp_call(rpc, m, p):
    """One bounded RPC call for the LP check. Order guard: if a monitor() pass is due (stops/exits pending) -> abort, no call.
    Budget guard: calls/time left in this tick. Wall-clock timeout min(LP_CALL_TIMEOUT_SEC, time left) via a daemon thread;
    no retries, no sleeps, no fallback RPCs (rpc.call_once). Any failure -> LPCheckAbort (reject)."""
    import threading
    now_ = time.time()
    if now_ - _last_monitor_done[0] >= T.MONITOR_EVERY_SEC: raise LPCheckAbort("monitor pass due (stops/exits first)")
    left = _lp_budget["deadline"] - now_
    if _lp_budget["calls"] <= 0 or left <= 0: raise LPCheckAbort("per-tick LP budget exhausted")
    _lp_budget["calls"] -= 1
    to = min(float(getattr(T, "LP_CALL_TIMEOUT_SEC", 2.0)), 2.0, left)
    once_ = getattr(rpc, "call_once", None)
    if once_ is None: raise LPCheckAbort("rpc has no bounded call_once")
    box = {}
    def run():
        try: box["r"] = once_(m, p, to)
        except Exception as e: box["e"] = e
    th = threading.Thread(target=run, daemon=True, name="lp_check"); th.start(); th.join(to)
    if th.is_alive(): raise LPCheckAbort(f"{m} timeout > {to:.2f}s")
    if "e" in box: raise LPCheckAbort(mask(f"{m}: {type(box['e']).__name__}: {box['e']}")[:160])
    return box.get("r")
def lp_lock_onchain(rpc, pool):
    """-> (ok, info). ok only if the burned/permanently-locked share is KNOWN and >= T.LP_MIN_LOCKED_PCT. Threshold None (not approved)
    -> reject, no RPC. Unknown program, short account, zero/inconsistent counters, missing supply -> (False, reason).
    Timeout / budget / rpc error -> LPCheckAbort (hard_filter -> lp_lock reject)."""
    import base64
    need = getattr(T, "LP_MIN_LOCKED_PCT", None)
    if need is None or isinstance(need, bool) or not isinstance(need, (int, float)) or not 0 < need <= 100:
        return False, {"status": "unknown", "reason": "LP lock: lp threshold not approved (LP_MIN_LOCKED_PCT unset; 069 must approve a value)"}
    acc = (_lp_call(rpc, "getAccountInfo", [pool, {"encoding": "base64"}]) or {}).get("value")
    if not acc: return False, {"reason": "LP lock unknown: pool account not found", "status": "unknown"}
    prog = acc.get("owner"); b = base64.b64decode((acc.get("data") or [""])[0])
    info = {"program": prog, "status": "unknown"}
    if prog == MET_DAMM_V2 and len(b) >= 568:
        liq, perm = int.from_bytes(b[360:376], "little"), int.from_bytes(b[552:568], "little")
        if not liq or perm > liq: return False, {**info, "reason": f"LP lock unknown: DAMM v2 liquidity={liq} permanent_lock={perm}"}
        pct = 100 * perm / liq; info.update(kind="damm_v2_permanent_lock", locked_pct=round(pct, 3))
    elif (prog == RAY_AMM_V4 and len(b) >= 728) or (prog == RAY_CPMM and len(b) >= 341):
        lo, ro = (464, 720) if prog == RAY_AMM_V4 else (136, 333)
        lp_mint, ref = _b58(b[lo:lo + 32]), int.from_bytes(b[ro:ro + 8], "little")
        sup = ((_lp_call(rpc, "getTokenSupply", [lp_mint]) or {}).get("value") or {}).get("amount")
        if sup is None or not str(sup).isdecimal() or not ref or int(sup) > ref:
            return False, {**info, "lp_mint": lp_mint, "reason": f"LP lock unknown: lp supply={sup} pool lp counter={ref}"}
        sup = int(sup); burned = ref - sup; incin = 0
        if 100 * burned / ref < need and sup:   # LP sent to the incinerator instead of burned (only then: extra calls)
            largest = (_lp_call(rpc, "getTokenLargestAccounts", [lp_mint]) or {}).get("value") or []
            owners = (_lp_call(rpc, "getMultipleAccounts", [[a["address"] for a in largest], {"encoding": "jsonParsed"}]) or {}).get("value") or []
            for a, o in zip(largest, owners):
                if (((o or {}).get("data") or {}).get("parsed") or {}).get("info", {}).get("owner") == INCINERATOR and str(a.get("amount", "")).isdecimal():
                    incin += int(a["amount"])
        pct = 100 * (burned + min(incin, sup)) / ref
        info.update(kind="raydium_amm_v4" if prog == RAY_AMM_V4 else "raydium_cpmm", lp_mint=lp_mint, lp_supply=sup, lp_counter=ref, incinerator=incin, locked_pct=round(pct, 3))
    else:
        return False, {**info, "reason": f"LP lock unknown: unsupported pool program {prog} (len {len(b)})"}
    eff = need + DAMM_V2_MARGIN_PP if info["kind"] == "damm_v2_permanent_lock" else need   # r2.1: DAMM v2 parse can overestimate (<= 0.33 pp seen)
    info["threshold_pct"] = eff
    if pct < eff: return False, {**info, "status": "below", "reason": f"LP burned/locked {pct:.1f}% < {eff:g}% ({info['kind']})"}
    info["status"] = "ok"; return True, info

def hard_filter(rpc, pool, mint, name, d):
    """Returns (list of (rule, reason) failures, state dict). Missing mandatory data = failure."""
    why, st = [], {}
    liq = (d.get("liquidity") or {}).get("usd"); vol = (d.get("volume") or {}).get("h24")
    st.update(data_sources=d.get("_sources", {}), liquidity=liq, volume_h24=vol, volume_h6=(d.get("volume") or {}).get("h6"), volume_h1=(d.get("volume") or {}).get("h1"), dex=d.get("dexId"),
              price_usd=d.get("priceUsd"), txns=d.get("txns"), pair_created=d.get("pairCreatedAt"), symbol=(d.get("baseToken") or {}).get("symbol"))
    if liq is None: return [("liquidity", "missing liquidity (not on DexScreener nor GeckoTerminal)")], st
    if d.get("dexId") in T.BONDING_CURVE_DEXES: return [("bonding_curve", f"still on bonding curve (dex '{d.get('dexId')}'), not migrated")], st
    if liq < T.MIN_LIQUIDITY_USD: why.append(("liquidity", f"liquidity ${liq:,.0f} < ${T.MIN_LIQUIDITY_USD:,}"))
    st["tier"] = "A"
    if vol is None or vol < T.MIN_VOLUME_24H_USD:
        if vol is not None and vol >= T.TIER_B_MIN_VOLUME_24H_USD and tier_b_allowed(): st["tier"] = "B"
        else: why.append(("volume_24h", f"24h volume ${vol or 0:,.0f} < ${T.MIN_VOLUME_24H_USD:,}"
                          + ("" if tier_b_allowed() else " (tier B off: cluster/sniper filters not implemented)")))
    lp_onchain = d.get("dexId") not in T.LP_BURNED_DEXES and d.get("dexId") in getattr(T, "LP_ONCHAIN_DEXES", ())   # nodata1
    if d.get("dexId") not in T.LP_BURNED_DEXES and not lp_onchain: why.append(("lp_lock", f"LP burn/lock unverifiable on dex '{d.get('dexId')}'"))
    if why: return why, st   # cheap checks first; spare the rate-limited RPC
    try:
        if lp_onchain:   # nodata1 r2: bounded single-shot calls (<= LP_CALL_TIMEOUT_SEC each, per-tick budget); only after cheap checks
            try: ok, lpi = lp_lock_onchain(rpc, pool)
            except LPCheckAbort as e: ok, lpi = False, {"status": "unknown", "reason": f"LP lock check failed: {e}"}   # never a pass
            st["lp_lock"] = lpi
            if not ok: return [("lp_lock", lpi["reason"])], st   # unknown or below threshold -> reject, skip the rest of the RPC
        acc = rpc.call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])["value"]
        info = acc["data"]["parsed"]["info"]
        st.update(decimals=info.get("decimals"), mint_authority=info.get("mintAuthority"), freeze_authority=info.get("freezeAuthority"), program=acc["owner"])
        if info.get("mintAuthority"): why.append(("mint_authority", "mint authority not revoked"))
        if info.get("freezeAuthority"): why.append(("freeze_authority", "freeze authority not revoked"))
        if acc["owner"] == TOKEN2022:
            exts = {e.get("extension") for e in info.get("extensions", [])}
            st["token2022_extensions"] = sorted(x for x in exts if x)
            bad = exts & T.BANNED_TOKEN2022_EXT
            if bad: why.append(("token2022", f"dangerous Token-2022 extensions: {sorted(bad)}"))
        supply = float(rpc.call("getTokenSupply", [mint])["value"]["uiAmount"] or 0)
        largest = rpc.call("getTokenLargestAccounts", [mint])["value"]
        owners = rpc.call("getMultipleAccounts", [[a["address"] for a in largest], {"encoding": "jsonParsed"}])["value"]
        pcts = []
        for a, o in zip(largest, owners):
            own = (((o or {}).get("data") or {}).get("parsed") or {}).get("info", {}).get("owner")
            if own in AMM_AUTHORITIES or own == pool or own == INCINERATOR: continue   # pool vault / burn
            pcts.append(100 * float(a["uiAmount"] or 0) / supply if supply else 100)
        st.update(top_holder_pct=max(pcts or [0]), top10_pct=sum(sorted(pcts, reverse=True)[:10]))
        if st["top_holder_pct"] > T.MAX_SINGLE_WALLET_PCT: why.append(("top_holder", f"wallet holds {st['top_holder_pct']:.1f}% > {T.MAX_SINGLE_WALLET_PCT}%"))
        if st["top10_pct"] > T.MAX_TOP10_PCT: why.append(("top10", f"top10 {st['top10_pct']:.1f}% > {T.MAX_TOP10_PCT}%"))
    except hashlog.HashlogCorrupt: raise
    except Exception as e:
        return why + [("rpc", f"on-chain check failed: {e}")], st
    # copycat: an older Solana token with the same symbol or name and a different mint
    try:
        sym = st["symbol"] or ""; nm = (d.get("baseToken") or {}).get("name", "")
        res = (dexscreener_get("https://api.dexscreener.com/latest/dex/search", params={"q": sym}) or {}).get("pairs") or []
        for p in res:
            b = p.get("baseToken") or {}
            if p.get("chainId") == "solana" and b.get("address") != mint and \
               (b.get("symbol", "").lower() == sym.lower() or b.get("name", "").lower() == nm.lower()) and \
               (p.get("pairCreatedAt") or 9e15) < (d.get("pairCreatedAt") or 0):
                why.append(("copycat", f"duplicates older token {b.get('symbol')} {b.get('address')}")); break
    except hashlog.HashlogCorrupt: raise
    except Exception as e: why.append(("copycat", f"copycat check failed: {e}"))
    return why, st

# ---------------- risk ----------------
def mark_position(pos):
    """Value remaining qty with a Jupiter SELL quote (fill-consistent mark). None = no route.
    r10: inside a monitor pass with MARKS10_PER_MINT, ONE quote per mint for the mint's TOTAL open qty, split pro rata (cached for the pass)."""
    tot = _r10_tot.get(pos["mint"]) if MARKS10_PER_MINT else None
    if tot and tot > pos["qty_left_raw"] > 0:
        if pos["mint"] in _r10_mq:
            res, last = _r10_mq[pos["mint"]]; _jup_last[0] = last; _r10_stats["mint_cache_hits"] += 1
        else:
            q = jup_quote(pos["mint"], USDC, tot); res = (int(q["outAmount"]) / 1e6, float(q["priceImpactPct"]) * 100) if q else None
            if _jup_last[0] and _jup_last[0][0] in ("ok", "no_route"): _r10_mq[pos["mint"]] = (res, _jup_last[0])   # transient failures are not cached
        return (res[0] * pos["qty_left_raw"] / tot, res[1]) if res else None
    q = jup_quote(pos["mint"], USDC, pos["qty_left_raw"]) if pos["qty_left_raw"] > 0 else None
    return (int(q["outAmount"]) / 1e6, float(q["priceImpactPct"]) * 100) if q else None

def open_positions(c, strat=None):
    c.row_factory = sqlite3.Row
    rows = c.execute("SELECT * FROM positions WHERE status='open'" + (" AND strategy=?" if strat else ""), (strat,) if strat else ()).fetchall()
    c.row_factory = None; return [dict(r) for r in rows]

def mark_valued(c, pos, quote="fetch"):
    """Value one open position. Returns dict(usd, impact, status, stale_min).
    ok: live Jupiter sell quote (persisted as last valid mark). stale: no route now, last valid mark <= STALE_MARK_TTL_SEC old
    -> that mark. no_route: no route and (no valid mark ever, or last one older than TTL) -> $0. Real quote always wins."""
    m = mark_position(pos) if quote == "fetch" else quote
    t = now(); pid = pos["id"]
    if quote == "fetch":   # v2: every real quote attempt (ok or failed) is timestamped for the 300 s guarantee
        c.execute("INSERT INTO position_marks(position_id,last_attempt_ts) VALUES(?,?) ON CONFLICT(position_id) DO UPDATE SET last_attempt_ts=excluded.last_attempt_ts", (pid, t.isoformat()))
    row = c.execute("SELECT last_ok_ts,last_ok_usd,fail_since_ts,last_fail_logged_ts,fails,last_ok_qty FROM position_marks WHERE position_id=?", (pid,)).fetchone()
    if m:
        c.execute("""INSERT INTO position_marks(position_id,last_ok_ts,last_ok_usd,last_ok_impact,last_ok_qty,fail_since_ts,last_fail_logged_ts,fails)
            VALUES(?,?,?,?,?,NULL,NULL,0) ON CONFLICT(position_id) DO UPDATE SET last_ok_ts=excluded.last_ok_ts,
            last_ok_usd=excluded.last_ok_usd, last_ok_impact=excluded.last_ok_impact, last_ok_qty=excluded.last_ok_qty,
            fail_since_ts=NULL, fails=0, last_fail_kind=NULL, last_ttl_alert_ts=NULL""", (pid, t.isoformat(), m[0], m[1], pos["qty_left_raw"]))
        return {"usd": m[0], "impact": m[1], "status": "ok", "stale_min": 0.0, "qty": pos["qty_left_raw"]}
    last_ok_ts, last_ok_usd, fail_since, last_logged, fails, last_qty = row or (None, None, None, None, 0, None)
    if last_ok_usd is not None and last_qty: last_ok_usd = last_ok_usd * pos["qty_left_raw"] / last_qty   # after partial exits
    age = (t - dt.datetime.fromisoformat(last_ok_ts)).total_seconds() if last_ok_ts else None
    # r3 diag: the failure kind decides the mark. Only a true no_route (or a pre-computed None quote, legacy) uses the TTL/$0 rule and
    # starts the no-route clock (fail_since_ts). rate_limited/timeout/server_error/quote_error -> status 'quote_error': last good mark
    # kept (flagged, any age; $0 only if never marked), fail_since_ts NOT set, so quote_plan/monitor_backlog/noroute don't see a no-route.
    kind = ((_jup_last[0] or ("quote_error",))[0] if quote == "fetch" else "no_route")
    if kind == "ok": kind = "quote_error"   # defensive: no mark but diag says ok
    transient = kind in TRANSIENT_QUOTE_FAILURES
    expired = False
    if transient:   # r4: same STALE_MARK_TTL_SEC as no_route: past TTL (or never marked) -> $0, still quote_error + flag, no clock
        status = "quote_error"; expired = age is None or age > T.STALE_MARK_TTL_SEC
        usd = 0.0 if expired else last_ok_usd
    else:
        status = "stale" if age is not None and age <= T.STALE_MARK_TTL_SEC else "no_route"
        usd = last_ok_usd if status == "stale" else 0.0
    log_it = (fail_since is None and not transient) or last_logged is None or \
        (t - dt.datetime.fromisoformat(last_logged)).total_seconds() >= T.MARK_FAILED_LOG_EVERY_SEC
    c.execute("""INSERT INTO position_marks(position_id,fail_since_ts,last_fail_logged_ts,fails,last_fail_kind) VALUES(?,?,?,1,?)
        ON CONFLICT(position_id) DO UPDATE SET fail_since_ts=COALESCE(fail_since_ts, excluded.fail_since_ts),
        last_fail_logged_ts=?, fails=fails+1, last_fail_kind=excluded.last_fail_kind""",
        (pid, None if transient else t.isoformat(), t.isoformat() if log_it else None, kind, t.isoformat() if log_it else last_logged))
    if log_it:
        hashlog.append("mark_failed", position_id=pid, strategy=pos["strategy"], lane=pos.get("lane"), ts_fail=t.isoformat(),
                       fail_since=fail_since or t.isoformat(), consecutive_fails=(fails or 0) + 1, mark_status=status, valued_usd=usd,
                       last_valid_mark_ts=last_ok_ts, last_valid_mark_age_sec=round(age, 1) if age is not None else None,
                       quote_result=(list(_jup_last[0]) if (quote == "fetch" and _jup_last[0]) else None),   # r2: kind, http_status, error
                       fail_kind=kind, no_route_clock=not transient)   # r3
    if transient and expired:   # r4: chain alert on crossing the TTL, then every MARK_FAILED_LOG_EVERY_SEC (own per-position timestamp)
        la = c.execute("SELECT last_ttl_alert_ts FROM position_marks WHERE position_id=?", (pid,)).fetchone()[0]
        if la is None or (t - dt.datetime.fromisoformat(la)).total_seconds() >= T.MARK_FAILED_LOG_EVERY_SEC:
            c.execute("UPDATE position_marks SET last_ttl_alert_ts=? WHERE position_id=?", (t.isoformat(), pid))
            hashlog.append("mark_quote_error_ttl_expired", position_id=pid, strategy=pos["strategy"], lane=pos.get("lane"), ts=t.isoformat(),
                           fail_kind=kind, valued_usd=0.0, last_valid_mark_ts=last_ok_ts,
                           last_valid_mark_age_sec=round(age, 1) if age is not None else None, ttl_sec=T.STALE_MARK_TTL_SEC, no_route_clock=False)
    return {"usd": usd, "impact": None, "status": status, "stale_min": round(age / 60, 1) if age is not None else None, "qty": pos["qty_left_raw"],
            "fail_kind": kind, "flagged": transient, "ttl_expired": expired}

def mark_cached(c, pos):
    """week1c: value from the persisted last valid mark, NO quote and no failure bookkeeping. Same rule as mark_valued:
    a mark <= STALE_MARK_TTL_SEC old (scaled by qty left) counts; older or never marked -> $0 (no_route). Never cost."""
    row = c.execute("SELECT last_ok_ts,last_ok_usd,last_ok_qty,fail_since_ts,last_fail_kind FROM position_marks WHERE position_id=?", (pos["id"],)).fetchone()
    qe = bool(row and row[3] is None and row[4] in TRANSIENT_QUOTE_FAILURES)   # r3: last failure transient, no no-route clock
    if not row or row[0] is None or row[1] is None:
        return {"usd": 0.0, "impact": None, "status": "quote_error" if qe else "no_route", "stale_min": None, "qty": pos["qty_left_raw"], "cached": True,
                **({"flagged": True, "ttl_expired": True} if qe else {})}   # r5: never marked = past TTL
    age = (now() - dt.datetime.fromisoformat(row[0])).total_seconds()
    usd = row[1] * pos["qty_left_raw"] / row[2] if row[2] else row[1]
    if qe:   # r4: TTL applies to transient failures too: past STALE_MARK_TTL_SEC -> $0 (status quote_error, flagged, no clock)
        exp = age > T.STALE_MARK_TTL_SEC
        return {"usd": 0.0 if exp else usd, "impact": None, "status": "quote_error", "stale_min": round(age / 60, 1), "qty": pos["qty_left_raw"],
                "cached": True, "flagged": True, "ttl_expired": exp}
    if age > T.STALE_MARK_TTL_SEC: return {"usd": 0.0, "impact": None, "status": "no_route", "stale_min": round(age / 60, 1), "qty": pos["qty_left_raw"], "cached": True}
    return {"usd": usd, "impact": None, "status": "stale" if row[3] else "ok", "stale_min": round(age / 60, 1), "qty": pos["qty_left_raw"], "cached": True}

def cash(c, strat):
    """Free paper cash of a strategy: bankroll + sells net of fees - buys incl. entry fees (open positions' cost is spent)."""
    return T.PAPER_BANKROLL_USD + c.execute("""SELECT COALESCE(SUM(f.usd - f.fee_usd),0) FROM fills f JOIN positions p ON p.id=f.position_id
        WHERE p.strategy=? AND f.side='sell'""", (strat,)).fetchone()[0] - \
        c.execute("SELECT COALESCE(SUM(cost_usd + entry_fee_usd),0) FROM positions WHERE strategy=?", (strat,)).fetchone()[0]

def equity(c, strat, marks=None, info=None):
    """marks: {position_id: mark_valued() dict} already computed this cycle. week1c: equity() NEVER quotes; positions
    missing from marks (or whose qty changed) are valued with mark_cached() (persisted mark, TTL rule, else $0).
    info (dict, optional) receives marks_missing (stale + no_route) and marks_stale. Risk caps use this equity."""
    realized = c.execute("""SELECT COALESCE(SUM(f.usd - f.fee_usd),0) FROM fills f JOIN positions p ON p.id=f.position_id
        WHERE p.strategy=? AND f.side='sell'""", (strat,)).fetchone()[0] - \
        c.execute("SELECT COALESCE(SUM(cost_usd + entry_fee_usd),0) FROM positions WHERE strategy=?", (strat,)).fetchone()[0]
    unreal, missing, stale = 0.0, 0, 0
    for p in open_positions(c, strat):
        m = (marks or {}).get(p["id"])
        if not m or m.get("qty") != p["qty_left_raw"]: m = mark_cached(c, p)   # no second quote (auditor); TTL rule applies
        unreal += m["usd"]; missing += m["status"] != "ok"
        stale += m["status"] == "stale" or (m["status"] == "quote_error" and m.get("ttl_expired") is False)   # r5: within-TTL quote_error = stale (as base)
    if info is not None: info.update(marks_missing=missing, marks_stale=stale)
    # realized counts all cost incl. open positions; unreal adds back their current value
    return realized, unreal, T.PAPER_BANKROLL_USD + realized + unreal

# marks1fix r2 (aud44/aud45): risk valuation for the daily cap AND the drawdown (incl. close_all, which follows a HALT_ written here).
# Per open leg: reliable mark (<= STALE_MARK_TTL_SEC, base rule) -> that value. Transient quote failure (no no-route clock: fail_since_ts
# NULL, e.g. rate_limited/timeout/backoff/skipped:mint_429) with last good mark <= RISK_STALE_MAX_SEC old -> last good mark, counted
# UNRELIABLE. Truly dead (no-route clock), never marked, or last good > RISK_STALE_MAX_SEC -> $0, counted KNOWN (not unreliable).
# risk_eq (an optimistic bound) is ALWAYS evaluated by risk_halt: a daily/drawdown trip still trips (latch, HALT_, close_all).
# Only if nothing trips and unreliable legs > MARKS_UNRELIABLE_FRAC of open (or all) -> 'marks unreliable': entries blocked,
# no latch, no HALT_; event via _defer['real'] (survives a monitor rollback). Equity rows store risk_eq (day_start/peak from it).
RISK_STALE_MAX_SEC = 1800.0
MARKS_UNRELIABLE_FRAC = 0.30
def risk_equity(c, strat, marks=None):
    """-> (eq_for_risk, add_vs_display_equity, n_open, n_unreliable)."""
    r, u, eq = equity(c, strat, marks)
    n = bad = 0; add = 0.0
    for p in open_positions(c, strat):
        n += 1
        m = (marks or {}).get(p["id"])
        if not m or m.get("qty") != p["qty_left_raw"]: m = mark_cached(c, p)
        if m["status"] == "ok" or (m["usd"] > 0 and (m.get("stale_min") or 0) * 60 <= T.STALE_MARK_TTL_SEC): continue   # reliable
        row = c.execute("SELECT last_ok_ts,last_ok_usd,last_ok_qty,fail_since_ts FROM position_marks WHERE position_id=?", (p["id"],)).fetchone()
        if not row or row[0] is None or row[1] is None or row[3] is not None: continue   # never marked / truly dead -> base value ($0), known
        age = (now() - dt.datetime.fromisoformat(row[0])).total_seconds()
        if age > RISK_STALE_MAX_SEC: continue   # too old -> base value ($0), known
        bad += 1
        if m["usd"] <= 0: add += row[1] * p["qty_left_raw"] / row[2] if row[2] else row[1]
    return eq + add, add, n, bad
_unreliable = {}   # strat -> {"key": (bad, n), "since": epoch}
_last_risk_add = {}
def _unrel_log(kind, **kw): (_defer["real"] or hashlog.append)(kind, **kw)   # r2: bypass monitor deferral like _qs_log
def risk_gate(c, strat, marks=None, write_halt=False):
    """marks1fix r2: risk_halt(risk_eq) always; if it does not trip and marks are unreliable -> 'marks unreliable' (entries only)."""
    eq, add, n, bad = risk_equity(c, strat, marks); _last_risk_add[strat] = add
    halt = risk_halt(c, strat, eq, write_halt)
    unreliable = n > 0 and bad > 0 and (bad == n or bad > MARKS_UNRELIABLE_FRAC * n)
    if not unreliable:
        if strat in _unreliable: _unrel_log("marks_unreliable_end", strategy=strat, n_open=n, n_unreliable=bad, risk_equity=round(eq, 4),
                                            duration_sec=round(time.time() - _unreliable.pop(strat)["since"], 1))
        return halt
    st = _unreliable.setdefault(strat, {"key": None, "since": time.time()})
    dur = time.time() - st["since"]
    if st["key"] != (bad, n):
        st["key"] = (bad, n)
        _unrel_log("marks_unreliable", strategy=strat, n_open=n, n_unreliable=bad, risk_equity=round(eq, 4), duration_sec=round(dur, 1),
                   tripped=halt, action="block_entries")
    return halt or f"marks unreliable ({bad}/{n} open legs without a reliable mark; entries blocked, no latch)"

def risk_halt(c, strat, eq, write_halt=False):
    """week1e: only monitor() (fresh quotes) passes write_halt=True; cached-mark callers only block entries."""
    if os.path.exists(T.KILL_SWITCH_FILE): return "kill switch"
    if os.path.exists(os.path.join(HERE, f"HALT_{strat}")): return "drawdown halt (manual restart: delete HALT file)"
    rows = c.execute("SELECT ts, equity FROM equity WHERE strategy=?", (strat,)).fetchall()
    peak = max([e for _, e in rows] + [T.PAPER_BANKROLL_USD, eq])
    if eq <= peak * (1 - T.MAX_DRAWDOWN_PCT / 100):
        if write_halt: open(os.path.join(HERE, f"HALT_{strat}"), "w").write(now().isoformat())
        return f"drawdown cap {T.MAX_DRAWDOWN_PCT:g}% hit"
    today = dt.date.today().isoformat()   # local (America/Mexico_City) day
    if _daily_cap_suspended(strat, today): return None   # suspend1: ONLY the daily cap is skipped (kill/drawdown checked above); no latch
    f, latched = _daily_halt_latch(strat, today)   # dailycap1: a daily trip stays until the local day changes
    if latched: return latched
    start = next((e for t, e in rows if dt.datetime.fromisoformat(t).astimezone().date().isoformat() == today), None)
    if start is None:
        prev = [e for _, e in rows]; start = prev[-1] if prev else T.PAPER_BANKROLL_USD
    if eq <= start * (1 - T.DAILY_LOSS_CAP_PCT / 100): return _daily_trip(strat, today, f, "daily loss cap 5% hit (halt until tomorrow)", eq, start, write_halt)
    return None

# dailycap1 r2: daily-loss latch. Disk: DAILY_HALT_<strat>_<YYYYMMDD> (survives restarts); memory: _daily_latch[(strat, day)]
# (holds even if the disk write fails). Only write_halt=True callers (monitor, fresh quotes) latch. Precedence: kill switch >
# drawdown HALT/cap (checked before the latch, can still escalate) > latched daily trip (first reason of the day, verbatim).
_daily_latch = {}
_daily_suspend_logged = set()
def _daily_cap_suspended(strat, today):
    """suspend1: DAILY_CAP_SUSPEND_<strat>_<YYYYMMDD> for TODAY's local date -> daily cap off for that strategy today. Other dates ignored.
    Logs daily_cap_suspended once per strategy+day (per process)."""
    if not os.path.exists(os.path.join(HERE, f"DAILY_CAP_SUSPEND_{strat}_{today.replace('-', '')}")): return False
    if (strat, today) not in _daily_suspend_logged:
        _daily_suspend_logged.add((strat, today)); hashlog.append("daily_cap_suspended", strategy=strat, day=today)
    return True
_daily_err_logged = set()
def _daily_err(kind, key, **kw):
    if (kind, key) in _daily_err_logged: return
    _daily_err_logged.add((kind, key)); hashlog.append(kind, **kw)
def _daily_halt_latch(strat, today):
    """-> (file, latched reason or None). Earlier days' files are renamed .released + daily_halt_unlock (rename failure logged, continue)."""
    import glob
    f = os.path.join(HERE, f"DAILY_HALT_{strat}_{today.replace('-', '')}")
    for k in [k for k in _daily_latch if k[0] == strat and k[1] != today]:
        _daily_latch.pop(k); hashlog.append("daily_halt_unlock", strategy=strat, day=today, released_day=k[1], source="memory")
    for old in glob.glob(os.path.join(HERE, f"DAILY_HALT_{strat}_[0-9]*")):
        if old == f or old.endswith(".released") or old.endswith(".tmp"): continue
        try:
            os.replace(old, old + ".released")
            hashlog.append("daily_halt_unlock", strategy=strat, day=today, released=os.path.basename(old), source="file")
        except OSError as e:
            _daily_err("daily_halt_unlock_error", old, strategy=strat, day=today, file=os.path.basename(old), error=f"{type(e).__name__}: {e}")
    if (strat, today) in _daily_latch: return f, _daily_latch[(strat, today)]
    if os.path.exists(f):
        try: r = json.loads(open(f).read()).get("reason")
        except (OSError, ValueError, AttributeError): r = None
        _daily_latch[(strat, today)] = r or "daily loss cap hit (halt until tomorrow)"
        return f, _daily_latch[(strat, today)]
    return f, None
def _daily_trip(strat, today, f, reason, eq, start, write_halt):
    if not write_halt or (strat, today) in _daily_latch: return reason
    _daily_latch[(strat, today)] = reason   # memory first: the halt holds even if the file write below fails
    try:
        tmp = f + ".tmp"
        with open(tmp, "w") as fh: fh.write(json.dumps({"ts": now().isoformat(), "reason": reason, "equity": round(eq, 4), "day_start": round(start, 4)}))
        os.replace(tmp, f)
    except OSError as e:
        _daily_err("daily_halt_write_error", f, strategy=strat, day=today, file=os.path.basename(f), error=f"{type(e).__name__}: {e}")
    hashlog.append("daily_halt_trip", strategy=strat, day=today, reason=reason, equity=round(eq, 4), day_start=round(start, 4))
    return reason

# ---------------- entries ----------------
def try_entry(c, strat, cand, ticket, decision_extra):
    _jup_purpose[0] = "entry"
    pyr = _pyramid.pop((strat, cand["mint"]), None)
    if pyr: decision_extra = {**decision_extra, "pyramid": pyr}
    last = c.execute("SELECT COALESCE(MAX(id),0) FROM positions").fetchone()[0]
    try: return _try_entry(c, strat, cand, ticket, decision_extra)
    finally:
        _jup_purpose[0] = "exit"
        new = c.execute("SELECT MAX(id) FROM positions").fetchone()[0]
        if pyr and new and new > last: c.execute("UPDATE positions SET pyramid_of=? WHERE id=?", (pyr["base_position_id"], new)); c.commit()
        if strat == "simple" and new and new > last and c.execute("SELECT strategy FROM positions WHERE id=?", (new,)).fetchone()[0] == "simple":
            _pair_entry(c, new, cand, pyr)   # exits1 r2 (B2): simple_tp mirrors this exact fill (never raises)

def _try_entry(c, strat, cand, ticket, decision_extra):
    mint, pool = cand["mint"], cand["pool"]
    lane = cand["lane"]; assert lane in ("meme", "liquid")
    q = jup_quote(USDC, mint, ticket * 1e6)
    if q is None:
        time.sleep(T.QUOTE_RETRY_DELAY_SEC); q = jup_quote(USDC, mint, ticket * 1e6)
    if q is None: return reject(c, strat, pool, mint, cand["name"], "no_route", "no Jupiter route (after 1 retry)", cand["state"])
    impact = float(q["priceImpactPct"]) * 100
    if impact > T.MAX_SLIPPAGE_PCT[lane]:
        return reject(c, strat, pool, mint, cand["name"], "slippage", f"quote impact {impact:.2f}% > {T.MAX_SLIPPAGE_PCT[lane]}% ({lane})", cand["state"])
    fee = T.PRIORITY_FEE_USD + T.NETWORK_FEE_USD
    h = hashlog.append("decision", strategy=strat, mint=mint, pool=pool, action="enter", lane=lane, rule=decision_extra.pop("rule"),
                       size_usd=ticket, quote={k: q[k] for k in ("inAmount", "outAmount", "priceImpactPct")},
                       state=cand["state"], thresholds_sha=thresholds_sha(), **decision_extra)
    cur = c.execute("""INSERT INTO positions(strategy,mint,pool,symbol,opened_ts,qty_raw,qty_left_raw,cost_usd,entry_fee_usd,entry_impact_pct,peak_mark,decision_hash,lane,decimals)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (strat, mint, pool, cand["state"].get("symbol"), now().isoformat(), int(q["outAmount"]),
        int(q["outAmount"]), ticket, fee, impact, ticket / int(q["outAmount"]), h, lane, cand["state"].get("decimals")))
    c.execute("INSERT INTO fills(position_id,ts,side,qty_raw,usd,price_impact_pct,fee_usd,reason,lane) VALUES(?,?,?,?,?,?,?,?,?)",
              (cur.lastrowid, now().isoformat(), "buy", int(q["outAmount"]), ticket, impact, fee, "entry", lane))
    print(f"  PAPER ENTRY [{strat}] #{cur.lastrowid} {cand['name']} ${ticket:.2f} impact {impact:.2f}%")

def small_ticket_reject(c, strat, cand, sb):
    """export1: final ticket < $0.01. 'bank_cap_mint' ONLY when the per-mint 6% cap is the binding constraint (it reduced the
    bank amount); any other sub-cent ticket (liq/risk/cash/exposure caps) -> 'ticket_below_min'. reject() clears _pyramid."""
    if sb.get("mint_cap_binding"):
        reject(c, strat, cand["pool"], cand["mint"], cand["name"], "bank_cap_mint", f"open legs ${sb['mint_open_cost_usd']:.2f} vs {T.MAX_TICKET_PCT_BANK}% of equity: ticket ${sb['final_ticket']:.2f}", cand["state"])
    else:
        reject(c, strat, cand["pool"], cand["mint"], cand["name"], "ticket_below_min", f"final ticket ${sb['final_ticket']:.2f} < $0.01", cand["state"])
    _pyramid.pop((strat, cand["mint"]), None)

def mint_open_cost(c, strat, mint):
    """export1: remaining cost basis of this strategy's open legs on one mint."""
    return sum(p["cost_usd"] * p["qty_left_raw"] / p["qty_raw"] for p in open_positions(c, strat) if p["qty_raw"] and p["mint"] == mint)

def open_cost(c, strat):
    """week1d: remaining cost basis of open positions (cost_usd scaled by qty_left/qty; entry fees excluded)."""
    return sum(p["cost_usd"] * p["qty_left_raw"] / p["qty_raw"] for p in open_positions(c, strat) if p["qty_raw"])

_pyramid = {}   # (strategy, mint) -> add-on info for the next try_entry
REREADD_FLOOR_SCOPE = "readd_only"   # reentry1 r3: 3% gain floor on "all" add-ons or only "readd_only" (base already had an add-on)

def can_open(c, strat, mint):
    op = open_positions(c, strat)
    if T.MAX_OPEN_POSITIONS is not None and len(op) >= T.MAX_OPEN_POSITIONS: return f"max {T.MAX_OPEN_POSITIONS} concurrent positions"
    _pyramid.pop((strat, mint), None)
    held = [p for p in op if p["mint"] == mint]
    if len(held) >= T.MAX_POSITIONS_PER_TOKEN:
        if not T.PYRAMID_ENABLED or len(held) >= T.MAX_ENTRIES_PER_MINT: return f"already holding this token ({len(held)} open legs, max {T.MAX_ENTRIES_PER_MINT if T.PYRAMID_ENABLED else T.MAX_POSITIONS_PER_TOKEN})"
        # week1f: the gain is measured on the BASE leg of the group, found via pyramid_of (never by list order)
        bases = [q for q in held if not q.get("pyramid_of")]
        if any(q.get("pyramid_of") for q in held) or len(bases) != 1:
            return "already holding this token; add-on already used for this open position"   # an add-on leg is open
        p = bases[0]
        # reentry1 r2: a CLOSED add-on frees the slot, capped: <=2 add-ons per base per rolling 24h, 30 min cooldown after one closes
        t0 = now(); _ad = c.execute("SELECT opened_ts, closed_ts FROM positions WHERE pyramid_of=?", (p["id"],)).fetchall()
        try:
            if sum(1 for o, _ in _ad if o and (t0 - dt.datetime.fromisoformat(o)).total_seconds() < 86400) >= 2:
                return "already holding this token; re-add cap (2 add-ons per base per 24h)"
            if any(cl and (t0 - dt.datetime.fromisoformat(cl)).total_seconds() < 1800 for _, cl in _ad):
                return "already holding this token; re-add cooldown (30 min after add-on close)"
        except Exception as e:   # r3: malformed add-on timestamp -> block only this mint, never abort the cycle
            print(f"reentry1: bad add-on timestamp for base {p['id']} ({type(e).__name__}); blocking {mint}")
            return "already holding this token; re-add blocked (malformed add-on timestamp)"
        _jup_purpose[0] = "entry"
        try: mv = mark_valued(c, p)   # week1e pyramid: FRESH real sell quote of the open position
        finally: _jup_purpose[0] = "exit"
        if mv["status"] != "ok": return "already holding this token; pyramid add needs a fresh mark (no route)"
        chg = 100 * ((mv["usd"] / p["qty_left_raw"]) / (p["cost_usd"] / p["qty_raw"]) - 1)
        _mg = max(T.PYRAMID_MIN_GAIN_PCT, 3.0) if (REREADD_FLOOR_SCOPE != "readd_only" or _ad) else T.PYRAMID_MIN_GAIN_PCT   # reentry1 r3.1: unknown scope fails closed (=all)
        if chg <= _mg: return f"already holding this token; pyramid add needs profit at fresh mark ({chg:.2f}% <= {_mg}%)"
        _pyramid[(strat, mint)] = {"add_on": 2, "base_position_id": p["id"], "fresh_mark_usd": round(mv["usd"], 6), "fresh_mark_chg_pct": round(chg, 3)}
    return None

def size_breakdown(liq, eq, rf, base_pct=None, tier="A", cash_avail=None, open_cost_usd=None, mint_open_cost_usd=None):
    """Every sizing input + the final ticket (logged on each entry). Caps unchanged: 6% bank, 2% pool liquidity.
    week1c: eq = TOTAL strategy equity (cash + open positions at mark_valued/mark_cached value; no_route -> $0, never cost).
    cash_avail: free cash; the final ticket + entry fees never exceed it (cash can't go negative)."""
    base_pct = base_pct or T.FIXED_TICKET_PCT_BANK   # Kelly disabled until auditor verifies calibration
    pre = min(eq * base_pct / 100, eq * T.MAX_TICKET_PCT_BANK / 100)
    mint_cap = None   # export1: 6% bank cap is per POSITION = sum of the token's open legs (base + add-ons), not per trade
    if mint_open_cost_usd is not None:
        mint_cap = max(0.0, eq * T.MAX_TICKET_PCT_BANK / 100 - mint_open_cost_usd); pre = min(pre, mint_cap)
    # export1 NOTE: the per-mint 6% cap is applied BEFORE FACTOR_NO_X (and tier/liq/risk/cash/exposure), so the real ticket is
    # at most FACTOR_NO_X * (6% equity - open cost of the mint), e.g. 0.6 * (6% eq - mint open cost); the mint never fills its 6%.
    mint_bind = mint_cap is not None and mint_cap < min(eq * base_pct / 100, eq * T.MAX_TICKET_PCT_BANK / 100)
    t = pre * T.FACTOR_NO_X   # X account not collected yet
    tier_f = 1.0
    if tier == "B":
        if not tier_b_allowed(): raise RuntimeError("tier B candidate while tier B gate is closed")
        tier_f = T.TIER_B_SIZE_FACTOR; t *= tier_f
    liq_cap = liq * T.MAX_TICKET_PCT_LIQ / 100
    pre_rf = round(min(t, liq_cap), 2)
    fee = T.PRIORITY_FEE_USD + T.NETWORK_FEE_USD
    final = round(pre_rf * rf, 2); cash_cap = None
    if cash_avail is not None:
        cash_cap = max(0.0, int((cash_avail - fee) * 100) / 100)
        if final > cash_cap: final = cash_cap
    exp_cap = exp_pct = None; exp_bind = False   # week1d: open cost + ticket <= MAX_EXPOSURE_PCT x total equity
    if open_cost_usd is not None:
        exp_cap = max(0.0, int((eq * T.MAX_EXPOSURE_PCT - open_cost_usd) * 100) / 100)
        exp_bind = final > exp_cap
        if exp_bind: final = exp_cap
        exp_pct = round((open_cost_usd + final) / eq, 4) if eq > 0 else None
    return {"exposure_pct": exp_pct, "exposure_open_cost_usd": None if open_cost_usd is None else round(open_cost_usd, 4),
            "max_exposure_pct": T.MAX_EXPOSURE_PCT, "exposure_cap_usd": exp_cap, "exposure_cap_binding": exp_bind,
            "equity": round(eq, 4), "size_base_equity": round(eq, 4), "size_base": "total_equity", "cash_available": None if cash_avail is None else round(cash_avail, 4),
            "cash_cap_usd": cash_cap, "cash_cap_binding": cash_cap is not None and round(pre_rf * rf, 2) > cash_cap, "base_pct": base_pct, "max_ticket_pct_bank": T.MAX_TICKET_PCT_BANK, "mint_open_cost_usd": None if mint_open_cost_usd is None else round(mint_open_cost_usd, 4), "mint_bank_cap_usd": None if mint_cap is None else round(mint_cap, 4), "mint_cap_binding": mint_bind, "bank_amount": round(pre, 4),
            "factor_no_x": T.FACTOR_NO_X, "tier": tier, "tier_factor": tier_f, "liq_usd": liq, "liq_cap_pct": T.MAX_TICKET_PCT_LIQ,
            "liq_cap_usd": round(liq_cap, 4), "liq_cap_binding": liq_cap < t, "pre_risk_ticket": pre_rf, "risk_factor": rf,
            "final_ticket": final}

def ticket_size(liq, eq, base_pct=None, tier="A"):
    return size_breakdown(liq, eq, 1.0, base_pct, tier)["pre_risk_ticket"]

def base_rate_stats(c):
    """Win rate / avg win / avg loss of closed SIMPLE-rule trades; defaults until 20 trades."""
    rows = c.execute("""SELECT p.cost_usd + p.entry_fee_usd, SUM(f.usd - f.fee_usd) FROM positions p JOIN fills f
        ON f.position_id=p.id AND f.side='sell' WHERE p.strategy='simple' AND p.status='closed' GROUP BY p.id""").fetchall()
    if len(rows) < T.MIN_TRADES_FOR_STATS: return T.EDGE_BASE_RATE_DEFAULT, T.EDGE_AVG_WIN, T.EDGE_AVG_LOSS, len(rows)
    r = [o / cost - 1 for cost, o in rows]; w = [x for x in r if x > 0]; l = [-x for x in r if x <= 0]
    return len(w) / len(r), (sum(w) / len(w) if w else T.EDGE_AVG_WIN), (sum(l) / len(l) if l else T.EDGE_AVG_LOSS), len(r)

def roundtrip_cost(mint, ticket):
    _jup_purpose[0] = "entry"
    try: return _roundtrip_cost(mint, ticket)
    finally: _jup_purpose[0] = "exit"

def _roundtrip_cost(mint, ticket):
    """costs = 1 - (USDC back from selling what the buy quote gives) / ticket + fees, from live Jupiter quotes."""
    b = jup_quote(USDC, mint, ticket * 1e6)
    if not b: return None
    s = jup_quote(mint, USDC, int(b["outAmount"]))
    if not s: return None
    fees = 2 * (T.PRIORITY_FEE_USD + T.NETWORK_FEE_USD)
    return 1 - int(s["outAmount"]) / 1e6 / ticket + fees / ticket

def edge_est(p_exhausted, base_rate, avg_win, avg_loss, costs):
    p = 0.5 * (1 - p_exhausted) + 0.5 * base_rate
    return p * avg_win - (1 - p) * avg_loss - costs, p


# ---------------- q4b: organic traction (replaces narrative Q4 while there is no X/narrative data) ----------------
# Narrative Q4 (v0) is INACTIVE: its state fields are built (null, no source) and kept in the record, never asked, never a filter.
# Q4b is a distinct question with its own id/version; thresholds unchanged (JEV_Q4_MIN_SCORE / JEV_Q4_MIN_CONF).
# Q4B_PHASE1 selects the package variant (one line; activate.py --variant checks the staged sha of each variant):
#   True  -> Q4b_organic_traction v2p1 = base fields + narrative phase 1 (holders_count / holder_growth_h24_pct via Helius
#            at decision time, n_similar_tokens_24h by a deterministic rule). Separable layer: all phase-1 code is below.
#   False -> Q4b_organic_traction v1.1 = base fields only (fallback if phase 1 is not cleared by 2026-11-05).
# X fields (mentions_x_24h, mention_growth_pct; narrative phase 2) NEVER reach Jev: they are not in any Q4b field list.
Q4B_PHASE1 = True
Q4_NARRATIVE_ID, Q4_NARRATIVE_VERSION = "Q4_narrative_strength", "v0"
Q4_NARRATIVE_FIELDS = ("narrative_tag", "n_similar_tokens_24h", "mentions_x_24h", "mention_growth_pct", "volume_h24", "holders_count", "holder_growth_h24_pct")
Q4_NARRATIVE_RUBRIC = ("1-2 sin narrativa / ruido; 3-4 tema debil o saturado; 5-6 tema activo sin traccion propia; "
                       "7-8 tema en alza con traccion medible; 9-10 narrativa dominante del dia con crecimiento fuerte")
Q4B_ID = "Q4b_organic_traction"
Q4B_BASE_FIELDS = ("buys_h1", "sells_h1", "buys_h6", "sells_h6", "buys_h24", "sells_h24", "buy_sell_ratio_h1", "buy_sell_ratio_h6",
                   "buy_sell_ratio_h24", "volume_h1", "volume_h6", "volume_h24", "vol_ratio_h1", "vol_ratio_h6", "token_age_h",
                   "top10_pct")   # nodata1/v2p2: top10_change_h24_pp removed until 24 h of own snapshots (Q4B_HISTORY_FIELDS hook)
Q4B_PHASE1_FIELDS = ("holders_count", "holders_count_is_lower_bound", "n_similar_tokens_24h")   # v2p2: holder_growth_h24_pct removed
# HOOK (future version, NOT active): history fields re-added only by a new Q4b version once >= 24 h of own snapshots exist.
Q4B_HISTORY_FIELDS = ()   # e.g. ("top10_change_h24_pp", "holder_growth_h24_pct") in a later version bump
Q4B_RUBRIC_BASE = ("1-2: sin traccion organica (pocas compras, ventas dominan o volumen residual); "
                   "3-4: traccion debil o decayendo (buy/sell < 1 o volumen h1 muy por debajo de la media h24); "
                   "5-6: traccion estable sin aceleracion (buy/sell ~1, volumen sostenido); "
                   "7-8: traccion creciente y medible (compras > ventas en h1 y h6, volumen h1/h6 por encima de la media h24); "
                   "9-10: traccion fuerte y amplia (compras claramente dominantes en todas las ventanas, volumen acelerando). "
                   "Criterios primarios: compras vs ventas y volumen por ventana. top10_pct solo como contexto. ")
Q4B_RUBRIC_PHASE1 = ("Contexto adicional (no criterio primario): holders_count, si hay dato, puede subir o bajar un punto; "
                     "holders_count_is_lower_bound: lower_bound=true significa al menos 5000; "
                     "n_similar_tokens_24h alto = tema saturado (copias), no suma traccion. ")
sha256s = lambda x: hashlib.sha256(x.encode()).hexdigest()
def _q4b_variant(P):
    """All variant-dependent Q4b constants (P = phase 1 on). Same text as before r4, so prompt/rubric hashes are unchanged."""
    Q4B_VERSION = "v2p2" if P else "v1.2"   # nodata1: field lists changed -> new versions (v2p1 / v1.1 not reused)
    Q4B_FIELDS = Q4B_BASE_FIELDS + (Q4B_PHASE1_FIELDS if P else ()) + Q4B_HISTORY_FIELDS
    Q4B_STRUCTURAL_NULL = ()   # v2p2: no history fields are sent -> no structural nulls
    Q4B_RUBRIC = (Q4B_RUBRIC_BASE + (Q4B_RUBRIC_PHASE1 if P else "") + "null = sin dato: no lo supongas.")
    Q4B_PROMPT = (f"{Q4B_ID}_{Q4B_VERSION}. Con SOLO los datos del state (calculados en codigo; null = sin dato), puntua de 1 a 10 la "
                  "traccion organica del token: compras vs ventas y volumen por ventana. Rubrica: " + Q4B_RUBRIC)
    Q4B_SOURCES = {"buys_*/sells_*/buy_sell_ratio_*": "DexScreener pair txns (counts of swaps, not unique wallets)",
                   "volume_h1/h6/h24, vol_ratio_*": "DexScreener pair volume (h24/h6 may fall back to GeckoTerminal)",
                   "token_age_h": "pairCreatedAt (DexScreener, GeckoTerminal fallback)",
                   "top10_pct": "Solana RPC getTokenLargestAccounts at scan time"}
    if P: Q4B_SOURCES.update({
                   "holders_count": "Helius DAS getTokenAccounts at decision time (owners with amount>0), own rate limiter, free plan; fail -> null; >= 5000 accounts -> 5000",
                   "holders_count_is_lower_bound": "true = holders_count is a lower bound (>= 5000, 5-page budget); null when holders_count is null",
                   "n_similar_tokens_24h": "own scans table, rule SIMILAR_RULE_VERSION (deterministic, no LLM), ts <= decision ts"})
    Q4B_PROMPT_SHA, Q4B_RUBRIC_SHA = sha256s(Q4B_PROMPT), sha256s(Q4B_RUBRIC)
    return dict(Q4B_VERSION=Q4B_VERSION, Q4B_FIELDS=Q4B_FIELDS, Q4B_STRUCTURAL_NULL=Q4B_STRUCTURAL_NULL, Q4B_RUBRIC=Q4B_RUBRIC,
                Q4B_PROMPT=Q4B_PROMPT, Q4B_SOURCES=Q4B_SOURCES, Q4B_PROMPT_SHA=Q4B_PROMPT_SHA, Q4B_RUBRIC_SHA=Q4B_RUBRIC_SHA)
globals().update(_q4b_variant(Q4B_PHASE1)); _P1_ON = Q4B_PHASE1   # _P1_ON: phase 1 in use right now (Helius guard can turn it off)
HOLDER_SNAPSHOT_LOOKBACK = (20 * 3600, 28 * 3600)   # a "24h ago" snapshot must be 20-28 h older than as_of
# preregistered auto-inactivation (per question version): after >= 30 Q4b answers, if >= 90% have score <= 2 or confidence < 0.5,
# Q4b status -> inactive_no_data: a q4b_auto_inactive record is chained ONCE with notify=[069, Chefus Maximus] (dashboard field
# jev.q4b_notify + a NOTIFY line in loop.log). The Q4 filter STAYS ACTIVE: Q4b is still asked and 6/0.50 still applies, fail-closed.
# Jev never decides on Q1/Q2 alone; while inactive, anything that keeps Q4b from being evaluated -> reject jev_Q4b_inactive.
Q4B_AUTO_INACTIVE = {"min_responses": 30, "frac": 0.90, "score_le": 2, "conf_lt": 0.5}

def q4b_degenerate(answers, rule=Q4B_AUTO_INACTIVE):
    """answers: list of (score, confidence). True when the preregistered rule trips."""
    if len(answers) < rule["min_responses"]: return False
    bad = sum(1 for s, cf in answers if s is None or s <= rule["score_le"] or (cf or 0) < rule["conf_lt"])
    return bad / len(answers) >= rule["frac"]

_q4b_hist = {}   # q4_version -> [(score, conf)]
def q4b_history():
    """Q4b answers of THIS version from the hash log (loaded once, then appended in-process)."""
    k = f"{Q4B_ID}_{Q4B_VERSION}"
    if k not in _q4b_hist:
        _q4b_hist[k] = h = []
        try:
            for l in open(hashlog.PATH, "rb"):
                try: r = json.loads(l)
                except ValueError: continue
                a = (r.get("answers") or {}).get("Q4b")
                if r.get("kind") == "decision" and r.get("q4_version") == k and a: h.append(tuple(a))
        except OSError: pass
    return _q4b_hist[k]

Q4B_NOTIFY = ["069", "Chefus Maximus"]
_q4b_inactive_logged = set()   # versions already notified in this process
def q4b_status():
    """'active' or 'inactive_no_data' (status flag only: Q4b keeps being asked and keeps filtering)."""
    if not q4b_degenerate(q4b_history()): return "active"
    if Q4B_VERSION not in _q4b_inactive_logged:
        _q4b_inactive_logged.add(Q4B_VERSION)
        if not any(r.get("kind") == "q4b_auto_inactive" and r.get("version") == Q4B_VERSION for r in _chain_kinds(("q4b_auto_inactive",))):
            hashlog.append("q4b_auto_inactive", question_id=Q4B_ID, version=Q4B_VERSION, rule=Q4B_AUTO_INACTIVE, n=len(q4b_history()),
                           status="inactive_no_data", notify=Q4B_NOTIFY, filter_still_active=True)
        print(f"NOTIFY {'+'.join(Q4B_NOTIFY)}: Q4b {Q4B_VERSION} status inactive_no_data (preregistered rule tripped, n={len(q4b_history())}); "
              "all Jev candidates rejected (jev_Q4b_inactive)", flush=True)
    return "inactive_no_data"

def _chain_kinds(kinds):
    try:
        out = []
        for l in open(hashlog.PATH, "rb"):
            if any(k.encode() in l for k in kinds):
                try: out.append(json.loads(l))
                except ValueError: pass
        return out
    except OSError: return []

# ---- phase 1 layer (only used when Q4B_PHASE1) ----
class _RateLimiter:
    """Own limiter for the holder-count Helius calls: separate session + budget, never shares Jupiter's or the RPC loop's."""
    def __init__(s, min_gap=0.5, per_min=20): s.min_gap, s.per_min, s.calls = min_gap, per_min, []
    def take(s):
        t = time.time(); s.calls = [x for x in s.calls if t - x < 60]
        if len(s.calls) >= s.per_min: return False   # budget exhausted -> caller returns null (fail closed), never waits on the loop
        if s.calls and t - s.calls[-1] < s.min_gap: time.sleep(s.min_gap - (t - s.calls[-1]))
        s.calls.append(time.time()); return True
HOLDERS_LIMITER = _RateLimiter(); HOLDERS_MAX_PAGES = 5; HOLDERS_PAGE = 1000
_HS = requests.Session()

HOLDERS_LOWER_BOUND = HOLDERS_MAX_PAGES * HOLDERS_PAGE   # nodata1: 5000 (free plan budget: 5 pages max per mint)
holders_meta = {}   # nodata1: meta of the LAST holders_count_source call: {"reason", "lower_bound", "owners_seen"}
def _holders_null(reason, ts=None):
    holders_meta.clear(); holders_meta.update(reason=reason, lower_bound=None)
    print(f"holders_count null: reason={reason}", flush=True); return None, ts or time.time()
def helius_holders_count(mint):
    """(count, as_of_ts) from Helius DAS getTokenAccounts (free plan, own limiter). Failure -> (None, ts) with holders_meta['reason']
    in no_key / budget / rpc_error:<code> / bad_response / exception:<Type>. >= HOLDERS_LOWER_BOUND token accounts (5 full pages) ->
    (5000, ts) with holders_meta['lower_bound'] = True (flag sent to Q4b as holders_count_is_lower_bound). Never 0 on error."""
    if not HELIUS: return _holders_null("no_key")
    owners = set()
    try:
        for page in range(1, HOLDERS_MAX_PAGES + 1):
            if not HOLDERS_LIMITER.take(): return _holders_null("budget")
            j = _HS.post(HELIUS, json={"jsonrpc": "2.0", "id": "h", "method": "getTokenAccounts",
                                       "params": {"mint": mint, "page": page, "limit": HOLDERS_PAGE}}, timeout=10).json()
            if "error" in j: return _holders_null(f"rpc_error:{mask(str((j.get('error') or {}).get('code') if isinstance(j.get('error'), dict) else 'x'))}")
            if "result" not in j: return _holders_null("bad_response")
            accs = j["result"].get("token_accounts") or []
            owners.update(a["owner"] for a in accs if a.get("owner") and float(a.get("amount") or 0) > 0)
            if len(accs) < HOLDERS_PAGE:
                holders_meta.clear(); holders_meta.update(reason=None, lower_bound=False, owners_seen=len(owners)); return len(owners), time.time()
        holders_meta.clear(); holders_meta.update(reason="lower_bound", lower_bound=True, owners_seen=len(owners))
        print(f"holders_count lower bound: >= {HOLDERS_LOWER_BOUND} token accounts (owners seen {len(owners)})", flush=True)
        return HOLDERS_LOWER_BOUND, time.time()
    except Exception as e:
        return _holders_null(f"exception:{mask(type(e).__name__)}")

# Helius guard (phase 1 only): during the first live hour of this process, if >= HELIUS_GUARD_MIN_READS holders reads were made and
# > 50% were null/error, phase 1 is BLOCKED: chain record q4b_phase1_blocked (once), flag file LIVE/Q4B_PHASE1_BLOCKED, NOTIFY line.
# The process then switches IN-PROCESS to the fallback variant (v1.1 constants; same prompt/rubric hashes as staged_fallback) and a
# restart with the flag present also starts in fallback. Every first-hour read logs q4b_holders_null_rate. activate.py --variant fallback
# makes it permanent (activate.py refuses --variant phase1 while the flag exists). No file swap from inside the loop.
HELIUS_GUARD_WINDOW_SEC, HELIUS_GUARD_MIN_READS, HELIUS_GUARD_MAX_NULL_FRAC = 3600, 10, 0.5
PHASE1_BLOCK_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Q4B_PHASE1_BLOCKED")
_guard = {"t0": time.time(), "reads": []}
def switch_to_fallback():
    """In-process switch to the fallback variant (v1.1 constants, identical to staged_fallback): no file swap, no restart."""
    global _P1_ON
    globals().update(_q4b_variant(False)); _P1_ON = False
def helius_guard_note(ok):
    if time.time() - _guard["t0"] <= HELIUS_GUARD_WINDOW_SEC:
        _guard["reads"].append(bool(ok)); r = _guard["reads"]
        print(f"q4b_holders_null_rate={r.count(False) / len(r):.3f} reads={len(r)} first_hour=1", flush=True)
    r = _guard["reads"]
    if _P1_ON and phase1_blocked(): switch_to_fallback(); return
    if len(r) >= HELIUS_GUARD_MIN_READS and r.count(False) / len(r) > HELIUS_GUARD_MAX_NULL_FRAC and not phase1_blocked():
        reason = f"{r.count(False)}/{len(r)} holders_count reads null/error in the first live hour (> {HELIUS_GUARD_MAX_NULL_FRAC:.0%})"
        hashlog.append("q4b_phase1_blocked", question_id=Q4B_ID, version=Q4B_VERSION, reason=reason, notify=Q4B_NOTIFY,
                       q4b_holders_null_rate=round(r.count(False) / len(r), 3), reads=len(r),
                       action="switched in-process to fallback v1.1; make permanent with activate.py --variant fallback")
        with open(PHASE1_BLOCK_FLAG, "w") as f: f.write(f"{dt.datetime.now(dt.timezone.utc).isoformat()}\n{reason}\nrun activate.py --variant fallback\n")
        print(f"NOTIFY {'+'.join(Q4B_NOTIFY)}: Q4b phase 1 BLOCKED ({reason}); switched to fallback v1.1; run activate.py --variant fallback", flush=True)
        switch_to_fallback()
def phase1_blocked(): return os.path.exists(PHASE1_BLOCK_FLAG)

holders_count_source = helius_holders_count

SIMILAR_RULE_VERSION = "sim_v1"
SIMILAR_RULE = ("sim_v1: norm(x) = NFKD, ASCII only, lowercase, keep [a-z0-9]. Another mint (!= this mint) is similar if seen in our own "
                "scans table with ts in [decision_ts - 24h, decision_ts] and (norm(symbol) equal and len >= 2) or (norm(name) equal and "
                "len >= 3) or difflib.SequenceMatcher(norm(symbol_a), norm(symbol_b)).ratio() >= 0.85 with both len >= 4. Distinct mints counted.")
def _norm(x):
    import unicodedata
    return "".join(ch for ch in unicodedata.normalize("NFKD", str(x or "")).encode("ascii", "ignore").decode().lower() if ch.isalnum())
def _similar(sa, na, sb, nb):
    import difflib
    if sa and sa == sb and len(sa) >= 2: return True
    if na and na == nb and len(na) >= 3: return True
    return len(sa) >= 4 and len(sb) >= 4 and difflib.SequenceMatcher(None, sa, sb).ratio() >= 0.85
def n_similar_tokens_24h(c, mint, symbol, name, decision_ts):
    """Deterministic (no LLM). Only scans with ts <= decision_ts (no look-ahead). None if no db / no symbol."""
    if c is None or not (symbol or name): return None
    lo = dt.datetime.fromtimestamp(decision_ts - 86400, dt.timezone.utc).isoformat()
    sa, na = _norm(symbol), _norm(name); seen = set()
    for ts, m, nm, sj in c.execute("SELECT ts, mint, name, state FROM scans WHERE ts >= ? AND mint != ?", (lo, mint)):
        try:
            if dt.datetime.fromisoformat(ts).timestamp() > decision_ts or m in seen: continue
            sym = (json.loads(sj or "{}") or {}).get("symbol")
        except (ValueError, TypeError): continue
        if _similar(sa, na, _norm(sym), _norm(nm)): seen.add(m)
    return len(seen)

def write_holder_snapshot(c, mint, st, ts_epoch, holders=None, holders_src=None):
    """Collector's own holder snapshot, written at observation time (never reconstructed later)."""
    if c is None or (holders is None and st.get("top10_pct") is None): return
    try:
        c.execute("INSERT INTO holder_snapshots VALUES(?,?,?,?,?,?,?)", (ts_epoch, dt.datetime.fromtimestamp(ts_epoch, dt.timezone.utc).isoformat(),
                  mint, holders, holders_src, st.get("top10_pct"), st.get("top_holder_pct")))
    except sqlite3.Error as e: print("holder_snapshot write failed:", mask(e))

def _num(x):
    try: return None if x is None or isinstance(x, bool) else float(x)
    except (TypeError, ValueError): return None

def _past(c, mint, as_of, col):
    """Latest own snapshot with a non-null `col`, 20-28 h before as_of (never after as_of)."""
    lo, hi = HOLDER_SNAPSHOT_LOOKBACK
    if c is None: return None
    return c.execute(f"SELECT ts_epoch, {col} FROM holder_snapshots WHERE mint=? AND {col} IS NOT NULL AND ts_epoch<=? AND ts_epoch>=? "
                     + ("AND COALESCE(holders_source,'') != 'helius_das_lb' " if col == "holders_count" else "") +
                     "ORDER BY ts_epoch DESC LIMIT 1", (mint, as_of - lo, as_of - hi)).fetchone()

def q4b_state(st, mint, decision_ts, c=None, phase1=None):
    """Builds the Q4b state ONLY from data with as_of_ts <= decision_ts. Missing -> None (never 0). Returns (state, missing, err).
    phase1: dict(holders_count, holders_as_of_ts, n_similar_tokens_24h) fetched at decision time, or None."""
    as_of = st.get("data_as_of_ts")
    if as_of is None: return None, list(Q4B_FIELDS), "data_as_of_ts missing"
    if as_of > decision_ts: return None, list(Q4B_FIELDS), f"future-dated data: as_of {as_of} > decision {decision_ts}"
    tx = st.get("txns") or {}; v = {}
    for w in ("h1", "h6", "h24"):
        b, s_ = _num((tx.get(w) or {}).get("buys")), _num((tx.get(w) or {}).get("sells"))
        v[f"buys_{w}"], v[f"sells_{w}"] = b, s_
        v[f"buy_sell_ratio_{w}"] = round(b / s_, 4) if b is not None and s_ else None
    for w in ("h1", "h6", "h24"): v[f"volume_{w}"] = _num(st.get(f"volume_{w}"))
    h24 = v["volume_h24"]
    v["vol_ratio_h1"] = round(v["volume_h1"] / (h24 / 24), 4) if v["volume_h1"] is not None and h24 else None
    v["vol_ratio_h6"] = round(v["volume_h6"] / (h24 / 4), 4) if v["volume_h6"] is not None and h24 else None
    pc = _num(st.get("pair_created"))
    v["token_age_h"] = round((as_of - pc / 1000) / 3600, 2) if pc is not None and pc / 1000 <= as_of else None
    v["top10_pct"] = _num(st.get("top10_pct"))
    old = _past(c, mint, as_of, "top10_pct")
    v["top10_change_h24_pp"] = round(v["top10_pct"] - old[1], 3) if old and v["top10_pct"] is not None else None
    extra = dict(symbol=st.get("symbol"), as_of_ts=as_of, top10_snapshot_ts=old[0] if old else None)
    if _P1_ON:
        p = phase1 or {}; h_as_of = p.get("holders_as_of_ts")
        if h_as_of is not None and h_as_of > decision_ts: return None, list(Q4B_FIELDS), f"future-dated holders: {h_as_of} > decision {decision_ts}"
        n_now = p.get("holders_count"); oh = _past(c, mint, h_as_of if h_as_of is not None else as_of, "holders_count")
        v["holders_count"] = n_now; v["n_similar_tokens_24h"] = p.get("n_similar_tokens_24h")
        v["holders_count_is_lower_bound"] = (bool(p.get("holders_count_is_lower_bound")) if n_now is not None else None)
        v["holder_growth_h24_pct"] = round(100 * (n_now - oh[1]) / oh[1], 2) if oh and oh[1] and n_now is not None else None
        extra.update(holders_as_of_ts=h_as_of, holders_snapshot_ts=oh[0] if oh else None, similar_rule=SIMILAR_RULE_VERSION)
    state = {k: v[k] for k in Q4B_FIELDS}; state.update(extra)
    missing = [k for k in Q4B_FIELDS if state[k] is None]
    return state, missing, None

def q4b_phase1_fetch(c, cand):
    """Phase 1 data at decision time; writes the holders snapshot (own history for future growth). Fail closed -> nulls."""
    st = cand["state"]; holders_meta.clear(); n, h_ts = holders_count_source(cand["mint"]); helius_guard_note(n is not None)
    if not _P1_ON: return None   # guard just switched to fallback
    lb = bool(holders_meta.get("lower_bound")) if n is not None else None
    write_holder_snapshot(c, cand["mint"], {}, h_ts, holders=n, holders_src=("helius_das_lb" if lb else "helius_das") if n is not None else None)
    ns = n_similar_tokens_24h(c, cand["mint"], st.get("symbol"), (cand.get("name") or ""), time.time())
    return {"holders_count": n, "holders_count_is_lower_bound": lb, "holders_null_reason": holders_meta.get("reason") if n is None else None,
            "holders_as_of_ts": h_ts, "n_similar_tokens_24h": ns}

def q4_narrative_inactive(st):
    """Narrative Q4 v0: fields shown as null (no X/narrative source), never asked, never filters."""
    return {"question_id": Q4_NARRATIVE_ID, "version": Q4_NARRATIVE_VERSION, "status": "inactive_no_data",
            "rubric": Q4_NARRATIVE_RUBRIC, "state": {k: (_num(st.get(k)) if k == "volume_h24" else None) for k in Q4_NARRATIVE_FIELDS},
            "missing_fields": [k for k in Q4_NARRATIVE_FIELDS if k != "volume_h24" or st.get(k) is None]}

# Q1/Q2 keep EXACTLY their pre-q4v1 state (no version bump): fields added for Q4b are stripped before asking them.
Q12_HIDDEN = ("volume_h1", "data_as_of_ts")
q12_state = lambda st: {k: v for k, v in st.items() if k not in Q12_HIDDEN}

_jev_db = None   # set by cycle() so _jev_decide can read holder_snapshots / scans

_jev_evals = []
def jev_decide(judge, cand):
    """Wrapper: records the exact Jev model id returned (auditor pin) in every Jev decision record.
    Only called for hard-filter survivors. Hourly cap: over cap -> no Jev call, no entry."""
    if len([t for t in _jev_evals if time.time() - t < 3600]) >= T.JEV_MAX_EVALS_PER_HOUR:
        return False, {"state_shown": cand["state"], "answers": {}, "rule": "jev_hourly_cap", "error": f">= {T.JEV_MAX_EVALS_PER_HOUR} evals/h"}
    _jev_evals.append(time.time())
    ok, rec = _jev_decide(judge, cand)
    rec["jev_model"] = getattr(judge, "last_model", None)
    rec["jev_expected_model"] = getattr(sys.modules.get("judge"), "EXPECTED_MODEL", None)
    return ok, rec

def _jev_decide(judge, cand):
    """Runs Q1, Q2 (pre-q4v1 state), Q4b for one candidate. Any abstain/error/unconfigured -> (False, record). Narrative Q4 v0 inactive."""
    rec = {"state_shown": q12_state(cand["state"]), "answers": {}, "q4_version": f"{Q4B_ID}_{Q4B_VERSION}",
           "q4_narrative": q4_narrative_inactive(cand["state"])}
    try:
        s12 = q12_state(cand["state"])
        for key, q, thr in (("Q1", Noul("whale distributing?"), T.JEV_Q1_WHALE_REJECT_P),
                            ("Q2", Noul("momentum exhausted?"), T.JEV_Q2_EXHAUSTED_REJECT_P)):
            a = judge.ask(q, s12); rec["answers"][key] = a.p_yes
            if a.p_yes is None: return False, {**rec, "rule": f"jev_{key}_abstain"}
            if a.p_yes >= thr: return False, {**rec, "rule": f"jev_{key}_p>={thr}"}
        if _P1_ON and phase1_blocked(): switch_to_fallback()   # flag from an earlier process/hour
        p1 = q4b_phase1_fetch(_jev_db, cand) if _P1_ON else None
        status = q4b_status(); rec["q4_version"] = f"{Q4B_ID}_{Q4B_VERSION}"
        q4s, missing, err = q4b_state(cand["state"], cand["mint"], time.time(), _jev_db, p1)
        rec.update(q4b=dict(question_id=Q4B_ID, version=Q4B_VERSION, status=status, prompt_sha256=Q4B_PROMPT_SHA, rubric_sha256=Q4B_RUBRIC_SHA, phase1=_P1_ON,
                            state_shown=q4s, missing_fields=missing, structural_null=[k for k in missing if k in Q4B_STRUCTURAL_NULL],
                            as_of_ts=(q4s or {}).get("as_of_ts"),
                            holders_null_reason=(p1 or {}).get("holders_null_reason")))   # nodata1: logged in the record, NOT sent to Jev
        inact = status == "inactive_no_data"
        if err: return False, {**rec, "rule": "jev_Q4b_inactive" if inact else "jev_Q4b_data_invalid", "error": err}
        try: a = judge.ask(Score(Q4B_PROMPT), q4s)
        except JevUnavailable as e:
            if inact: return False, {**rec, "rule": "jev_Q4b_inactive", "error": str(e)}
            raise
        rec["answers"]["Q4b"] = (a.score, a.confidence)
        q4b_history().append((a.score, a.confidence))
        if inact: return False, {**rec, "rule": "jev_Q4b_inactive"}   # auditor r3: inactive -> always reject (still asked + recorded)
        if a.score is None or a.score < T.JEV_Q4_MIN_SCORE or (a.confidence or 0) < T.JEV_Q4_MIN_CONF:
            return False, {**rec, "rule": "jev_Q4b_below"}
        return True, {**rec, "rule": "jev_Q1-Q2-Q4b_pass", "P_exhausted": rec["answers"]["Q2"]}
    except JevUnavailable as e: return False, {**rec, "rule": "jev_unavailable", "error": str(e)}
    except hashlog.HashlogCorrupt: raise
    except Exception as e: return False, {**rec, "rule": "jev_error", "error": mask(repr(e))}

# ---------------- monitor / exits ----------------
def sell(c, p, qty_raw, reason):
    q = jup_quote(p["mint"], USDC, qty_raw)
    if q is None:
        hashlog.append("exit_failed", position_id=p["id"], reason=reason, error="no sell route"); return False
    usd = int(q["outAmount"]) / 1e6; fee = T.PRIORITY_FEE_USD + T.NETWORK_FEE_USD
    c.execute("INSERT INTO fills(position_id,ts,side,qty_raw,usd,price_impact_pct,fee_usd,reason,lane) VALUES(?,?,?,?,?,?,?,?,?)",
              (p["id"], now().isoformat(), "sell", qty_raw, usd, float(q["priceImpactPct"]) * 100, fee, reason, p["lane"]))
    left = p["qty_left_raw"] - qty_raw
    c.execute("UPDATE positions SET qty_left_raw=?, status=?, closed_ts=? WHERE id=?",
              (left, "open" if left > 0 else "closed", None if left > 0 else now().isoformat(), p["id"]))
    hashlog.append("outcome", position_id=p["id"], lane=p["lane"], decision_hash=p["decision_hash"], reason=reason, qty_raw=qty_raw, usd=usd)
    p["qty_left_raw"] = left; print(f"  PAPER EXIT #{p['id']} {reason} ${usd:.2f}")
    return {"usd": usd, "fee_usd": fee, "qty_raw": qty_raw}   # r3: truthy like before; noroute books recovery from this (condition 9)

_exit_pending_logged = {}
def exit_pending(c, p, mv, force=None):
    """Auditor C1: an exit check was skipped (stale/no_route). Log `exit_pending` when the position is older than
    EXIT_PENDING_AGE_HOURS or its last valid mark is already at/below the stop; rate-limited per position."""
    row = c.execute("SELECT last_ok_usd,last_ok_qty,last_ok_ts FROM position_marks WHERE position_id=?", (p["id"],)).fetchone()
    age_h = (now() - dt.datetime.fromisoformat(p["opened_ts"])).total_seconds() / 3600
    chg = None
    if row and row[0] is not None and row[1] and p["qty_raw"]:
        chg = 100 * ((row[0] / row[1]) / (p["cost_usd"] / p["qty_raw"]) - 1)
    reasons = ([f"age {age_h:.1f}h >= {T.EXIT_PENDING_AGE_HOURS}h"] if age_h >= T.EXIT_PENDING_AGE_HOURS else []) + \
              ([f"last mark {chg:.1f}% <= stop {T.STOP_LOSS_PCT}%"] if chg is not None and chg <= T.STOP_LOSS_PCT else []) + \
              ([force] if force else [])
    if not reasons: return False
    t = now().timestamp(); last = _exit_pending_logged.get(p["id"])
    if last is not None and t - last < T.EXIT_PENDING_LOG_EVERY_SEC: return False
    _exit_pending_logged[p["id"]] = t
    hashlog.append("exit_pending", position_id=p["id"], strategy=p["strategy"], lane=p.get("lane"), mark_status=mv["status"],
                   reasons=reasons, age_hours=round(age_h, 2), last_mark_chg_pct=round(chg, 2) if chg is not None else None,
                   last_valid_mark_ts=row[2] if row else None, stop_pct=T.STOP_LOSS_PCT, ts=now().isoformat())
    return True

def _mark_info(c, p):
    """(last_attempt_ts, cached chg % vs entry from the last valid mark (any age) or None, failing streak?)"""
    r = c.execute("SELECT last_attempt_ts,last_ok_usd,last_ok_qty,fail_since_ts FROM position_marks WHERE position_id=?", (p["id"],)).fetchone()
    la = (r[0] if r else None) or p["opened_ts"]   # the entry quote counts as the first quote
    chg = 100 * ((r[1] / r[2]) / (p["cost_usd"] / p["qty_raw"]) - 1) if r and r[1] is not None and r[2] and p["qty_raw"] and p["cost_usd"] else None
    return la, chg, bool(r and r[3])

def _mark_recovery(c, p):
    """marks1-r1: (in_recovery, reason, last_ok_age_sec). In recovery = never marked, last quote failed (any kind), or last good mark > MARKS1_STALE_AFTER_SEC."""
    r = c.execute("SELECT last_ok_ts,fail_since_ts,last_fail_kind FROM position_marks WHERE position_id=?", (p["id"],)).fetchone()
    age = (now() - dt.datetime.fromisoformat(r[0])).total_seconds() if r and r[0] else None
    if age is None: return True, "never_marked" + (f":{r[2]}" if r and r[2] else ""), None
    if r[1] or r[2]: return True, r[2] or "no_route", age
    if age > MARKS1_STALE_AFTER_SEC: return True, "stale", age
    return False, "ok", age
_r10_mq = {}                           # r10: mint -> (quote result tuple or None, _jup_last) for this pass
_r10_tot = {}                          # r10: mint -> total open qty (set by monitor for the pass; empty outside monitor -> own-size quote)
_r10_px_seen = {}                      # r10: mint -> last Price API blockId (or 'priced' marker)
_r10_px_calls = []                     # r10: Price API call timestamps (own 60 s window)
_r10_stats = {"price_calls": 0, "price_ids": 0, "triaged": 0, "mint_cache_hits": 0}
_r11_cb = {"until": 0.0, "trips": 0}   # r11: circuit breaker state
_r11_pool = []                         # r11: (position_id, mint, since) of this pass's suspicious legs (filled by quote_plan, used by monitor)
_r11_urgent = set()                    # r11: legs quoted BEFORE the price call: urgent (stop zone / old age) + live legs that missed the last pass
def _r11_price_ok(t_pass):
    """r11: None if a Price call may go now, else the reason (breaker / per-pass / time left)."""
    if time.time() < _r11_cb["until"]: return "breaker"
    if MARKS2_PASS_DEADLINE_SEC - (time.monotonic() - t_pass) < MARKS11_PRICE_MIN_LEFT_SEC: return "no_time"
    return None
def _r10_price_triage(mints):
    """r10: mints whose Price API blockId advanced since the last call (a fresh on-chain swap). First sighting = baseline (not a candidate).
    Own limiter (MARKS10_PRICE_MAX_PER_MIN); any error -> no candidates (the r9 lane still runs)."""
    out = set(); ms = list(dict.fromkeys(mints))[:MARKS10_PRICE_IDS_PER_CALL]   # r11: caller orders by longest-unquoted; 1 call per pass
    for i in range(0, min(len(ms), MARKS10_PRICE_IDS_PER_CALL), MARKS10_PRICE_IDS_PER_CALL):
        t = time.time(); _r10_px_calls[:] = [x for x in _r10_px_calls if t - x < 60]
        if len(_r10_px_calls) >= MARKS10_PRICE_MAX_PER_MIN: break
        ch = ms[i:i + MARKS10_PRICE_IDS_PER_CALL]; _r10_px_calls.append(t); _r10_stats["price_calls"] += 1; _r10_stats["price_ids"] += len(ch)
        try:
            _to = MARKS11_PRICE_TIMEOUT_SEC if _pass_t0[0] is None else min(MARKS11_PRICE_TIMEOUT_SEC, MARKS2_PASS_DEADLINE_SEC - (time.monotonic() - _pass_t0[0]))
            if _to < MARKS11_PRICE_MIN_LEFT_SEC: break
            r = S.get(MARKS10_PRICE_URL, params={"ids": ",".join(ch)}, headers={"x-api-key": _JK} if _JK else None, timeout=_to)
            if r.status_code != 200: raise RuntimeError(f"HTTP {r.status_code}")
            j = r.json() if r.content else None
            if not isinstance(j, dict): raise ValueError("malformed price body")
        except Exception as e:   # r11 circuit breaker: 5 min without Price (r9 behaviour), one chain event per trip
            _r11_cb["until"] = time.time() + MARKS11_PRICE_CB_SEC; _r11_cb["trips"] += 1
            hashlog.append("price_api_breaker", error=mask(type(e).__name__ + ": " + str(e))[:120], off_sec=MARKS11_PRICE_CB_SEC, trips=_r11_cb["trips"])
            return set()
        for m in ch:
            v = j.get(m)
            if not isinstance(v, dict): continue
            b = v.get("blockId"); k = b if isinstance(b, (int, float)) else "priced"; old = _r10_px_seen.get(m)
            if old is not None and (b > old if isinstance(b, (int, float)) and isinstance(old, (int, float)) else k != old): out.add(m)
            _r10_px_seen[m] = k
    return out
def _r9_borrow(c, p, mv, byid, mq):
    """r9 borrowed mark ('marca prestada'). p just got a real no_route ($0). If another OPEN leg of the SAME mint has a good mark < MARKS9_BORROW_MAX_AGE_SEC
    (5 min; own fail_since_ts NULL), ONE quote per mint per pass is taken for the TOTAL size of all open legs of that mint and distributed pro rata
    (p gets out * qty_left / total). No sibling < 5 min, no window room, or that quote fails -> mv unchanged ($0). Persisted in marks9_borrowed for the export."""
    t = now(); best = None
    for q in byid.values():
        if q["id"] == p["id"] or q["mint"] != p["mint"] or q.get("status", "open") != "open" or q["qty_left_raw"] <= 0: continue
        r = c.execute("SELECT last_ok_ts,fail_since_ts FROM position_marks WHERE position_id=?", (q["id"],)).fetchone()
        if not r or not r[0] or r[1]: continue
        a = (t - dt.datetime.fromisoformat(r[0])).total_seconds()
        if a < MARKS9_BORROW_MAX_AGE_SEC and (best is None or a < best[1]): best = (q["id"], a)
    if best is None: return mv
    if MARKS10_PER_MINT:   # r10: sibling's mark (< 5 min) was the per-mint TOTAL-size quote -> its per unit x this leg's qty; no extra quote
        r = c.execute("SELECT last_ok_usd,last_ok_qty FROM position_marks WHERE position_id=?", (best[0],)).fetchone()
        if not r or r[0] is None or not r[1]: return mv
        tq = _r10_tot.get(p["mint"]) or p["qty_left_raw"]; usd = r[0] / r[1] * p["qty_left_raw"]
        c.execute("INSERT OR REPLACE INTO marks9_borrowed(position_id,src_position_id,ts,usd,qty,mint_qty) VALUES(?,?,?,?,?,?)", (p["id"], best[0], t.isoformat(), usd, p["qty_left_raw"], tq))
        hashlog.append("mark_borrowed", position_id=p["id"], src_position_id=best[0], src_mark_age_sec=round(best[1], 1), mint_qty=tq, valued_usd=usd, ts=t.isoformat())
        return dict(mv, usd=usd, status="stale", borrowed=True, borrowed_from=best[0], label="marca prestada", borrowed_mint_qty=tq)
    tot = sum(q["qty_left_raw"] for q in byid.values() if q["mint"] == p["mint"] and q.get("status", "open") == "open" and q["qty_left_raw"] > 0)
    if p["mint"] not in mq:
        mq[p["mint"]] = None
        if not _jup_avail("exit", need=1):
            r = jup_quote(p["mint"], USDC, tot); mq[p["mint"]] = (int(r["outAmount"]) / 1e6, tot) if r else None
    if not mq[p["mint"]]: return mv
    out, tq = mq[p["mint"]]; usd = out * p["qty_left_raw"] / tq
    c.execute("INSERT OR REPLACE INTO marks9_borrowed(position_id,src_position_id,ts,usd,qty,mint_qty) VALUES(?,?,?,?,?,?)", (p["id"], best[0], t.isoformat(), usd, p["qty_left_raw"], tq))
    hashlog.append("mark_borrowed", position_id=p["id"], src_position_id=best[0], src_mark_age_sec=round(best[1], 1), mint_qty=tq, valued_usd=usd, ts=t.isoformat())
    return dict(mv, usd=usd, status="stale", borrowed=True, borrowed_from=best[0], label="marca prestada", borrowed_mint_qty=tq)
def quote_plan(c, ops):
    """marks1-r6: fair queue where a dead leg can never beat a live one (r5 let the uncapped age of no-route legs win forever).
    dead (r8) = >= 2 CONSECUTIVE no_route answers (marks8_dead.noroutes; an ok quote or a transient failure resets it), at once (no 300 s wait).
    r8: per-leg backoff 1/2/4/8 passes (cap 15 min), 3 lane slots (probe, overdue, due), cohort wake on route-back (see the r8 block).
    score = sec since last attempt + min(last-good age, 300) (never marked: since open, capped) + 60 if in recovery + 60 if urgent.
    Order: 1) MARKS6_DEAD_PER_PASS (1) dead leg, oldest attempt first: a fixed 1-slot lane, so every dead leg is retried round-robin
    (each one at least every n_dead passes); 2) live legs that missed the last pass (last attempt >= 90 s), score desc; 3) other live legs,
    score desc; 4) other dead legs (only with slots left after every live leg), oldest attempt first.
    Total <= MONITOR_MAX_QUOTES_PER_PASS."""
    live0, dead, live1, wcand, susp, spool, _uq = [], [], [], [], [], [], set(); nr = rc = ur = 0; tn = now()
    c.execute("CREATE TABLE IF NOT EXISTS marks8_dead(position_id INTEGER PRIMARY KEY, noroutes INTEGER, level INTEGER, next_ts TEXT)")   # r8 state
    _r8_events[:] = [(e, s) for e, s in _r8_events if (tn - e).total_seconds() <= MARKS8_WAKE_TTL_SEC]
    for p in ops:
        la, chg, _f = _mark_info(c, p); inrec, _why, age = _mark_recovery(c, p)
        age_h = (tn - dt.datetime.fromisoformat(p["opened_ts"])).total_seconds() / 3600
        urg = (chg is not None and chg <= T.URGENT_MARK_CHG_PCT) or age_h >= T.EXIT_PENDING_AGE_HOURS
        nr += bool(_f); rc += inrec; ur += urg and not inrec
        since = (tn - dt.datetime.fromisoformat(la)).total_seconds()
        _r8 = c.execute("SELECT m.fails,m.last_fail_kind,m.fail_since_ts,d.noroutes,d.next_ts FROM position_marks m LEFT JOIN marks8_dead d "
                        "ON d.position_id=m.position_id WHERE m.position_id=?", (p["id"],)).fetchone() if _f else None
        # r8 consecutive no_route count: marks8_dead.noroutes; legacy legs (no r8 row yet) count fails only if the last failure was no_route
        _n8 = (_r8[3] if _r8[3] is not None else ((_r8[0] or 0) if _r8[1] == "no_route" else 0)) if _r8 else 0
        if _n8 >= 1:   # r9: suspects (1 no_route) too: cohort wake + probe pool
            fs = dt.datetime.fromisoformat(_r8[2]); _ev9 = [(e, abs((dt.datetime.fromisoformat(s) - fs).total_seconds())) for e, s in _r8_events]
            _ev9 = [(e, x) for e, x in _ev9 if x <= MARKS8_COHORT_SEC]; ev = [e for e, _ in _ev9]
            d8 = (p["id"], since, (tn - fs).total_seconds(), _r8[4] is None or dt.datetime.fromisoformat(_r8[4]) <= tn, age, _n8, min([x for _, x in _ev9] or [0.0]))   # r9: + good age, noroutes, event distance
            if ev and _r8_woken.get(p["id"]) != max(ev): wcand.append((d8, max(ev), since + MARKS6_AGE_CAP_SEC + MARKS5_RECOVERY_BONUS_SEC))   # cohort wake candidate
            elif _n8 >= MARKS8_DEAD_MIN_NOROUTES: dead.append(d8)
            else: susp.append((-since, p["id"])); spool.append(d8)   # r8: suspect's 2nd try through the dead lane, else after every live leg; r9: also probe pool
            continue
        a = age if age is not None else (tn - dt.datetime.fromisoformat(p["opened_ts"])).total_seconds()
        score = since + MARKS5_AGE_WEIGHT * min(a, MARKS6_AGE_CAP_SEC) + MARKS5_RECOVERY_BONUS_SEC * inrec + MARKS5_URGENT_BONUS_SEC * urg
        (live0 if since >= MARKS5_MISSED_PASS_SEC else live1).append((-score, p["id"]))
        if urg or (age is not None and age >= MARKS11_URGENT_MARK_AGE_SEC): _uq.add(p["id"])   # r11: stop zone / age-urgent, or mark near the 300 s TTL -> before the Price call
    # r8 dead lane (MARKS8_DEAD_SLOTS = 3, 2 when tight): 1) one probe: oldest-attempt dead leg with a no-route streak < 15 min, backoff ignored
    # (finds a returning route, which then wakes its cohort); 1b) suspects (exactly 1 no_route), oldest attempt first; 2) overdue (no try for >= 15 min - 1 pass), oldest attempt first; 3) legs whose backoff is
    # due, oldest attempt first. Other due/overdue legs: after every live leg. Legs still in backoff: not quoted.
    _tot = _jup_limits()[0]; _sh = _close_ctx.get("share", _tot); cap = int((_tot - (_sh if _sh < _tot else 0)) * MARKS2_PASS_DEADLINE_SEC / 60)   # marks per 45 s pass (HALT: minus the close share)
    nl = len(live0) + len(live1); spare = cap - (nl + 1) // 2
    slots = (MARKS8_DEAD_SLOTS if _sh >= _tot else MARKS8_DEAD_SLOTS_MIN) + MARKS9_EXTRA_PROBE_SLOTS   # r9: 4 (3 under HALT): + the 2nd probe
    wcand.sort(key=lambda w: (round(w[0][6] / MARKS8_PASS_SEC), not (w[0][4] is not None and w[0][4] < MARKS9_WAKE_GOOD_SEC), w[0][5], -w[0][1])); wk = 0   # r9 wake order
    for d8, ev, sc in wcand:
        if wk < max(MARKS8_WAKE_MIN, spare - slots): _r8_woken[d8[0]] = ev; wk += 1; (live0 if d8[1] >= MARKS5_MISSED_PASS_SEC else live1).append((-sc, d8[0]))
        elif d8[5] >= MARKS8_DEAD_MIN_NOROUTES: dead.append(d8)   # not woken this pass: next pass (event still valid)
        else: susp.append((-d8[1], d8[0])); spool.append(d8)
    _tr = []; _mi = {p["id"]: p["mint"] for p in ops}   # r11: the Price call moved to monitor() (after the urgent legs); here only the pool
    _r11_pool[:] = [(d[0], _mi[d[0]], d[1]) for d in sorted(dead + spool, key=lambda d: -d[1])] if MARKS10_TRIAGE else []
    _r11_urgent.clear(); _r11_urgent.update(_uq)
    young = sorted((d for d in dead + spool if d[2] < MARKS9_PROBE_YOUNG_SEC), key=lambda d: -d[1])   # r9: probe pool = young dead + suspects
    over = sorted((d for d in dead if d[1] >= MARKS8_DEAD_MAX_GAP_SEC - MARKS8_PASS_SEC), key=lambda d: -d[1])
    due = sorted((d for d in dead if d[3]), key=lambda d: -d[1]); lane = []
    susp.sort(); sq = [(i,) for _, i in susp]
    # r9 probes: young legs are grouped into streak clusters (sorted by streak start; a gap > MARKS9_CLUSTER_GAP_SEC starts a new cluster).
    # A = oldest-attempt young leg (r8 probe); B = the most recent streak cluster's oldest-attempt leg (!= A).
    _cl = []
    for d in sorted(young, key=lambda d: -d[2]):
        if _cl and _cl[-1][-1][2] - d[2] <= MARKS9_CLUSTER_GAP_SEC: _cl[-1].append(d)
        else: _cl.append([d])
    _cl = [sorted(g, key=lambda d: -d[1]) for g in _cl]; _pr = []
    if young:
        _ya = [d for d in young if d[5] >= MARKS8_DEAD_MIN_NOROUTES] or young
        _pr.append(_ya[0])   # A: oldest-attempt young DEAD leg (as r8's single probe)
        _pb = [d for d in _cl[-1] if d[0] != _ya[0][0]]   # B: most recent streak cluster, its oldest-attempt leg
        if not _pb and len(_cl) > 1: _pb = _cl[-2]
        _pr += _pb[:1]
    for d in _pr + sq + over + due:   # r9: probe A (oldest attempt), probe B (most recent streak), suspects, overdue, due
        if len(lane) < slots and d[0] not in lane: lane.append(d[0])
    tail = list(dict.fromkeys([d[0] for d in sq if d[0] not in lane] + [d[0] for d in over + due if d[0] not in lane]))
    _r8_lane.clear(); _r8_lane.update(lane); _r8_tail.clear(); _r8_tail.update(tail)   # monitor: tail legs only while no live leg was skipped for capacity this pass
    plan = lane + [i for _, i in sorted(live0)] + [i for _, i in sorted(live1)] + tail
    return plan[:T.MONITOR_MAX_QUOTES_PER_PASS], {"recovery": rc, "urgent": ur, "healthy": len(ops) - rc - ur, "no_route": nr, "dead": len(dead),
                                                 "dead_lane": len(lane), "triaged": len(_tr), "dead_backoff": sum(1 for d in dead if not d[3]), "woken": wk, "dead_slots": slots}

def quote_plan_r0(c, ops):   # pre-marks1 plan (unused)
    """v2 C1: which positions get a real Jupiter quote this pass. Urgent (cached mark <= -10% or age >= 24h) first, then
    healthy oldest-attempt first, then no_route (failing) oldest first, max NO_ROUTE_MAX_QUOTES_PER_PASS; total <= MONITOR_MAX_QUOTES_PER_PASS."""
    urgent, healthy, noroute = [], [], []
    for p in ops:
        la, chg, failing = _mark_info(c, p)
        age_h = (now() - dt.datetime.fromisoformat(p["opened_ts"])).total_seconds() / 3600
        urg = (chg is not None and chg <= T.URGENT_MARK_CHG_PCT) or age_h >= T.EXIT_PENDING_AGE_HOURS
        if failing: noroute.append((not urg, la, p["id"]))   # week1e: failing (no_route) always behind healthy, capped 5; urgent first among them
        elif urg: urgent.append((la, p["id"]))
        else: healthy.append((la, p["id"]))
    plan = [i for _, i in sorted(urgent)] + [i for _, i in sorted(healthy)] + [x[-1] for x in sorted(noroute)[:T.NO_ROUTE_MAX_QUOTES_PER_PASS]]
    return plan[:T.MONITOR_MAX_QUOTES_PER_PASS], {"urgent": len(urgent), "healthy": len(healthy), "no_route": len(noroute)}

def monitor_backlog(c, exclude=("simple_tp",)):
    """v2 C2: reason string if any open position has gone > STALE_MARK_TTL_SEC without a real quote attempt, or the
    queue cannot be cleared within it (passes per TTL x per-pass budgets). None = OK. Blocks new entries only.
    exits1 r2 (B3): simple_tp legs are excluded by default (they can never block simple/jev); the pairing checks exclude=()."""
    ops = [p for p in open_positions(c) if p["strategy"] not in exclude]
    if not ops: return None
    t = now(); old = []; nr = 0
    for p in ops:
        la, _, failing = _mark_info(c, p); nr += failing
        age = (t - dt.datetime.fromisoformat(la)).total_seconds()
        if age > T.STALE_MARK_TTL_SEC: old.append((p["id"], round(age)))
    passes = max(1, T.STALE_MARK_TTL_SEC // T.MONITOR_EVERY_SEC)
    if old: return f"{len(old)} open position(s) not quoted for > {T.STALE_MARK_TTL_SEC}s (e.g. #{old[0][0]} {old[0][1]}s)"
    tot, ent = _jup_limits(); cap = min(T.MONITOR_MAX_QUOTES_PER_PASS * passes, (tot - ent) * T.STALE_MARK_TTL_SEC // 60)
    if len(ops) > cap:   # marks1-r1: no per-pass no_route cap any more -> only the total budget matters
        return f"quote backlog: {len(ops)} open ({nr} no_route) > {cap} marks ({T.NO_ROUTE_MAX_QUOTES_PER_PASS * passes} no_route) per {T.STALE_MARK_TTL_SEC}s"
    return None

def _by_value_desc(c, ops):
    """KILL / close_all order: highest cached mark value first (fallback: remaining cost)."""
    def v(p):
        m = mark_cached(c, p)
        return m["usd"] if m["usd"] else p["cost_usd"] * p["qty_left_raw"] / max(p["qty_raw"], 1)
    return sorted(ops, key=v, reverse=True)

_monitor_halt = {}   # strategy -> halt reason from the last monitor pass (fresh marks); read by cycle()

def _exit_logic(c, p, mv, vols):
    """Exits on a REAL quote (mv status ok) only."""
    per_unit = mv["usd"] / p["qty_left_raw"]; entry_unit = p["cost_usd"] / p["qty_raw"]
    chg = 100 * (per_unit / entry_unit - 1)
    if per_unit > p["peak_mark"]:
        _pts = now().isoformat()   # exits1: + peak_ts (logging only)
        c.execute("UPDATE positions SET peak_mark=?, peak_ts=? WHERE id=?", (per_unit, _pts, p["id"])); p["peak_mark"] = per_unit; p["peak_ts"] = _pts
    _dec = p.get("decimals")   # exits1 part 2: real-quote price row (USD per token if decimals known, else per raw unit); memory only
    _ph_push(p, per_unit * 10 ** _dec if isinstance(_dec, int) else per_unit, chg, _hist_src[0], 100 * (p["peak_mark"] / entry_unit - 1))
    v = (vols.get(p["mint"]) or {}).get("volume") or {}
    vr = (v.get("h6") or 0) / ((v.get("h24") or 0) / 4) if v.get("h24") else None
    age_h = (now() - dt.datetime.fromisoformat(p["opened_ts"])).total_seconds() / 3600
    if chg <= T.STOP_LOSS_PCT: sell(c, p, p["qty_left_raw"], f"stop {chg:.1f}%"); return
    if p["strategy"] == "simple":
        if age_h >= T.SIMPLE_MAX_HOLD_HOURS: sell(c, p, p["qty_left_raw"], "24h time exit")
        return                                   # simple baseline: stop + 24h only
    if p["strategy"] == "simple_tp" and age_h >= T.SIMPLE_MAX_HOLD_HOURS: sell(c, p, p["qty_left_raw"], "24h time exit"); return   # exits1: simple's 24h expiry
    if vr is not None and vr < T.VOL_DECAY_MIN_RATIO and (p["strategy"] != "simple_tp" or SIMPLE_TP_VOL_DECAY): sell(c, p, p["qty_left_raw"], f"volume decay {vr:.2f}"); return
    for i, (gain, frac) in enumerate(T.TAKE_PROFIT):
        if p["tp_done"] <= i and chg >= gain and sell(c, p, min(int(p["qty_raw"] * frac), p["qty_left_raw"]), f"take profit +{gain}%"):
            c.execute("UPDATE positions SET tp_done=? WHERE id=?", (i + 1, p["id"])); p["tp_done"] = i + 1
    if p["tp_done"] >= 1 and p["qty_left_raw"] > 0 and per_unit <= p["peak_mark"] * (1 - T.TRAILING_STOP_PCT / 100):
        sell(c, p, p["qty_left_raw"], "trailing stop 10%")

def _noroute_step(c, p, mv, vols, out):
    """r3 noroute wiring (only when NOROUTE_ENABLED). quote = the real executable mark (status ok) else None with diag's failure kind."""
    import noroute
    q = dict(usd=mv["usd"], impact=mv["impact"], fee_usd=T.PRIORITY_FEE_USD + T.NETWORK_FEE_USD) if mv["status"] == "ok" else None
    def _sell(pos, quote, reason):
        return sell(c, p, p["qty_left_raw"], "noroute: " + reason) if p["qty_left_raw"] > 0 else None
    out.append(noroute.step(c, dict(p, decimals=p.get("decimals") or 0), q, (vols or {}).get(p["mint"]), abs(T.STOP_LOSS_PCT) / 100,
                            hashlog.append, sell=_sell, failure=None if q else (mv.get("fail_kind") or "no_route")))

def _noroute_block(c, strat, mint, ticket, eq, sb):
    """r3 (7): extra entry gate when NOROUTE_ENABLED; only this strategy's own open positions; per-mint on sb['bank_amount'] (pre FACTOR_NO_X)."""
    if not NOROUTE_ENABLED: return None
    import noroute
    return noroute.can_add(c, open_positions(c, strat), mint, ticket, eq, strat, bank_amount_usd=sb.get("bank_amount"))

def marks_pass_record(c, marks, plan, groups, skipped=None, pass_sec=None):
    """marks1-r1: one chain record per monitor pass: unmarked legs (status != ok) with reason; sets the entry reserve."""
    legs, reasons = [], {}
    for p in open_positions(c):
        mv = marks.get(p["id"]) or mark_cached(c, p)
        if mv.get("status") == "ok" and not mv.get("ttl_expired"): continue
        inrec, why, age = _mark_recovery(c, p)
        why = mv.get("fail_kind") if mv.get("fail_kind") and not why.startswith("never") else why
        if p["id"] in (skipped or {}): why = f"{why}|skipped:{skipped[p['id']]}"   # r2: not quoted this pass (and why)
        reasons[why] = reasons.get(why, 0) + 1
        legs.append({"id": p["id"], "strategy": p["strategy"], "reason": why, "status": mv.get("status"), "valued_usd": round(mv.get("usd") or 0.0, 6),
                     "at_zero": not mv.get("usd"), "last_ok_age_sec": round(age, 1) if age is not None else None})
    t = time.time(); tot, _ent = _jup_limits(); _recovery_reserve[0] = min(sum(1 for x in legs if x["strategy"] != "simple_tp"), tot - 1)   # exits1 r2 (B3)
    _hl = {s for s in STRATS if os.path.exists(os.path.join(HERE, f"HALT_{s}"))}; _ages = []   # r5: entries yield while marks are behind
    for p in open_positions(c):
        if p["strategy"] in _hl or p["strategy"] == "simple_tp": continue   # exits1 r2 (B3): simple_tp marks never make simple/jev entries yield
        _a = _mark_recovery(c, p)[2]; _ages.append(1e12 if _a is None else _a)
    _entry_yield[0] = any(a > MARKS5_ENTRY_YIELD_AGE_SEC for a in _ages)
    hashlog.append("marks_pass", n_open=len(open_positions(c)), n_quoted=len(plan), plan_groups=groups, unmarked=len(legs),
                   unmarked_at_zero=sum(1 for x in legs if x["at_zero"]), reasons=reasons, legs=legs,
                   jup_calls_60s=len([x for x in _jup_calls if t - x < 60]), jup_limit_per_min=tot, http_429_total=_jup_stats["http_429"],
                   backoff_left_sec=round(max(0.0, _jup_backoff["until"] - t), 1), entry_reserve=_recovery_reserve[0],
                   entry_deferred_total=_jup_stats["entry_deferred"], n_skipped=len(skipped or {}),
                   skipped_reasons={k: list((skipped or {}).values()).count(k) for k in set((skipped or {}).values())},
                   pass_sec=pass_sec, deadline_sec=MARKS2_PASS_DEADLINE_SEC, local_skips_total=_jup_stats["local_skips"],
                   deadline_sell_skips_total=_jup_stats["deadline_sell_skips"],
                   bucket_tokens=round(_jup_bucket["tok"], 2), close_share=_close_ctx["share"], close_spent_sec=round(_close_ctx["spent"], 3),
                   close_leg_backoff=len(_close_leg_backoff), close_leg_backoff_skips_total=_close_ctx["leg_backoff_skips"],
                   close_budget_skips_total=_close_ctx["budget_skips"], entry_yield=_entry_yield[0],
                   healthy_max_mark_age_sec=(round(max(_ages), 1) if max(_ages) < 1e12 else "never_marked") if _ages else None)

def _close_sell(c, p, reason):
    """r3: kill / close_all sell on the PRIORITY limiter path ('close': backoff + 60 s window only, bucket not required). Never sleeps."""
    _jup_purpose[0] = "close"
    try: return sell(c, p, p["qty_left_raw"], reason)
    finally: _jup_purpose[0] = "exit"
def _close_legs(c, legs, reason, t_pass, tried, pending_label=None):
    """r4: kill / close_all sells, highest value first, one attempt per leg per pass; skips legs in per-leg backoff; stops at the close
    budget and at the pass deadline. Never sleeps."""
    legs = sorted(legs, key=lambda p: _close_leg_backoff.get(p["id"], {}).get("n", 0))   # r4: legs that failed before go last (stable: value order kept)
    for p in legs:
        if p["id"] in tried: continue
        if time.monotonic() - t_pass >= MARKS2_PASS_DEADLINE_SEC: _jup_stats["deadline_sell_skips"] += 1; continue   # next pass
        if _close_ctx["spent"] >= _close_ctx["budget"]: _close_ctx["budget_skips"] += 1; continue                # r4: next pass
        b = _close_leg_backoff.get(p["id"])
        if b and time.time() < b["until"]: _close_ctx["leg_backoff_skips"] += 1; continue                         # r4: per-leg backoff
        tried.add(p["id"]); t0 = time.monotonic()
        ok = _close_sell(c, p, reason)
        _close_ctx["spent"] += time.monotonic() - t0
        if ok: _close_leg_backoff.pop(p["id"], None); continue
        k = (_jup_last[0] or ("no_route",))[0]
        if k in MARKS4_LEG_BACKOFF_KINDS:
            n = (b or {}).get("n", 0) + 1
            _close_leg_backoff[p["id"]] = {"until": time.time() + min(MARKS4_LEG_BACKOFF_BASE_SEC * 2 ** (n - 1), MARKS4_LEG_BACKOFF_MAX_SEC), "n": n}
        if pending_label:
            exit_pending(c, p, {"status": "no_route"}, force=f"{pending_label}: no sell route (valued $0)" if k == "no_route" else
                         f"{pending_label}: sell failed ({k}{'' if _jup_last[0] is None or _jup_last[0][2] is None else ', ' + str(_jup_last[0][2])}); retried next pass")
def _close_all(c, s, t_pass, tried):
    """r3/r4: drawdown close_all for strategy s (see _close_legs)."""
    _close_legs(c, _by_value_desc(c, open_positions(c, s)), f"drawdown {T.MAX_DRAWDOWN_PCT:g}% close_all", t_pass, tried, "drawdown close_all")

def monitor(c, kill=False):
    _pass_t0[0] = time.monotonic()   # r5: HTTP timeouts in this pass are capped at the time left
    try: return _monitor(c, kill)
    finally: _pass_t0[0] = None
def _monitor(c, kill=False):
    ops = open_positions(c); _t_pass = time.monotonic()   # r2: one wall-clock deadline for the whole pass (kill sells, marks, close_all)
    _close_ctx.update(share=_jup_limits()[0], budget=MARKS2_PASS_DEADLINE_SEC, spent=0.0)   # r4: kill / nothing healthy -> full window
    if kill:   # highest value first (each sell = 1 Jupiter quote through the shared limiter: 54/min keyed, 27/min keyless)
        _close_legs(c, _by_value_desc(c, ops), "kill switch", _t_pass, set())   # r4: per-leg backoff; full window (nothing healthy under kill)
        ops = []
    vols = dexscreener(list({p["mint"] for p in ops})) if ops else {}   # batched 30 mints/call
    marks = {}
    _halted = {s for s in STRATS if os.path.exists(os.path.join(HERE, f"HALT_{s}"))}   # r3: legs being closed -> cached mark only
    _dd_tried = set()
    if not kill and _halted and any(p["strategy"] not in _halted for p in ops):   # r4: a healthy strategy has open legs -> reserved share for its marks
        _nh = sum(1 for p in ops if p["strategy"] not in _halted); _tot = _jup_limits()[0]   # r5: marks need ceil(n/2) per pass (2-pass round-robin)
        _close_ctx.update(share=max(MARKS5_CLOSE_MIN_PER_MIN, min(MARKS4_CLOSE_SHARE_PER_MIN, _tot - 2 - (_nh + 1) // 2)), budget=MARKS4_CLOSE_BUDGET_SEC)
    if not kill and _halted:   # r3: drawdown close_all runs FIRST (before any mark spends a slot); the tail loop only handles a HALT written this pass
        for _s in sorted(_halted): _close_all(c, _s, _t_pass, _dd_tried)
        ops = open_positions(c)
    plan, _groups = quote_plan(c, [p for p in ops if p["strategy"] not in _halted and p["strategy"] != "simple_tp"]); byid = {p["id"]: p for p in ops}
    _tpl = sorted((p for p in ops if p["strategy"] == "simple_tp" and p["strategy"] not in _halted), key=lambda p: _mark_info(c, p)[0])   # exits1 r2: after every simple/jev leg, oldest attempt first
    plan = plan + [p["id"] for p in _tpl]; qset = set(plan)
    nr_res = []; _r9_mq = {}   # r9: per-mint total-size quote cache (this pass)
    _r10_mq.clear(); _r10_tot.clear()   # r10: per-mint quote for the TOTAL open qty of each mint (all open legs, halted included)
    for _q in ops: _r10_tot[_q["mint"]] = _r10_tot.get(_q["mint"], 0) + max(_q["qty_left_raw"] or 0, 0)
    c.execute("CREATE TABLE IF NOT EXISTS marks9_borrowed(position_id INTEGER PRIMARY KEY, src_position_id INTEGER, ts TEXT, usd REAL, qty INTEGER, mint_qty INTEGER)")
    _skipped = {}   # r2: legs skipped (backoff / bucket / window / pass_deadline) -> cached mark + exit_pending
    if _r11_pool and _r11_urgent: plan = [x for x in plan if x in _r11_urgent] + [x for x in plan if x not in _r11_urgent]   # r11: urgent legs first, then Price + triaged, then lane/live
    _r111["streak"] = 0; _rq111 = {}; _d111 = {}   # r11.1: per-pass retry state
    _r11_left = set(_r11_urgent) & set(plan); _r11_done = not _r11_pool; _r11_i = 0
    while _r11_i < len(plan):   # C1 priority order (r11: index loop so triaged legs can be inserted)
        pid = plan[_r11_i]; _r11_i += 1
        if not _r11_done and not (_r11_left - set(plan[:_r11_i - 1])):   # r11: every urgent leg already went -> ONE Price call, then the triaged legs
            _r11_done = True; _ok = _r11_price_ok(_t_pass)
            if _ok is None:
                _seen = set(plan[:_r11_i - 1]); _pool = [x for x in _r11_pool if x[0] not in _seen]
                _cm = _r10_price_triage([m for _, m, _ in _pool])
                _ins = [i for i, m, _ in _pool if m in _cm][:max(MARKS8_WAKE_MIN, MARKS10_TRIAGE_MAX_PER_PASS)]
                if _ins:
                    _r10_stats["triaged"] += len(_ins); _rest = [x for x in plan[_r11_i - 1:] if x not in _ins]
                    plan[_r11_i - 1:] = _ins + _rest; qset.update(_ins); pid = plan[_r11_i - 1]
                    for _x in _ins: _r8_tail.discard(_x); _r8_lane.add(_x)   # r11: triaged = dead-lane budget (27/60 s window, not the 20-token bucket)
            else: _r10_stats["price_skips_" + _ok] = _r10_stats.get("price_skips_" + _ok, 0) + 1
        _m111 = byid[pid]["mint"]   # r11.1: mint skipped after a 429 this pass -> move the leg to the end (bounded), no wait
        if time.time() < _r111["mint_skip"].get(_m111, 0.0) and _d111.get(pid, 0) < MARKS111_DEFERS and _r11_i < len(plan):
            _d111[pid] = _d111.get(pid, 0) + 1; _r111["deferred"] += 1; plan.append(pid); continue
        _m111skip = pid not in _rq111 and time.time() < _r111["mint_skip"].get(_m111, 0.0)   # first try of this leg this pass, mint inside its 2 s skip (e.g. from the previous pass), nothing to defer behind -> cached mark, no HTTP; in-pass retries may go
        p = byid[pid]   # r2: NO sleep in monitor (r3 _space_live_quote spacing replaced by the token bucket)
        why = "pass_deadline" if time.monotonic() - _t_pass >= MARKS2_PASS_DEADLINE_SEC else _jup_avail("exit", need=2)   # 2 = mark + a possible exit sell
        if why in ("bucket", "reserve", "window") and p["strategy"] == "simple_tp" and p["mint"] in _r10_mq and MARKS10_PER_MINT: why = None   # exits1 r2: per-mint cache hit, no HTTP
        if not why and _m111skip: why = "mint_429"   # r11.1: mint inside its 2 s 429 skip, nothing to defer behind -> cached mark
        if not why and pid in _r8_tail and _skipped: why = "dead_tail"   # r8: a live leg already missed this pass -> no dead-tail quote
        if pid in _r8_lane and why == "bucket":   # r8: dead-lane leg: window-only check (27/60 s hard ceiling), not the bucket
            _r8_lane_call[0] = True; why = _jup_avail("exit", need=1); _r8_lane_call[0] = bool(not why)
        if why:
            mv = mark_cached(c, p); marks[pid] = mv; _skipped[pid] = why; exit_pending(c, p, mv); continue
        _r8p = c.execute("SELECT fail_since_ts,fails,last_fail_kind FROM position_marks WHERE position_id=?", (pid,)).fetchone()   # r8: state before this quote
        mv = mark_valued(c, p); marks[pid] = mv; _r8_lane_call[0] = False   # r8: an exit sell after a lane mark uses the bucket again
        if mv["status"] != "ok" and _jup_last[0] and _jup_last[0][1] == 429:   # r11.1: real gateway 429 -> skip this mint 2 s, retry later in THIS pass
            _r111["mint_skip"][p["mint"]] = time.time() + MARKS111_MINT_SKIP_SEC
            if _rq111.get(pid, 0) < MARKS111_RETRIES:
                _rq111[pid] = _rq111.get(pid, 0) + 1; _r111["requeued"] += 1; plan.append(pid); _r8_tail.discard(pid); _r8_lane.add(pid); continue   # retry = window-only (60 s cap), not the bucket
        _r8r = c.execute("SELECT noroutes,level FROM marks8_dead WHERE position_id=?", (pid,)).fetchone()   # r8: consecutive no_route + backoff
        if mv["status"] == "ok" or mv["status"] == "quote_error":   # ok / transient failure: streak broken (only no_route counts)
            c.execute("DELETE FROM marks8_dead WHERE position_id=?", (pid,))
            if mv["status"] == "ok" and _r8p and _r8p[0]: _r8_events.append((now(), _r8p[0]))   # route came back after a no-route streak
        else:   # stale / no_route = a true no_route answer
            _r8n = ((_r8r[0] if _r8r else ((_r8p[1] or 0) if _r8p[2] == "no_route" else 0)) if _r8p and _r8p[0] else 0) + 1   # legacy row: r7 fails/kind
            _r8l = (_r8r[1] or 0) if _r8r and _r8p and _r8p[0] else 0
            if _r8n >= MARKS8_DEAD_MIN_NOROUTES: _r8w = min(MARKS8_PASS_SEC * 2 ** _r8l, MARKS8_BACKOFF_MAX_SEC); _r8l += 1   # 1, 2, 4, 8 passes, cap 15 min
            else: _r8w = 0.0
            c.execute("INSERT OR REPLACE INTO marks8_dead(position_id,noroutes,level,next_ts) VALUES(?,?,?,?)",
                      (pid, _r8n, _r8l, (now() + dt.timedelta(seconds=max(_r8w - MARKS8_PASS_SEC / 2, 0))).isoformat()))   # half a pass of jitter slack
        if NOROUTE_ENABLED:   # r3 wiring (OFF): failure kind from diag (mark_valued fail_kind), proceeds from sell()'s return
            _noroute_step(c, p, mv, vols, nr_res)
            if p["qty_left_raw"] <= 0: continue
        if mv["status"] == "no_route": mv = _r9_borrow(c, p, mv, byid, _r9_mq); marks[pid] = mv   # r9: borrowed mark (sibling < 5 min, per-mint total-size quote)
        elif mv["status"] == "ok": c.execute("DELETE FROM marks9_borrowed WHERE position_id=?", (pid,))
        if mv.get("borrowed"):
            _bc = 100 * ((mv["usd"] / p["qty_left_raw"]) / (p["cost_usd"] / p["qty_raw"]) - 1)
            if _bc <= T.STOP_LOSS_PCT and sell(c, p, p["qty_left_raw"], f"stop {_bc:.1f}% (marca prestada #{mv['borrowed_from']})"): continue   # own quote executes the sale
        if mv["status"] != "ok": exit_pending(c, p, mv); continue          # exits only on a real quote; C1 log
        _exit_logic(c, p, mv, vols)
    if NOROUTE_ENABLED and nr_res:
        import noroute; noroute.cycle_summary(nr_res, hashlog.append)
    _r10_tot.clear(); _r10_mq.clear()   # r10: outside the mark loop every quote is own-size again
    for p in ops:      # C2: not quoted this pass -> exit checks on the cached mark; only exit_pending (rate-limited), no fills
        if p["id"] in qset: continue
        mv = mark_cached(c, p); marks[p["id"]] = mv; exit_pending(c, p, mv)
    plan = list(dict.fromkeys(plan))   # r11.1: re-queued legs appear once in the pass record
    marks_pass_record(c, marks, plan, _groups, _skipped, round(time.monotonic() - _t_pass, 3))
    for s in STRATS:
        inf = {}
        r, u, e = equity(c, s, {k: v for k, v in marks.items()}, inf)   # positions sold this cycle are closed -> not re-marked
        halt = risk_gate(c, s, marks, write_halt=True) if not kill else None   # marks1fix r2
        if not kill: u, e = u + _last_risk_add.get(s, 0.0), e + _last_risk_add.get(s, 0.0)   # r2: equity row = risk_eq (day_start/peak)   # C4: after this pass's quotes, before this row joins the day-start/peak history
        c.execute("INSERT INTO equity(ts,strategy,realized,unrealized,equity,marks_missing,marks_stale) VALUES(?,?,?,?,?,?,?)",
                  (now().isoformat(), s, r, u, e, inf["marks_missing"], inf["marks_stale"]))
        _monitor_halt[s] = halt
        stopped = os.path.exists(os.path.join(HERE, f"HALT_{s}"))   # drawdown level: strategy KILL until 069 reviews
        if halt and halt != _monitor_halt.get("_logged_" + s):
            hashlog.append("risk_halt", strategy=s, reason=halt, level="drawdown" if stopped else "daily" if "daily" in halt else "other",
                           action=T.DRAWDOWN_ACTION if stopped else T.DAILY_LOSS_ACTION, equity=round(e, 4))
        _monitor_halt["_logged_" + s] = halt
        if stopped and not kill:   # close everything still open (every pass until flat), highest value first, real quotes only
            _close_all(c, s, _t_pass, _dd_tried)   # r3: legs already tried earlier in this pass are skipped (one attempt per leg per pass)
    c.commit(); _last_monitor_done[0] = time.time()   # r2: LP checks only run while no monitor pass is due

# ---------------- exits1 r2/r3: fast exit detection (thread) + execution (main thread) ----------------
# r2 design (aud50 B1/B3): the thread ONLY detects and queues. It never quotes Jupiter, never writes paper.db and never touches the
# hash log; it reads paper.db read-only (mode=ro) and writes price_history.db. Every exit runs in the MAIN thread through the unchanged
# mark_valued + _exit_logic: (a) during the scanloop's inter-tick wait on its own short transaction, (b) inside a cycle at the existing
# safe points (before each source / token / strategy) on the CYCLE connection, i.e. in the cycle's own transaction (no commit added).
# No lock exists: entries never wait, cycle atomicity is unchanged, paper.db mtime (watchdog) only moves with main-thread writes.
import threading
FASTMON_ENABLED = True
FASTMON_EVERY_SEC = 15.0               # detection cadence for open legs (independent of the scan cycle)
FASTMON_TRIGGER_MARGIN_PCT = 3.0       # queue when the DexScreener estimate is within 3 points of stop / next TP / trailing level
FASTMON_QUEUE_TTL_SEC = 60.0           # a queued check older than this is dropped (the next tick re-queues it if still near)
FASTMON_MAX_QUOTES_PER_MIN = 12        # fast-exec quotes inside the shared Jupiter window (54/min keyed)
FASTMON_IDLE_MAX_PER_CALL = 4          # legs per inter-tick exec call
FASTMON_CYCLE_MAX_PER_CALL = 1         # legs per in-cycle exec call (stop triggers only; bounds the cycle delay to ~1 quote + 1 sell)
FASTMON_MIN_REQUOTE_SEC = 10.0         # a leg with a real quote attempt < 10 s ago (or after the queue time) is not re-quoted
FASTMON_HISTORY_EVERY_SEC = 60.0       # estimate row per leg at most every 60 s (or on a >= 2 point move)
FASTMON_DS_TIMEOUT_SEC = 5.0
FASTMON_DS_RESERVE = 10                # leave >= 10 of DEXSCREENER_MAX_CALLS_PER_MIN for the scan cycle
FASTMON_EXEC_DEADLINE_SEC = 12.0       # caps HTTP timeouts of a fast-exec call (via _pass_t0, main thread only)
FASTMON_STALL_SEC = 120.0              # main thread alerts if the thread made no tick for 120 s (or died)
FASTMON_MAX_RESTARTS_PER_HOUR = 3
FASTMON_STATS_EVERY_SEC = 600.0
SIMPLE_TP_VOL_DECAY = False            # simple_tp exits = stop/TP/trailing/24h (Jev's volume-decay exit NOT included)
PH_DB = os.path.join(HERE, "price_history.db")   # exits1 r2 (B1): history outside paper.db
PH_EST_FULL_HOURS = 72                 # M1: estimate rows kept in full for 72 h, then one per position per 10 min
PH_EST_KEEP_DAYS = 30                  # M1: estimate rows deleted after 30 days
PH_QUOTE_KEEP_DAYS = 180               # M1: real-quote rows deleted after 180 days
PH_SCHEMA = """CREATE TABLE IF NOT EXISTS position_price_history(id INTEGER PRIMARY KEY AUTOINCREMENT, position_id INTEGER NOT NULL, ts TEXT NOT NULL,
    price REAL, upnl_pct REAL, source TEXT, strategy TEXT, qty_left_raw INTEGER, peak_upnl_pct REAL, is_estimate INTEGER NOT NULL DEFAULT 0)"""
# is_estimate=1 / source 'dexscreener_est' (M4): DexScreener priceUsd (mid, highest-liquidity pair), upnl adjusted by the last quote impact.
# NOT a Jupiter mark: never use as an observed executable price. is_estimate=0: real Jupiter sell quote (quote_monitor / quote_fast).
_fm = {"queue": {}, "qlock": threading.Lock(), "event": threading.Event(), "thread": None, "last_tick": None, "flag": None, "restarts": [],
       "last_stats": 0.0, "last_prune": 0.0, "quotes": [], "hist_last": {}}
_fm_stats = {"ticks": 0, "ds_fail_chunks": 0, "ds_skip": 0, "queued": 0, "exec_calls": 0, "quotes": 0, "exits": 0, "jup_skips": 0, "dropped_ttl": 0,
             "hist_rows": 0, "hist_errors": 0, "hist_dropped": 0, "pruned": 0, "cyc_rollbacks": 0, "requeued_after_rollback": 0,
             "stale_with_fastexec": 0, "stale_caused_by_fastexec": 0, "cyc_exec_sec_total": 0.0, "errors": 0, "max_tick_sec": 0.0, "started": None}
FASTMON_DEAD_LONG_SEC = 3600.0         # r3 (C4): FASTMON_DEAD older than 1 h -> fastmon_dead_long (quote-history rows may be dropping)
_fm_cyc_t = {"spent": 0.0, "n": 0}     # r3: in-cycle fast-exec seconds / legs of the running cycle
_fm_cyc_pending = {}                   # r3 (C1): id(cycle conn) -> {"conn": c, "events": [(kind, fields)], "pids": [sold position ids]}
_FM_EVENTS = collections.deque(maxlen=1000)   # thread -> main thread hash-log events (the thread never appends itself)
_fm_S = requests.Session(); _fm_S.headers["User-Agent"] = "readonly-collector/0.2 fastmon"
def _ph_conn():
    c = sqlite3.connect(PH_DB, timeout=5); c.execute(PH_SCHEMA)
    c.execute("CREATE INDEX IF NOT EXISTS pph_pid_ts ON position_price_history(position_id, ts)"); return c
def _ph_flush():
    """thread: write buffered rows to price_history.db; on failure rows go back into the buffer."""
    rows = []
    while _PH_BUF: rows.append(_PH_BUF.popleft())
    if not rows: return 0
    try:
        c = _ph_conn()
        try:
            c.executemany("INSERT INTO position_price_history(position_id,ts,price,upnl_pct,source,strategy,qty_left_raw,peak_upnl_pct,is_estimate) VALUES(?,?,?,?,?,?,?,?,?)", rows); c.commit()
        finally: c.close()
        _fm_stats["hist_rows"] += len(rows); return len(rows)
    except sqlite3.Error as e:
        _fm_stats["hist_errors"] += 1; _PH_BUF.extendleft(reversed(rows)); print("price history flush failed:", type(e).__name__); return 0
def _ph_prune(now_dt=None):
    """M1 retention (thread, hourly): estimates full 72 h -> 1 per position per 10 min -> deleted after 30 d; quotes deleted after 180 d."""
    t = now_dt or now(); a = (t - dt.timedelta(hours=PH_EST_FULL_HOURS)).isoformat(); b = (t - dt.timedelta(days=PH_EST_KEEP_DAYS)).isoformat()
    q = (t - dt.timedelta(days=PH_QUOTE_KEEP_DAYS)).isoformat(); c = _ph_conn()
    try:
        n = c.execute("DELETE FROM position_price_history WHERE is_estimate=1 AND ts < ?", (b,)).rowcount
        n += c.execute("DELETE FROM position_price_history WHERE is_estimate=0 AND ts < ?", (q,)).rowcount
        n += c.execute("""DELETE FROM position_price_history WHERE is_estimate=1 AND ts < ? AND id NOT IN (SELECT MIN(id) FROM position_price_history
                          WHERE is_estimate=1 AND ts < ? GROUP BY position_id, substr(ts,1,15))""", (a, a)).rowcount
        c.commit(); _fm_stats["pruned"] += n; return n
    finally: c.close()
def _fm_prices(mints):
    """mint -> DexScreener priceUsd (highest-liquidity pair). M5: a failed/skipped 30-mint chunk only loses its own mints. Never sleeps."""
    out = {}
    for i in range(0, len(mints), 30):
        t = time.time()
        if len([x for x in list(_ds_calls) if t - x < 60]) >= T.DEXSCREENER_MAX_CALLS_PER_MIN - FASTMON_DS_RESERVE: _fm_stats["ds_skip"] += 1; continue
        _ds_calls.append(t)
        try:
            r = _fm_S.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(mints[i:i + 30]), timeout=FASTMON_DS_TIMEOUT_SEC)
            if r.status_code != 200: raise RuntimeError(f"HTTP {r.status_code}")
            liq = {}
            for p in r.json() or []:
                m = p["baseToken"]["address"]; lq = (p.get("liquidity") or {}).get("usd") or 0
                try: px = float(p.get("priceUsd"))
                except (TypeError, ValueError): continue
                if px > 0 and lq >= liq.get(m, -1): liq[m] = lq; out[m] = px
        except Exception as e:
            _fm_stats["ds_fail_chunks"] += 1; print("fastmon dexscreener chunk failed:", mask(type(e).__name__))
    return out
def _fm_triggers(p, est_chg, est_unit, age_h):
    m = FASTMON_TRIGGER_MARGIN_PCT; why = []
    if est_chg <= T.STOP_LOSS_PCT + m: why.append("stop")
    if p["strategy"] in ("simple", "simple_tp") and age_h >= T.SIMPLE_MAX_HOLD_HOURS: why.append("expiry")
    if p["strategy"] in ("jev", "simple_tp"):
        if p["tp_done"] < len(T.TAKE_PROFIT) and est_chg >= T.TAKE_PROFIT[p["tp_done"]][0] - m: why.append("take_profit")
        if p["tp_done"] >= 1 and p["peak_mark"] and est_unit <= p["peak_mark"] * (1 - T.TRAILING_STOP_PCT / 100) * (1 + m / 100): why.append("trailing")
    return why
def fm_tick():
    """Thread: one detection tick (read-only paper.db, DexScreener, queue, history buffer). Returns list of queued (id, why, est)."""
    t0 = time.monotonic(); _fm_stats["ticks"] += 1; out = []
    if os.path.exists(T.KILL_SWITCH_FILE): return out
    halted = {s for s in STRATS if os.path.exists(os.path.join(HERE, f"HALT_{s}"))}
    rc = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=2)   # read-only: never changes paper.db (mtime stays a main-thread signal)
    try:
        rc.row_factory = sqlite3.Row
        ops = [dict(r) for r in rc.execute("SELECT * FROM positions WHERE status='open'")]
        ops = [p for p in ops if p["strategy"] not in halted and p["qty_left_raw"] > 0 and p["qty_raw"] and p["cost_usd"]]
        pm = {r[0]: tuple(r)[1:] for r in rc.execute("SELECT position_id,last_ok_impact,last_attempt_ts,fail_since_ts FROM position_marks")}
    finally: rc.close()
    if not ops: return out
    px = _fm_prices(list(dict.fromkeys(p["mint"] for p in ops)))
    tn = now()
    for p in ops:
        pr = px.get(p["mint"]); dec = p.get("decimals")
        if pr is None or not isinstance(dec, int): continue
        imp, la, fail = pm.get(p["id"], (None, None, None))
        imp = p["entry_impact_pct"] if imp is None else imp
        est_unit = pr / 10 ** dec * (1 - max(min(imp or 0.0, 99.0), 0.0) / 100); entry_unit = p["cost_usd"] / p["qty_raw"]
        est_chg = 100 * (est_unit / entry_unit - 1); age_h = (tn - dt.datetime.fromisoformat(p["opened_ts"])).total_seconds() / 3600
        last = _fm["hist_last"].get(p["id"])
        if last is None or time.time() - last[0] >= FASTMON_HISTORY_EVERY_SEC or abs(est_chg - last[1]) >= 2.0:
            _ph_push(p, pr, est_chg, "dexscreener_est", 100 * (p["peak_mark"] / entry_unit - 1) if p["peak_mark"] else None, est=1)
            _fm["hist_last"][p["id"]] = (time.time(), est_chg)
        why = _fm_triggers(p, est_chg, est_unit, age_h)
        if not why or fail: continue   # no-route legs: the monitor's dead lane handles them
        if la and (tn - dt.datetime.fromisoformat(la)).total_seconds() < FASTMON_MIN_REQUOTE_SEC: continue
        with _fm["qlock"]: _fm["queue"][p["id"]] = (0 if "stop" in why else 1, round(est_chg, 2), why, time.time())
        out.append((p["id"], why, round(est_chg, 2)))
    if out: _fm_stats["queued"] += len(out); _fm["event"].set()
    _fm_stats["max_tick_sec"] = max(_fm_stats["max_tick_sec"], round(time.monotonic() - t0, 2))
    return out
def _fastmon_run():
    while True:
        t0 = time.time()
        try: fm_tick()
        except Exception as e:   # M5: a failed tick never ends the thread
            _fm_stats["errors"] += 1; print("fastmon error:", mask(repr(e))); _FM_EVENTS.append(("loop_error", {"error": mask(repr(e))[:300], "where": "exits1 fastmon tick"}))
        try:
            _ph_flush()
            if time.time() - _fm["last_prune"] >= 3600: _fm["last_prune"] = time.time(); _ph_prune()
        except Exception as e: _fm_stats["hist_errors"] += 1; print("fastmon history error:", type(e).__name__)
        _fm["last_tick"] = time.time()
        time.sleep(max(1.0, FASTMON_EVERY_SEC - (time.time() - t0)))
def fastmon_start():
    if not FASTMON_ENABLED: return None
    _fm_stats["started"] = _fm_stats["started"] or now().isoformat(); _fm["last_tick"] = time.time()
    th = threading.Thread(target=_fastmon_run, daemon=True, name="exits1_fastmon"); th.start(); _fm["thread"] = th
    hashlog.append("fastmon_start", every_sec=FASTMON_EVERY_SEC, margin_pct=FASTMON_TRIGGER_MARGIN_PCT, max_quotes_per_min=FASTMON_MAX_QUOTES_PER_MIN,
                   design="thread detects+queues (DexScreener, read-only paper.db, price_history.db); main thread quotes+exits", history_db=os.path.basename(PH_DB))
    return th
def fast_exec(c=None, max_n=FASTMON_IDLE_MAX_PER_CALL, stop_only=False):
    """MAIN thread: execute queued checks with a real Jupiter quote + the unchanged _exit_logic. c=None -> own connection + commit;
    c given (cycle) -> writes join the caller's transaction (no commit here). Returns number of legs quoted."""
    if not FASTMON_ENABLED or _in_monitor[0] or _pass_t0[0] is not None or threading.current_thread() is not threading.main_thread(): return 0
    t = time.time(); _fm["quotes"][:] = [x for x in _fm["quotes"] if t - x < 60]
    room = min(max_n, FASTMON_MAX_QUOTES_PER_MIN - len(_fm["quotes"]))
    with _fm["qlock"]:
        for k in [k for k, v in _fm["queue"].items() if t - v[3] > FASTMON_QUEUE_TTL_SEC]: _fm["queue"].pop(k); _fm_stats["dropped_ttl"] += 1
        cand = sorted(((v[0], v[1], k, v) for k, v in _fm["queue"].items() if not stop_only or v[0] == 0))[:max(room, 0)]
        for *_, k, _v in cand: _fm["queue"].pop(k, None)
    if not cand or os.path.exists(T.KILL_SWITCH_FILE): return 0
    _fm_stats["exec_calls"] += 1; own = c is None; n = 0; old_src = _hist_src[0]; evs = []; sold_ids = []; t_exec = time.monotonic()
    if own: c = db()
    buf = None; real_append = hashlog.append
    if not own:   # r3 (C1): every hash-log event of an in-cycle exec (outcome, mark_failed, fastmon_exec, ...) waits for the cycle's commit
        buf = []; hashlog.append = lambda kind, **f: buf.append((kind, dict(f, event_ts=now().isoformat())))
    try:
        _pass_t0[0] = time.monotonic() - (MARKS2_PASS_DEADLINE_SEC - FASTMON_EXEC_DEADLINE_SEC); _hist_src[0] = "quote_fast"; t1 = time.monotonic()
        for i, (_, est, pid, (_, _, why, qts)) in enumerate(cand):
            if time.monotonic() - t1 >= FASTMON_EXEC_DEADLINE_SEC: break
            ps = [p for p in open_positions(c) if p["id"] == pid]
            if not ps or ps[0]["qty_left_raw"] <= 0 or os.path.exists(os.path.join(HERE, f"HALT_{ps[0]['strategy']}")): continue
            p = ps[0]; la = _mark_info(c, p)[0]
            if la and dt.datetime.fromisoformat(la).timestamp() >= qts: continue   # monitor already re-quoted it after the queue time
            if _jup_avail("exit", need=2):
                _fm_stats["jup_skips"] += 1
                with _fm["qlock"]:
                    for _, e2, pid2, v2 in cand[i:]: _fm["queue"].setdefault(pid2, v2)   # back to the queue (TTL applies)
                break
            q0 = p["qty_left_raw"]; _fm["quotes"].append(time.time()); _fm_stats["quotes"] += 1; n += 1
            mv = mark_valued(c, p)
            if mv["status"] == "ok": _exit_logic(c, p, mv, {})   # same exit rules as the monitor (vols empty: no volume-decay exit here)
            sold = q0 - p["qty_left_raw"]; _fm_stats["exits"] += bool(sold)
            if sold: sold_ids.append(pid)
            evs.append(dict(position_id=pid, strategy=p["strategy"], triggers=why, est_chg_pct=est, mark_status=mv["status"], in_cycle=not own,
                            quote_chg_pct=round(100 * ((mv["usd"] / q0) / (p["cost_usd"] / p["qty_raw"]) - 1), 2) if mv["status"] == "ok" and q0 else None,
                            sold_qty_raw=sold, queued_sec=round(time.time() - qts, 1), since_last_monitor_sec=round(time.time() - _last_monitor_done[0], 1)))
        if own: c.commit()
    finally:
        _pass_t0[0] = None; _hist_src[0] = old_src
        if buf is not None:
            hashlog.append = real_append
            for e in evs: buf.append(("fastmon_exec", dict(e, event_ts=now().isoformat())))
            if buf or sold_ids:
                pend = _fm_cyc_pending.setdefault(id(c), {"conn": c, "events": [], "pids": []})
                pend["events"] += buf; pend["pids"] += sold_ids
            dtx = time.monotonic() - t_exec; _fm_cyc_t["spent"] += dtx; _fm_cyc_t["n"] += n; _fm_stats["cyc_exec_sec_total"] += dtx
        if own: _close_db(c, "fast_exec failed")
    if own:
        for e in evs: hashlog.append("fastmon_exec", **e)   # own conn: after its commit
    return n
def _fm_cyc_commit(conn, pend=None):
    """r3 (C1): the cycle transaction holding in-cycle fast exits committed -> append their deferred events now (in order).
    r4 (aud52 F1): the pending entry is detached by _Conn.commit right after super().commit(); writes go through _hl_write."""
    if pend is None: pend = _fm_cyc_pending.pop(id(conn), None)
    if not pend or pend["conn"] is not conn: return 0
    _hl_write(pend["events"], hashlog.append, "fastmon_cycle_commit")
    return len(pend["events"])
def _fm_cyc_rollback(conn, why):
    """r3 (C1/C2): the cycle rolled back -> its in-cycle sells no longer exist: their events are NOT logged as such; one
    fastmon_exec_rolled_back record keeps them under 'discarded' (like db_rollback), and the sold legs are re-queued at once."""
    pend = _fm_cyc_pending.pop(id(conn), None)
    if not pend or pend["conn"] is not conn: return []
    _fm_stats["cyc_rollbacks"] += 1; t = time.time(); pids = list(dict.fromkeys(pend["pids"]))
    with _fm["qlock"]:
        for pid in pids: _fm["queue"][pid] = (0, -999.0, ["stop", "requeue_after_rollback"], t)
    if pids: _fm_stats["requeued_after_rollback"] += len(pids); _fm["event"].set()
    hashlog.append("fastmon_exec_rolled_back", reason=mask(str(why))[:300], position_ids=pids, n_discarded=len(pend["events"]),
                   discarded=[{"kind": k, **f} for k, f in pend["events"]], requeued=pids)
    return pids
def _fm_stale_note(age, n_survivors):
    """r3: cycle_stale while in-cycle fast exits ran in this cycle -> log how much they contributed; 'caused' = stale only because of them."""
    sp = _fm_cyc_t["spent"]
    if sp <= 0: return None
    caused = age - sp <= T.MAX_DATA_AGE_SEC; _fm_stats["stale_with_fastexec"] += 1; _fm_stats["stale_caused_by_fastexec"] += caused
    hashlog.append("cycle_stale_fastmon", data_age_sec=round(age, 2), fastexec_sec=round(sp, 2), fastexec_legs=_fm_cyc_t["n"],
                   caused_by_fastexec=caused, survivors_lost=n_survivors, max_data_age_sec=T.MAX_DATA_AGE_SEC)
    return caused
def _fm_exec_safe(c):
    """Cycle hook: stop triggers only, at most FASTMON_CYCLE_MAX_PER_CALL leg, on the cycle connection. Non-DB errors never break the cycle."""
    try: return fast_exec(c, max_n=FASTMON_CYCLE_MAX_PER_CALL, stop_only=True)
    except (hashlog.HashlogCorrupt, sqlite3.Error): raise   # DB errors belong to the cycle's own handling (rollback + close)
    except Exception as e:
        print("fast_exec (cycle) failed:", mask(repr(e))); hashlog.append("loop_error", error=mask(repr(e))[:300], where="exits1 fast_exec cycle"); return 0
def _fm_idle(sec):
    """scanloop inter-tick wait: serve queued checks (own short transaction) while waiting."""
    end = time.time() + max(0.0, sec)
    while True:
        if _fm["queue"]:
            try: fast_exec(None)
            except hashlog.HashlogCorrupt: raise
            except Exception as e: print("fast_exec (idle) failed:", mask(repr(e))); hashlog.append("loop_error", error=mask(repr(e))[:300], where="exits1 fast_exec idle")
        left = end - time.time()
        if left <= 0: return
        _fm["event"].wait(min(left, 1.0)); _fm["event"].clear()
def _fm_watch():
    """MAIN thread, every scanloop tick (M2): flush thread events; thread dead or no tick for FASTMON_STALL_SEC -> fastmon_dead event + FASTMON_DEAD
    flag file (once per episode) and a restart (max FASTMON_MAX_RESTARTS_PER_HOUR); healthy again -> flag removed + fastmon_recovered."""
    while _FM_EVENTS: k, f = _FM_EVENTS.popleft(); hashlog.append(k, **f)
    _hl_check()   # r6 (H3): periodic cap/age check of _hl_unwritten; _fm_watch runs in the MAIN thread (scanloop), never the fast thread
    if not FASTMON_ENABLED or _fm["thread"] is None: return None
    t = time.time(); alive = _fm["thread"].is_alive(); age = t - (_fm["last_tick"] or t)
    bad = None if alive and age < FASTMON_STALL_SEC else ("thread dead" if not alive else f"no tick for {age:.0f}s")
    flag = os.path.join(HERE, "FASTMON_DEAD")
    if bad and _fm["flag"] is not None and not _fm.get("long_alerted") and t - _fm.get("flag_since", t) >= FASTMON_DEAD_LONG_SEC:
        _fm["long_alerted"] = True   # r3 (C4): history buffer (maxlen 50000) may be dropping quote rows silently
        hashlog.append("fastmon_dead_long", reason=bad, down_sec=round(t - _fm["flag_since"]), history_buffer=len(_PH_BUF),
                       history_buffer_max=_PH_BUF.maxlen, hist_dropped=_fm_stats["hist_dropped"])
        try:
            with open(flag, "a") as fh: fh.write(f"{now().isoformat()} DOWN > {FASTMON_DEAD_LONG_SEC/3600:g} h: price-history rows may be dropped (dropped so far {_fm_stats['hist_dropped']})\n")
        except OSError: pass
    if bad and _fm["flag"] is None:
        _fm["flag_since"] = t; _fm["long_alerted"] = False
        _fm["flag"] = bad; hashlog.append("fastmon_dead", reason=bad, stats=dict(_fm_stats))
        try:
            with open(flag, "w") as fh: fh.write(f"{now().isoformat()}\n{bad}\nfast exit thread down; monitor_pass still runs every {T.MONITOR_EVERY_SEC}s\n")
        except OSError as e: print("FASTMON_DEAD write failed:", type(e).__name__)
    if bad and not alive:
        _fm["restarts"][:] = [x for x in _fm["restarts"] if t - x < 3600]
        if len(_fm["restarts"]) < FASTMON_MAX_RESTARTS_PER_HOUR: _fm["restarts"].append(t); fastmon_start()
    if not bad and _fm["flag"] is not None:
        hashlog.append("fastmon_recovered", was=_fm["flag"]); _fm["flag"] = None
        try: os.remove(flag)
        except FileNotFoundError: pass
    if t - _fm["last_stats"] >= FASTMON_STATS_EVERY_SEC:
        _fm["last_stats"] = t; hashlog.append("fastmon_stats", queue=len(_fm["queue"]), history_buffer=len(_PH_BUF), **_fm_stats)
    return bad

# ---- exits1 r2 (B2): simple_tp = paired mirror of every simple entry ----
_pair_skips = {}
def _pair_skip(sid, mint, reason):
    _pair_skips[reason] = _pair_skips.get(reason, 0) + 1
    hashlog.append("pair_skip", strategy="simple_tp", paired_with=sid, mint=mint, reason=reason)
def _pair_entry(c, sid, cand, pyr):
    """Called inside simple's try_entry, same connection/transaction, right after the simple fill: the simple_tp leg gets the SAME
    quote (qty_raw, cost/ticket, impact, fee), the SAME opened_ts and the same lane/decimals; ticket = simple's ticket (no resizing).
    simple_tp's own risk state can only SKIP the pair (logged pair_skip, counted in pair_divergence), never change it. Never raises
    (except HashlogCorrupt); a DB error rolls back only the pair (SAVEPOINT)."""
    try:
        c.row_factory = sqlite3.Row; s = dict(c.execute("SELECT * FROM positions WHERE id=?", (sid,)).fetchone()); c.row_factory = None
        st = "simple_tp"; ticket = s["cost_usd"]; fee = s["entry_fee_usd"]; mint = s["mint"]
        halt = _monitor_halt.get(st) or risk_gate(c, st)
        if halt: return _pair_skip(sid, mint, f"risk:{halt}")
        bl = monitor_backlog(c, exclude=())
        if bl: return _pair_skip(sid, mint, "monitor_backlog")
        op = open_positions(c, st)
        if T.MAX_OPEN_POSITIONS is not None and len(op) >= T.MAX_OPEN_POSITIONS: return _pair_skip(sid, mint, "position_limit")
        base_tp = None
        if s.get("pyramid_of"):   # simple add-on -> mirror onto the simple_tp leg paired with simple's base
            b = [p for p in op if p.get("paired_with") == s["pyramid_of"]]
            if not b: return _pair_skip(sid, mint, "pair_base_not_open")
            if any(p.get("pyramid_of") == b[0]["id"] for p in op): return _pair_skip(sid, mint, "pair_addon_open")
            base_tp = b[0]["id"]
        elif any(p["mint"] == mint for p in op): return _pair_skip(sid, mint, "pair_mint_held")   # divergent state (e.g. simple already exited)
        r, u, eq = equity(c, st); oc = open_cost(c, st)
        if oc + ticket > eq * T.MAX_EXPOSURE_PCT: return _pair_skip(sid, mint, "exposure")
        if cash(c, st) - ticket - fee < 0.01: return _pair_skip(sid, mint, "cash")
        if mint_open_cost(c, st, mint) + ticket > eq * T.MAX_TICKET_PCT_BANK / 100 + 0.01: return _pair_skip(sid, mint, "bank_cap_mint")
        h = hashlog.append("decision", strategy=st, mint=mint, pool=s["pool"], action="enter", lane=s["lane"], rule="simple_tp_paired", paired_with=sid,
                           paired_decision_hash=s["decision_hash"], size_usd=ticket, qty_raw=s["qty_raw"], entry_impact_pct=s["entry_impact_pct"],
                           opened_ts=s["opened_ts"], pyramid_of=base_tp, state=cand.get("state"), thresholds_sha=thresholds_sha())
        c.execute("SAVEPOINT exits1_pair")
        try:
            cur = c.execute("""INSERT INTO positions(strategy,mint,pool,symbol,opened_ts,qty_raw,qty_left_raw,cost_usd,entry_fee_usd,entry_impact_pct,peak_mark,decision_hash,lane,decimals,pyramid_of,paired_with)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (st, mint, s["pool"], s["symbol"], s["opened_ts"], s["qty_raw"], s["qty_raw"], ticket, fee,
                s["entry_impact_pct"], s["peak_mark"], h, s["lane"], s["decimals"], base_tp, sid))
            c.execute("INSERT INTO fills(position_id,ts,side,qty_raw,usd,price_impact_pct,fee_usd,reason,lane) VALUES(?,?,?,?,?,?,?,?,?)",
                      (cur.lastrowid, s["opened_ts"], "buy", s["qty_raw"], ticket, s["entry_impact_pct"], fee, "entry (paired)", s["lane"]))
            c.execute("RELEASE exits1_pair")
        except sqlite3.Error as e:
            c.execute("ROLLBACK TO exits1_pair"); c.execute("RELEASE exits1_pair"); return _pair_skip(sid, mint, f"db_error:{type(e).__name__}")
        print(f"  PAPER ENTRY [simple_tp] #{cur.lastrowid} paired with #{sid} ${ticket:.2f}")
        return cur.lastrowid
    except hashlog.HashlogCorrupt: raise
    except Exception as e:
        try: _pair_skip(sid, cand.get("mint"), f"error:{type(e).__name__}")
        except hashlog.HashlogCorrupt: raise
        except Exception: pass
        return None
def pair_divergence(c, since=None):
    """B2 metric: pairing rate, skip reasons, entry-price identity, realized PnL simple vs simple_tp on closed pairs."""
    c.row_factory = sqlite3.Row
    tp = [dict(r) for r in c.execute("SELECT * FROM positions WHERE strategy='simple_tp' AND paired_with IS NOT NULL")]
    since = since or (min(p["opened_ts"] for p in tp) if tp else None)
    sl = [dict(r) for r in c.execute("SELECT * FROM positions WHERE strategy='simple' AND opened_ts >= ?", (since,))] if since else []
    c.row_factory = None
    sb = {p["id"]: p for p in sl}; paired = {p["paired_with"]: p for p in tp}
    def pnl(p): return c.execute("SELECT COALESCE(SUM(usd - fee_usd),0) FROM fills WHERE position_id=? AND side='sell'", (p["id"],)).fetchone()[0] - p["cost_usd"] - p["entry_fee_usd"]
    closed = [(sb[k], v) for k, v in paired.items() if k in sb and sb[k]["status"] == "closed" and v["status"] == "closed"]
    px = [abs(v["cost_usd"] / v["qty_raw"] - sb[k]["cost_usd"] / sb[k]["qty_raw"]) for k, v in paired.items() if k in sb]
    ps, pt = sum(pnl(a) for a, _ in closed), sum(pnl(b) for _, b in closed)
    def mtm(p): return pnl(p) + (mark_cached(c, p)["usd"] if p["status"] == "open" else 0.0)   # r3 (C3): realized + cached mark (TTL rule: stale/no mark -> $0)
    opn = [(sb[k], v) for k, v in paired.items() if k in sb and (sb[k]["status"] == "open" or v["status"] == "open")]
    ms, mt = sum(mtm(a) for a, _ in opn), sum(mtm(b) for _, b in opn)
    return dict(since=since, n_simple=len(sl), n_paired=sum(1 for k in paired if k in sb), pair_rate=round(sum(1 for k in paired if k in sb) / len(sl), 4) if sl else None,
                skips_this_process=dict(_pair_skips), entry_price_max_abs_diff=max(px) if px else None,
                same_ts=all(v["opened_ts"] == sb[k]["opened_ts"] for k, v in paired.items() if k in sb),
                n_closed_pairs=len(closed), pnl_simple=round(ps, 4), pnl_simple_tp=round(pt, 4), pnl_diff=round(pt - ps, 4),
                n_open_tp=sum(1 for p in tp if p["status"] == "open"),
                n_open_pairs=len(opn), mtm_pnl_simple_open_pairs=round(ms, 4), mtm_pnl_simple_tp_open_pairs=round(mt, 4), mtm_diff_open_pairs=round(mt - ms, 4),
                total_pnl_diff_all_pairs=round((pt + mt) - (ps + ms), 4),
                n_simple_base=sum(1 for p in sl if not p.get("pyramid_of")), n_simple_addons=sum(1 for p in sl if p.get("pyramid_of")),
                limits="entry_price_max_abs_diff/same_ts are 0/True by construction (sanity only); skips_this_process resets on restart (pair_skip events in the "
                       "hash log are the durable count); closed-pair PnL is biased toward fast exits (simple_tp closes earlier) -> use total_pnl_diff_all_pairs "
                       "(closed + mark-to-market of open pairs; cached marks, $0 past TTL); n_simple includes add-ons (see n_simple_base/n_simple_addons)")
_pair_last = [0.0]
def pair_stats_maybe(every=3600.0):
    if time.time() - _pair_last[0] < every: return None
    _pair_last[0] = time.time(); c = db()
    try: d = pair_divergence(c)
    finally: _close_db(c)
    hashlog.append("pair_divergence", **d); return d

# ---------------- cycle ----------------
_cycle_conn = [None]
# cyclebudget1: scanloop cycle wall-clock budget + mid-cycle monitor passes (a slow scan tick no longer starves stops/marks or the watchdog)
CYCLE_BUDGET_SEC = 120.0
_in_monitor = [False]   # re-entrancy guard for monitor_pass()
_cb_stats = {"budget_stops": 0, "mid_monitors": 0}
def _cb_over(t0):
    return time.time() - t0 >= CYCLE_BUDGET_SEC
def _cb_mid_monitor(c):
    """cyclebudget1: monitor pass due inside a scanloop cycle -> commit the cycle's writes (frees the write lock), run monitor_pass()."""
    if _in_monitor[0] or time.time() - _last_monitor_done[0] < T.MONITOR_EVERY_SEC: return False
    if c is not None and c.in_transaction: c.commit()
    _cb_stats["mid_monitors"] += 1
    try: monitor_pass()
    except hashlog.HashlogCorrupt: raise
    except Exception as e: print("mid-cycle monitor failed:", e); hashlog.append("loop_error", error=mask(repr(e)), where="cyclebudget1 mid-cycle monitor")
    return True
def cycle(*a, **k):
    """dblock1: one connection per cycle, ALWAYS rolled back if needed and closed (also on exceptions). Cycle events are NOT
    deferred: try_entry stores the decision hash returned by hashlog.append in positions.decision_hash."""
    global _jev_db
    try: return _cycle(*a, **k)
    finally:
        c, _cycle_conn[0] = _cycle_conn[0], None
        if _jev_db is c: _jev_db = None
        _close_db(c, "cycle exception")
        if c is not None: _fm_cyc_rollback(c, "cycle closed without commit")   # exits1 r3: leftovers (should be none) never stay pending

def monitor_pass(kill=None):
    """dblock1 r2: monitor + baseline row on its own connection. Hash-log events of the pass are written only after the commit
    that persists them; on any exception the transaction is rolled back and the events go into one db_rollback record."""
    if _in_monitor[0]:   # cyclebudget1: never nested
        print("monitor_pass: re-entry skipped"); hashlog.append("monitor_reentry_skipped", note="cyclebudget1 guard"); return
    _in_monitor[0] = True
    try: mc = db(); _defer.update(buf=[], real=hashlog.append); hashlog.append = _defer_append
    except BaseException:   # cyclebudget1 r2 (aud47): db()/defer setup failed -> guard must not stay set
        if _defer.get("real") is not None: hashlog.append = _defer["real"]; _defer.update(buf=None, real=None)
        _in_monitor[0] = False; raise
    try:
        monitor(mc, kill=os.path.exists(T.KILL_SWITCH_FILE) if kill is None else kill)
        try:
            b, s = spot("BTC"), spot("SOL"); f0 = mc.execute("SELECT btc_usd, sol_usd FROM baselines LIMIT 1").fetchone() or (b, s)
            mc.execute("INSERT INTO baselines VALUES(?,?,?,?,?)", (now().isoformat(), b, s, T.PAPER_BANKROLL_USD * b / f0[0], T.PAPER_BANKROLL_USD * s / f0[1])); mc.commit()
        except Exception as e:   # as before: a baseline failure never fails the pass (monitor() already committed its own writes)
            print("baseline fetch failed:", e)
            if mc.in_transaction: mc._why = f"baseline failed: {type(e).__name__}"; mc.rollback()
        _defer_flush()       # events of a pass without DB writes after the last commit
    except BaseException as e:
        _close_db(mc, f"monitor pass failed: {type(e).__name__}: {e}"); mc = None; raise
    finally:
        hashlog.append = _defer["real"]
        if _defer["buf"]: _defer_discard("monitor pass ended with unflushed events")
        _defer.update(buf=None, real=None); _close_db(mc)
        _in_monitor[0] = False
        try: _quote_stale_check()   # combo1 r2
        except OSError as e: print("quote_stale check failed:", type(e).__name__)

def _monitor_only():
    monitor_pass()

def _cycle(judge=None, fetch=None, rpc=None, sources=None, do_monitor=True, seen=None, max_fallbacks=None):
    """sources: GeckoTerminal paths for this call (default all SCAN_SOURCES). seen: dict pool->ts for dedupe (scanloop)."""
    global _jev_db
    _fm_cyc_t["spent"] = 0.0; _fm_cyc_t["n"] = 0   # exits1 r3: in-cycle fast-exec time of THIS cycle (cycle_stale attribution)
    _cb_t0 = time.time(); _cb_on = not do_monitor   # cyclebudget1: only the scanloop path (monitor runs outside the cycle)
    judge = judge or Judge(); c = _cycle_conn[0] = db(); ts = now().isoformat(); _jev_db = c   # q4v1: Q4b reads holder_snapshots
    ph, err = phase()
    if err: print("ABORT:", err); hashlog.append("cycle_abort", reason=err); return
    rpc = rpc or RPC()
    if fetch is None:
        url, why = rpc.freshness()
        if not url: print("Skipping cycle:", why); hashlog.append("cycle_skip", reason=why); return
        pools, _addr = [], set()   # migrated PumpSwap pools + trending pools (no bonding-curve new_pools feed)
        extra = []
        for src in (T.SCAN_SOURCES if sources is None else sources):
            if _cb_on: _fm_exec_safe(c)   # exits1 r2
            if _cb_on and _cb_over(_cb_t0): _cb_stats["budget_stops"] += 1; print("cycle budget: sources cut"); hashlog.append("cycle_budget_stop", budget_sec=CYCLE_BUDGET_SEC, stage="sources", source=src); break
            if src.startswith("ds:"):
                try: extra += ds_source(src[3:])
                except hashlog.HashlogCorrupt: raise
                except Exception as e: print("dexscreener source failed:", src, e)
                continue
            try: batch = gecko(src)["data"]
            except hashlog.HashlogCorrupt: raise
            except Exception as e: print("gecko source failed:", src, e); batch = []
            for p in batch:
                if p["attributes"]["address"] not in _addr: _addr.add(p["attributes"]["address"]); pools.append(p)
        items = [(p["attributes"]["address"], p["relationships"]["base_token"]["data"]["id"].split("_", 1)[1], p["attributes"]["name"]) for p in pools]
        if extra:   # DS supplement: resolve mint -> best pair (pool) via the /tokens batch below
            dsx = dexscreener(list({m for _, m, _ in extra}))
            known = {x[0] for x in items}
            for pool, m, nm in extra:
                pr = dsx.get(m) or {}
                pool = pool or pr.get("pairAddress")
                if pool and pool not in known: known.add(pool); items.append((pool, m, nm))
        if seen is not None:   # dedupe: skip pools evaluated within SCAN_DEDUPE_TTL_SEC
            tnow = time.time()
            for k in [k for k, v in seen.items() if tnow > v]: del seen[k]   # value = expiry time
            items = [x for x in items if x[0] not in seen]
        have = {x[0] for x in items}   # gecko429-r1: due liquidity_unknown pools come back even if their source page is not in this tick
        items = [(p, m, n) for p, m, n in rq_due(c, time.time()) if p not in have] + items
        if seen is not None and not items: return {"scanned": 0, "survivors": 0, "counts": {}}
        gk = {p["attributes"]["address"]: (p["attributes"], ((p.get("relationships") or {}).get("dex") or {}).get("data", {}).get("id")) for p in pools}
        raw = dexscreener(list({m for _, m, _ in items}))
        ds = {}; liq_unknown = {}   # gecko429-r1: pool -> why liquidity could not be determined
        for pool, mint, _ in items:   # requeued pools are first in items -> they get the fallback budget first
            dsp = raw.get(mint, {}); ga, gd = gk.get(pool, ({}, None))
            if dsp and ((dsp.get("liquidity") or {}).get("usd") is None or (dsp.get("volume") or {}).get("h24") is None) \
               and ga.get("reserve_in_usd") is None:
                if max_fallbacks is not None and max_fallbacks <= 0: liq_unknown[pool] = "budget_skipped"   # not fetched this tick
                else:
                    ga, gd, fk = gecko_pool(pool)
                    if fk in UNKNOWN_KINDS: liq_unknown[pool] = fk
                    if max_fallbacks is not None: max_fallbacks -= 1
            ds[mint] = merge_sources(dsp, ga, gd)
    else: items, ds = fetch()
    t_data = time.time()
    if do_monitor: monitor(c, kill=os.path.exists(T.KILL_SWITCH_FILE))
    lp_budget_reset()   # r2: LP budget starts AFTER this tick's stops/exits
    survivors, counts = [], {}
    err0 = _source_errors[0]
    if fetch is not None: liq_unknown = {}
    for _cb_i, (pool, mint, name) in enumerate(items):
        if _cb_on:
            _fm_exec_safe(c)   # exits1 r2: queued fast exits, cycle connection, no commit
            _cb_mid_monitor(c)
            if _cb_over(_cb_t0):   # leftovers: not in seen / still in requeue -> picked up by later ticks
                _cb_stats["budget_stops"] += 1
                hashlog.append("cycle_budget_stop", budget_sec=CYCLE_BUDGET_SEC, evaluated=_cb_i, left=len(items) - _cb_i)
                break
        if pool in liq_unknown and (ds.get(mint, {}).get("liquidity") or {}).get("usd") is None:   # gecko429-r1: requeue, not a reject
            rq = rq_get(c, pool) or {"tries": 0}; tries = rq["tries"] + 1; kind = liq_unknown[pool]
            if tries <= LIQ_UNKNOWN_MAX_RETRIES:
                delay = max(LIQ_UNKNOWN_RETRY_SEC, _gecko_backoff["until"] - time.time())
                rq_put(c, pool, mint, name, time.time() + delay, tries, kind)
                if seen is not None: seen[pool] = time.time() + delay
                counts["liquidity_unknown"] = counts.get("liquidity_unknown", 0) + 1
                hashlog.append("liquidity_unknown", pool=pool, mint=mint, name=name, fail_kind=kind, tries=tries, max_tries=LIQ_UNKNOWN_MAX_RETRIES,
                               retry_in_sec=round(delay, 1), note="liquidity undeterminable (GeckoTerminal unavailable / not fetched); requeued, not rejected")
                continue
            rq_pop(c, pool)
            reject(c, "filter", pool, mint, name, "liquidity", f"liquidity undeterminable after {LIQ_UNKNOWN_MAX_RETRIES} retries (last: {kind})",
                   {"liquidity": None, "liquidity_unknown_kind": kind, "tries": tries - 1})
            counts["liquidity"] = counts.get("liquidity", 0) + 1
            if seen is not None: seen[pool] = time.time() + T.SCAN_DEDUPE_TTL_SEC
            continue
        rq_pop(c, pool)
        why, st = hard_filter(rpc, pool, mint, name, ds.get(mint, {}))
        st["data_as_of_ts"] = time.time()   # q4b: per candidate, AFTER hard_filter (its RPC/top10/copycat calls ran before this)
        write_holder_snapshot(c, mint, st, st["data_as_of_ts"], holders_src="scan_top10_only")   # nodata1: top10-only by design, not a failed read
        if seen is not None:   # dedupe only after evaluation; incomplete data / source_error -> short TTL (<= 60 s)
            incomplete = _source_errors[0] != err0 or st.get("liquidity") is None or st.get("volume_h24") is None \
                or any(r == "rpc" or "check failed" in m or "missing" in m for r, m in why)
            seen[pool] = time.time() + (T.SCAN_DEDUPE_INCOMPLETE_TTL_SEC if incomplete else T.SCAN_DEDUPE_TTL_SEC)
        c.execute("INSERT INTO scans VALUES(?,?,?,?,?,?,?,?,?,?)", (ts, pool, mint, name, st.get("dex"), st.get("liquidity"),
                  st.get("volume_h24"), None, int(not why), json.dumps(st, default=str)))
        if why:
            for rule, _ in why: counts[rule] = counts.get(rule, 0) + 1
            reject(c, "filter", pool, mint, name, ",".join(r for r, _ in why), "; ".join(r for _, r in why), st)
        else: survivors.append({"pool": pool, "mint": mint, "name": name, "state": st, "lane": "meme"})
    if time.time() - t_data > T.MAX_DATA_AGE_SEC and fetch is None:
        print("data went stale during filtering; no entries"); hashlog.append("cycle_stale", n=len(survivors)); _fm_stale_note(time.time() - t_data, len(survivors)); survivors = []
    ctl = control.load()   # re-read every cycle; invalid -> no entries (exits unaffected: monitor() ran above)
    rf = ctl["risk_factor"] if ctl else 0.0
    backlog = monitor_backlog(c)   # v2 C2
    for strat in STRATS:
        if strat == "simple_tp": continue   # exits1 r2 (B2): entries only as the pair of a simple fill (_pair_entry)
        _fm_exec_safe(c)   # exits1 r2: queued fast exits on the CYCLE connection (same transaction, no extra commit)
        r, u, eq = equity(c, strat); halt = _monitor_halt.get(strat) or risk_gate(c, strat); jev_ok = []   # C4: last monitor pass (fresh quotes) or cached
        n_new = 0; lim = T.MAX_ENTRIES_PER_CYCLE
        n_open = lambda: c.execute("SELECT COUNT(*) FROM positions WHERE strategy=?", (strat,)).fetchone()[0]
        for cand in survivors:
            lb = control.entry_block(ctl, cand["lane"])
            if lb: reject(c, strat, cand["pool"], cand["mint"], cand["name"], "lane_paused", lb, cand["state"]); continue
            if halt: reject(c, strat, cand["pool"], cand["mint"], cand["name"], "risk_limit", halt, cand["state"]); continue
            if backlog: reject(c, strat, cand["pool"], cand["mint"], cand["name"], "monitor_backlog", backlog, cand["state"]); continue
            if strat == "simple" and lim is not None and n_new >= lim:
                reject(c, strat, cand["pool"], cand["mint"], cand["name"], "cycle_entry_limit", f"max {lim} new entries per cycle", cand["state"]); continue
            blocked = can_open(c, strat, cand["mint"])
            if blocked: reject(c, strat, cand["pool"], cand["mint"], cand["name"], "position_limit", blocked, cand["state"]); continue
            liq = cand["state"]["liquidity"]; cash_avail = cash(c, strat)   # re-read per candidate (entries this cycle spend cash)
            oc = open_cost(c, strat)   # week1d exposure cap, re-read per candidate
            if eq <= 0 or int((eq * T.MAX_EXPOSURE_PCT - oc) * 100) / 100 < 0.01:
                reject(c, strat, cand["pool"], cand["mint"], cand["name"], "exposure",
                       f"open cost ${oc:.2f} >= {T.MAX_EXPOSURE_PCT:.0%} of equity ${eq:.2f}", cand["state"]); continue
            if cash_avail - (T.PRIORITY_FEE_USD + T.NETWORK_FEE_USD) < 0.01:
                reject(c, strat, cand["pool"], cand["mint"], cand["name"], "cash", f"free cash ${cash_avail:.2f} too low", cand["state"]); continue
            if strat == "simple":
                tier = cand["state"].get("tier", "A"); sb = size_breakdown(liq, eq, rf, tier=tier, cash_avail=cash_avail, open_cost_usd=oc, mint_open_cost_usd=mint_open_cost(c, strat, cand["mint"]))
                if sb["final_ticket"] < 0.01:   # export1: bank_cap_mint only if the mint cap binds, else ticket_below_min
                    small_ticket_reject(c, strat, cand, sb); continue
                nb = _noroute_block(c, strat, cand["mint"], sb["final_ticket"], eq, sb)
                if nb: reject(c, strat, cand["pool"], cand["mint"], cand["name"], "noroute_limit", nb, cand["state"]); continue
                k0 = n_open(); try_entry(c, strat, cand, sb["final_ticket"], {"rule": "simple_hard_filter_pass", "risk_factor": rf, "tier": tier, "size_breakdown": sb})
                n_new += n_open() > k0
            else:
                ok, rec = jev_decide(judge, cand)
                if not ok:
                    c.execute("INSERT INTO rejections VALUES(?,?,?,?,?,?,?)", (now().isoformat(), strat, cand["pool"], cand["mint"], cand["name"], rec["rule"], rec.get("error", "")))
                    _pyramid.pop((strat, cand["mint"]), None)
                    hashlog.append("decision", strategy=strat, mint=cand["mint"], action="none", thresholds_sha=thresholds_sha(), **rec); continue
                sb = size_breakdown(liq, eq, rf, tier=cand["state"].get("tier", "A"), cash_avail=cash_avail, open_cost_usd=oc, mint_open_cost_usd=mint_open_cost(c, strat, cand["mint"])); ticket = sb["final_ticket"]   # risk factor <= 1 only reduces
                if ticket < 0.01:   # export1
                    small_ticket_reject(c, strat, cand, sb); continue
                br, aw, al, n = base_rate_stats(c); costs = roundtrip_cost(cand["mint"], ticket)
                if costs is None:
                    reject(c, strat, cand["pool"], cand["mint"], cand["name"], "no_route", "no Jupiter round-trip quote for edge costs", cand["state"]); continue
                e, p = edge_est(rec["P_exhausted"], br, aw, al, costs)
                inputs = {"p": p, "P_exhausted": rec["P_exhausted"], "base_rate": br, "avg_win": aw, "avg_loss": al, "costs": costs, "n_simple_trades": n, "edge_est": e}
                if e < T.EDGE_MIN:
                    reject(c, strat, cand["pool"], cand["mint"], cand["name"], "edge", f"edge_est {e:.4f} < {T.EDGE_MIN}", {**cand["state"], "edge_inputs": inputs}); continue
                jev_ok.append((e, cand, ticket, {**rec, "edge_inputs": inputs, "size_breakdown": sb}))
        if strat == "jev" and jev_ok:   # highest edge_est first, at most MAX_ENTRIES_PER_CYCLE (same limit as simple)
            for j, (e, cand, ticket, rec) in enumerate(sorted(jev_ok, key=lambda x: -x[0])):
                if lim is not None and n_new >= lim:
                    reject(c, strat, cand["pool"], cand["mint"], cand["name"], "cycle_entry_limit", f"max {lim} new entries per cycle", cand["state"]); continue
                if j and (can_open(c, strat, cand["mint"]) or open_cost(c, strat) + ticket > eq * T.MAX_EXPOSURE_PCT or ticket + 0.011 > cash(c, strat)):
                    reject(c, strat, cand["pool"], cand["mint"], cand["name"], "exposure", "re-check after earlier jev entry this cycle", cand["state"]); continue
                nb = _noroute_block(c, strat, cand["mint"], ticket, eq, rec["size_breakdown"])
                if nb: reject(c, strat, cand["pool"], cand["mint"], cand["name"], "noroute_limit", nb, cand["state"]); continue
                k0 = n_open(); try_entry(c, strat, cand, ticket, {"rule": "jev_edge_est>=%.2f" % T.EDGE_MIN, "jev": rec, "size_breakdown": rec["size_breakdown"]})
                n_new += n_open() > k0
    _pyramid.clear()   # week1e: nothing pending survives the cycle
    if do_monitor:
      try:
        b, s = spot("BTC"), spot("SOL"); first = c.execute("SELECT btc_usd, sol_usd FROM baselines LIMIT 1").fetchone() or (b, s)
        c.execute("INSERT INTO baselines VALUES(?,?,?,?,?)", (ts, b, s, T.PAPER_BANKROLL_USD * b / first[0], T.PAPER_BANKROLL_USD * s / first[1]))
      except Exception as e: print("baseline fetch failed:", e)
    c.commit()
    print(f"phase={ph} scanned={len(items)} rejected={len(items)-len(survivors)} passed={len(survivors)}")
    print("rejection rules:", counts)
    return {"scanned": len(items), "survivors": len(survivors), "counts": counts}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "cycle"
    if cmd == "cycle": cycle()
    elif cmd == "monitor": _monitor_only()
    elif cmd == "loop":   # monitor every 60s, full scan every 5 min (GeckoTerminal limit)
        open(os.path.join(HERE, "loop.pid"), "w").write(str(os.getpid())); n = 0
        while True:
            try: cycle() if n % 5 == 0 else _monitor_only()
            except hashlog.HashlogCorrupt as e: print("FATAL:", e); raise SystemExit(2)
            except Exception as e: print("error:", e); hashlog.append("loop_error", error=mask(repr(e)))
            n += 1; time.sleep(60)
    elif cmd == "scanloop":   # continuous: one rotating GeckoTerminal source every SCAN_TICK_SEC, monitor every 60s
        open(os.path.join(HERE, "loop.pid"), "w").write(str(os.getpid()))
        hashlog.append("scan_mode", mode="scanloop", tick_sec=T.SCAN_TICK_SEC, sources=list(T.SCAN_ROTATION) + list(T.DS_SUPPLEMENT),
                       dedupe_ttl_sec=T.SCAN_DEDUPE_TTL_SEC, monitor_every_sec=T.MONITOR_EVERY_SEC,
                       jev_max_evals_per_hour=T.JEV_MAX_EVALS_PER_HOUR, previous="loop: full scan every 300s (new_pools p1)",
                       thresholds_sha=thresholds_sha())
        seen, i, last_mon, judge = {}, 0, 0.0, Judge()
        fastmon_start()   # exits1: detect-only fast thread (DexScreener every FASTMON_EVERY_SEC); exits run in this thread
        while True:
            t0 = time.time()
            try:
                if t0 - max(last_mon, _last_monitor_done[0]) >= T.MONITOR_EVERY_SEC:   # cyclebudget1: a mid-cycle pass counts
                    monitor_pass(); last_mon = t0   # dblock1 r2: own connection, closed; events after commit
                srcs = [T.SCAN_ROTATION[i % len(T.SCAN_ROTATION)], T.DS_SUPPLEMENT[i % len(T.DS_SUPPLEMENT)]]; i += 1
                r = cycle(judge=judge, sources=srcs, do_monitor=False, seen=seen, max_fallbacks=T.SCAN_MAX_POOL_FALLBACKS_PER_TICK)
            except hashlog.HashlogCorrupt as e: print("FATAL:", e); raise SystemExit(2)
            except Exception as e: print("error:", e); hashlog.append("loop_error", error=mask(repr(e)))
            try: _fm_watch(); pair_stats_maybe()   # exits1 r2: thread-dead alert (M2) + hourly pair divergence (B2)
            except hashlog.HashlogCorrupt as e: print("FATAL:", e); raise SystemExit(2)
            except Exception as e: print("exits1 watch error:", mask(repr(e)))
            _fm_idle(T.SCAN_TICK_SEC - (time.time() - t0))   # exits1 r2: inter-tick wait serves queued fast exits (own short transaction)
    elif cmd == "verify": print("hash chain ok=%s records=%s" % hashlog.verify())
