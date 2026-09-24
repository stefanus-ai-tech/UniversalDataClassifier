# Universal Adaptive Data Classifier

Local data operations console for text and tabular datasets. It profiles input, proposes a bounded cleaning plan, records every transformation, extracts compact semantic facts with Groq, sends typed questions to Laya, gates actions by confidence, and exports an 11-sheet Excel workbook.

## Start

1. Create a virtual environment and install `requirements.txt`.
2. Copy `.env.example` to `.env` and add your `GROQ_API_KEY`. The Groq model defaults to `openai/gpt-oss-120b`.
3. Optionally set `LAYA_URL` in `.env` to the origin of a [self-hosted Laya server](https://github.com/NandhaKishorM/laya), such as `http://127.0.0.1:8001`. UADC sends decisions to `POST /v1/systemone`. Set `LAYA_API_KEY` if your server needs one.
4. Run `python -m uvicorn app:app --reload --port 8000` from the project directory and open `http://127.0.0.1:8000`.

Windows PowerShell example:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# Edit .env and insert your GROQ_API_KEY; optionally add LAYA_URL.
.\.venv\Scripts\python.exe -m uvicorn app:app --reload --port 8000
```

Do not paste API keys into commands or commit `.env`.

## Current scope

- Input: CSV, TSV, JSON, JSONL, XLSX, TXT, LOG, Markdown, up to 100 MB. CSV, TSV, JSONL, and XLSX are processed iteratively. Large JSON arrays should be converted to JSONL.
- Profiling and cleaning: field types, missing values, duplicate estimate, safe operation plan, deterministic execution, and per-field audit. The plan has an allowlist of operations and never executes LLM-generated Python.
- Semantic layer: Groq processes batches of 20 records and returns facts with field-level evidence. If Groq is unavailable, original fields pass through and the run records this fallback.
- Classification: Laya uses the contract's typed questions and returns choice, confidence, and other answers. Without Laya, lexical rules provide a visible demo prediction with **no model confidence** and no auto-approved action.
- Output: paged record explorer, review queue, mock API trace, CSV, JSON, and Excel dashboard. API actions are simulated only.

The bundled contracts are `support` and `sentiment`; add another JSON contract in `classifiers/` to change labels and questions. The confidence thresholds are examples and need evaluation on labeled data before production automation. Laya's own [benchmark notes](https://github.com/NandhaKishorM/laya#calibration) report overconfidence for base checkpoints.

## Test

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
