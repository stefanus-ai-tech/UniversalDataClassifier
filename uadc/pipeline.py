from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .cleaning import clean_record, validate_plan
from .contracts import load_contract
from .decision import classify, gate, simulate_action
from .ingest import profile, read_records
from .llm import interpret_batch, make_plan

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "data" / "runs"
UPLOADS = ROOT / "data" / "uploads"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _path(run_id: str) -> Path:
    try:
        uuid.UUID(run_id)
    except ValueError as exc:
        raise FileNotFoundError("Unknown run") from exc
    path = RUNS / f"{run_id}.json"
    if not path.is_file():
        raise FileNotFoundError("Unknown run")
    return path


def get_run(run_id: str) -> dict:
    run = json.loads(_path(run_id).read_text(encoding="utf-8"))
    run["contract_warning"] = contract_warning(run["profile"], run["contract"])
    return run


def contract_warning(data_profile: dict, contract_name: str) -> str | None:
    samples = data_profile.get("samples", [])
    looks_pgn = data_profile.get("format") == "pgn" or any(
        isinstance(sample.get("text"), str) and sample["text"].startswith(("[Site \"", "[Event \"", "[GameID \""))
        for sample in samples if isinstance(sample, dict)
    )
    if looks_pgn and contract_name in {"support", "sentiment"}:
        return "This file looks like chess PGN, but the selected classifier is for customer text. Its labels and confidence do not describe chess games."
    return None


def _save(run: dict) -> None:
    path = RUNS / f"{run['id']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(_dump(run), encoding="utf-8")
    tmp.replace(path)


def create_run(path: Path, contract_name: str, objective: str, display_name: str | None = None) -> dict:
    contract = load_contract(contract_name)
    data_profile = profile(path)
    if display_name:
        data_profile["filename"] = display_name
    warning = contract_warning(data_profile, contract_name)
    if warning:
        raise ValueError(warning)
    if not data_profile["records"]:
        raise ValueError("No records found")
    plan = make_plan(data_profile, objective)
    validate_plan(plan, {column["name"] for column in data_profile["columns"]})
    run = {
        "id": str(uuid.uuid4()), "status": "READY", "stage": "plan_ready", "created_at": _now(),
        "source_file": str(path), "contract": contract_name, "contract_id": contract["id"],
        "objective": objective[:500], "profile": data_profile, "plan": plan,
        "metrics": {"input": data_profile["records"], "processed": 0, "duplicates": 0, "rejected": 0},
    }
    RUNS.mkdir(parents=True, exist_ok=True)
    _save(run)
    return run


def update_plan(run_id: str, plan: dict) -> dict:
    run = get_run(run_id)
    if run["status"] != "READY":
        raise ValueError("Plan can only be edited before execution")
    validate_plan(plan, {column["name"] for column in run["profile"]["columns"]})
    run["plan"] = {"version": 1, "generated_by": "user_edited", "operations": plan["operations"], "deduplicate_exact": bool(plan.get("deduplicate_exact", True))}
    _save(run)
    return run


def _connect(run_id: str) -> sqlite3.Connection:
    connection = sqlite3.connect(RUNS / f"{run_id}.sqlite")
    connection.row_factory = sqlite3.Row
    return connection


