"""
mirofish/predictor.py – High-level MiroFish prediction interface.

Given a question and context, orchestrates the full MiroFish pipeline
and extracts a YES probability in [0, 1].

Usage:
    async with MiroFishPredictor(base_url="http://localhost:5001") as p:
        prob = await p.predict("Will X happen?", context="Recent news...", rounds=10)
        # prob = 0.73 → 73% chance YES
"""

from __future__ import annotations

import re
from typing import Any, Optional

from mirofish.client import MiroFishClient, MiroFishError
from utils.logger import get_logger

log = get_logger(__name__)

# Regex patterns tried in order — last lines of report get priority
_PROB_PATTERNS = [
    # Canonical format from prompt: "PROBABILITY: 73%"
    re.compile(r"^probability[:\s]+[~≈]?\s*([0-9]{1,3}(?:\.[0-9]+)?)\s*%", re.IGNORECASE | re.MULTILINE),
    # "Final probability: 73%"
    re.compile(r"final\s+probability[:\s]+[~≈]?\s*([0-9]{1,3}(?:\.[0-9]+)?)\s*%", re.IGNORECASE),
    # "probability of 73%"
    re.compile(r"probability\s+of\s+[~≈]?\s*([0-9]{1,3}(?:\.[0-9]+)?)\s*%", re.IGNORECASE),
    # Generic "X% probability/chance/likely"
    re.compile(r"([0-9]{1,3}(?:\.[0-9]+)?)\s*%\s+(?:probability|chance|likelihood)", re.IGNORECASE),
    # "chance: 73%"
    re.compile(r"chance[:\s]+[~≈]?\s*([0-9]{1,3}(?:\.[0-9]+)?)\s*%", re.IGNORECASE),
    # Decimal: "probability: 0.73"
    re.compile(r"probability[:\s]+(0\.[0-9]+)", re.IGNORECASE),
    # Fraction: "73/100"
    re.compile(r"\b([0-9]{1,3})\s*/\s*100\b"),
]


def _extract_probability(text: str) -> Optional[float]:
    """
    Parse a YES probability from report text.
    Checks the last 10 lines first (where the answer should be), then full text.
    Returns None only if genuinely not found.
    """
    last_lines = "\n".join(text.strip().splitlines()[-10:])
    for search_text in (last_lines, text):
        for pat in _PROB_PATTERNS:
            m = pat.search(search_text)
            if m:
                value = float(m.group(1))
                if value > 1.0:
                    value = value / 100.0
                # Reject exact 0.5 found only via generic patterns (likely a coincidence)
                if 0.01 <= value <= 0.99:
                    return value
    return None


class MiroFishPredictor:
    """
    Orchestrates the full MiroFish prediction workflow:
      upload seed → build graph → prepare simulation → run → report → extract prob
    """

    def __init__(
        self,
        base_url: str = "http://localhost:5001",
        simulation_rounds: int = 10,
        cleanup_after: bool = True,
    ) -> None:
        self._base_url = base_url
        self._rounds = simulation_rounds
        self._cleanup = cleanup_after
        self._client: Optional[MiroFishClient] = None

    async def __aenter__(self) -> "MiroFishPredictor":
        self._client = MiroFishClient(self._base_url)
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.__aexit__(*args)

    @property
    def client(self) -> MiroFishClient:
        assert self._client is not None
        return self._client

    async def is_available(self) -> bool:
        """Return True if MiroFish is reachable."""
        return await self.client.health()

    async def predict(
        self,
        question: str,
        context: str,
        resolution_date: str = "",
        market_id: str = "",
    ) -> float:
        """
        Run a full prediction workflow for a binary question.

        Args:
            question:        The YES/NO question (e.g. "Will BTC exceed $100k by Dec 2025?")
            context:         Seed text — recent news, background info, market data
            resolution_date: ISO date when the market resolves (optional, added to seed)
            market_id:       Polymarket condition ID, used as project title suffix

        Returns:
            Estimated YES probability in [0.0, 1.0].
            Falls back to 0.5 if extraction fails.
        """
        title = f"poly_{market_id[:12] if market_id else 'pred'}_{question[:40]}"
        seed = self._build_seed(question, context, resolution_date)

        log.info("[Predictor] Starting prediction: %s", question[:60])
        project_id: Optional[str] = None

        try:
            project_id = await self.client.create_project(title)
            await self.client.upload_seed_text(project_id, seed)
            await self.client.build_graph(project_id)
            await self.client.prepare_simulation(project_id)
            await self.client.run_simulation(project_id, rounds=self._rounds)
            report_id = await self.client.generate_report(project_id)
            report_text = await self.client.get_report_text(project_id, report_id)

            log.debug(
                "[Predictor] Raw report text (%d chars):\n%s",
                len(report_text),
                report_text[:2000],
            )

            prob = _extract_probability(report_text)
            if prob is not None:
                log.debug("[Predictor] Extracted probability: %.3f", prob)
            else:
                log.warning(
                    "[Predictor] Could not extract probability from report — defaulting to 0.5\n"
                    "Last 300 chars of report: %r",
                    report_text.strip()[-300:],
                )
                prob = 0.5

            log.info("[Predictor] %s → YES prob=%.3f", question[:60], prob)
            return prob

        except MiroFishError as exc:
            log.error("[Predictor] MiroFish error: %s", exc)
            return 0.5   # neutral on failure — no bet placed at 0.5

        finally:
            if self._cleanup and project_id:
                try:
                    await self.client.delete_project(project_id)
                except Exception:
                    pass

    @staticmethod
    def _build_seed(question: str, context: str, resolution_date: str) -> str:
        parts = [
            "# Prediction Market Analysis",
            "",
            f"## Question\n{question}",
            "",
        ]
        if resolution_date:
            parts += [f"## Resolution Date\n{resolution_date}", ""]
        parts += [
            "## Market Data & Context",
            context,
        ]
        return "\n".join(parts)
