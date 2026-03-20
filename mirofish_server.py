"""
mirofish_server.py – MiroFish-compatible prediction API server.

Implements the REST API expected by mirofish/client.py.
Uses Gemini 2.0 Flash with Google Search grounding for predictions.

Improvements over v1:
  - Independent analysis first (no anchoring to market price)
  - Multi-pass self-consistency (configurable, default 3 passes)
  - Gemini Google Search grounding for real-time news
  - Median of passes → more robust probability estimate

Usage:
    GEMINI_API_KEY=... python mirofish_server.py
    # Runs on http://localhost:5001
"""

from __future__ import annotations

import re
import statistics
import uuid
from typing import Dict, Any, List, Optional

import logging
import os

log = logging.getLogger("mirofish_server")

from dotenv import load_dotenv
load_dotenv()

import requests
from fastapi import FastAPI, UploadFile, File, HTTPException
import uvicorn

app = FastAPI(title="MiroFish Prediction Server")

# In-memory storage
_projects: Dict[str, Dict[str, Any]] = {}  # project_id → {title, seed, report}
_tasks: Dict[str, Dict[str, str]] = {}      # task_id → {status, result_key, project_id}

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY environment variable is not set")
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models"
    "/gemini-2.0-flash:generateContent"
)

# Number of independent Gemini passes per prediction (median is taken).
# More passes → more stable but slower. 3 is a good default.
PREDICTION_PASSES = int(os.environ.get("MIROFISH_PASSES", "3"))

# Superforecaster prompt — independent analysis, then compare to market
_PROMPT_TEMPLATE = """\
{seed}

## Forecasting Instructions

You are an expert superforecaster with access to real-time web search.
Perform each step in order:

Step 1 — SEARCH & CURRENT STATE:
Search for the latest news, data, or developments directly relevant to this question.
Summarise the 2-3 most important current facts.

Step 2 — BASE RATE:
What is the historical frequency for events of this type?
Give a rough range (e.g. "30-40% of similar situations resolve YES").

Step 3 — EVIDENCE BALANCE:
List the 2-3 factors that push probability HIGHER and 2-3 factors that push it LOWER.

Step 4 — INDEPENDENT ESTIMATE:
Based on steps 1-3 ONLY (ignore the market price for now), state your best probability.
Use specific values like 23%, 67%, 84% — not round numbers unless fully justified.

Step 5 — MARKET DIVERGENCE CHECK:
The current market-implied probability is shown in the context above.
• If your step-4 estimate is within 5 percentage points of the market: accept the market as a better-informed prior and align with it.
• If your estimate differs by MORE than 5 percentage points: you may have found an edge. Briefly explain WHY you disagree with the crowd (new information, overlooked factor, or model failure).

Step 6 — FINAL ANSWER:
State your final calibrated probability, incorporating step 5.

End your response with exactly this line (no other text after it):
Probability: X%
"""

# Lightweight regex to pull a probability out of one pass's text
_PROB_RE = re.compile(
    r"probability[:\s]+([0-9]{1,3})[\s]*%",
    re.IGNORECASE,
)


def _extract_prob_fast(text: str) -> Optional[float]:
    m = _PROB_RE.search(text)
    if m:
        v = float(m.group(1))
        return v / 100.0 if v > 1.0 else v
    return None


