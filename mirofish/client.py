"""
mirofish/client.py – Low-level async HTTP client for the MiroFish API.

MiroFish must be running locally (docker compose up -d) before use.
Default base URL: http://localhost:5001

Workflow stages:
  1. create_project(title) → project_id
  2. upload_seed(project_id, text) → uploads seed text (news/context)
  3. build_graph(project_id) → task_id → wait → graph ready
  4. prepare_simulation(project_id) → task_id → wait → entities ready
  5. run_simulation(project_id, rounds) → simulation runs
  6. generate_report(project_id) → task_id → wait → report_id
  7. get_report(project_id, report_id) → full report text
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import aiohttp

from utils.logger import get_logger

log = get_logger(__name__)

_POLL_INTERVAL = 3.0   # seconds between task-status polls
_POLL_TIMEOUT  = 300.0 # max seconds to wait for any task


class MiroFishError(Exception):
    """Raised when MiroFish returns an unexpected response."""


class MiroFishClient:
    """Async client for the MiroFish REST API."""

    def __init__(self, base_url: str = "http://localhost:5001") -> None:
        self.base_url = base_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None

    # ── Session lifecycle ─────────────────────────────────────────────────────

    async def __aenter__(self) -> "MiroFishClient":
        # NOTE: do NOT set Content-Type globally – file uploads need multipart
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60),
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("Use MiroFishClient as async context manager")
        return self._session

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    async def _get(self, path: str, **params: Any) -> Any:
        url = f"{self.base_url}{path}"
        async with self.session.get(url, params=params) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise MiroFishError(f"GET {path} → {resp.status}: {data}")
            return data

    async def _post(self, path: str, json: Any = None, data: Any = None) -> Any:
        url = f"{self.base_url}{path}"
        async with self.session.post(url, json=json, data=data) as resp:
            body = await resp.json(content_type=None)
            if resp.status >= 400:
                raise MiroFishError(f"POST {path} → {resp.status}: {body}")
            return body

    # ── Health ────────────────────────────────────────────────────────────────

    async def health(self) -> bool:
        try:
            await self._get("/health")
            return True
        except Exception:
            return False

    # ── Projects ──────────────────────────────────────────────────────────────

    async def create_project(self, title: str) -> str:
        """Create a new project and return project_id."""
        resp = await self._post("/api/graph/projects", json={"name": title})
        project_id = resp.get("project_id") or resp.get("id")
        if not project_id:
            raise MiroFishError(f"No project_id in response: {resp}")
        log.debug("[MiroFish] Project created: %s (id=%s)", title, project_id)
        return str(project_id)

    async def delete_project(self, project_id: str) -> None:
        await self._post(f"/api/graph/projects/{project_id}/delete")

    # ── Seed upload ───────────────────────────────────────────────────────────

    async def upload_seed_text(self, project_id: str, text: str,
                               filename: str = "seed.txt") -> None:
        """Upload plain-text seed data as a virtual file."""
        import aiohttp as _aiohttp
        data = _aiohttp.FormData()
        data.add_field(
            "file",
            text.encode(),
            filename=filename,
            content_type="text/plain",
        )
        url = f"{self.base_url}/api/graph/projects/{project_id}/upload"
        async with self.session.post(url, data=data) as resp:
            body = await resp.json(content_type=None)
            if resp.status >= 400:
                raise MiroFishError(f"upload_seed → {resp.status}: {body}")
        log.debug("[MiroFish] Seed uploaded (%d chars)", len(text))

    # ── Graph building ────────────────────────────────────────────────────────

    async def build_graph(self, project_id: str) -> None:
        """Trigger graph construction and wait for completion."""
        resp = await self._post(f"/api/graph/projects/{project_id}/build")
        task_id = resp.get("task_id")
        if task_id:
            await self._wait_task(f"/api/graph/tasks/{task_id}", "graph build")
        log.debug("[MiroFish] Graph built for project %s", project_id)

    # ── Simulation preparation ────────────────────────────────────────────────

    async def prepare_simulation(self, project_id: str) -> None:
        """Extract entities and prepare agent profiles."""
        resp = await self._post(
            f"/api/simulation/projects/{project_id}/prepare"
        )
        task_id = resp.get("task_id")
        if task_id:
            await self._wait_task(
                f"/api/simulation/tasks/{task_id}", "simulation prepare"
            )
        log.debug("[MiroFish] Simulation prepared for project %s", project_id)

    # ── Simulation run ────────────────────────────────────────────────────────

    async def run_simulation(self, project_id: str, rounds: int = 10) -> None:
        """Run the multi-agent simulation."""
        resp = await self._post(
            f"/api/simulation/projects/{project_id}/run",
            json={"rounds": rounds},
        )
        task_id = resp.get("task_id")
        if task_id:
            await self._wait_task(
                f"/api/simulation/tasks/{task_id}", "simulation run",
                timeout=600.0,
            )
        log.debug("[MiroFish] Simulation finished for project %s", project_id)

    # ── Report generation ─────────────────────────────────────────────────────

    async def generate_report(self, project_id: str) -> str:
        """Generate analysis report and return report_id."""
        resp = await self._post(
            f"/api/report/projects/{project_id}/generate"
        )
        task_id = resp.get("task_id")
        if task_id:
            resp = await self._wait_task(
                f"/api/report/tasks/{task_id}", "report generation",
                return_result=True,
            )
        report_id = (resp or {}).get("report_id") or (resp or {}).get("id")
        if not report_id:
            raise MiroFishError(f"No report_id in response: {resp}")
        log.debug("[MiroFish] Report generated: %s", report_id)
        return str(report_id)

    async def get_report_text(self, project_id: str, report_id: str) -> str:
        """Retrieve full report content as a string."""
        resp = await self._get(
            f"/api/report/projects/{project_id}/reports/{report_id}"
        )
        # Try common field names
        for key in ("content", "text", "report", "summary", "result"):
            if key in resp and isinstance(resp[key], str):
                return resp[key]
        # Fallback: join all string values
        parts = [str(v) for v in resp.values() if isinstance(v, str)]
        return "\n".join(parts)

    # ── Task polling ──────────────────────────────────────────────────────────

    async def _wait_task(
        self,
        path: str,
        label: str,
        timeout: float = _POLL_TIMEOUT,
        return_result: bool = False,
    ) -> Any:
        """Poll a task endpoint until status is 'completed' or 'failed'."""
        elapsed = 0.0
        while elapsed < timeout:
            await asyncio.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL
            try:
                resp = await self._get(path)
            except Exception as exc:
                log.warning("[MiroFish] Poll error for %s: %s", label, exc)
                continue

            status = resp.get("status", "").lower()
            log.debug("[MiroFish] %s → %s (%.0fs)", label, status, elapsed)

            if status in ("completed", "done", "success", "finished"):
                return resp
            if status in ("failed", "error"):
                raise MiroFishError(f"{label} failed: {resp}")

        raise MiroFishError(f"{label} timed out after {timeout}s")
