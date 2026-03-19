"""
mirofish_server.py – MiroFish-compatible prediction API server.

Implements the REST API expected by mirofish/client.py.
Uses Claude (claude-haiku-4-5) to generate YES/NO probability predictions.

Usage:
    ANTHROPIC_API_KEY=sk-... python mirofish_server.py
    # Runs on http://localhost:5001
"""

from __future__ import annotations

import uuid
from typing import Dict, Any

import os

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


def _gemini_predict(prompt: str) -> str:
    resp = requests.post(
        GEMINI_URL,
        params={"key": GEMINI_API_KEY},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"]


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


# ── Report generation (calls Claude for prediction) ───────────────────────────

@app.post("/api/report/projects/{project_id}/generate")
async def generate_report(project_id: str):
    if project_id not in _projects:
        raise HTTPException(404, "Project not found")

    project = _projects[project_id]
    seed = project["seed"]

    # Call Claude for probability prediction
    prompt = (
        f"{seed}\n\n"
        "Based on the information above, provide a probability estimate (0–100%) "
        "for the question resolving YES. "
        "Analyse all relevant factors carefully. "
        "End your response with exactly: 'Probability: X%' where X is your estimate."
    )

    try:
        report_text = _gemini_predict(prompt)
    except Exception as e:
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
