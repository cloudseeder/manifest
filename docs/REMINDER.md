# Reminder Service

SQLite-backed reminder service for AI agents. Supports one-time, recurring, and place-based reminders.

## Features

- **Time-based reminders**: due date + optional time, with recurrence (daily, weekly, monthly, yearly)
- **Place-based reminders**: triggered when the user says they're going somewhere ("I'm heading to the store")
- **Date range queries**: "reminders due this week", "what's coming up next month"
- **Title-based operations**: complete/delete/get by title, not just ID
- **Recurring auto-creation**: completing a recurring reminder creates the next occurrence
- **iCal feed**: subscribe from Apple/Google Calendar at `/feed.ics`

## API

Entry point: `oap-reminder-api` (:8304)

### Dispatch (for LLM tool calls)

`POST /api` with `{"action": "...", ...params}`

| Action | Description |
|--------|-------------|
| **ask** | **Natural language question — returns all pending reminders for Claude to interpret** |
| create | New reminder with title, optional due_date, due_time, recurring, place |
| complete | Mark done by ID or title. Auto-creates next occurrence for recurring. |
| delete | Remove by ID or title |
| update | Modify fields by ID or title |
| cleanup | Purge old completed reminders |
| due | Date-range query — reminders due today/overdue, supports `after`/`before` |
| place | Reminders for a location (internal — `ask` covers this) |
| list | All reminders by status (internal — `ask` covers this) |
| get | Fetch single by ID or title (internal) |

`ask` is the primary read interface exposed by the manifest. `due`, `place`, `list`, and `get` remain available as internal REST endpoints.

### REST Endpoints

- `POST /reminders` — create
- `GET /reminders` — list with status/limit/offset filters
- `GET /reminders/due` — due/overdue with optional `before`/`after` date range
- `GET /reminders/{id}` — get single
- `PATCH /reminders/{id}` — update
- `DELETE /reminders/{id}` — delete
- `POST /reminders/{id}/complete` — mark complete
- `POST /reminders/cleanup` — purge old completed
- `GET /feed.ics` — iCalendar subscription feed
- `GET /health` — health check

## Place-Based Reminders

Reminders with a `place` field and no `due_date` surface only when the user mentions going somewhere.

**Creation**: "Next time I go to the store remind me to buy milk"
→ `{"action": "create", "title": "Buy milk", "place": "store"}`

**Surfacing**: "I'm heading to the store"
→ `{"action": "place", "place": "store"}`
→ Returns all pending reminders with place matching "store"

**Matching**: Case-insensitive substring with article stripping. "the store" matches "store", "grocery store" matches "store".

Place-only reminders (no due_date) are **excluded** from the `due` action so they don't clutter daily briefings.

## Date Range Queries

The `due` action supports `after` and `before` parameters:

- `{"action": "due"}` — due today or overdue (default)
- `{"action": "due", "after": "2026-04-01", "before": "2026-04-30"}` — due in April
- `{"action": "due", "before": "2026-03-22"}` — due this week

## Recurrence

Supported: `daily`, `weekly`, `monthly`, `yearly`.

When a recurring reminder is completed, the next occurrence is auto-created:
- Daily: +1 day
- Weekly: +7 days
- Monthly: +1 month (day clamped: Jan 31 → Feb 28)
- Yearly: +1 year

The new occurrence inherits title, notes, time, recurrence, and place.

## Data Model

```sql
CREATE TABLE reminders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    notes        TEXT,
    created_at   TEXT NOT NULL,
    due_date     TEXT,          -- YYYY-MM-DD
    due_time     TEXT,          -- HH:MM
    recurring    TEXT,          -- daily|weekly|monthly|yearly
    status       TEXT DEFAULT 'pending',
    completed_at TEXT,
    place        TEXT           -- location trigger
);
```

## Configuration

```yaml
# config.yaml (optional — defaults work without it)
database:
  path: "oap_reminder.db"

api:
  host: "127.0.0.1"
  port: 8304
```

DB path resolved relative to config file directory; defaults to `$HOME/oap_reminder.db` without config.

## Safety Guardrails

The discovery system prompt includes: "NEVER complete, delete, or modify reminders unless the user EXPLICITLY asks you to." This prevents the LLM from autonomously completing reminders (like bill payments) without being asked — a real safety issue discovered in testing.

## Manifest

`discovery/manifests/oap-reminder.json` — auto-indexed by discovery on startup.

### Intent-First Design

Same principle as the email manifest: expose what you can *ask*, not what operations exist. The manifest has one primary read action (`ask`) and explicit write actions (`create`, `complete`, `delete`, `update`).

**Reading** — pass any question, get all pending reminders back, Claude answers:

```json
{"action": "ask", "question": "anything due today?"}
{"action": "ask", "question": "I'm heading to the store"}
{"action": "ask", "question": "what's coming up this week?"}
{"action": "ask", "question": "do I have anything for Michelle's?"}
```

**Writing** — Claude derives structured params from natural language and calls the right action:

```json
{"action": "create", "title": "Call dentist", "due_date": "2026-03-20", "due_time": "09:00"}
{"action": "create", "title": "Get paper towels", "place": "Costco"}
{"action": "complete", "title": "Call dentist"}
{"action": "delete", "title": "Old reminder"}
{"action": "update", "title": "Call dentist", "due_date": "2026-03-27"}
```

Read operations don't need filters — all 200 pending reminders fit easily in context, and Claude can answer any question about them without the service needing to parse intent.

## Cleanup

`POST /reminders/cleanup?older_than_days=30` or CLI: `oap-reminder-api --cleanup 30`. Deletes completed reminders older than the specified days.
