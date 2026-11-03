#!/usr/bin/env python3
"""Export paper-trading dashboard data as JSON (read-only: sqlite mode=ro, never writes).
Usage: python3 -B dashboard_data.py [out.json]   (prints to stdout if no path; ALWAYS -B)"""
import sqlite3, json, sys, os, subprocess, datetime as dt
sys.dont_write_bytecode = True   # pycache_fix2: never write __pycache__ into /workspace/trading, even if run as plain `python3 dashboard_data.py`
from collections import Counter
from zoneinfo import ZoneInfo
# ---- labels1 shared scrub block: keep IDENTICAL in datasource.py and dashboard_data.py (test asserts it) ----
# r3: copies, not a shared module: datasource lives in the site and runs dashboard_data.py (in /workspace/trading) as a
# subprocess; a third file loaded by path would have to be deployed to both places anyway and a missing file must not
# fail open, so the block is duplicated byte-for-byte and the test compares both copies.
import re, os, unicodedata, base64 as _b64, urllib.parse
# r3 (Code Auditor): prefixed keys first, base58 last; clean() applies SECRET_PFX before SECRET_VAL so a key glued to an
# 80+ base58 run is fully masked.
SECRET_PFX = re.compile(r"(sk-or-[A-Za-z0-9_\-]{12,}|sk-[A-Za-z0-9_\-]{12,}|github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|Bearer\s+\S+|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,})")
SECRET_VAL = re.compile(r"(sk-or-[A-Za-z0-9_\-]{12,}|sk-[A-Za-z0-9_\-]{12,}"
    r"|github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9]{20,}"            # tokens de GitHub
    r"|Bearer\s+\S+"
    r"|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"    # JWT
    r"|\b(?:0x)?[0-9a-fA-F]{64,}\b"                                       # hex de 64+ (llaves privadas)
    r"|\[\s*(?:\d{1,3}\s*,\s*){31,}\d{1,3}\s*\]"                         # arreglos de bytes (32+)
    r"|[1-9A-HJ-NP-Za-km-z]{80,})")                                       # base58 80+ (último)
URL_Q = re.compile(r"(https?://[^\s'\"]*?)\?[^\s'\"]*", re.I)
KEYQ = re.compile(r"(api[-_]?key|key|token)=[^\s&'\"]*", re.I)
RPC_HOST = re.compile(r"(?:https?://|wss?://)?[A-Za-z0-9.\-]*(?:helius|rpc)[A-Za-z0-9.\-]*(?::\d+)?(?:/[^\s'\"]*)?", re.I)
ENVP = re.compile(r"[^\s'\"]*\.env\b[^\s'\"]*", re.I)
SECRET_ENVS = ("OPENROUTER_API_KEY", "HELIUS_API_KEY", "JEV_API_KEY", "DASHBOARD_GH_TOKEN", "JUPITER_API_KEY")   # export1: + Jupiter
def _forms(v):
    out = {v, v.lower(), urllib.parse.quote(v, safe=""), urllib.parse.quote_plus(v), _b64.b64encode(v.encode()).decode().rstrip("=")}
    if len(v) >= 16: out.add(v[:12])  # prefijo (truncado)
    return [f for f in out if len(f) >= 8]
def secret_forms():
    return [f for n in SECRET_ENVS if os.environ.get(n) for f in _forms(os.environ[n])]
def contains_secret(text):
    low = text.lower(); return any(f.lower() in low for f in secret_forms())
def clean(t):
    for f in sorted(secret_forms(), key=len, reverse=True):
        t = re.sub(re.escape(f), "[oculto]", t, flags=re.I)
    t = SECRET_PFX.sub("[oculto]", t)
    t = URL_Q.sub(r"\1", t); t = KEYQ.sub("[oculto]", t); t = RPC_HOST.sub("[rpc]", t); t = ENVP.sub("[oculto]", t)
    return SECRET_VAL.sub("[oculto]", t)
# r3 (Risk Auditor): paths, hosts, IPs, JSON bodies
_JSON = re.compile(r"\{[^{}]*\}")
_URL = re.compile(r"\b(?:https?|wss?|ftp)://[^\s'\"]*", re.I)
_WSPATH = re.compile(r"(?:~|\B/(?:workspace|home|root|tmp|etc|usr|var|opt|srv|mnt|data|app|proc))/[^\s'\"]*|(?<![A-Za-z0-9])~(?=[\s'\"]|$)")
_WINPATH = re.compile(r"\b[A-Za-z]:\\[^\s'\"]*|\\\\[^\s'\"]+")
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b|\[[0-9a-fA-F:]{2,}\](?::\d+)?")
_LOCAL = re.compile(r"\blocalhost(?::\d+)?\b", re.I)
_HOST = re.compile(r"\b(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,24}\b(?::\d+)?")   # any TLD
def _scrub_text(s):
    s = unicodedata.normalize("NFKC", str(s))   # r4: fullwidth /ｏｐｔ/ etc. become ASCII before matching
    s = "".join(" " if ch in "\r\n\t" else ch for ch in str(s) if ch >= " " or ch in "\r\n\t")
    s = s.replace("\x7f", "")
    s = clean(s)
    prev = None
    while prev != s: prev, s = s, _JSON.sub("[cuerpo]", s)
    s = _URL.sub("[url]", s); s = _WINPATH.sub("[ruta]", s); s = _WSPATH.sub("[ruta]", s)
    s = _IP.sub("[host]", s); s = _LOCAL.sub("[host]", s); s = _HOST.sub("[host]", s)
    return re.sub(r" {2,}", " ", s).strip()
