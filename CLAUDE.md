# CLAUDE.md

## Project Overview

Manifest — a companion chat app with autonomous task execution, powered by local AI. Four services running on a Mac Mini: discovery (tool bridge + experience cache), agent (chat UI + task scheduler), reminder, and email scanner. All inter-service communication is HTTP. The agent never talks to Ollama directly — it calls `/v1/chat` on the discovery service for all LLM and tool work.

## Repository Structure

```
manifest/
├── setup.sh                     # One-command install + launchd setup
├── discovery/                   # Discovery API, tool bridge, experience cache
│   ├── pyproject.toml
│   ├── config.yaml.example
│   ├── credentials.example.yaml
│   ├── seeds.txt
│   ├── manifests/               # Curated HTTP/service manifests
│   └── oap_discovery/           # Python package
├── agent/                       # Chat UI + task scheduler
│   ├── pyproject.toml
│   ├── config.yaml.example
│   ├── oap_agent/               # Python package (includes static/ built SPA)
│   └── frontend/                # Vite + React source
├── reminder/                    # Reminder service
│   ├── pyproject.toml
│   └── oap_reminder/
├── email/                       # Email scanner
│   ├── pyproject.toml
│   ├── config.yaml.example
│   └── oap_email/
└── docs/
    ├── AGENT.md                 # Architecture rationale
    ├── PIPER.md                 # Voice setup
    └── SECURITY.md              # Security model
```

## Commands

```bash
# Create venv and install
$(brew --prefix python@3.12)/bin/python3.12 -m venv ~/.oap-venv
source ~/.oap-venv/bin/activate
pip install -e discovery
pip install -e agent
pip install -e reminder
pip install -e email

# Copy configs
cp discovery/config.yaml.example discovery/config.yaml
cp agent/config.yaml.example agent/config.yaml
cp email/config.yaml.example email/config.yaml

# Start all services via launchd
./setup.sh
```

## Services

### Discovery (`discovery/oap_discovery/`)

Crawls domains for OAP manifests, embeds descriptions into ChromaDB via Ollama (nomic-embed-text), and serves a discovery API that matches natural language tasks to manifests using vector search + FTS5 keyword search + small LLM (qwen3:8b).

- Entry points: `oap-api` (:8300), `oap-crawl`, `oap`
- Config: `config.yaml` (Ollama URL, ChromaDB path, FTS path, crawler settings)
- Key files: `models.py`, `validate.py`, `crawler.py`, `db.py` (ChromaDB), `fts_store.py` (SQLite FTS5), `discovery.py` (vector search + FTS5 + LLM + intent extraction), `api.py` (FastAPI), `ollama_client.py`, `openapi_server.py`, `config.py`, `cli.py`
- **Tool bridge** (`tool_api.py`): `POST /v1/chat` and `POST /api/chat` — transparent Ollama proxy that discovers tools, injects them, executes tool calls, and loops. The `/api/chat` alias makes it a drop-in Ollama replacement. Key files: `tool_models.py`, `tool_converter.py`, `tool_executor.py`, `tool_api.py`
- **oap_exec meta-tool**: Built-in tool always injected first. Accepts `command` + optional `stdin`. Security: `shlex.split()`, PATH allowlist, `blocked_commands` config, sandbox via `sandbox.py`
- **Experience cache**: Dual-store (SQLite + ChromaDB vectors). Vector similarity for cache lookup, exact fingerprint as fallback. Key files: `experience_models.py`, `experience_store.py`, `experience_engine.py`, `experience_api.py`
- **Big LLM escalation** (`escalation.py`): Final reasoning step sent to external LLM (GPT-4, Claude, Gemini). Config: `escalation:` section in `config.yaml`. Provider env vars: `OAP_OPENAI_API_KEY`, `OAP_ANTHROPIC_API_KEY`, `OAP_GOOGLEAI_API_KEY`
- **Credential injection** (`tool_executor.py`): API keys from `credentials.yaml` injected into tool calls at execution time
- **Auth**: `X-Backend-Token` / `OAP_BACKEND_SECRET` on protected routes (`/v1/discover`, `/v1/manifests`, `/health`, `/v1/experience/*`). Tool bridge routes (`/v1/chat`, `/api/chat`) are unprotected (local-only)

### Agent (`agent/oap_agent/`)

Chat + autonomous task execution. Thin orchestrator that calls `/v1/chat` on discovery for all LLM work.

- Entry point: `oap-agent-api` (:8303) — serves both FastAPI API and Vite SPA
- Config: `config.yaml` (host, port, SQLite path, discovery URL/model/timeout, debug, max_tasks)
- Key files: `config.py`, `db.py` (SQLite: conversations, messages, tasks, task_runs, notifications, agent_settings, user_facts, llm_usage), `executor.py`, `scheduler.py` (APScheduler), `events.py` (EventBus), `api.py`, `memory.py`
- **Frontend** (`frontend/`): Vite 6 + React 19 + React Router 7 + Tailwind CSS 4. Built output in `oap_agent/static/`. Dev: `cd frontend && npm run dev`. Build: `npm run build`
- **Chat priority over tasks**: When a user sends a message while a background task is running on Ollama, the task is cancelled (or escalated to big LLM if configured). Key files: `scheduler.py`, `executor.py`
- **Personality + user memory**: Named persona prepended as system message. User facts extracted from conversations via fire-and-forget LLM call. Key file: `memory.py`
- **Voice**: STT via faster-whisper, TTS via Piper. Key files: `transcribe.py`, `tts.py`

### Reminder (`reminder/oap_reminder/`)

SQLite-backed reminder service. Supports one-time and recurring reminders.

- Entry point: `oap-reminder-api` (:8304)
- API: `POST /reminders`, `GET /reminders`, `GET /reminders/due`, `PATCH/DELETE /reminders/{id}`, `POST /reminders/{id}/complete`, `POST /reminders/cleanup`

### Email (`email/oap_email/`)

IMAP email scanner with LLM classification and auto-filing.

- Entry point: `oap-email-api` (:8305)
- Config: `config.yaml` (IMAP credentials, classifier settings, auto-file folders)
- API: `POST /scan`, `GET /messages`, `GET /summary`, `POST /classify`, `POST /file`, `GET /health`

## Key Design Principles

- The agent never talks to Ollama directly — all LLM work goes through discovery's `/v1/chat`
- All inter-service communication is HTTP
- All file path resolution uses `Path(__file__).parent.parent` (no hardcoded paths)
- `setup.sh` is the single entry point for deployment — creates launchd plists for all services
- Services auto-start on reboot via launchd `KeepAlive`

## Architecture

```
Mac Mini (M4, 16GB)
┌──────────────────────────────────┐
│  Ollama (qwen3:8b + nomic)       │
│  Discovery API (:8300)           │
│  Agent (:8303) ← chat UI + tasks │
│  Reminder API (:8304)            │
│  Email Scanner (:8305)           │
│  Crawler (hourly cron)           │
│  ChromaDB (local dir)            │
│  SQLite (*.db)                   │
└──────────────────────────────────┘
```
