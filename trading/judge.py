"""Jev judge (default provider: OpenRouter decisions API).
Old header: Jev judge interface (questions.md section 2) -> Vercel AI Gateway decision endpoint.
Same wire format as @ai-sdk/gateway GatewayDecisionModel.doDecide (ai experimental_decide).
Any error/refusal/invalid output/timeout raises JevUnavailable (caller: NO ENTRY). Key is never logged."""
import os, json, time, urllib.request, urllib.error
from dataclasses import dataclass, field
from typing import Optional

PROVIDER = "openrouter"
EXPECTED_MODEL = "typesafe/jev-1.14-20261014"   # auditor pin: any other returned id -> JevUnavailable
MODEL_FALLBACK = "~typesafe/jev-latest"         # only used if the exact slug 404s
MODEL = EXPECTED_MODEL
OR_URL = "https://openrouter.ai/api/alpha/decisions"   # chat/completions rejects decisions models (400)
VERCEL_MODEL = "typesafe-ai/jev"
URL = "https://ai-gateway.vercel.sh/v4/ai/decision-model"
TIMEOUT, BACKOFF = 30, (2, 4)

@dataclass
class Noul:
    question: str; p_yes: Optional[float] = None
@dataclass
class Score:
    question: str; score: Optional[int] = None; confidence: Optional[float] = None
@dataclass
class Choice:
    question: str; options: list = field(default_factory=list); choice: Optional[str] = None; confidence: Optional[float] = None

class JevUnavailable(Exception): pass

def _p(x):
    if not isinstance(x, (int, float)) or not (0 <= x <= 1): raise JevUnavailable("invalid probability")
    return float(x)

class VercelJudge:  # legacy path (account blocked: 403 customer_verification_required)
    def __init__(self):
        self.key = os.environ.get("JEV_API_KEY"); self.configured = bool(self.key)

    def _post(self, body):
        if not self.key: raise JevUnavailable("Jev not configured (no key)")
        hdr = {"Authorization": "Bearer " + self.key, "Content-Type": "application/json",
               "ai-gateway-protocol-version": "0.0.1", "ai-gateway-auth-method": "api-key",
               "ai-decision-model-specification-version": "4", "ai-model-id": VERCEL_MODEL}
        last = None
        for i in range(len(BACKOFF) + 1):
            try:
                req = urllib.request.Request(URL, json.dumps(body).encode(), hdr, method="POST")
                with urllib.request.urlopen(req, timeout=TIMEOUT) as f: return json.loads(f.read())
            except urllib.error.HTTPError as e:
                try: msg = json.loads(e.read()).get("error", {}).get("type", "")
                except Exception: msg = ""
                last = f"HTTP {e.code} {msg}"
                if e.code == 404 and body.get("model") == EXPECTED_MODEL:
                    self.request_model = MODEL_FALLBACK; body = {**body, "model": MODEL_FALLBACK}; continue
                if e.code < 500 and e.code != 429: break
            except Exception as e: last = type(e).__name__
            if i < len(BACKOFF): time.sleep(BACKOFF[i])
        raise JevUnavailable(f"Jev request failed: {last}")

    def ask(self, q, state: dict):
        if isinstance(q, Noul): spec = {"type": "boolean", "instructions": q.question}
        elif isinstance(q, Score): spec = {"type": "score", "instructions": q.question, "criteria": [str(i) for i in range(1, 11)]}
        elif isinstance(q, Choice):
            if not q.options: raise JevUnavailable("choice without options")
            spec = {"type": "choice", "instructions": q.question, "criteria": {str(o): str(o) for o in q.options}}
        else: raise JevUnavailable("unknown question type")
        r = self._post({"stateParts": [{"type": "json", "value": state}], "questions": {"q": spec}})
        if r.get("model") not in (None, VERCEL_MODEL) and not str(r.get("model")).startswith(VERCEL_MODEL):
            raise JevUnavailable(f"unexpected model {r.get('model')}")
        a = (r.get("answers") or {}).get("q")
        if not isinstance(a, dict) or a.get("type") != spec["type"]: raise JevUnavailable(f"invalid/abstain answer: {a and a.get('type')}")
        if isinstance(q, Noul): q.p_yes = _p(a.get("probability")); return q
        probs = a.get("probabilities") or {}
        if isinstance(q, Score):
            s = a.get("score")
            if not isinstance(s, (int, float)) or not 1 <= s <= 10: raise JevUnavailable("invalid score")
            q.score = int(round(s)); q.confidence = _p(probs.get(str(q.score), max(probs.values(), default=0))); return q
        c = a.get("choice")
        if c not in spec["criteria"]: raise JevUnavailable("invalid choice")
        q.choice = c; q.confidence = _p(probs.get(c, 0)); return q


