# Email Manager Sub-Agent — Implementation Plan

Autonomous email management built on top of the existing email service. Human becomes the exception handler, not the inbox manager.

## Design Principle

The email service already handles scan, classify, and file. The manager adds autonomous *action* on top of that — with a hard trust boundary: file/archive/unsubscribe freely, draft replies but never send without explicit human approval.

## Architecture

Extend the existing email service — no new service needed. Three new tables, one new module, new dispatch actions.

```
IMAP Server
  ↓ (scan)
SQLite cache (oap_email.db)
  ↓ (classify)
LLM classifier → category + priority
  ↓ (auto-file)
IMAP folders
  ↓ (NEW: manage)
manager.py → preferences check → action
  → archive / unsubscribe / draft reply
  → management_log (append-only audit trail)
  → email_drafts (held for human approval)
```

## Schema Additions to `oap_email.db`

Four new tables added via the existing `_migrate()` pattern in `db.py`:

### `email_preferences`
Learned and explicit rules for autonomous action.

```sql
CREATE TABLE email_preferences (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern      TEXT NOT NULL UNIQUE,  -- @domain, exact email, or "category:offers"
    action       TEXT NOT NULL,         -- "unsubscribe" | "archive" | "draft_reply" | "flag" | "ignore"
    condition    TEXT,                  -- JSON: {"priority": "noise"} or null
    confidence   REAL NOT NULL DEFAULT 0.5,  -- raised by confirmation, lowered by rejection
    created_at   TEXT NOT NULL,
    last_applied TEXT,
    apply_count  INTEGER NOT NULL DEFAULT 0,
    source       TEXT NOT NULL DEFAULT 'learned'  -- "learned" | "explicit"
);
```

### `email_drafts`
Draft replies held for human approval before sending.

```sql
CREATE TABLE email_drafts (
    id           TEXT PRIMARY KEY,      -- uuid
    message_id   TEXT NOT NULL,         -- FK → messages.id
    thread_id    TEXT,
    draft_body   TEXT NOT NULL,
    draft_subject TEXT NOT NULL,
    to_addr      TEXT NOT NULL,         -- JSON: {"name": "...", "email": "..."}
    status       TEXT NOT NULL DEFAULT 'pending',  -- "pending" | "approved" | "rejected" | "sent"
    created_at   TEXT NOT NULL,
    reviewed_at  TEXT,
    sent_at      TEXT,
    notes        TEXT                   -- why the agent drafted this
);
```

### `sender_relationships`
Observed interaction history per sender.

```sql
CREATE TABLE sender_relationships (
    email              TEXT PRIMARY KEY,
    name               TEXT,
    first_seen         TEXT NOT NULL,
    last_seen          TEXT NOT NULL,
    message_count      INTEGER NOT NULL DEFAULT 1,
    reply_count        INTEGER NOT NULL DEFAULT 0,
    avg_response_hours REAL,
    notes              TEXT,            -- LLM summary: "quarterly invoices from accountant"
    updated_at         TEXT NOT NULL
);
```

### `management_log`
Append-only audit trail for every autonomous action.

```sql
CREATE TABLE management_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id    TEXT NOT NULL,
    action        TEXT NOT NULL,   -- "filed" | "archived" | "unsubscribed" | "draft_created"
    reason        TEXT NOT NULL,
    preference_id INTEGER,         -- FK → email_preferences if rule-triggered
    created_at    TEXT NOT NULL
);
```

## New Module: `manager.py`

Core autonomous logic (~200 lines). Called after classification, before returning from `manage` dispatch action.

### `async def process_message(msg, db, cfg) -> list[dict]`

Main entry point. Returns list of actions taken. Decision tree:

1. Update `sender_relationships` for this sender
2. Check `email_preferences` for pattern match (exact email > @domain > category match)
3. If matched preference with `confidence >= 0.7`: execute action
4. If no preference match and category is `offers`/`spam` with priority `noise`: apply default rules
5. Log all actions to `management_log`

### `async def _unsubscribe(msg, cfg) -> bool`

- Check `List-Unsubscribe` header first (RFC 2369 format `<https://...>`)
- Fall back to scanning body for unsubscribe links (`href` containing "unsubscribe")
- Make GET request only, HTTPS only, timeout=10s
- Never POST, never follow redirects to non-HTTPS
- Returns True if HTTP 200, False otherwise
- Logs result regardless

### `async def _generate_draft(msg, db, cfg, discovery_url) -> dict`

- Only fires for `category == "personal"` and `priority in ("urgent", "important")`
- Fetches thread history from DB for context
- Calls `/v1/chat` on discovery service with thread + draft instructions
- Saves to `email_drafts` with `status='pending'`
- Returns draft dict