# labels1: limit kind from the reason text (display only; collector unchanged). Pyramid/add-on texts also contain
# "already holding this token", so they are matched first. Unknown position_limit text -> "unknown" (never guessed).
def limit_kind(rule, reason, error=None):
    r = str(reason or "") + " " + str(error or "")   # r2: also inspect error
    if rule == "exposure": return "exposure"
    if rule != "position_limit": return None
    if re.search(r"max \d+ concurrent", r): return "global_cap"
    if "add-on already used" in r: return "rebuy_used"
    if "pyramid add needs profit" in r: return "pyramid_loss"
    if "pyramid add needs a fresh mark" in r: return "pyramid_no_mark"
    if "already holding this token" in r: return "per_mint_max"
    return "unknown"
def clean_reason(v, n=240):
    """Text for display: newlines -> spaces, control chars removed, secrets/paths/hosts/IPs/JSON masked, THEN capped."""
    if v is None: return None
    return _scrub_text(v)[:n]
def err_summary(e):
    """'tipo · fuente' only (no paths, bodies, hosts or raw exception)."""
    low = str(e).lower()
    kind = next((k for k, pats in (("timeout", ("timeout", "timed out")), ("conexión", ("connectionerror", "connection", "newconnection")),
        ("http 429", ("429", "too many")), ("http 5xx", (" 500", " 502", " 503", " 504")), ("json", ("jsondecode", "json")))
        if any(p in low for p in pats)), "error")
    src = next((s for s in ("dexscreener", "geckoterminal", "openrouter", "coingecko", "jupiter", "birdeye") if s in low), None)
    if src is None and ("rpc" in low or "helius" in low or "gettokensupply" in low or "getaccountinfo" in low): src = "rpc"
    return f"{kind} · {src or 'otra fuente'}"
def public_error(v, n=120):
    """r2 (Risk Auditor): raw error never goes public. summarize -> clean -> truncate. None/'' stays None."""
    if v is None or v == "": return None
    return clean_reason(err_summary(v), n)
# r4 (Risk Auditor): FULL templates (re.fullmatch per '; ' part, after NFKC). Anything else -> NOT_PUBLIC.
_N = r"[-+]?\$?\d{1,3}(?:,\d{3})*(?:\.\d{1,8})?"   # r4.1: number = 1-3 digits, ',ddd' groups, optional decimals
KNOWN_DEX = ("pumpfun", "pump-fun", "meteoradbc", "meteora-dbc", "meteora", "meteora-damm-v2", "raydium", "raydium-launchlab", "orca",
             "stonkfun", "pancakeswap", "letsbonk-fun", "bags", "moonit", "memefun", "fluxbeam")   # r4.1: every dex id seen in the real log
_D = "'(?:" + "|".join(re.escape(x) for x in sorted(KNOWN_DEX, key=len, reverse=True)) + ")'"
KNOWN_EXT = ("transferHook", "permanentDelegate", "nonTransferable", "defaultAccountState", "transferFeeConfig", "confidentialTransferMint",
             "mintCloseAuthority", "interestBearingConfig", "pausableConfig")   # r4.1: Token-2022 extension names
