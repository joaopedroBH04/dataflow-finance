# Contributing to DataFlow Finance

## Prerequisites

- Python 3.11+
- `make` (GNU Make or compatible)
- A local `.env` based on `.env.example`

## Setup

```bash
git clone https://github.com/joaopedroBH04/dataflow-finance.git
cd dataflow-finance
pip install -r requirements-dev.txt
cp .env.example .env
```

## Development workflow

```bash
make setup        # First-run: copies .env.example → .env and installs dev deps
make run          # Start dev server at http://localhost:8000
make test         # Run test suite (pytest)
make lint         # Lint + auto-fix with ruff
make type-check   # Static type check with mypy
make check        # lint + type-check in one command (CI shortcut)
make clean        # Remove __pycache__, logs, caches
```

Run `make help` for the full list.

> Tool configuration (ruff, mypy, pytest) lives in `pyproject.toml` — no flags needed in CLI calls.

## Project structure

```
etl_service/
├── config.py           # All env-driven config (pydantic-settings)
├── main.py             # FastAPI app + ETL endpoint
├── api/
│   ├── alerts.py       # Webhook alert subscriptions
│   ├── leads.py        # Landing page lead capture
│   └── metrics.py      # Dashboard KPI endpoint
├── extractors/         # BaseExtractor + iFood / PDV / Stone-Cielo connectors
├── transformers/       # FinancialTransformer + DRE + gap detection
├── validators/         # Pydantic v2 schemas per data source
└── loaders/            # Excel + JSON report writer
```

## Adding a new data source

1. Create `etl_service/extractors/my_source.py`
2. Subclass `BaseExtractor` and implement `_read_raw` and `_normalise_columns`
3. Add a Pydantic schema in `validators/schemas.py`
4. Wire it into `main.py` following the iFood/PDV pattern

No changes to existing code are needed — the Open/Closed Principle is enforced by `BaseExtractor`.

## Commit conventions

This project uses [Conventional Commits](https://www.conventionalcommits.org/):

| Prefix | When to use |
|---|---|
| `feat(frontend):` | New feature in HTML/JS |
| `feat(api):` | New endpoint or backend feature |
| `fix(frontend):` | Bug fix in HTML/CSS/JS |
| `fix(api):` | Bug fix in Python |
| `perf(frontend):` | Performance improvement |
| `a11y(frontend):` | Accessibility improvement |
| `copy(frontend):` | Copy / UX text change |
| `security:` | Security improvement |
| `docs:` | Documentation only |
| `refactor:` | Refactor without behaviour change |

Example:
```
feat(api): add configurable CORS origins via env var

- Add allowed_origins field to config.py (pydantic-settings)
- Wire settings.allowed_origins into CORSMiddleware in main.py
- Fix .env.example to remove phantom DATAFLOW_API_KEY variable
```

## Environment variables

See `.env.example` for the full list. Key variables:

| Variable | Default | Description |
|---|---|---|
| `DATAFLOW_DEBUG` | `false` | Enable debug mode |
| `DATAFLOW_LOG_LEVEL` | `INFO` | Loguru level |
| `DATAFLOW_ALLOWED_ORIGINS` | `["*"]` | CORS origins (JSON array) |
| `DATAFLOW_LEADS_API_KEY` | *(empty)* | Protects `GET /leads` in production |
| `DATAFLOW_OUTPUT_DIR` | `./output` | Where ETL output files are written |

## Automated daily improvements

This repository uses a scheduled Claude Code agent that runs every day and applies one category of improvement per weekday:

| Day | Category |
|---|---|
| Monday | UX & Accessibility |
| Tuesday | Performance & SEO |
| Wednesday | Copy & Conversion |
| Thursday | Backend & API |
| Friday | Design & Animations |
| Saturday | Security & LGPD |
| Sunday | Documentation & DevEx |

Each run produces at least one commit following the conventions above. Check `CHANGELOG.md` for the running log of changes.
