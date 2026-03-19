.PHONY: install install-dev test test-v run run-live creds clean

# ── Setup ─────────────────────────────────────────────────────────────────────

install:
	pip install -r requirements.txt

install-dev:
	pip install -r requirements-dev.txt

# ── Tests ─────────────────────────────────────────────────────────────────────

test:
	pytest tests/

test-v:
	pytest tests/ -v

test-cov:
	pytest tests/ --cov=. --cov-report=term-missing

# ── Run ───────────────────────────────────────────────────────────────────────

run:
	DRY_RUN=true python polymarket_main.py

run-live:
	DRY_RUN=false python polymarket_main.py

# ── Credentials ───────────────────────────────────────────────────────────────

creds:
	python tools/generate_poly_creds.py

# ── Cleanup ───────────────────────────────────────────────────────────────────

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; \
	find . -name "*.pyc" -delete; \
	rm -rf .pytest_cache
