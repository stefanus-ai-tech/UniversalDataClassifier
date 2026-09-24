from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone


def _laya_url() -> str | None:
    base = os.getenv("LAYA_URL", "").strip().rstrip("/")
    if not base:
        return None
    if not re.fullmatch(r"https?://[^/]+", base):
        raise ValueError("LAYA_URL must be an HTTP(S) origin")
    return base + "/v1/systemone"


def classify(state: dict, contract: dict) -> dict:
    url = _laya_url()
    if url:
        body = json.dumps({"state": state["facts"], "questions": contract["questions"]}, ensure_ascii=False, default=str).encode()
        headers = {"Content-Type": "application/json"}
        if os.getenv("LAYA_API_KEY"):
            headers["Authorization"] = "Bearer " + os.environ["LAYA_API_KEY"]
        try:
            with urllib.request.urlopen(urllib.request.Request(url, body, headers), timeout=30) as response:
                result = json.load(response)
            answer = result["answers"][contract["primary_question"]]
            label = answer["choice"]
            if label not in contract["questions"][contract["primary_question"]]["criteria"]:
                raise ValueError("Laya returned a label outside the contract")
            probabilities = _probabilities(answer, contract)
            confidence = float(answer.get("confidence", probabilities.get(label, 0)))
            if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise ValueError("Invalid confidence from Laya")
            return {"label": label, "confidence": confidence, "alternatives": probabilities, "engine": "laya", "answers": result["answers"], "routing": result.get("routing")}
        except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return {"label": None, "confidence": None, "alternatives": {}, "engine": "laya_error", "error": str(exc)[:300]}
    return _rule_classify(state, contract)


def _probabilities(answer: dict, contract: dict) -> dict:
    candidates = answer.get("probabilities", answer.get("distribution", {}))
    if isinstance(candidates, dict):
        return {str(k): float(v) for k, v in candidates.items() if isinstance(v, (int, float))}
    if isinstance(candidates, list):
        names = list(contract["questions"][contract["primary_question"]]["criteria"])
        return {name: float(value) for name, value in zip(names, candidates) if isinstance(value, (int, float))}
    return {}


def _rule_classify(state: dict, contract: dict) -> dict:
    """Demo-only lexical matching. Its score is never presented as model confidence."""
    criteria = contract["questions"][contract["primary_question"]]["criteria"]
    text = " ".join(str(value) for value in state["facts"].values()).lower()
    words = set(re.findall(r"[\w]+", text))
    scores = {}
    for label, description in criteria.items():
        keywords = set(re.findall(r"[\w]+", description.lower())) - {"the", "and", "or", "a", "an", "yang", "dan"}
        scores[label] = len(words & keywords)
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return {"label": None, "confidence": None, "alternatives": {}, "engine": "rules_demo", "error": "No keyword match"}
    return {"label": best, "confidence": None, "alternatives": scores, "engine": "rules_demo", "match_count": scores[best]}


def gate(decision: dict, contract: dict) -> dict:
    if decision["engine"] != "laya" or decision["label"] is None:
        return {"status": "UNRESOLVED", "reason": "Model confidence unavailable; demo rules are not auto approved"}
    confidence = decision["confidence"]
    thresholds = contract["thresholds"]
    if confidence >= thresholds["auto_approve"]:
        return {"status": "AUTO_APPROVED", "reason": None}
    if confidence >= thresholds["needs_review"]:
        return {"status": "NEEDS_REVIEW", "reason": "Confidence below auto approval threshold"}
    return {"status": "UNRESOLVED", "reason": "Low confidence"}


def simulate_action(record_id: str, decision: dict, review: dict, contract: dict) -> dict:
    label = decision["label"]
    if not label or review["status"] != "AUTO_APPROVED":
        return {"status": "NOT_SENT", "reason": "Review required or no decision"}
    endpoint = contract.get("actions", {}).get(label, "/mock/classifications")
    request = {"record_id": record_id, "label": label}
    return {
        "status": "SIMULATED_SUCCESS", "method": "POST", "endpoint": endpoint,
        "request": request, "response_code": 200,
        "response": {"status": "routed", "queue": label},
        "latency_ms": 0, "timestamp": datetime.now(timezone.utc).isoformat(),
    }