_EXT = "'(?:" + "|".join(KNOWN_EXT) + ")'"
# r4.1: symbol inside a reason: SYM_RE-like, <= 16 chars, no % ~ ; : \ {} [] <> @ = ? & # | ` quotes; '/' only inside (no leading '/', '~',
# drive letter or '..'); non-ASCII letters allowed (real symbols like '咪咪', '███').
_SYMC = r"[^\s%~;:\\{}\[\]<>@=?&#|`\"'/]"
_SYMT = rf"(?!.*\.\.){_SYMC}(?:[^%~;:\\{{}}\[\]<>@=?&#|`\"']{{0,14}}{_SYMC})?"
_MINT = r"[1-9A-HJ-NP-Za-km-z]{32,44}"
REASON_TEMPLATES = tuple(re.compile(p) for p in (
    rf"still on bonding curve \(dex {_D}\), not migrated",
    r"missing liquidity \(not on DexScreener(?: nor GeckoTerminal)?\)",
    rf"LP burn/lock unverifiable on dex {_D}",
    rf"duplicates older token {_SYMT} {_MINT}",
    rf"liquidity {_N} < {_N}",
    r"liquidity undeterminable after \d{1,3} retries \(last: [a-z_]{1,30}\)",
    rf"\d{{1,3}}h volume {_N} < {_N}(?: \(tier B off: cluster/sniper filters not implemented\))?",
    rf"wallet holds {_N}% > {_N}%",
    rf"top\d{{1,3}} {_N}% > {_N}%",
    r"already holding this token(?: \(\d{1,3} open legs?, max \d{1,3}\))?",
    r"already holding this token; add-on already used(?: for this open position)?",
    rf"pyramid add needs profit at fresh mark \({_N}%(?: <= {_N}%)?\)",
    r"pyramid add needs a fresh mark(?: \((?:no route|no mark|stale mark)\))?",
    r"add-on already used(?: for this open position)?",
    rf"daily loss cap {_N}% hit(?: \(halt until tomorrow\))?",
    rf"dangerous Token-2022 extensions: \[{_EXT}(?:, {_EXT}){{0,8}}\]",
    rf"quote impact {_N}% > {_N}% \([a-z]{{1,20}}\)",
    r"max \d{1,3} concurrent positions",
    r"max \d{1,3} new entries per cycle",
    rf"edge_est {_N} < {_N}",
    rf"stop {_N}%",
    rf"take profit \+{_N}%",   # r4.1
))
FAIL_HEADS = ("copycat", "on-chain", "liquidity", "lp", "volume", "holders", "jev", "quote", "rpc")
FAIL_RE = re.compile(r"\bcheck failed:|\bfailed:|\bvia https?://|\bvia wss?://", re.I)
NOT_PUBLIC = "motivo no publicable"
def _nfkc(s): return unicodedata.normalize("NFKC", str(s))
def _public_part(p):
    p = " ".join(_nfkc(p).split())
    if not p: return None
    if p.lower().startswith("stale/no rpc data:"): return "stale/no RPC data: " + err_summary(p)
    m = FAIL_RE.search(p)
    if m:
        head = p[:m.start()].strip().rstrip(":").lower()
        if head in FAIL_HEADS and m.group(0).lower() == "check failed:": return f"{head} check failed: {err_summary(p)}"
        return NOT_PUBLIC
    if not any(t.fullmatch(p) for t in REASON_TEMPLATES): return NOT_PUBLIC
    q = _scrub_text(p)
    return q if q == p else NOT_PUBLIC      # a template match that still needed redaction is not published
def public_reason(v, n=240):
    """Reason for the public title: each '; ' part must fullmatch a template (after NFKC); failures -> 'head check failed: tipo · fuente'."""
    if v is None or v == "": return None
    out = []
    for part in re.split(r";\s*", " ".join(_nfkc(v).replace("\x00", "").split())):
        x = _public_part(part)
        if x and x not in out: out.append(x)
    return ("; ".join(out) or NOT_PUBLIC)[:n]
# ---- end labels1 shared scrub block ----
D = "/workspace/trading"; TZ = ZoneInfo("America/Mexico_City")
START = dt.date(2026, 10, 29); EVAL_START = START + dt.timedelta(days=7)
def phase(ts):
    d = dt.datetime.fromisoformat(ts).astimezone(TZ).date()
    return "evaluation" if d >= EVAL_START else "calibration"
def local(ts): return dt.datetime.fromisoformat(ts).astimezone(TZ).strftime("%Y-%m-%d %H:%M")
c = sqlite3.connect(f"file:{D}/paper.db?mode=ro", uri=True); c.row_factory = sqlite3.Row
q = lambda s: [dict(r) for r in c.execute(s)]
# equity table: net of fees/impact, open positions marked at the last logged Jupiter sell quote (collector)
_ecols = [r[1] for r in c.execute("PRAGMA table_info(equity)")]
eq = q("select ts,strategy,realized,unrealized,equity" + (",marks_missing" if "marks_missing" in _ecols else ",NULL marks_missing")
       + (",marks_stale" if "marks_stale" in _ecols else ",NULL marks_stale") + " from equity order by ts")
bl = q("select ts,btc_value_of_bank btc,sol_value_of_bank sol from baselines order by ts")
curves = {}
for r in eq: curves.setdefault(phase(r["ts"]), {}).setdefault(r["strategy"], []).append({"t": local(r["ts"]), "v": round(r["equity"], 2),
    "realized": round(r["realized"], 4), "unrealized": round(r["unrealized"], 4), "marks_missing": r["marks_missing"], "marks_stale": r["marks_stale"]})
