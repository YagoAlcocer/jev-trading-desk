#!/usr/bin/env python3
"""Steering controls (paper only). Usage:
  python3 control.py pausa <meme|liquid|all> <motivo...> [--actor 069]
  python3 control.py reanuda <meme|liquid|all> [--actor 069]
  python3 control.py apaga carril <meme|liquid> [motivo...] [--actor 069]
  python3 control.py baja riesgo <factor 0-1> [--actor 069]
  python3 control.py estado
Rules: pause/off applies to the whole lane (both strategies). Risk can only be REDUCED (factor <= current <= 1).
Hard caps (1% trade, 6% position, 5% daily, 25% total, 2% pool, max 3 positions, KILL file) live in thresholds.py
and are NOT settable here. Every command (accepted or refused) is logged via hashlog.append."""
import json, os, sys, fcntl, datetime as dt
import hashlog
PATH = "/workspace/trading/controls.json"
LANES = ("meme", "liquid")
now = lambda: dt.datetime.now(dt.timezone.utc)

def default(): return {"risk_factor": 1.0, "lanes": {l: {"state": "active", "reason": None, "since": None, "paused_seconds": 0.0} for l in LANES}}

def load():
    """Fail-safe reader for collector: unreadable/invalid -> None (caller blocks entries)."""
    if not os.path.exists(PATH): return default()
    try:
        c = json.load(open(PATH)); f = float(c["risk_factor"])
        if not (0 < f <= 1) or set(c["lanes"]) != set(LANES): return None
        return c
    except Exception: return None

def paused_hours(c, lane, at=None):
    L = c["lanes"][lane]; s = L.get("paused_seconds", 0.0)
    if L["state"] != "active" and L.get("since"): s += ((at or now()) - dt.datetime.fromisoformat(L["since"])).total_seconds()
    return round(s / 3600, 2)

def entry_block(c, lane):
    if c is None: return "controls.json unreadable/invalid (fail-safe: no entries)"
    L = c["lanes"].get(lane)
    if L is None: return f"unknown lane {lane}"
    if L["state"] != "active": return f"lane {lane} {L['state']}: {L.get('reason') or ''}".strip()
    return None

def run(argv, actor="069"):
    cmd = " ".join(argv); t = now()
    def log(result, reason=None, **kw):
        hashlog.append("control", ts_cmd=t.isoformat(), command=cmd, reason=reason, actor=actor, result=result, **kw)
        return result
    with open(PATH + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        c = load()
        if c is None: return log("refused: controls.json invalid")
        a = [x.lower() for x in argv]
        def lanes(x):
            if x == "all": return list(LANES)
            if x in LANES: return [x]
            raise ValueError(f"carril desconocido '{x}' (usa meme, liquid o all)")
        try:
            if a[:1] == ["pausa"] and len(a) >= 3:
                reason = " ".join(argv[2:])
                for l in lanes(a[1]):
                    L = c["lanes"][l]
                    if L["state"] == "active": L.update(state="paused", reason=reason, since=t.isoformat())
                    else: L["reason"] = reason
                res = f"ok: paused {a[1]}"; why = reason
            elif a[:1] == ["reanuda"] and len(a) == 2:
                for l in lanes(a[1]):
                    L = c["lanes"][l]
                    if L["state"] != "active":
                        L["paused_seconds"] = paused_hours(c, l, t) * 3600; L.update(state="active", reason=None, since=None)
                res = f"ok: resumed {a[1]}"; why = None
            elif a[:2] == ["apaga", "carril"] and len(a) >= 3:
                if a[2] == "all": raise ValueError("apaga carril requiere un carril (meme o liquid)")
                reason = " ".join(argv[3:]) or "apagado desde chat"
                for l in lanes(a[2]):
                    L = c["lanes"][l]
                    since = L["since"] if L["state"] != "active" else t.isoformat()
                    L.update(state="off", reason=reason, since=since)
                res = f"ok: lane {a[2]} off"; why = reason
            elif a[:2] == ["baja", "riesgo"] and len(a) == 3:
                f = float(a[2]); cur = float(c["risk_factor"])
                if not (0 < f <= 1): return log(f"refused: factor {f} fuera de (0,1]; solo se puede reducir")
                if f > cur: return log(f"refused: {f} > factor actual {cur}; subir riesgo no se permite desde chat")
                c["risk_factor"] = f; res = f"ok: risk_factor {cur} -> {f}"; why = None
            else:
                return log("refused: comando no reconocido / campo no ajustable desde controles")
        except ValueError as e: return log(f"refused: {e}")
        tmp = PATH + ".tmp"; json.dump(c, open(tmp, "w"), indent=1); os.replace(tmp, PATH)
        return log(res, why, risk_factor=c["risk_factor"], lanes={l: c["lanes"][l]["state"] for l in LANES})

if __name__ == "__main__":
    args = sys.argv[1:]; actor = "069"
    if "--actor" in args: i = args.index("--actor"); actor = args[i + 1]; del args[i:i + 2]
    if args[:1] == ["estado"] or not args:
        c = load(); print(json.dumps(c, indent=1)); 
        if c: print({l: paused_hours(c, l) for l in LANES})
    else: print(run(args, actor))