def _gemini_call(prompt: str, use_search: bool = True) -> str:
    """Single Gemini API call. Enables Google Search grounding when requested."""
    payload: Dict[str, Any] = {
        "contents": [{"parts": [{"text": prompt}]}],
    }
    if use_search:
        payload["tools"] = [{"google_search": {}}]
    resp = requests.post(
        GEMINI_URL,
        params={"key": GEMINI_API_KEY},
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    # When google_search tool is active Gemini may return multiple parts:
    # parts[0] could be a functionCall (the search query), parts[1] the text.
    # We scan all parts for the last one that carries a "text" field.
    parts = data["candidates"][0]["content"]["parts"]
    for part in reversed(parts):
        if "text" in part:
            return part["text"]
    return ""


def _gemini_predict(seed: str) -> str:
    """
    Run PREDICTION_PASSES independent Gemini calls and return the text
    whose extracted probability is the median of all passes.

    Falls back to a single pass on repeated failures.
    """
    prompt = _PROMPT_TEMPLATE.format(seed=seed)
    results: List[tuple[float, str]] = []  # (prob, text)

    for i in range(PREDICTION_PASSES):
        try:
            text = _gemini_call(prompt, use_search=True)
            prob = _extract_prob_fast(text)
            if prob is None:
                prob = 0.5  # can't parse → neutral, still include for diversity
            results.append((prob, text))
            log.info("[MiroFish] Pass %d/%d → prob=%.3f", i + 1, PREDICTION_PASSES, prob)
        except Exception as exc:
            log.warning("[MiroFish] Pass %d failed: %s", i + 1, exc)

    if not results:
        return "Analysis unavailable. Probability: 50%"

    if len(results) == 1:
        return results[0][1]

    # Sort by probability and pick the median pass
    results.sort(key=lambda x: x[0])
    median_idx = len(results) // 2
    all_probs = [r[0] for r in results]
    log.info("[MiroFish] Pass probs: %s → median=%.3f", all_probs, results[median_idx][0])
    return results[median_idx][1]


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


# ── Projects ──────────────────────────────────────────────────────────────────

@app.post("/api/graph/projects")
async def create_project(body: dict):
    pid = str(uuid.uuid4())
    _projects[pid] = {"title": body.get("name", ""), "seed": "", "report": None}
    return {"project_id": pid, "id": pid}


@app.post("/api/graph/projects/{project_id}/delete")
async def delete_project(project_id: str):
    _projects.pop(project_id, None)
    return {"ok": True}


# ── Seed upload ───────────────────────────────────────────────────────────────

@app.post("/api/graph/projects/{project_id}/upload")
async def upload_seed(project_id: str, file: UploadFile = File(...)):
    if project_id not in _projects:
        raise HTTPException(404, "Project not found")
    content = await file.read()
    _projects[project_id]["seed"] = content.decode("utf-8", errors="replace")
    return {"ok": True}


# ── Graph build (no-op — just return completed task) ─────────────────────────

@app.post("/api/graph/projects/{project_id}/build")
async def build_graph(project_id: str):
    tid = str(uuid.uuid4())
    _tasks[tid] = {"status": "completed", "project_id": project_id}
    return {"task_id": tid}


@app.get("/api/graph/tasks/{task_id}")
async def graph_task_status(task_id: str):
    t = _tasks.get(task_id, {"status": "completed"})
    return {"status": t["status"]}


# ── Simulation prepare (no-op) ────────────────────────────────────────────────

@app.post("/api/simulation/projects/{project_id}/prepare")
async def prepare_simulation(project_id: str):
    tid = str(uuid.uuid4())
    _tasks[tid] = {"status": "completed", "project_id": project_id}
    return {"task_id": tid}


@app.get("/api/simulation/tasks/{task_id}")
async def simulation_task_status(task_id: str):
    t = _tasks.get(task_id, {"status": "completed"})
    return {"status": t["status"]}


# ── Simulation run (no-op — prediction happens at report stage) ───────────────

@app.post("/api/simulation/projects/{project_id}/run")
async def run_simulation(project_id: str, body: dict = {}):
    tid = str(uuid.uuid4())
    _tasks[tid] = {"status": "completed", "project_id": project_id}
    return {"task_id": tid}


# ── Report generation ─────────────────────────────────────────────────────────

@app.post("/api/report/projects/{project_id}/generate")
async def generate_report(project_id: str):
    if project_id not in _projects:
        raise HTTPException(404, "Project not found")

    project = _projects[project_id]
    seed = project["seed"]

    try:
        report_text = _gemini_predict(seed)
        log.info("[MiroFish] Report OK for project %s (%.0f chars)", project_id[:8], len(report_text))
    except Exception as e:
        log.error("[MiroFish] Gemini API ERROR: %s", e)
        report_text = f"Analysis unavailable. Probability: 50%\nError: {e}"

    report_id = str(uuid.uuid4())
    project["report"] = report_text
    project["report_id"] = report_id

    tid = str(uuid.uuid4())
    _tasks[tid] = {
        "status": "completed",
        "project_id": project_id,
        "report_id": report_id,
    }
    return {"task_id": tid, "report_id": report_id}


@app.get("/api/report/tasks/{task_id}")
async def report_task_status(task_id: str):
    t = _tasks.get(task_id, {"status": "completed"})
    return {"status": t.get("status", "completed"), "report_id": t.get("report_id")}


@app.get("/api/report/projects/{project_id}/reports/{report_id}")
async def get_report(project_id: str, report_id: str):
    project = _projects.get(project_id)
    if not project or not project.get("report"):
        raise HTTPException(404, "Report not found")
    return {"content": project["report"]}


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5001, log_level="info")
