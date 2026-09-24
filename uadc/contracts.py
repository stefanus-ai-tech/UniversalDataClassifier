from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACT_DIR = ROOT / "classifiers"


def load_contract(name: str = "support") -> dict:
    path = (CONTRACT_DIR / f"{name}.json").resolve()
    if path.parent != CONTRACT_DIR.resolve() or not path.is_file():
        raise ValueError("Unknown classifier contract")
    contract = json.loads(path.read_text(encoding="utf-8"))
    questions = contract.get("questions", {})
    primary = contract.get("primary_question")
    if not primary or questions.get(primary, {}).get("type") != "choice":
        raise ValueError("Contract needs a primary choice question")
    if len(questions[primary].get("criteria", {})) < 2:
        raise ValueError("Primary question needs at least two labels")
    return contract


def list_contracts() -> list[dict]:
    result = []
    for path in sorted(CONTRACT_DIR.glob("*.json")):
        try:
            data = load_contract(path.stem)
            result.append({"id": path.stem, "name": data["name"], "description": data.get("description", "")})
        except (ValueError, KeyError, json.JSONDecodeError):
            continue
    return result
