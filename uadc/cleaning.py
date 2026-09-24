from __future__ import annotations

import re
from datetime import datetime


def build_plan(profile: dict) -> dict:
    """A bounded, inspectable transformation spec. No generated Python is executed."""
    operations = []
    for column in profile["columns"]:
        name = column["name"]
        if column["type"] == "text":
            operations.append({"field": name, "op": "trim_whitespace"})
        if column["type"] == "number":
            operations.append({"field": name, "op": "parse_number"})
        if re.search(r"date|time|tanggal|waktu", name, re.I):
            operations.append({"field": name, "op": "normalize_date"})
        if re.search(r"phone|telp|telepon|no_hp", name, re.I):
            operations.append({"field": name, "op": "normalize_phone_id"})
    return {"version": 1, "generated_by": "deterministic_profiler", "operations": operations, "deduplicate_exact": True}


def validate_plan(plan: dict, fields: set[str]) -> None:
    allowed = {"trim_whitespace", "parse_number", "normalize_date", "normalize_phone_id", "lowercase"}
    if not isinstance(plan, dict) or not isinstance(plan.get("operations"), list):
        raise ValueError("Invalid cleaning plan")
    if len(plan["operations"]) > 200:
        raise ValueError("Too many cleaning operations")
    for operation in plan["operations"]:
        if not isinstance(operation, dict) or operation.get("field") not in fields or operation.get("op") not in allowed:
            raise ValueError("Cleaning plan contains an unsupported operation or field")


def _apply(value, operation: str):
    if value is None:
        return None
    if operation == "trim_whitespace":
        return re.sub(r"\s+", " ", str(value)).strip()
    if operation == "lowercase":
        return str(value).lower()
    if operation == "parse_number":
        text = str(value).strip().replace(" ", "")
        if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
            number = float(text)
            return int(number) if number.is_integer() else number
        return value
    if operation == "normalize_phone_id":
        digits = re.sub(r"\D", "", str(value))
        if digits.startswith("0") and 9 <= len(digits) <= 14:
            return "+62" + digits[1:]
        if digits.startswith("62") and 10 <= len(digits) <= 15:
            return "+" + digits
        return value
    if operation == "normalize_date":
        if isinstance(value, datetime):
            return value.isoformat()
        for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(str(value).strip(), pattern).isoformat()
            except ValueError:
                pass
        return value
    raise ValueError("Unsupported operation")


def clean_record(row: dict, plan: dict) -> tuple[dict, list[dict]]:
    cleaned = dict(row)
    audit = []
    for operation in plan["operations"]:
        field, op = operation["field"], operation["op"]
        if field not in cleaned:
            continue
        before = cleaned[field]
        after = _apply(before, op)
        if before != after:
            cleaned[field] = after
            audit.append({"field": field, "before": before, "after": after, "operation": op})
    return cleaned, audit
