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


def laya_health() -> dict:
    base = os.getenv("LAYA_URL", "").strip().rstrip("/")
    if not base:
        return {"configured": False, "reachable": False, "message": "Laya is not configured; demo rules will be used"}
    try:
        url = _laya_url()
        with urllib.request.urlopen(url.removesuffix("/v1/systemone") + "/health", timeout=2) as response:
            payload = json.load(response)
        if payload.get("status") == "ok":
            return {"configured": True, "reachable": True, "message": "Laya server is ready", "loaded": payload.get("loaded", [])}
        return {"configured": True, "reachable": False, "message": "Laya health check did not return OK"}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"configured": True, "reachable": False, "message": f"Laya server is unreachable at {base}: {type(exc).__name__}"}


def classify(state: dict, contract: dict) -> dict:
    url = _laya_url()
    if url:
        request_data = {"state": state["facts"], "questions": contract["questions"]}
        model = os.getenv("LAYA_MODEL", "multilingual").strip()
        if model and model != "auto":
            request_data["model"] = model
        body = json.dumps(request_data, ensure_ascii=False, default=str).encode()
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
            raw_confidence = answer.get("answer_confidence", probabilities.get(label))
            confidence = float(raw_confidence) if raw_confidence is not None else None
            if confidence is not None and (not math.isfinite(confidence) or not 0 <= confidence <= 1):
                raise ValueError("Invalid confidence from Laya")
            return {"label": label, "confidence": confidence, "distribution_confidence": answer.get("confidence"), "alternatives": probabilities, "engine": "laya", "answers": result["answers"], "routing": result.get("routing")}
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
    if confidence is None:
        return {"status": "NEEDS_REVIEW", "reason": "Laya did not return answer confidence"}
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
