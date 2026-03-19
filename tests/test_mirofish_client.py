"""Unit tests for mirofish/client.py — all HTTP calls mocked."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from mirofish.client import MiroFishClient, MiroFishError


@pytest.fixture
async def client():
    async with MiroFishClient("http://localhost:5001") as c:
        yield c


# ── health() ─────────────────────────────────────────────────────────────────

async def test_health_returns_true_on_200(client):
    with patch.object(client, "_get", new=AsyncMock(return_value={"status": "ok"})):
        assert await client.health() is True

async def test_health_returns_false_on_exception(client):
    with patch.object(client, "_get", new=AsyncMock(side_effect=Exception("refused"))):
        assert await client.health() is False


# ── create_project() ──────────────────────────────────────────────────────────

async def test_create_project_returns_id(client):
    with patch.object(client, "_post", new=AsyncMock(return_value={"project_id": "proj-123"})):
        pid = await client.create_project("test project")
    assert pid == "proj-123"

async def test_create_project_also_accepts_id_field(client):
    with patch.object(client, "_post", new=AsyncMock(return_value={"id": "proj-456"})):
        pid = await client.create_project("test")
    assert pid == "proj-456"

async def test_create_project_raises_on_missing_id(client):
    with patch.object(client, "_post", new=AsyncMock(return_value={})):
        with pytest.raises(MiroFishError):
            await client.create_project("test")


# ── generate_report() ────────────────────────────────────────────────────────

async def test_generate_report_returns_report_id(client):
    with patch.object(client, "_post", new=AsyncMock(return_value={"report_id": "rpt-789"})):
        rid = await client.generate_report("proj-123")
    assert rid == "rpt-789"

async def test_generate_report_raises_on_missing_id(client):
    with patch.object(client, "_post", new=AsyncMock(return_value={})):
        with pytest.raises(MiroFishError):
            await client.generate_report("proj-123")


# ── get_report_text() ────────────────────────────────────────────────────────

async def test_get_report_text_prefers_content_key(client):
    resp = {"content": "final report text", "other": "ignored"}
    with patch.object(client, "_get", new=AsyncMock(return_value=resp)):
        text = await client.get_report_text("proj", "rpt")
    assert text == "final report text"

async def test_get_report_text_falls_back_to_text_key(client):
    resp = {"text": "report via text key"}
    with patch.object(client, "_get", new=AsyncMock(return_value=resp)):
        text = await client.get_report_text("proj", "rpt")
    assert text == "report via text key"

async def test_get_report_text_joins_all_strings_if_no_known_key(client):
    resp = {"custom_key": "part one", "another": "part two"}
    with patch.object(client, "_get", new=AsyncMock(return_value=resp)):
        text = await client.get_report_text("proj", "rpt")
    assert "part one" in text
    assert "part two" in text


# ── _wait_task() timeout ─────────────────────────────────────────────────────

async def test_wait_task_raises_on_timeout(client):
    with patch.object(client, "_get", new=AsyncMock(return_value={"status": "running"})):
        with patch("mirofish.client._POLL_INTERVAL", 0.01):
            with pytest.raises(MiroFishError, match="timed out"):
                await client._wait_task("/api/tasks/123", "test task", timeout=0.05)

async def test_wait_task_raises_on_failed_status(client):
    with patch.object(client, "_get", new=AsyncMock(return_value={"status": "failed", "error": "oops"})):
        with patch("mirofish.client._POLL_INTERVAL", 0.01):
            with pytest.raises(MiroFishError, match="failed"):
                await client._wait_task("/api/tasks/123", "test task", timeout=1.0)

async def test_wait_task_returns_on_completed(client):
    with patch.object(client, "_get", new=AsyncMock(return_value={"status": "completed", "data": "ok"})):
        with patch("mirofish.client._POLL_INTERVAL", 0.01):
            result = await client._wait_task("/api/tasks/123", "test task", timeout=1.0, return_result=True)
    assert result["data"] == "ok"


# ── session guard ─────────────────────────────────────────────────────────────

def test_session_raises_outside_context():
    c = MiroFishClient()
    with pytest.raises(RuntimeError):
        _ = c.session