# dailycap1 r2: existing fields unchanged (v, realized = collector cash flow incl. open cost, unrealized = market value of open legs).
# Added: realized_closed = sum of CLOSED positions' net (sells net of fee - cost - entry fee, closed_ts <= t);
# upnl = unrealized - open cost (cost + entry fee - sells net of fee so far, of legs open at t). Identity: v = bankroll + realized_closed + upnl.
sys.path.insert(0, D); from thresholds import PAPER_BANKROLL_USD as _BANK
_pp = {x["id"]: x for x in q("select id,strategy,opened_ts,closed_ts,status,cost_usd+entry_fee_usd c from positions")}
_sf = {}
for x in q("select position_id,ts,usd-coalesce(fee_usd,0) n from fills where side='sell'"): _sf.setdefault(x["position_id"], []).append((dt.datetime.fromisoformat(x["ts"]), x["n"]))
for x in _pp.values():
    x["o"] = dt.datetime.fromisoformat(x["opened_ts"]); x["cl"] = dt.datetime.fromisoformat(x["closed_ts"]) if x["closed_ts"] and x["status"] != "open" else None
_rc = {}
for r in eq:
    t = dt.datetime.fromisoformat(r["ts"]); rc = oc = 0.0
    for pid, x in _pp.items():
        if x["strategy"] != r["strategy"] or x["o"] > t: continue
        net = sum(n for ft, n in _sf.get(pid, []) if ft <= t) - (x["c"] or 0)
        if x["cl"] and x["cl"] <= t: rc += net
        else: oc -= net
    _rc.setdefault(r["strategy"], []).append((round(rc, 4), round(r["unrealized"] - oc, 4)))
_it = {k: iter(v) for k, v in _rc.items()}
for _ph in curves.values():
    for _s, _pts in _ph.items():
        if _s in _it:
            for _pt in _pts: _pt["realized_closed"], _pt["upnl"] = next(_it[_s])
for r in bl:
    for k in ("btc", "sol"): curves.setdefault(phase(r["ts"]), {}).setdefault("hold_" + k, []).append({"t": local(r["ts"]), "v": round(r[k], 2)})
_pcols = [r[1] for r in c.execute("PRAGMA table_info(positions)")]
pos = q("select id,strategy,lane,symbol,mint,pool,opened_ts,closed_ts,status,cost_usd,entry_fee_usd,entry_impact_pct,qty_raw,qty_left_raw,decision_hash"
        + (",decimals" if "decimals" in _pcols else ",NULL decimals") + (",pyramid_of" if "pyramid_of" in _pcols else ",NULL pyramid_of")
        + " from positions order by opened_ts")
_has_marks = bool(list(c.execute("select name from sqlite_master where type='table' and name='position_marks'")))
_marks = {r["position_id"]: r for r in q("select * from position_marks")} if _has_marks else {}
sys.path.insert(0, D)
from thresholds import STOP_LOSS_PCT as _STOP, STALE_MARK_TTL_SEC as _TTL   # single source (week1c)
_recs_by_hash = {}
for _l in open(f"{D}/decisions.log.jsonl", "rb").read().split(b"\n")[:-1]:   # week1e: skip partial last line (as below)
    if not _l.strip(): continue
    _r = json.loads(_l)
    if _r.get("action") == "enter": _recs_by_hash[_r["hash"]] = _r
def _mark_status(p):
    m = _marks.get(p["id"])
    if p["status"] != "open": return None, None, None
    if not m: return "unknown", None, None   # collector without position_marks (pre-week1b)
    age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(m["last_ok_ts"])).total_seconds() if m.get("last_ok_ts") else None
    if not m.get("fail_since_ts") and m.get("last_ok_usd") is not None: return "ok", round(age / 60, 1), m["last_ok_usd"]
    if age is not None and age <= _TTL: return "stale", round(age / 60, 1), m["last_ok_usd"] * p["qty_left_raw"] / (m.get("last_ok_qty") or p["qty_left_raw"])
    return "no_route", round(age / 60, 1) if age is not None else None, 0.0
