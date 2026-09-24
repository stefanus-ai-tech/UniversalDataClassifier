from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from uadc.contracts import list_contracts
from uadc.export import make_workbook
from uadc.ingest import SUPPORTED
from uadc.llm import available
from uadc.pipeline import UPLOADS, create_run, execute_run, get_run, iter_csv, iter_json, list_records, summary, update_plan

app = FastAPI(title="Universal Adaptive Data Classifier", version="0.1.0")
ROOT = Path(__file__).resolve().parent
UPLOADS.mkdir(parents=True, exist_ok=True)


@app.get("/api/config")
def config():
    return {"contracts": list_contracts(), "groq_configured": available(), "laya_configured": bool(os.getenv("LAYA_URL")), "formats": sorted(SUPPORTED)}


@app.post("/api/intake")
async def intake(file: UploadFile = File(...), contract: str = Form("support"), objective: str = Form("")):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in SUPPORTED:
        raise HTTPException(400, f"Supported formats: {', '.join(sorted(SUPPORTED))}")
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", Path(file.filename or "input").name)[:100]
    path = UPLOADS / f"{uuid.uuid4()}_{safe_name}"
    size = 0
    try:
        with path.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 100 * 1024 * 1024:
                    raise HTTPException(413, "Maximum file size is 100 MB")
                output.write(chunk)
        run = create_run(path, contract, objective, file.filename)
        return run
    except (ValueError, json.JSONDecodeError) as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(400, str(exc)) from exc
    except HTTPException:
        path.unlink(missing_ok=True)
        raise


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    try:
        run = get_run(run_id)
        run["summary"] = summary(run_id)
        return run
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.put("/api/runs/{run_id}/plan")
def edit_plan(run_id: str, plan: dict):
    try:
        return update_plan(run_id, plan)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/runs/{run_id}/execute")
def execute(run_id: str, background: BackgroundTasks):
    try:
        run = get_run(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    if run["status"] != "READY":
        raise HTTPException(409, "Run already started")
    background.add_task(execute_run, run_id)
    return {"status": "QUEUED", "run_id": run_id}


@app.get("/api/runs/{run_id}/records")
def records(run_id: str, offset: int = 0, limit: int = 50, review: str | None = None):
    if not 0 <= offset or not 1 <= limit <= 200:
        raise HTTPException(400, "Invalid pagination")
    try:
        return list_records(run_id, offset, limit, review)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/runs/{run_id}/export/{format}")
def export(run_id: str, format: str):
    try:
        run = get_run(run_id)
        if run["status"] != "COMPLETE":
            raise HTTPException(409, "Run is not complete")
        if format == "xlsx":
            return FileResponse(make_workbook(run_id), filename=f"UADC_{run_id[:8]}.xlsx")
        if format == "csv":
            return StreamingResponse(iter_csv(run_id), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="UADC_{run_id[:8]}.csv"'})
        if format == "json":
            return StreamingResponse(iter_json(run_id), media_type="application/json", headers={"Content-Disposition": f'attachment; filename="UADC_{run_id[:8]}.json"'})
        raise HTTPException(400, "Supported exports: xlsx, csv, json")
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
def home():
    return FileResponse(ROOT / "static" / "index.html")
