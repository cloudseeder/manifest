# Discovery Service — Architecture and Design

The discovery service (`oap_discovery`, :8300) is the core of the OAP stack. It does three things: finds the right tool for any natural language task, executes that tool securely, and remembers what worked. The agent, CLI, MCP server, and Open WebUI all route through it — nothing talks to Ollama directly.

## Why a Separate Service?

Every AI system that calls tools faces the same problem: you can't hardcode tools at model load time. New APIs appear, old ones change, and the set of useful tools for one user is completely different from another. OAP solves this by making tool discovery a runtime operation — a database query + LLM ranking that happens on every request.

The discovery service is stateless per request (no conversations, no sessions) and focused on a single job: given a natural language task, find the best tool, execute it, and return the result. The agent adds the stateful layer (conversations, scheduling, notifications) on top.

## Architecture

```
POST /v1/chat  (tool bridge — Ollama-compatible)
POST /api/chat (alias — drop-in Ollama replacement)
        │
        ├─ oap_exec (always injected, CLI bridge)
        ├─ Manifest discovery
        │    ├─ Experience cache (vector similarity + SQLite)
        │    ├─ ChromaDB vector search
        │    ├─ FTS5 keyword search (BM25)
        │    └─ LLM ranking (qwen3:8b)
        │
        └─ Tool execution
             ├─ HTTP manifest invocations (httpx)
             ├─ stdio manifest invocations (subprocess)
             ├─ Credential injection (credentials.yaml)
             └─ Sandbox (macOS sandbox-exec)
```

## Manifest Discovery

A manifest is a JSON file that describes a capability: what it does, how to call it, what it returns. The discovery service indexes manifests and finds the right one at runtime.

### Three-layer ranking

1. **Experience cache** — checked first. Embeds the task with nomic-embed-text (~50ms), searches ChromaDB by cosine similarity (threshold 0.25). A cache hit replays the known-good invocation without LLM ranking. Falls back to exact fingerprint match in SQLite. Cache misses proceed to full discovery.

2. **Vector + FTS5 search** — ChromaDB cosine similarity on manifest descriptions, plus SQLite FTS5 BM25 keyword matching. Intent extraction normalizes the task before embedding: strips inline data, drops trailing prepositions, normalizes colloquial verbs (`pull out` → `filter`). The two search paths complement each other — vector search handles semantic similarity, FTS5 handles exact keyword matches where vector search drifts.

