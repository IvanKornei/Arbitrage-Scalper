"""Unit tests for mirofish/predictor.py"""
import pytest
from mirofish.predictor import _extract_probability, _majority_vote, MiroFishPredictor


# ── _extract_probability() ────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("Probability: 73%",                         0.73),
    ("probability: 45%",                         0.45),
    ("There is a 60% chance",                    0.60),
    ("likelihood: 80%",                          0.80),
    ("YES: 55%",                                 0.55),
    ("The event will likely happen. 90%",        0.90),
    ("probability: 0.72",                        0.72),
    ("65/100",                                   0.65),
    ("chance: 30%",                              0.30),
    ("I am 85% confident this will happen",      0.85),
])
def test_extract_probability_found(text, expected):
    result = _extract_probability(text)
    assert result is not None
    assert result == pytest.approx(expected, abs=0.001)

def test_extract_probability_none_when_no_number():
    assert _extract_probability("This is vague text with no probability.") is None

def test_extract_probability_ignores_out_of_range():
    """Values outside 0-100 should not be extracted."""
    assert _extract_probability("probability: 150%") is None

def test_extract_probability_case_insensitive():
    assert _extract_probability("PROBABILITY: 55%") is not None

def test_extract_probability_decimal_normalised():
    """0.73 decimal form should return 0.73, not 73."""
    result = _extract_probability("probability: 0.73")
    assert result == pytest.approx(0.73)


# ── _majority_vote() ─────────────────────────────────────────────────────────

def test_majority_vote_bullish_text():
    text = "The market will likely rise. Positive outlook expected. Will succeed."
    result = _majority_vote(text)
    assert result is not None
    assert result > 0.5

def test_majority_vote_bearish_text():
    text = "Unlikely to happen. Will fail. No chance. Loss expected."
    result = _majority_vote(text)
    assert result is not None
    assert result < 0.5

def test_majority_vote_neutral_returns_none():
    """Text with no sentiment words → None."""
    result = _majority_vote("The quick brown fox jumps over the lazy dog.")
    assert result is None

def test_majority_vote_returns_float_between_0_and_1():
    text = "likely likely unlikely"
    result = _majority_vote(text)
    assert result is not None
    assert 0.0 <= result <= 1.0


# ── MiroFishPredictor._build_seed() ──────────────────────────────────────────

def test_build_seed_contains_question():
    seed = MiroFishPredictor._build_seed("Will BTC hit 100k?", "context here", "")
    assert "Will BTC hit 100k?" in seed

def test_build_seed_contains_context():
    seed = MiroFishPredictor._build_seed("Q?", "important context", "")
    assert "important context" in seed

def test_build_seed_contains_resolution_date_when_provided():
    seed = MiroFishPredictor._build_seed("Q?", "ctx", "2025-06-01")
    assert "2025-06-01" in seed

def test_build_seed_no_resolution_date_section_when_empty():
    seed = MiroFishPredictor._build_seed("Q?", "ctx", "")
    assert "Resolution Date" not in seed

def test_build_seed_instructs_probability_output():
    seed = MiroFishPredictor._build_seed("Q?", "ctx", "")
    assert "Probability" in seed