fills = q("select position_id,lane,side,usd,fee_usd,reason from fills")
for p in pos:
    f = [x for x in fills if x["position_id"] == p["id"]]
    sells = sum(x["usd"] - (x["fee_usd"] or 0) for x in f if x["side"] == "sell")
    buys = sum(x["usd"] + (x["fee_usd"] or 0) for x in f if x["side"] == "buy") or (p["cost_usd"] or 0) + (p["entry_fee_usd"] or 0)
    dec_ = p["decimals"] if p["decimals"] is not None else 6
    units = p["qty_raw"] / 10 ** dec_ if p["qty_raw"] else None
    entry_px = p["cost_usd"] / units if units else None
    ms, ms_min, val = _mark_status(p)
    left_units = (p["qty_left_raw"] or 0) / 10 ** dec_
    rec = _recs_by_hash.get(p["decision_hash"]) or {}
    p.update(entry_price=entry_px, decimals_assumed=p["decimals"] is None,
             current_price=(val / left_units) if (val is not None and left_units and ms in ("ok", "stale")) else None,
             current_value=round(val, 4) if val is not None else None,
             unrealized_pnl=round(val - p["cost_usd"] * (p["qty_left_raw"] / p["qty_raw"]), 4) if (val is not None and p["qty_raw"]) else None,
             mark_status=ms, minutes_since_valid_mark=ms_min,
             stop_price=entry_px * (1 + _STOP / 100) if entry_px else None, stop_pct=_STOP,
             size_breakdown=rec.get("size_breakdown") or {"final_ticket": p["cost_usd"], "risk_factor": rec.get("risk_factor"), "note": "pre-week1b entry: breakdown not logged"})
    p.pop("decision_hash", None)
    # export1: pyramid legs. group_id = base leg id (pyramid_of for an add-on, own id for a base); open AND closed legs
    p.update(group_id=p["pyramid_of"] or p["id"], leg="add_on" if p["pyramid_of"] else "base", ca=p["mint"],
             opened_at=dt.datetime.fromisoformat(p["opened_ts"]).astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S"))
    p.update(phase=phase(p["opened_ts"]), opened=local(p["opened_ts"]),
             closed=local(p["closed_ts"]) if p["closed_ts"] else None, net_pnl=round(sells - buys, 2) if p["status"] != "open" else None,
             exit_reason=next((x["reason"] for x in reversed(f) if x["side"] == "sell"), None))
_grp = {}
for p in pos: _grp.setdefault((p["strategy"], p["group_id"]), []).append(p)
for p in pos:   # per-leg stop stays on each leg (stop_price/stop_pct); group totals for the Designer
    g = _grp[(p["strategy"], p["group_id"])]; og = [x for x in g if x["status"] == "open"]
    p.update(group_legs=len(g), group_legs_open=len(og),
             group_open_cost_usd=round(sum(x["cost_usd"] * (x["qty_left_raw"] or 0) / x["qty_raw"] for x in og if x["qty_raw"]), 4))
    # export1: group avg entry = sum(cost_usd) / sum(units) over ALL legs with a stored price (cost-weighted by qty);
    # group uPnL = sum of OPEN legs' unrealized_pnl (same mark logic as each leg: ok/stale mark; no_route = $0; unknown -> excluded).
    # For the grouped card only; per-leg rows keep their own entry_price.
    _u = [(x["cost_usd"], x["qty_raw"] / 10 ** (x["decimals"] if x["decimals"] is not None else 6)) for x in g if x["qty_raw"] and x["cost_usd"] is not None]
    _up = [x["unrealized_pnl"] for x in og if x["unrealized_pnl"] is not None]
    p.update(group_avg_entry_price=(sum(a for a, _ in _u) / sum(b for _, b in _u)) if _u and sum(b for _, b in _u) else None,
             group_upnl=round(sum(_up), 4) if _up else None)
progress = {}
for p in pos:
    if p["status"] != "open" and p["phase"] == "evaluation":
        k = f'{p["strategy"]}/{p["lane"]}'; progress[k] = progress.get(k, 0) + 1
sc = q("select ts,passed from scans")
# export1: cycle_id. The collector logs no cycle id; every cycle() inserts its scans with ONE ts, so a decision belongs to the
# latest scans.ts <= its ts (cycles run serially). Derived here; a collector-side cycle_id is listed in pending_collector.
import bisect
_cyc_ts = sorted({r["ts"] for r in sc})
def cycle_id(ts):
    i = bisect.bisect_right(_cyc_ts, ts) - 1
    return _cyc_ts[i] if i >= 0 else None
rules = Counter()
for r in q("select rule from rejections"):
    for x in (r["rule"] or "unknown").split(","): rules[x] += 1
_raw = open(f"{D}/decisions.log.jsonl", "rb").read().split(b"\n")[:-1]   # v2: skip a partial last line (append in progress)
recs = [json.loads(l) for l in _raw if l.strip()]
calib = [r for r in recs if r.get("kind") == "outcome" and r.get("jev")]  # Jev predictions with resolved outcomes
# export1: symbol per mint (filter/simple log state.symbol, jev logs state_shown.symbol; positions have symbol)
# NOTE: _sym_by_mint is built from the WHOLE log (and positions), not as-of each decision's ts, so an old decision may show
# a symbol that was only registered later for that mint (last logged symbol wins).
_sym_by_mint = {}
for r in recs:
    for k_ in ("state", "state_shown"):
        st_ = r.get(k_)
        if isinstance(st_, dict) and st_.get("symbol") and r.get("mint"): _sym_by_mint[r["mint"]] = st_["symbol"]
for p in pos:
    if p.get("symbol"): _sym_by_mint.setdefault(p["mint"], p["symbol"])
import re as _re
_QRE = _re.compile(r"^jev_(Q[1-9])(?:_|$)")
def jev_failed_question(r):
    if r.get("strategy") != "jev" or r.get("action") == "enter": return None
    m = _QRE.match(str(r.get("rule") or "")); return m.group(1) if m else None
# --- chain_status (week1d v2) ---
def chain_status(D):
    """Run `collector.py verify` isolated from the caller's env (publish-common.sh exports PYTHONSAFEPATH=1, which made
    collector.py miss thresholds -> empty stdout -> false 'CADENA ROTA'). Returns (text, state): state ok | broken | unverified.
    Non-zero exit / no parseable result -> 'unverified' (stderr never exposed)."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONSAFEPATH", "PYTHONPATH")}; env["PYTHONDONTWRITEBYTECODE"] = "1"
    try: r = subprocess.run([sys.executable, "-B", f"{D}/collector.py", "verify"], cwd=D, env=env, capture_output=True, text=True, timeout=60)
    except Exception: return "unverified", "unverified"
    out = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    if r.returncode != 0 or "ok=" not in out: return "unverified", "unverified"
    return out, "ok" if "ok=True" in out else "broken"
# --- end chain_status ---
v, chain_state = chain_status(D)
try: pid = int(open(f"{D}/loop.pid").read().strip())
except Exception: pid = None
pid_alive = bool(pid) and os.path.exists(f"/proc/{pid}") and b"collector.py" in open(f"/proc/{pid}/cmdline", "rb").read()
now = dt.datetime.now(TZ)
out = dict(generated=now.strftime("%Y-%m-%d %H:%M"), loop_running=pid_alive, loop_pid=pid,
  last_cycle=local(max(r["ts"] for r in eq)) if eq else None, chain=v, chain_ok=chain_state == "ok", chain_state=chain_state,
  chain_records=int(v.rsplit("records=", 1)[1]) if chain_state != "unverified" and "records=" in v and v.rsplit("records=", 1)[1].isdigit() else None,
  kill=os.path.exists(f"{D}/KILL"), quote_stale=os.path.exists(f"{D}/QUOTE_STALE"), phase="evaluation" if now.date() >= EVAL_START else "calibration",
  day=(now.date() - START).days + 1, eval_start=str(EVAL_START), curves=curves, positions=pos, eval_progress=progress,
  funnel=dict(scanned=len(sc), passed=sum(1 for r in sc if r["passed"]), rejected=len(sc) - sum(1 for r in sc if r["passed"]),
              rejections_total=sum(rules.values()), by_rule=rules.most_common()), calibration_points=len(calib))
# ---- extensions: controls, funnel, Jev status, coverage ----
sys.path.insert(0, D)
import control
EXPECTED_MODEL = "typesafe/jev-1.14-20261014"
STAGE = {"liquidity": "1 liquidez", "volume_24h": "2 volumen 24h", "lp_lock": "3 LP quemado", "mint_authority": "4 on-chain",
         "freeze_authority": "4 on-chain", "token2022": "4 on-chain", "top_holder": "4 on-chain", "top10": "4 on-chain",
         "rpc": "4 on-chain", "copycat": "5 copycat", "lane_paused": "6 carril pausado", "risk_limit": "6 límites de riesgo",
         "position_limit": "6 límites de riesgo", "exposure": "6 límites de riesgo", "no_route": "8 ejecución", "slippage": "8 ejecución", "edge": "7 edge_est"}
def stage(rule):
    if rule.startswith("jev_"): return "7 Jev"
    return STAGE.get(rule, "otro")
today = now.date().isoformat()
dec = [r for r in recs if r.get("kind") == "decision"]
def ldate(ts): return dt.datetime.fromisoformat(ts).astimezone(TZ).date().isoformat()
fun = {"hoy": Counter(), "desde_calibracion": Counter()}; rule_ct = {"hoy": Counter(), "desde_calibracion": Counter()}
for r in dec:
    if r.get("action") == "enter": continue
    rules_ = (r.get("rule") or "unknown").split(",")
    first = sorted(rules_, key=stage)[0]   # earliest failing stage = where the funnel lost it
    for k in ("hoy", "desde_calibracion") if ldate(r["ts"]) == today else ("desde_calibracion",):
        fun[k][stage(first)] += 1
        for x in rules_: rule_ct[k][x] += 1
entries = [r for r in dec if r.get("action") == "enter"]
jev_recs = [r for r in dec if r.get("strategy") == "jev" and ("jev_model" in r or str(r.get("rule", "")).startswith("jev"))]
jev_real = [r for r in jev_recs if r.get("jev_model") == EXPECTED_MODEL and any(v is not None for v in (r.get("answers") or {}).values())]
# q4v1: Q4 narrative v0 and every Q4b version (v1.1 fallback / v2p1 phase 1) are never mixed: records split by q4_version (absent = v0)
q4_by_version = Counter(r.get("q4_version") or "Q4_narrative_strength_v0" for r in jev_real)
q4_answers_by_version = {"Q4_narrative_strength_v0": [(r["answers"]["Q4"], local(r["ts"])) for r in jev_real if not r.get("q4_version") and (r.get("answers") or {}).get("Q4") is not None]}
for r in jev_real:
    if r.get("q4_version") and (r.get("answers") or {}).get("Q4b") is not None: q4_answers_by_version.setdefault(r["q4_version"], []).append((r["answers"]["Q4b"], local(r["ts"])))
# preregistered rule (same constants as collector Q4B_AUTO_INACTIVE): >=30 answers and >=90% score<=2 or conf<0.5 -> inactive_no_data
Q4B_AUTO_INACTIVE = {"min_responses": 30, "frac": 0.90, "score_le": 2, "conf_lt": 0.5}
def _q4b_rule(ans, R=Q4B_AUTO_INACTIVE):
    bad = sum(1 for (a, _t) in ans if a[0] is None or a[0] <= R["score_le"] or (a[1] or 0) < R["conf_lt"])
    return dict(n=len(ans), degenerate_frac=round(bad / len(ans), 3) if ans else None,
                status="inactive_no_data" if len(ans) >= R["min_responses"] and bad / len(ans) >= R["frac"] else "active", rule=R)
q4b_rule_status = {k: _q4b_rule(v) for k, v in q4_answers_by_version.items() if k.startswith("Q4b_")}
n_answers = sum(sum(1 for v in (r.get("answers") or {}).values() if v is not None) for r in jev_real)
ctl = control.load()
cov = Counter(); cov_today = Counter()
for r in dec:
    src = ((r.get("state") or {}).get("data_sources") or {}).get("liquidity")
    if "data_sources" in (r.get("state") or {}):
        cov[src or "sin datos"] += 1
        if ldate(r["ts"]) == today: cov_today[src or "sin datos"] += 1
closed = Counter(); openp = Counter()
for p in pos:
    k = f'{p["strategy"]}/{p["lane"]}'
    (openp if p["status"] == "open" else closed)[k] += 1
out.update(
  controls=dict(valid=ctl is not None, risk_factor=ctl["risk_factor"] if ctl else None,
                lanes={l: dict(state=ctl["lanes"][l]["state"], reason=ctl["lanes"][l]["reason"], paused_hours=control.paused_hours(ctl, l)) for l in control.LANES} if ctl else None),
  control_history=[dict(t=local(r["ts"]), command=r.get("command"), actor=r.get("actor"), reason=clean_reason(r.get("reason")), result=r.get("result")) for r in recs if r.get("kind") == "control"],
  adjustments=[dict(t=local(r["ts"]), kind=r.get("kind"), detail={k: v for k, v in r.items() if k not in ("hash", "prev_hash", "seq", "ts", "kind")}) for r in recs if r.get("kind") in ("calibration_start", "threshold_change", "adjustment")],
  trade_progress=dict(target=30, closed=dict(closed), open=dict(openp), entries_logged=len(entries)),
  funnel_stages={k: sorted(v.items()) for k, v in fun.items()}, rule_counts={k: v.most_common() for k, v in rule_ct.items()},
  jev=dict(model=EXPECTED_MODEL, provider="openrouter", questions_answered=n_answers, decisions_with_real_answers=len(jev_real), jev_decision_records=len(jev_recs), q4_by_version=dict(q4_by_version), q4_answers_by_version={k: v[-200:] for k, v in q4_answers_by_version.items()}, q4b_rule_status=q4b_rule_status, q4b_notify=[dict(t=local(r["ts"]), kind=r["kind"], version=r.get("version"), notify=r.get("notify"), reason=clean_reason(r.get("reason") or r.get("status"))) for r in recs if r.get("kind") in ("q4b_auto_inactive", "q4b_phase1_blocked")],
           calibration_start=local(jev_real[0]["ts"]) if jev_real else "pendiente",
           config_record=next((local(r["ts"]) for r in recs if r.get("kind") == "calibration_start"), None)),
  data_coverage=dict(total=dict(cov), hoy=dict(cov_today)),
  last_scan=local(max(r["ts"] for r in sc)) if sc else None)
# ---- extensions v2 (read-only): live feed, operators, stage of last token, cycle annotations ----
STAGE_ORDER = ["escaneo", "liquidez", "volumen", "LP", "on-chain", "copycat", "Jev", "entrada"]
STG_MAP = {"1 liquidez": "liquidez", "2 volumen 24h": "volumen", "3 LP quemado": "LP", "4 on-chain": "on-chain", "5 copycat": "copycat", "7 Jev": "Jev"}
import re

def lane_of(r): return r.get("lane")  # not logged on filter decisions yet
def feed_row(r):
    st = r.get("state") or r.get("state_shown") or {}; rules_ = (r.get("rule") or "").split(",") if r.get("rule") else []
    first = sorted(rules_, key=stage)[0] if rules_ else None
    return dict(t=dt.datetime.fromisoformat(r["ts"]).astimezone(TZ).strftime("%H:%M:%S"), lane=lane_of(r), token=st.get("symbol"),
                strategy=r.get("strategy"), rule=r.get("rule"), first_stage=STG_MAP.get(stage(first)) if first else None,
                result=r.get("action"), reason=public_reason(r.get("reason")), limit_kind=limit_kind(r.get("rule"), r.get("reason"), r.get("error")), error=public_error(r.get("error")),
                jev_answers=r.get("answers"), jev_model=r.get("jev_model"),
                symbol=st.get("symbol") or _sym_by_mint.get(r.get("mint")), mint=r.get("mint"), cycle_id=cycle_id(r["ts"]),
                cycle_id_source="derived:scans.ts", jev_failed_question=jev_failed_question(r))
last_dec = dec[-1] if dec else None
errs = [r for r in recs if r.get("kind") == "loop_error"]
def ago(ts): return round((now - dt.datetime.fromisoformat(ts).astimezone(TZ)).total_seconds() / 60, 1) if ts else None
last_filter = next((r["ts"] for r in reversed(dec) if r.get("strategy") == "filter"), None)
last_risk = next((r["ts"] for r in reversed(dec) if any(x in str(r.get("rule")) for x in ("risk_limit", "position_limit", "exposure", "lane_paused"))), None)
last_fill = next(iter(q("select ts from fills order by ts desc limit 1")), {}).get("ts")
cyc = Counter(dt.datetime.fromisoformat(r["ts"]).astimezone(TZ).strftime("%Y-%m-%d %H:%M") for r in dec if r.get("action") != "enter")
out.update(
  feed=[feed_row(r) for r in dec[-40:]][::-1],
  last_token=dict(**feed_row(last_dec), stages=STAGE_ORDER) if last_dec else None,
  operators=dict(
    escaner=dict(last=local(sc[-1]["ts"]) if sc else None, min_ago=ago(max(r["ts"] for r in sc)) if sc else None),
    filtros=dict(last=local(last_filter) if last_filter else None, min_ago=ago(last_filter)),
    jev=dict(last=local(jev_recs[-1]["ts"]) if jev_recs else None, min_ago=ago(jev_recs[-1]["ts"]) if jev_recs else None, records=len(jev_recs)),
    riesgo=dict(last=local(last_risk) if last_risk else None, min_ago=ago(last_risk)),
    ejecucion=dict(last=local(last_fill) if last_fill else None, min_ago=ago(last_fill)),
    reporte=dict(last=local(eq[-1]["ts"]) if eq else None, min_ago=ago(eq[-1]["ts"]) if eq else None)),
  loop_errors=[dict(t=local(r["ts"]), error=str(r.get("error"))[:120]) for r in errs],
  scanned_today=sum(1 for r in sc if ldate(r["ts"]) == today),
  rejections_by_minute=sorted(cyc.items()))
# export1: filter passes. The collector writes NO hashlog record for a filter pass (strategy 'filter', action 'pass');
# the equivalent is scans.passed = 1 (same cycle ts). Exported here; a collector-side record is listed in pending_collector.
_fp = q("select ts,pool,mint,name,dex,liquidity,volume_h24 from scans where passed=1 order by ts desc limit 40")
out.update(
  filter_passes=[dict(t=dt.datetime.fromisoformat(r["ts"]).astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S"), cycle_id=r["ts"], strategy="filter", action="pass",
                      symbol=_sym_by_mint.get(r["mint"]) or (r["name"] or "").split(" / ")[0] or None, mint=r["mint"], pool=r["pool"], dex=r["dex"],
                      liquidity=r["liquidity"], volume_h24=r["volume_h24"], source="scans.passed") for r in _fp],
  filter_passes_total=sum(1 for r in sc if r["passed"]),
  pending_collector=[
    dict(item="filter_pass_record", status="export-equivalent", note="collector logs no 'filter'/'pass' decision; export uses scans.passed=1"),
    dict(item="cycle_id", status="export-derived", note="collector logs no cycle id; export derives it from the cycle's scans.ts")])
s = json.dumps(out, indent=1, default=str)
open(sys.argv[1], "w").write(s) if len(sys.argv) > 1 else print(s)