### `def update_sender_relationship(from_email, from_name, db)`

Upsert into `sender_relationships` after each message processed.

### `async def learn_from_feedback(action, approved, preference_id, db)`

Called when user approves/rejects a draft. Raises or lowers `confidence` on the matching preference rule. Explicit approval: +0.2, explicit rejection: -0.3, auto-clamped to [0, 1].

## New Dispatch Actions

| Action | Description |
|---|---|
| `manage` | Run autonomous management on recent messages |
| `drafts_list` | List pending drafts awaiting review |
| `drafts_approve` | Approve a draft (status → approved) |
| `drafts_reject` | Reject a draft with optional feedback |
| `drafts_send` | Send an approved draft (Phase 3 only) |
| `preferences_list` | Show all learned/explicit preferences |
| `preferences_add` | Teach an explicit preference |
| `preferences_remove` | Delete a preference |
| `relationships_list` | Show sender relationship summaries |
| `log` | Show recent management log entries |

## Trust Boundaries (Hard Constraints)

Not configurable — enforced in `manager.py`:

- `_unsubscribe()`: GET only, HTTPS only, never on `category == "personal"` or `priority in ("urgent", "important")`
- `_generate_draft()`: never fires on `category in ("spam", "machine")`
- `send_draft()`: requires `status == "approved"` in DB; API layer double-checks before SMTP call
- `management_log`: append-only, no delete endpoint
- System prompt addition: "NEVER approve or send email drafts unless the user explicitly says so. Always show draft content before asking for approval."

## Config Additions

```yaml
manager:
  enabled: false                # opt-in
  draft_reply_enabled: false    # extra opt-in for draft generation
  draft_reply_categories:
    - personal
  draft_reply_priorities:
    - urgent
    - important
  unsubscribe_enabled: true
  archive_enabled: true
  learning_enabled: true
```

SMTP config (Phase 3):
```yaml
smtp:
  host: "smtp.gmail.com"
  port: 587
  username: "you@gmail.com"
  password: ""   # or OAP_SMTP_PASSWORD env var
  use_tls: true
```

## Manifest Update

Extend `oap-email.json` description to surface management:

> "Also autonomously manages your inbox — files, unsubscribes, and drafts replies to important emails. Drafts wait for your approval before sending. Ask 'what drafts need my review?', 'approve the draft to Amy', 'always unsubscribe from @retailer.com', 'show what the manager did today'."

## Agent Scheduler Integration

Cron task: "Email Manager" runs after each email scan (or on its own schedule). Notification: "Filed 4, unsubscribed from 2, drafted 1 reply to Amy — review pending."

## Phased Rollout

### Phase 1 — Foundation (~1 day)
- [ ] Add 4 tables to `db.py` via `_migrate()`
- [ ] `manager.py`: `process_message`, `update_sender_relationship`, `_archive()`
- [ ] `manage`, `preferences_*`, `relationships_list`, `log` dispatch actions
- [ ] `ManagerConfig` dataclass in `config.py`
- [ ] Config loading and `manager.enabled` gate
- [ ] Explicit preference teaching: "always archive newsletters from @medium.com"
- [ ] Management log viewable via `ask` ("what did the email manager do today?")

### Phase 2 — Unsubscribe + Learning (~1 day)
- [ ] `_unsubscribe()` with List-Unsubscribe header + body link fallback
- [ ] `preferences_add` via natural language: "always unsubscribe from promotional offers"
- [ ] Preference confidence learning from user feedback
- [ ] Agent scheduler cron task integration
- [ ] Notification with management summary

### Phase 3 — Draft Replies + Send (~1 day)
- [ ] `email_drafts` table
- [ ] `_generate_draft()` calling discovery `/v1/chat`
- [ ] `drafts_*` dispatch actions
- [ ] SMTP send in `imap.py` via `asyncio.to_thread`
- [ ] `SMTPConfig` dataclass
- [ ] System prompt update: never send without approval

## Open Questions

- **Unsubscribe reliability**: List-Unsubscribe header works ~70% of the time; many flows require web form interaction. Always log success/failure, never claim it worked without HTTP 200.
- **Draft quality**: qwen3:8b drafts will be mediocre for personal replies. Route draft generation through Claude escalation by default when `escalation.enabled`.
- **Learning signal**: How does the manager learn "always reply to client@company.com"? Phase 1: explicit only. Phase 2+: observe patterns and propose preferences ("I notice you always reply to Amy within an hour — want me to draft those automatically?")
- **SMTP credentials**: App passwords on Gmail/Outlook work for both IMAP and SMTP. Config needs explicit SMTP host/port — don't assume same as IMAP.
