# DataFlow Finance — developer shortcuts
# Usage: make <target>
# Requires: Python 3.11+, pip, GNU Make (or nmake on Windows)

.PHONY: help install install-dev run test lint type-check clean

help:
	@echo ""
	@echo "DataFlow Finance — available targets"
	@echo "-------------------------------------"
	@echo "  install       Install production dependencies"
	@echo "  install-dev   Install all dependencies (incl. dev/test)"
	@echo "  run           Start FastAPI dev server on port 8000"
	@echo "  test          Run test suite with pytest"
	@echo "  lint          Lint + auto-fix with ruff"
	@echo "  type-check    Run mypy static type checker"
	@echo "  clean         Remove __pycache__, .mypy_cache, logs"
	@echo ""

install:
	pip install -r requirements.txt

install-dev:
	pip install -r requirements-dev.txt

run:
	uvicorn etl_service.main:app --reload --port 8000

test:
	pytest tests/ -v --tb=short

lint:
	ruff check . --fix
	ruff format .

type-check:
	mypy etl_service/ --ignore-missing-imports

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	rm -rf logs/*.log logs/*.zip 2>/dev/null || true