def _init_db(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS records (
          record_id TEXT PRIMARY KEY, row_number INTEGER, raw_data TEXT, cleaned_data TEXT,
          semantic_state TEXT, evidence TEXT, decision TEXT, review TEXT, action TEXT, audit TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_records_row ON records(row_number);
        CREATE INDEX IF NOT EXISTS idx_records_review ON records(review);
        CREATE TABLE IF NOT EXISTS fingerprints (digest TEXT PRIMARY KEY);
    """)


def execute_run(run_id: str) -> None:
    run = get_run(run_id)
    if run["status"] != "READY":
        return
    run["status"] = "RUNNING"
    run["stage"] = "cleaning"
    _save(run)
    contract = load_contract(run["contract"])
    connection = _connect(run_id)
    _init_db(connection)
    batch: list[tuple[str, int, dict, dict, list]] = []
    try:
        for row_number, raw in read_records(Path(run["source_file"])):
            if "_parse_error" in raw:
                run["metrics"]["rejected"] += 1
                continue
            cleaned, audit = clean_record(raw, run["plan"])
            if run["plan"].get("deduplicate_exact"):
                fingerprint = hashlib.sha256(json.dumps(cleaned, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
                inserted = connection.execute("INSERT OR IGNORE INTO fingerprints VALUES (?)", (fingerprint,)).rowcount
                if not inserted:
                    run["metrics"]["duplicates"] += 1
                    continue
            record_id = f"REC-{row_number:08d}"
            batch.append((record_id, row_number, raw, cleaned, audit))
            if len(batch) >= 20:
                _process_batch(connection, batch, run, contract)
                batch.clear()
        if batch:
            _process_batch(connection, batch, run, contract)
        run["stage"] = "complete"
        run["status"] = "COMPLETE"
        run["completed_at"] = _now()
        _save(run)
    except Exception as exc:
        run["status"] = "FAILED"
        run["stage"] = "failed"
        run["error"] = f"{type(exc).__name__}: {exc}"[:500]
        _save(run)
    finally:
        connection.close()


def retry_laya(run_id: str) -> None:
    """Reclassify stored semantic states after the Laya service comes online."""
    run = get_run(run_id)
    if run["status"] != "COMPLETE":
        raise ValueError("Only a complete run can be retried")
    connection = _connect(run_id)
    try:
        rows = connection.execute("SELECT record_id, semantic_state, decision FROM records ORDER BY row_number").fetchall()
        targets = [(row[0], json.loads(row[1])) for row in rows if json.loads(row[2]).get("engine") == "laya_error"]
        if not targets:
            return
        contract = load_contract(run["contract"])
        run["status"] = "RUNNING"
        run["stage"] = "retrying_laya"
        run["metrics"]["retry_total"] = len(targets)
        run["metrics"]["retry_processed"] = 0
        _save(run)
        for record_id, facts in targets:
            decision = classify({"facts": facts}, contract)
            review = gate(decision, contract)
            action = simulate_action(record_id, decision, review, contract)
            connection.execute("UPDATE records SET decision=?, review=?, action=? WHERE record_id=?", (_dump(decision), _dump(review), _dump(action), record_id))
            run["metrics"]["retry_processed"] += 1
            if run["metrics"]["retry_processed"] % 20 == 0:
                connection.commit()
                _save(run)
        connection.commit()
        run["status"] = "COMPLETE"
        run["stage"] = "complete"
        run["completed_at"] = _now()
        _save(run)
    except Exception as exc:
        connection.rollback()
        run["status"] = "FAILED"
        run["stage"] = "retry_failed"
        run["error"] = f"{type(exc).__name__}: {exc}"[:500]
        _save(run)
    finally:
        connection.close()


def _process_batch(connection, batch, run, contract):
    run["stage"] = "interpreting_and_classifying"
    states = interpret_batch([(item[0], item[3]) for item in batch], run["objective"])
    for record_id, row_number, raw, cleaned, audit in batch:
        semantic = states[record_id]
        decision = classify(semantic, contract)
        review = gate(decision, contract)
        action = simulate_action(record_id, decision, review, contract)
        connection.execute(
            "INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record_id, row_number, _dump(raw), _dump(cleaned), _dump(semantic["facts"]),
             _dump(semantic["evidence"]), _dump(decision), _dump(review), _dump(action), _dump(audit)),
        )
        run["metrics"]["processed"] += 1
    connection.commit()
    _save(run)


def list_records(run_id: str, offset: int = 0, limit: int = 50, review: str | None = None) -> dict:
    get_run(run_id)
    connection = _connect(run_id)
    try:
        if review:
            where = "WHERE json_extract(review, '$.status') = ?"
            params = [review]
        else:
            where, params = "", []
        total = connection.execute(f"SELECT count(*) FROM records {where}", params).fetchone()[0]
        rows = connection.execute(f"SELECT * FROM records {where} ORDER BY row_number LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
        return {"total": total, "offset": offset, "records": [_decode(row) for row in rows]}
    finally:
        connection.close()


def _decode(row) -> dict:
    item = dict(row)
    for key in ("raw_data", "cleaned_data", "semantic_state", "evidence", "decision", "review", "action", "audit"):
        item[key] = json.loads(item[key])
    return item


def summary(run_id: str) -> dict:
    run = get_run(run_id)
    if run["status"] not in {"COMPLETE", "RUNNING"}:
        return {"status": run["status"], "counts": {}, "labels": {}, "engines": {}}
    connection = _connect(run_id)
    try:
        rows = connection.execute("SELECT decision, review FROM records")
        counts, labels, engines = Counter(), Counter(), Counter()
        confidence_sum = confidence_count = 0
        for row in rows:
            decision, review = json.loads(row[0]), json.loads(row[1])
            counts[review["status"]] += 1
            labels[decision["label"] or "unresolved"] += 1
            engines[decision["engine"]] += 1
            if decision.get("confidence") is not None:
                confidence_sum += decision["confidence"]
                confidence_count += 1
        return {"status": run["status"], "counts": dict(counts), "labels": dict(labels), "engines": dict(engines), "average_model_confidence": round(confidence_sum / confidence_count, 3) if confidence_count else None}
    finally:
        connection.close()


def iter_csv(run_id: str):
    get_run(run_id)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["record_id", "row_number", "label", "confidence", "engine", "review", "action", "raw_data", "cleaned_data", "semantic_state"])
    yield output.getvalue()
    output.seek(0)
    output.truncate(0)
    connection = _connect(run_id)
    try:
        for row in connection.execute("SELECT * FROM records ORDER BY row_number"):
            item = _decode(row)
            writer.writerow([item["record_id"], item["row_number"], item["decision"]["label"], item["decision"]["confidence"], item["decision"]["engine"], item["review"]["status"], item["action"]["status"], _dump(item["raw_data"]), _dump(item["cleaned_data"]), _dump(item["semantic_state"])])
            yield output.getvalue()
            output.seek(0)
            output.truncate(0)
    finally:
        connection.close()


def iter_json(run_id: str):
    run = get_run(run_id)
    yield '{"run":' + _dump(run) + ',"records":['
    connection = _connect(run_id)
    try:
        first = True
        for row in connection.execute("SELECT * FROM records ORDER BY row_number"):
            yield ("" if first else ",") + _dump(_decode(row))
            first = False
    finally:
        connection.close()
    yield "]}"
