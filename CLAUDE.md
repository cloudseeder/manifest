# CLAUDE.md

## Project Overview

Manifest — a companion chat app with autonomous task execution, powered by local AI. Four services running on a Mac Mini: discovery (tool bridge + experience cache), agent (chat UI + task scheduler), reminder, and email scanner. All inter-service communication is HTTP. The agent never talks to Ollama directly — it calls `/v1/chat` on the discovery service for all LLM and tool work.

Extracted from the [OAP monorepo](https://github.com/cloudseeder/oap-dev). The OAP repo retains the spec, Next.js website, trust service, dashboard, and MCP server.

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
- CLI auth: `oap --token <secret>` or `OAP_BACKEND_TOKEN` env var. Required when `OAP_BACKEND_SECRET` is set on the server.
- Config: `config.yaml` (Ollama URL, ChromaDB path, FTS path, crawler settings). Gitignored — track `config.yaml.example` instead. Copy to `config.yaml` on first deploy.
- Key files: `models.py` (Pydantic types), `validate.py` (validation), `crawler.py`, `db.py` (ChromaDB), `fts_store.py` (SQLite FTS5), `discovery.py` (vector search + FTS5 + LLM + intent extraction), `api.py` (FastAPI), `ollama_client.py` (Ollama API client), `openapi_server.py` (OpenAPI 3.1 tool server), `config.py` (configuration), `cli.py` (CLI entry point)
- **Intent extraction**: `discovery.py:_extract_search_query(task)` strips inline data and normalizes colloquial language before embedding. Drops data after `\n`, strips trailing prepositions, normalizes verbs (`pull out` → `filter`), appends domain hints. The cleaned query goes to vector search; the full task still goes to LLM ranking unchanged.
- **FTS5 keyword search**: `fts_store.py` provides SQLite FTS5 with BM25 ranking as a complement to vector search. Config: `fts.enabled` (bool, default false), `fts.db_path`. Env overrides: `OAP_FTS_ENABLED`, `OAP_FTS_DB_PATH`. Deterministic keyword matching filling gaps where vector search drifts.
- **Procedural memory** (enabled by default via `experience.enabled: true`): Dual-store architecture — SQLite (`oap_experience.db`) for record persistence + ChromaDB (`experience_vectors/`) for embedding similarity lookup. Two-path routing in tool bridge: (1) **cache_hit** — vector similarity match (cosine distance < 0.25) or exact fingerprint match → replay cached invocation; (2) **full_discovery** — no match → full vector search + LLM ranking → execute → cache. The `/v1/experience/invoke` endpoint retains three-path routing: cache_hit, partial_match, full_discovery. Files: `experience_models.py`, `experience_store.py` (`ExperienceStore` + `ExperienceVectorStore`), `experience_engine.py`, `experience_api.py` (router at `/v1/experience/`), `invoker.py`.
- **Ollama tool bridge** (enabled by default via `tool_bridge.enabled: true`): `POST /v1/chat` and `POST /api/chat` — transparent Ollama proxy that discovers tools, injects them, executes tool calls, and loops up to `max_rounds`. The `/api/chat` alias makes OAP a drop-in Ollama replacement (`OLLAMA_HOST=http://localhost:8300 ollama run qwen3:8b`). Streaming wraps final result in Ollama NDJSON format. **Ollama pass-through**: non-chat `/api/*` endpoints proxy directly to Ollama. Tool bridge routes have **no backend token auth** — local-only, secured by Cloudflare Tunnel path filtering. Key files: `tool_models.py`, `tool_converter.py`, `tool_executor.py`, `tool_api.py`.
- **Chat system prompt** (`tool_api.py`): "NEVER answer without calling a tool", API tool preference for web/API data, oap_exec for CLI tasks, pipe/jq examples, inline-text stdin guidance. States "API credentials are pre-configured" so LLMs don't refuse to call authed APIs. Combined with `think: false` (default) keeps qwen3:8b to ~12 tokens per round.
- **Conditional thinking**: `tool_bridge.think_prefixes` (list of fingerprint prefixes, default empty). When a task's fingerprint starts with a listed prefix, `think: true` is sent to Ollama so the model can verify tool output (e.g. arithmetic). Config: `think_prefixes: [compute]`. Debug output includes `thinking_enabled: true/false`.
- **Big LLM escalation**: `tool_bridge.escalate_prefixes` (list of fingerprint prefixes, default empty). When a task's fingerprint starts with a listed prefix and `escalation.enabled: true`, the final reasoning step is sent to an external big LLM (GPT-4, Claude, etc.) instead of the small model. The small model still handles tool discovery and execution. Additionally, large `oap_exec` results (>`summarize_threshold` chars) are automatically escalated to the big LLM when escalation is enabled, bypassing lossy map-reduce summarization — this catches any large-output scenario regardless of fingerprint prefix. Config: `escalate_prefixes: [compute]` + `escalation:` section with `provider` (`openai` or `anthropic`), `base_url`, `model`, `timeout`. API key resolution: `escalation.api_key` > `OAP_ESCALATION_API_KEY` > provider-specific (`OAP_OPENAI_API_KEY`, `OAP_ANTHROPIC_API_KEY`, `OAP_GOOGLEAI_API_KEY`). Per-provider env vars let you switch providers without redeploying. Provider `googleai` uses OpenAI-compatible path with `base_url: https://generativelanguage.googleapis.com/v1beta/openai`. Fails silently — falls back to small LLM response on any error. Debug output includes `escalated: true/false`. Key file: `escalation.py`.
- **Multi-tool injection**: `_discover_tools()` injects up to `MAX_INJECTED_TOOLS = 3` tools per chat round — LLM's top pick plus next highest-scoring candidates (deduped by domain).
- **oap_exec meta-tool**: Built-in tool always injected first in every `/v1/chat` round. Accepts `command` + optional `stdin`. Bridges LLM CLI knowledge to tool calls (LLMs write better regex in CLI syntax than in tool parameters). Supports shell-style pipes via `_split_pipeline()`. Security: `shlex.split()` parsing, PATH allowlist validation (`/usr/bin/`, `/usr/local/bin/`, `/bin/`, `/opt/homebrew/bin/`), `asyncio.create_subprocess_exec()` (no `shell=True`), `blocked_commands` config (default: `[rm, rmdir, dd, mkfs, shutdown, reboot]`) — bare-name matching per pipeline stage via `os.path.basename()` so both `rm` and `/bin/rm` are caught. Override via `tool_bridge.blocked_commands` in `config.yaml`; set to `[]` to allow all. File path detection (`_task_has_file_path`) suppresses discovery when file paths present — `oap_exec` becomes the only tool.
- **Sandbox** (`sandbox.py`): OS-level file-write protection via macOS `sandbox-exec`. All subprocess execution (oap_exec single commands, pipelines, and manifest stdio tools) is wrapped with a Seatbelt profile that denies file writes except to a configurable sandbox directory. Config: `tool_bridge.danger_will_robinson` (default `false` — sandbox ON; set `true` to disable), `tool_bridge.sandbox_dir` (default `/tmp/oap-sandbox`). Env overrides: `OAP_TOOL_BRIDGE_DANGER_WILL_ROBINSON`, `OAP_TOOL_BRIDGE_SANDBOX_DIR`. Graceful degradation on Linux (unsandboxed, warning logged). The system prompt tells the LLM to write output files to the sandbox directory. Three wrapped call sites: `tool_executor.py:_run_single()`, `tool_executor.py:_run_pipeline()`, `invoker.py:_invoke_stdio()`.
- **Stdio tool suppression**: After discovery, stdio tools are filtered out — only `oap_exec` and HTTP/API tools remain. Rationale: small LLMs prefer "named" tools over generic `oap_exec` but produce worse results with them.
- **Credential injection** (`tool_executor.py:_inject_credentials`): Injects API keys from `credentials.yaml` into tool calls at execution time. Supports two placement modes via manifest `invoke.auth_in`:
  - `auth_in: "header"` (default) — key added as HTTP header (name from `auth_name`, default `X-API-Key`)
  - `auth_in: "query"` — key returned as extra query params, merged into request params before `invoke_manifest`
  - `auth: "bearer"` — key added as `Authorization: Bearer <key>` header
  - **Domain lookup**: first tries the indexed domain (e.g. `local/alpha-vantage`), then falls back to the invoke URL hostname (e.g. `www.alphavantage.co`). This lets `credentials.yaml` use real domain names for local manifests.
  - **credentials.yaml format**: domain-keyed YAML, loaded via `config.py:load_credentials()`. Path configured in `tool_bridge.credentials_file` (default `credentials.yaml`, relative to CWD).
  - Credential injection is transparent to the LLM — the system prompt tells it "API credentials are pre-configured" so it always calls the tool.
- **OpenAPI tool server** (enabled when `tool_bridge.enabled: true`): `openapi_server.py` at `/v1/openapi.json` and `/v1/tools/call/{tool_name}`. Standard OpenAPI 3.1 tool server for Open WebUI, LangChain, etc. Exposes all manifests (no stdio suppression). Same security and credential injection as chat flow.
- **Experience cache in tool bridge**: `/v1/chat` uses procedural memory as a discovery cache with dual-store architecture: SQLite (system of record) + ChromaDB (vector index for similarity lookup). Primary path: embed task with nomic-embed-text (~50ms) → ChromaDB cosine search → cache hit if distance < `vector_similarity_threshold` (default 0.25) and confidence ≥ 0.85. Fallback: exact fingerprint match in SQLite. On miss, full discovery → cache on success. Vector similarity replaced fingerprint string matching as the cache key because LLM fingerprints are non-deterministic (same intent produces different fingerprints). Degradation: errors multiply confidence by 0.7, single failure drops below threshold. Negative caching stores failures with `CorrectionEntry` records for self-correction hints. Backfill migration: on startup, if vector collection is empty but SQLite has records, all task texts are embedded and upserted. Config: `experience.vector_similarity_threshold` (cosine distance: 0=identical, lower=more similar). ChromaDB collection stored at `<chromadb.path>/experience_vectors/`. Key files: `experience_store.py` (`ExperienceStore` + `ExperienceVectorStore`).
- **Fingerprint optimization**: `fingerprint_intent()` uses `chat(think=False, temperature=0, format="json")` for deterministic ~15-token output in ~1s. JSON-aware fingerprints separate JSON tasks from text tasks in fingerprint space. Fingerprints are still used for logging, failure tracking, blacklisting, experience hints, and conditional thinking/escalation prefix matching — just no longer the primary cache key.
- **Experience hints**: `_build_experience_hints(fingerprint)` injects past failure/success hints into system prompt. Only exact-match failures (prefix matching was too aggressive). Prefix successes suggest what works for similar tasks.
- Local manifests (`discovery/manifests/`): JSON files auto-indexed on startup under `local/<tool-name>` pseudo-domains. Curated HTTP API manifests: `alpha-vantage.json`, `newsapi-top-headlines.json`, `newsapi-everything.json`, `open-meteo.json`, `wikipedia.json`, plus service manifests: `oap-reminder.json`, `oap-email.json`.
- **Seed domain crawling on startup**: `api.py` lifespan crawls remote domains from `seeds.txt` after indexing local manifests. Seeds file: `discovery/seeds.txt`.
- **Map-reduce summarization**: fallback for large tool results when big LLM escalation is not configured. Hierarchy: big LLM (if `escalation.enabled`) → map-reduce via `ollama.generate()` → truncation. Configured via `ToolBridgeConfig` fields: `summarize_threshold` (default 16000 chars), `chunk_size`, `max_tool_result`.
- **Debug mode**: `POST /v1/chat` accepts `oap_debug: true` for full execution trace including tools discovered, experience cache status, fingerprint, hints, and per-round tool executions with timing.
- **Auth model**: Backend token auth (`X-Backend-Token` / `OAP_BACKEND_SECRET`) is per-route, not global. Protected: `/v1/discover`, `/v1/manifests`, `/v1/manifests/{domain}`, `/health`, `/v1/experience/*`. Unprotected (local-only): `/v1/chat`, `/v1/tools`, `/api/chat`, `/v1/openapi.json`, `/v1/tools/call/*`, `/api/tags`, `/api/show`, `/api/ps`, `/api/generate`, `/api/embed`, `/api/embeddings`
- **Ollama tuning**: `num_ctx: 4096` (caps VRAM on 16GB), `timeout: 120`, `keep_alive: "-1m"` (permanent model loading). Model warmup on startup via throwaway `generate("hello")`. Override with `OAP_OLLAMA_NUM_CTX`. qwen3:8b at 4k context uses ~5.9GB VRAM, fitting alongside nomic-embed-text.

### Agent (`agent/oap_agent/`)

Manifest — chat + autonomous task execution. Thin orchestrator that calls `/v1/chat` on the discovery service for all LLM and tool work — never talks to Ollama directly. Combines interactive conversation with cron-scheduled background tasks. Self-contained: `oap-agent-api` serves both API and UI at `http://localhost:8303` — no Node runtime, no Vercel involvement.

- Entry point: `oap-agent-api` (:8303) — serves both FastAPI backend and Vite SPA frontend
- Config: `config.yaml` (host, port, SQLite path, discovery URL/model/timeout, debug flag, max_tasks)
- Key files: `config.py`, `db.py` (SQLite: conversations, messages, tasks, task_runs, notifications, agent_settings, user_facts, llm_usage — WAL mode, foreign keys), `executor.py` (calls `/v1/chat` on discovery), `scheduler.py` (APScheduler 3.x + notification creation), `events.py` (EventBus), `api.py` (FastAPI + SSE + greeting briefing + notifications + StaticFiles mount), `memory.py` (user fact extraction via Ollama pass-through)
- **Frontend** (`frontend/`): Vite 6 + React 19 + React Router 7 + Tailwind CSS 4 SPA. Built output committed to `oap_agent/static/`. Dev: `cd frontend && npm run dev`. Build: `npm run build` outputs to `../oap_agent/static/`.
- SPA routes: `/` (redirect to `/chat`), `/chat`, `/chat/:id`, `/tasks`, `/tasks/:id`, `/settings`
- API routes: `/v1/agent/chat` (POST SSE), `/v1/agent/conversations` (CRUD), `/v1/agent/tasks` (CRUD), `/v1/agent/tasks/:id/run` (POST), `/v1/agent/tasks/:id/runs` (GET), `/v1/agent/settings` (GET/PATCH), `/v1/agent/memory` (GET), `/v1/agent/memory/:id` (DELETE), `/v1/agent/models` (GET — dynamic from Ollama), `/v1/agent/notifications` (GET/dismiss/count), `/v1/agent/tts` (POST audio/wav), `/v1/agent/tts/voices` (GET), `/v1/agent/voice/status` (GET), `/v1/agent/transcribe` (POST), `/v1/agent/events` (SSE), `/v1/agent/health` (GET)
- Task scheduling: APScheduler in-process, cron validation rejects intervals < 5 minutes, max 20 tasks
- **Chat priority over tasks**: Ollama processes requests serially, so a running background task blocks conversational responses. When a user sends a chat message while a task is running:

  | Ollama busy? | Conversational? | Escalation enabled? | Action |
  |---|---|---|---|
  | Yes | Yes | Yes | Escalate to big LLM — task keeps running on Ollama |
  | Yes | Yes | No | Cancel task, use Ollama |
  | Yes | No (tools) | — | Cancel task, use Ollama (tools need discovery) |
  | No | — | — | Normal path, no change |

  Escalation config in agent `config.yaml`: `escalation: {enabled: true, provider: anthropic, model: claude-sonnet-4-6}`. Uses same env var cascade as discovery: `OAP_ESCALATION_API_KEY` > `OAP_ANTHROPIC_API_KEY`. Falls back to cancel+Ollama if escalation fails. Cancelled tasks retry on next cron schedule. Key files: `scheduler.py` (`is_active()`, `cancel_active()`), `executor.py` (`execute_escalated()`).
- Input validation: model names validated by length (max 100 chars), available models fetched dynamically from Ollama `/api/tags`. `max_length` on all string fields
- **Notification queue**: Tasks produce notifications on completion (type=`task_result`, body=first 200 chars). Notifications power the greeting briefing and avatar badge. SSE `notification_new` events update the frontend badge count in real-time. See `docs/AGENT.md` for full event model.
- **Greeting briefing**: When the first message of a conversation is a greeting (hello, good morning, etc.), pending notifications are injected as system context and the LLM produces a natural briefing. Notifications are dismissed after presentation.
- **Personality + user memory** (agent-owned, configured via Settings UI). Named persona is prepended as a system message to every `/v1/chat` call. User memory learns facts about the user from conversations via fire-and-forget LLM extraction (calls `/api/generate` on discovery's Ollama pass-through). Facts stored in `user_facts` table with UNIQUE dedup and LRU eviction. Settings stored in `agent_settings` table, seeded with defaults on first run. Key file: `memory.py`.
- **Voice** (STT + TTS): Local-first voice input/output. STT via `faster-whisper` (CTranslate2) on the backend — mic → MediaRecorder WebM → `POST /v1/agent/transcribe` → text in chat input. TTS via Piper neural voices on the backend — `POST /v1/agent/tts` → WAV → `HTMLAudioElement` playback. Config: `voice.enabled` (default true), `voice.whisper_model` (tiny/base/small, default base), `voice.device` (auto/cpu/cuda), `voice.compute_type` (auto/int8/float16/float32), `voice.language` (null = auto-detect), `voice.tts_enabled` (default true), `voice.tts_model_path` (path to `.onnx` voice), `voice.tts_models_dir` (default `piper-voices`). Settings: `voice_input_enabled`, `voice_auto_send`, `voice_auto_speak` (persisted in `agent_settings`). Key files: `transcribe.py`, `tts.py`, `api.py` (transcribe + TTS endpoints), frontend hooks `useVoiceRecorder.ts` + `useTTS.ts`. See `docs/PIPER.md` for setup and voice model management.
- See `docs/AGENT.md` for full architecture rationale and design decisions

### Reminder (`reminder/oap_reminder/`)

SQLite-backed reminder service for AI agents. Supports one-time and recurring reminders (daily, weekly, monthly, yearly).

- Entry point: `oap-reminder-api` (:8304)
- Config: `config.yaml` (SQLite path, host, port). DB path resolved relative to config file directory; defaults to `$HOME/oap_reminder.db` without config.
- Schema: `id`, `title`, `notes`, `created_at`, `due_date`, `due_time`, `recurring`, `status`, `completed_at`
- Key files: `db.py` (SQLite CRUD + recurrence + cleanup), `models.py` (Pydantic validation), `api.py` (FastAPI), `config.py`
- API: `POST /reminders`, `GET /reminders`, `GET /reminders/due`, `GET/PATCH/DELETE /reminders/{id}`, `POST /reminders/{id}/complete`, `POST /reminders/cleanup`
- Recurring: completing a recurring reminder auto-creates the next occurrence with the computed due date
- Cleanup: `POST /reminders/cleanup?older_than_days=30` or `oap-reminder-api --cleanup 30` (for cron)
- Manifest: `discovery/manifests/oap-reminder.json`

### Email Scanner (`email/oap_email/`)

IMAP email scanner for AI agents with LLM-powered classification and auto-filing. Two-phase design: `POST /scan` fetches from IMAP and caches to SQLite, read endpoints query local cache. UID-based incremental scanning. Optional auto-filing moves classified messages into IMAP folders.

- Entry point: `oap-email-api` (:8305)
- Config: `config.yaml` (IMAP host/port/credentials, folders, SQLite path, default scan hours, classifier settings, auto-file settings)
- Key files: `config.py` (IMAPConfig + ClassifierConfig + AutoFileConfig + Config), `imap.py` (stdlib imaplib + asyncio.to_thread + IMAP move), `db.py` (SQLite message cache with upsert/search/thread grouping + category + filed tracking), `sanitize.py` (HTML→text + prompt injection filtering), `models.py` (Pydantic types), `api.py` (FastAPI + dispatch endpoint + background classification + auto-filing), `classifier.py` (LLM categorization via Ollama)
- API: `POST /scan`, `GET /messages`, `GET /messages/{id}`, `GET /threads/{thread_id}`, `GET /summary`, `POST /classify`, `POST /reclassify`, `POST /file`, `POST /api` (single-endpoint dispatcher for OAP manifests), `GET /health`
- **Classifier**: Categorizes messages using a local LLM via Ollama. Default categories: `personal`, `machine`, `mailing-list`, `spam`, `offers`. Categories are user-configurable via `classifier.categories` in config.yaml — each entry is a name + description that gets built into the system prompt. User categories merge with defaults; add a category name and description in YAML, no code changes needed. Runs in background after scan; `POST /classify` for manual trigger. `POST /reclassify` resets all categories and re-runs classification. Config: `classifier.enabled` (bool), `classifier.model`, `classifier.ollama_url`, `classifier.timeout`, `classifier.categories` (dict of name → description).
- **Auto-filing**: Moves classified messages to IMAP folders based on category. `POST /file` processes unfiled messages, COPY+DELETE via IMAP, creates target folders if they don't exist. Messages are marked as filed in the DB to prevent re-processing. Config: `auto_file.enabled` (bool), `auto_file.folders` (category → IMAP folder name mapping). Designed to run as a cron job after scan: `curl -s -X POST localhost:8305/scan && curl -s -X POST localhost:8305/file`.
- **Timestamp normalization**: All `received_at` timestamps are stored in UTC (`+00:00`) for correct SQLite string comparison across timezone offsets. Migration auto-normalizes existing data on startup.
- **Query parser**: Supports `OR` between terms and field prefixes (`from:`, `to:`, `subject:`, `body:`). Examples: `from:Amy OR from:Keric`, `from:amy@netgate.net subject:invoice`.
- Manifest: `discovery/manifests/oap-email.json`

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
