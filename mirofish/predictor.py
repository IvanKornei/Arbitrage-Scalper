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

# Regex patterns to extract a probability from report text
_PROB_PATTERNS = [
    r"probability[:\s]+([0-9]{1,3})[\s]*%",
    r"chance[:\s]+([0-9]{1,3})[\s]*%",
    r"likelihood[:\s]+([0-9]{1,3})[\s]*%",
    r"([0-9]{1,3})[\s]*%\s+(?:probability|chance|likely|confident)",
    r"YES[:\s]+([0-9]{1,3})[\s]*%",
    r"will\s+(?:likely\s+)?(?:happen|occur|win)[^\d]*([0-9]{1,3})[\s]*%",
    # Decimal form: "0.73" or "73/100"
    r"probability[:\s]+(0\.[0-9]+)",
    r"([0-9]+)\s*/\s*100",
]


def _extract_probability(text: str) -> Optional[float]:
    """Parse a YES probability from free-form report text. Returns None if not found."""
    text_lower = text.lower()
    for pattern in _PROB_PATTERNS:
        m = re.search(pattern, text_lower)
        if m:
            raw = m.group(1)
            value = float(raw)
            # Normalise: if > 1, treat as percentage
            if value > 1.0:
                value = value / 100.0
            if 0.0 <= value <= 1.0:
                return value
    return None


def _majority_vote(text: str) -> Optional[float]:
    """
    Fallback: count sentiment words to estimate YES/NO probability.
    Returns a soft score between 0 and 1.
    """
    yes_words = ["yes", "will", "likely", "probable", "expected", "positive",
                 "bullish", "increase", "rise", "grow", "success", "win"]
    no_words  = ["no", "won't", "unlikely", "improbable", "negative", "bearish",
                 "decrease", "fall", "fail", "loss", "lose", "doubt"]

    t = text.lower()
    yes_count = sum(t.count(w) for w in yes_words)
    no_count  = sum(t.count(w) for w in no_words)
    total = yes_count + no_count
    if total == 0:
        return None
    return yes_count / total


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

            prob = _extract_probability(report_text)
            if prob is None:
                prob = _majority_vote(report_text)
            if prob is None:
                log.warning("[Predictor] Could not extract probability, defaulting to 0.5")
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
            "## Background & Context",
            context,
            "",
            "## Task",
            (
                "Analyse the information above and simulate how different agents "
                "(market participants, experts, media) would react and form opinions. "
                "Based on the simulation, provide a probability estimate (0–100%) "
                "for the question resolving YES. "
                "Be explicit: state 'Probability: X%' in your final conclusion."
            ),
        ]
        return "\n".join(parts)