3. **LLM ranking** — top candidates from search go to qwen3:8b for final selection. The LLM sees the original task (not the normalized intent) so it can reason about nuance. Up to 3 tools are injected per round (LLM's top pick + next highest-scoring candidates, deduplicated by domain).

### Procedural memory (experience cache)

The experience cache is the discovery service's long-term memory for tool calls. It's a dual-store architecture:

- **SQLite** (`oap_experience.db`) — system of record. Stores task text, fingerprint, domain, invocation, result, confidence, and correction hints.
- **ChromaDB** (`experience_vectors/`) — vector index for similarity lookup. Embeddings of task text, searched by cosine distance.

On a cache hit, the service replays the cached invocation directly, skipping vector search and LLM ranking. This makes repeated tasks (daily email briefing, morning weather) near-instant. Errors degrade confidence (×0.7 per failure) until the entry falls below the hit threshold and triggers re-discovery.

Backfill migration: on startup, if the vector collection is empty but SQLite has records, all task texts are re-embedded and upserted. This handles upgrades from fingerprint-only to vector-similarity caching.

### `oap_exec` meta-tool

Always injected as the first tool in every `/v1/chat` round. Accepts `command` + optional `stdin`. Bridges LLM CLI knowledge to tool calls — LLMs write better regex, jq, and awk in shell syntax than in structured tool parameters. Supports multi-stage pipelines (`cmd1 | cmd2 | cmd3`).

Security: `shlex.split()` parsing, PATH allowlist (`/usr/bin/`, `/usr/local/bin/`, `/bin/`, `/opt/homebrew/bin/`), `asyncio.create_subprocess_exec()` (no `shell=True`), `blocked_commands` config. All execution wrapped by macOS `sandbox-exec` (file-write protection) unless `danger_will_robinson: true`.

## Local-First with Cloud Delegation

The discovery service uses local models (qwen3:8b + nomic-embed-text via Ollama) for everything by default. Cloud delegation is opt-in and targeted.

### Delegation paths

| Path | Config | Trigger | What goes to cloud |
|------|--------|---------|-------------------|
| Cloud tool bridge | `tool_bridge.use_cloud_tools: true` | All tool calls | Full tool loop: task, discovered tools, tool results, final response |
| Final reasoning escalation | `escalate_prefixes: [prefix, ...]` | Task fingerprint starts with listed prefix | Tool results + synthesis prompt (tools still run locally) |
| Large output escalation | `escalation.enabled: true` | Tool result > `summarize_threshold` (default 16K chars) | Raw tool result + task context |

### Cloud tool bridge (`cloud_tool_bridge.py`)

When `use_cloud_tools: true`, Claude (or GPT-4) handles the entire tool-calling loop instead of qwen3:8b. Discovery still runs locally to find the right manifests — tools are converted from Ollama format to Claude's `input_schema` format before the cloud call. Claude decides which tool to call, processes results, and produces the final response.

`tool_choice: {"type": "any"}` is forced on round 1 to prevent Claude from answering from training data instead of calling the tool it was given. This was added after observing Claude respond conversationally and claim success without executing anything.

Tradeoff: more reliable tool use and better reasoning on complex tasks, at the cost of sending the task (and tool results, which may include personal data) to the cloud API.

### Final reasoning escalation (`escalation.py`)

For tasks tagged with `escalate_prefixes`, the small LLM still handles tool discovery and execution — only the final synthesis step is delegated. This is useful when qwen3:8b's reasoning is the bottleneck but its tool-calling is fine. Example: complex arithmetic over tool results, or multi-step analysis.

The `escalate_prefixes` list matches against task fingerprints (short LLM-generated intent descriptors), so you can target specific task types without affecting everything else.

### Large output escalation

Tool results over `summarize_threshold` chars are automatically passed to the big LLM when escalation is enabled. The big LLM's large context window (200K for Claude, 128K for GPT-4) handles the full output without the lossy chunking that map-reduce requires.

**Map-reduce fallback:** When escalation is not configured, large results are split into chunks, each summarized via `ollama.generate()`, then combined. More lossy — especially for prose, markdown, and code — but keeps everything local.

### What never goes to cloud

- Embeddings (nomic-embed-text via local Ollama)
- Experience cache lookup and storage
- Manifest indexing and vector search
- Tool execution (HTTP/stdio invocations)
- Credential injection
- FTS5 keyword search

### Configuration

```yaml
# discovery/config.yaml
tool_bridge:
  enabled: true
  use_cloud_tools: false        # true = route ALL tool calling through Claude
  escalate_prefixes: []         # fingerprint prefixes to force cloud final reasoning
  summarize_threshold: 16000    # chars; above this, escalate or map-reduce
  chunk_size: 6000
  max_tool_result: 16000
  credentials_file: credentials.yaml
  blocked_commands: [rm, rmdir, dd, mkfs, shutdown, reboot]
  danger_will_robinson: false   # false = sandbox ON; true = disable macOS sandbox
  sandbox_dir: /tmp/oap-sandbox

escalation:
  enabled: false
  provider: anthropic            # or openai, googleai
  model: claude-sonnet-4-6
  base_url: ~                    # optional; defaults to provider's standard URL
  timeout: 60
  # api_key: optional — falls back to OAP_ESCALATION_API_KEY or OAP_ANTHROPIC_API_KEY
```

## Auth Model

Backend token auth (`X-Backend-Token` / `OAP_BACKEND_SECRET`) is per-route, not global.

**Protected** (require token): `/v1/discover`, `/v1/manifests`, `/v1/manifests/{domain}`, `/health`, `/v1/experience/*`

**Unprotected** (local-only, secured by Cloudflare Tunnel path filtering): `/v1/chat`, `/v1/tools`, `/api/chat`, `/v1/openapi.json`, `/v1/tools/call/*`, all `/api/*` Ollama pass-through endpoints

The tool bridge routes are unprotected by design — the Mac Mini is not publicly exposed, and the Cloudflare Tunnel is configured to block these paths from the internet.

## Files

```
discovery/oap_discovery/
  api.py                -- FastAPI app, lifespan (local manifest indexing + seed crawl), routes
  config.py             -- Config dataclass + YAML loader + credentials loader
  models.py             -- Pydantic types: InvokeSpec, Manifest, IOSpec
  discovery.py          -- Vector search + FTS5 + LLM ranking + intent extraction
  db.py                 -- ChromaDB: manifest embeddings + experience vectors
  fts_store.py          -- SQLite FTS5 BM25 keyword search
  experience_store.py   -- ExperienceStore (SQLite) + ExperienceVectorStore (ChromaDB)
  experience_engine.py  -- Cache hit/miss routing, confidence scoring, negative caching
  experience_api.py     -- /v1/experience/ router
  tool_api.py           -- /v1/chat, /v1/tools, /api/chat routes + system prompt
  tool_models.py        -- Tool registry types
  tool_converter.py     -- Manifest → Ollama tool format conversion
  tool_executor.py      -- Tool call execution: HTTP, stdio, oap_exec, credential injection
  cloud_tool_bridge.py  -- Claude/GPT-4 tool bridge (use_cloud_tools mode)
  escalation.py         -- Big LLM final reasoning + large output escalation
  invoker.py            -- HTTP + stdio manifest invocation (SSRF protection, redirect handling)
  ollama_client.py      -- Ollama API client (generate, chat, embed, tags)
  openapi_server.py     -- OpenAPI 3.1 tool server (/v1/openapi.json + /v1/tools/call/*)
  crawler.py            -- Domain crawler for OAP manifest discovery
  validate.py           -- Manifest validation
  sandbox.py            -- macOS sandbox-exec wrapper for subprocess security
  cli.py                -- oap CLI entry point
```