class Judge:
    """OpenRouter Jev. Same interface as before; any failure -> JevUnavailable."""
    provider = PROVIDER
    def __init__(self):
        self.key = os.environ.get("OPENROUTER_API_KEY"); self.configured = bool(self.key); self.last_model = None
        self.request_model = EXPECTED_MODEL

    def _post(self, body):
        if not self.key: raise JevUnavailable("Jev not configured (no OPENROUTER_API_KEY)")
        hdr = {"Authorization": "Bearer " + self.key, "Content-Type": "application/json"}
        last = None
        for i in range(len(BACKOFF) + 1):
            try:
                req = urllib.request.Request(OR_URL, json.dumps(body).encode(), hdr, method="POST")
                with urllib.request.urlopen(req, timeout=TIMEOUT) as f: return json.loads(f.read())
            except urllib.error.HTTPError as e:
                try: msg = str(json.loads(e.read()).get("error", {}).get("message", ""))[:200]
                except Exception: msg = ""
                last = f"HTTP {e.code} {msg}"
                if e.code < 500 and e.code != 429: break
            except Exception as e: last = type(e).__name__
            if i < len(BACKOFF): time.sleep(BACKOFF[i])
        raise JevUnavailable(f"Jev request failed: {last}")

    def ask(self, q, state: dict):
        levels = [str(i) for i in range(1, 11)]
        if isinstance(q, Noul): spec = {"type": "noul", "instructions": q.question}
        elif isinstance(q, Score): spec = {"type": "score", "instructions": q.question, "criteria": levels}
        elif isinstance(q, Choice):
            if not q.options: raise JevUnavailable("choice without options")
            spec = {"type": "choice", "instructions": q.question, "criteria": {str(o): str(o) for o in q.options}}
        else: raise JevUnavailable("unknown question type")
        self.last_model = None
        r = self._post({"model": self.request_model, "state": state, "questions": {"q": spec}})
        m = r.get("model")
        self.last_model = m
        if m != EXPECTED_MODEL: raise JevUnavailable(f"model mismatch: got {m!r}, expected {EXPECTED_MODEL!r}")
        a = (r.get("answers") or {}).get("q")
        if not isinstance(a, dict) or a.get("type") != spec["type"]: raise JevUnavailable(f"invalid/abstain answer: {isinstance(a, dict) and a.get('type')}")
        if isinstance(q, Noul): q.p_yes = _p(a.get("noul")); return q
        probs = a.get("probabilities")
        if not isinstance(probs, dict) or not probs: raise JevUnavailable("missing probabilities")
        conf = _p(a.get("confidence"))
        if isinstance(q, Score):
            legend = a.get("legend") or {}
            top = max(probs, key=lambda k: probs[k])
            lvl = legend.get(top)
            if lvl not in levels: raise JevUnavailable("invalid score level")
            q.score = int(lvl); q.confidence = conf; return q
        c = a.get("choice")
        if c not in spec["criteria"]: raise JevUnavailable("invalid choice")
        q.choice = c; q.confidence = conf; return q
