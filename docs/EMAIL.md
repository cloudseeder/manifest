# Email Scanner

IMAP email scanner with tiered pre-classification, LLM-powered category + priority scoring, and auto-filing.

## Architecture

Two-phase design: `POST /scan` fetches from IMAP and caches to SQLite, read endpoints query local cache. UID-based incremental scanning — only new messages are fetched.

```
IMAP Server
  ↓ (POST /scan — parses headers, body, attachments)
SQLite cache (oap_email.db)
  ↓ (auto-classify after scan)
Pre-classifier tiers (no LLM)
  → Sender overrides (DB then config)
  → List-Unsubscribe header → mailing-list
  → Blocked domain → spam
  → X-Spam-Status: Yes → spam
  → qwen3:1.7b fast spam/ham check → spam
  ↓ (only ambiguous mail reaches here)
Full LLM (Haiku or local) → category + priority
  ↓ (auto-file)
IMAP folders (Machine/, Mailing-List/, Spam/, Offers/)
```

## Pre-Classifier Tiers

Most mail never touches the expensive LLM. Tiers fire in order; first match wins.

| Tier | Trigger | Result | Cost |
|------|---------|--------|------|
| Sender override (DB) | Exact email or @domain in `classifier_overrides` table | Category/priority from override | None |
| Sender override (config) | Match in `classifier.sender_overrides` YAML | Category/priority from override | None |
| List-Unsubscribe header | RFC 2369 `List-Unsubscribe` header present | `mailing-list / informational` | None |
| Blocked domain | Domain in `spam_filter.blocked_domains` | `spam / noise` | None |
| X-Spam-Status header | `X-Spam-Status: Yes` header from upstream filter | `spam / noise` | None |
| Local spam model | qwen3:1.7b binary spam/ham check ≥ threshold | `spam / noise` | ~100ms, local |
| Full LLM | Everything that passed all tiers | category + priority | Haiku or local |

Logs show which tier classified each message:
```
mailing-list  informational [list-header]    quora@quora.com — Your Weekly Digest
spam          noise         [blocklist] evil.com  spammer@evil.com — Buy now!
spam          noise         [x-spam-header]  scam@random.net — You won!
spam          noise         [local-spam 92%] promo@unknown.biz — Amazing deal
personal      important                      kai@gmail.com — Re: Sunday plans
```

## Classification

Messages that pass all pre-classifier tiers get both a **category** and **priority** in a single LLM call using JSON mode.

### Categories

| Category | Description |
|----------|-------------|
| `personal` | Written by a real individual: colleagues, friends, family, clients, neighbors, community members. Strong signals: personal email address (gmail, yahoo, icloud, hotmail), real person's name as sender, conversational tone. HOA/community emails from a real person also count. **When in doubt between personal and mailing-list, prefer personal.** |
| `machine` | Automated/system-generated, no human author: server alerts, cron output, cPanel, disk space warnings, CI/CD, monitoring, auth codes |
| `mailing-list` | Informational newsletters, news digests, editorial content, industry bulletins. NOT social notifications about people you know (those are personal). NOT promotional offers (those are offers). |
| `spam` | Junk, phishing, unsolicited bulk email, adult content |
| `offers` | Selling something: sales, promotions, deals, coupons, discounts, subscription renewals |

### Priority Levels

| Priority | Description | Morning briefing? |
|----------|-------------|-------------------|
| `urgent` | Needs attention now: bank/financial alerts, password resets, security notices, direct requests requiring timely response | Yes |
| `important` | Should see today: CPA/accountant/lawyer, HOA notices, work correspondence, personal from real people, bills/invoices | Yes |
| `informational` | Nice to know: LinkedIn, news digests, community announcements, newsletters | No |
| `noise` | Safe to ignore: Facebook/Instagram notifications, marketing, promotional offers, bulk newsletters | No |

The morning briefing filters by `priority=urgent,important`.

### Classifier Options

- **Local LLM** (default): configurable Ollama model (e.g. `qwen3.5:9b`), JSON mode
- **Big LLM**: `classifier.use_escalation: true` routes to Claude/GPT-4 for better accuracy (~$0.001/email at Haiku 4.5 rates)

API key resolution for escalation: `escalation.api_key` → `OAP_ESCALATION_API_KEY` → `OAP_ANTHROPIC_API_KEY` → `ANTHROPIC_API_KEY`.

### Sender Overrides

Two layers checked before any LLM or pre-filter tier:

1. **Database overrides** (managed conversationally):
   - "Mark emails from joe@cpa.us as important"
   - "Treat @facebookmail.com as noise"
   - Stored in `classifier_overrides` table
   - Managed via `overrides_add`, `overrides_list`, `overrides_remove` actions

2. **Config overrides** (`config.yaml`):
   ```yaml
   classifier:
     sender_overrides:
       "mycpa@accounting.com":
         category: personal
         priority: important
       "@facebookmail.com":
         priority: noise
   ```

DB overrides take precedence over config. Both take precedence over all other tiers.

Pattern matching: exact email address first, then `@domain`. No regex.

## Spam Pre-Filter

The `spam_filter` config section enables fast local pre-classification before the expensive LLM.

```yaml
spam_filter:
  enabled: true
  local_model: "qwen3:1.7b"   # or qwen3:0.6b / qwen3:4b
  spam_threshold: 0.85         # only auto-classify spam above this confidence
  blocked_domains:             # instant spam, no model call
    - "spam-kingdom.com"
```

**Tier 1 — Header heuristics** (microseconds):
- `blocked_domains` list: instant spam decision, no model call
- `X-Spam-Status: Yes` header: honors upstream MTA/cloud filter decision

**Tier 2 — Local model** (~100ms):
- qwen3:1.7b binary spam/ham classification with confidence score
- Only fires if tier 1 didn't match
- Score ≥ `spam_threshold` → `spam / noise`; anything lower falls through to full LLM
- Safe by design: errors return `ham / 0.5` so no legitimate mail is silently dropped

The 1.7b model trades some accuracy for speed — use the logs to see what it catches confidently vs. what falls through to Haiku. Adjust `spam_threshold` to tune the tradeoff.

### Headers Stored

The scanner parses and stores these headers for the spam tiers:

| Header | DB column | Used by |
|--------|-----------|---------|
| `List-Unsubscribe` | `list_unsubscribe` | Pre-classifier, unsubscribe action |
| `List-Unsubscribe-Post` | `list_unsubscribe_post` | RFC 8058 one-click unsubscribe |
| `Received-SPF` | `received_spf` | Available for future heuristics |
| `Authentication-Results` | `auth_results` | Available for future heuristics |
| `X-Spam-Status` | `x_spam_status` | Spam pre-filter tier 1 |

## Reclassification

### Targeted reclassify

Reset a specific category and reclassify with the current model:

```json
{"action": "reclassify", "category": "mailing-list"}
```

Or reset everything:

```json
{"action": "reclassify"}
```

The previous category/priority are snapshotted into `prev_category` / `prev_priority` columns before clearing, enabling before/after comparison.

### Mailing list reclassify with big LLM

Reclassifies all mailing-list messages using Claude (forces escalation regardless of config — useful for fixing false positives):

```json
{"action": "reclassify_mailing_lists"}
```

Runs in the background (returns immediately, classification continues async). Check results with:

```json
{"action": "reclassify_diff", "prev_category": "mailing-list"}
```

Returns messages where the category changed from `mailing-list` to something else — the false positives that were corrected.

Model used: `escalation.model` from config (defaults to `claude-haiku-4-5` if not set). API key resolved at dispatch time from env vars so it works correctly under launchd.

## Unsubscribe

When a mailing-list message has a `List-Unsubscribe` header, the manager can send an unsubscribe request:

- **RFC 8058 one-click** (preferred): if `List-Unsubscribe-Post: List-Unsubscribe=One-Click` header is present, sends a POST request per the standard
- **GET fallback**: if no `List-Unsubscribe-Post` header, sends a GET to the unsubscribe URL
- HTTPS only — never follows HTTP unsubscribe links
- 10s timeout

## Mailing List Review

After scanning or reclassifying, the manager surfaces mailing lists that have no keep/unsubscribe preference set:

```json
{"action": "manage"}
```

Returns `mailing_lists_pending_review` — senders categorized as mailing-list with no preference. From there, set preferences:

```json
{"action": "prefer", "pattern": "@newsletter.com", "action": "unsubscribe"}
{"action": "prefer", "pattern": "digest@news.org", "action": "ignore"}
```

## Auto-Filing

Moves classified messages to IMAP folders based on category. Folders created if they don't exist. Messages are COPY+DELETE moved. Filed status tracked in DB to prevent re-processing.

```yaml
auto_file:
  enabled: true
  folders:
    personal: INBOX
    machine: Machine
    mailing-list: Mailing-List
    spam: Spam
    offers: Offers
```

## Manager

Autonomous inbox management. Opt-in via `manager.enabled: true`.

```yaml
manager:
  enabled: true
  archive_enabled: true          # allow archiving via preferences
  unsubscribe_enabled: true      # allow List-Unsubscribe requests
  draft_reply_enabled: false     # generate draft replies (requires smtp)
  archive_folder: "Archive"
  use_escalation: false          # use Claude for draft generation
  ollama_model: "qwen3:8b"       # local model for draft generation
```

The manager processes recent messages (last 48h) against stored preferences. Trust boundary: archives and unsubscribes freely; never sends email without explicit human approval of a draft.

## API

Entry point: `oap-email-api` (:8305)

### Dispatch (for LLM tool calls)

`POST /api` with `{"action": "...", ...params}`

| Action | Description |
|--------|-------------|
| **ask** | **Natural language question — scans, returns recent mail for Claude to interpret** |
| list | Search/filter cached messages (category, priority, query, since) |
| get | Single message by ID |
| thread | All messages in a thread |
| summary | Activity overview with counts and senders |
| scan | Fetch new from IMAP server |
| classify | Run classifier on uncategorized messages |
| reclassify | Reset categories and reclassify (`category` param for targeted reset) |
| reclassify_mailing_lists | Reclassify mailing-list messages using big LLM (background, async) |
| reclassify_diff | Show before/after for messages that changed from a category |
| overrides_list | Show all sender overrides |
| overrides_add | Add/update a sender override |
| overrides_remove | Delete a sender override |

`ask` is the primary action exposed by the manifest. The others are internal/REST.

### REST Endpoints

- `POST /scan` — fetch new from IMAP, classify, auto-file
- `GET /messages` — list with filters (folder, since, unread, query, category, priority)
- `GET /messages/{id}` — single message with full body
- `GET /threads/{thread_id}` — thread view
- `GET /summary` — activity overview
- `POST /classify` — run classifier
- `POST /reclassify?category=mailing-list` — reset and reclassify (optional category filter)
- `POST /file` — auto-file classified messages
- `GET /health` — health check

### Query Syntax

Supports `OR` between terms and field prefixes:

```
from:Amy OR from:Keric
from:amy@netgate.net subject:invoice
body:password reset
```

Prefixes: `from/sender`, `to`, `subject`, `body`. Without prefix searches all fields.

## Configuration

```yaml
imap:
  host: "imap.example.com"
  port: 993
  username: "you@example.com"
  password: "your-app-password"   # or OAP_EMAIL_PASSWORD env var
  use_ssl: true
  folders:
    - "INBOX"

database:
  path: "oap_email.db"

api:
  host: "127.0.0.1"
  port: 8305

max_cached: 500
default_scan_hours: 24

classifier:
  enabled: true
  model: "qwen3.5:9b"
  ollama_url: "http://localhost:11434"
  timeout: 30
  use_escalation: false           # use big LLM (escalation config) for better accuracy
  # sender_overrides:
  #   "@facebookmail.com":
  #     priority: noise
  #   "mycpa@firm.com":
  #     category: personal
  #     priority: important

# Spam pre-filter — fast tiers before the expensive LLM
spam_filter:
  enabled: true
  local_model: "qwen3:1.7b"      # qwen3:0.6b / qwen3:1.7b / qwen3:4b
  spam_threshold: 0.85            # confidence needed to auto-classify as spam
  # blocked_domains:
  #   - "known-spam-domain.com"

# Big LLM for classification / reclassification
escalation:
  enabled: true
  provider: anthropic
  model: claude-haiku-4-5         # ~$1/$5 per M tokens — good for classification
  timeout: 30

auto_file:
  enabled: true
  folders:
    personal: INBOX
    machine: Machine
    mailing-list: Mailing-List
    spam: Spam
    offers: Offers

# Manager — autonomous inbox actions (opt-in)
# manager:
#   enabled: true
#   archive_enabled: true
#   unsubscribe_enabled: true
#   draft_reply_enabled: false
#   use_escalation: false
#   ollama_model: "qwen3:8b"
```

## API Keys Under launchd

launchd agents don't inherit shell environment variables. Keys must be injected explicitly.

**`setup.sh` method** (permanent): Add keys to `~/.oap-keys`:

```bash
# ~/.oap-keys
OAP_ANTHROPIC_API_KEY=sk-ant-...
```

Then run `./setup.sh` — it reads `~/.oap-keys` and bakes the keys into the launchd plists as `EnvironmentVariables`.

**Immediate method** (until next reboot):

```bash
launchctl setenv OAP_ANTHROPIC_API_KEY sk-ant-...
```

## Scheduled Scanning

The email scan runs via launchd (`com.oap.email-scan`), configured in `setup.sh`. Fires every 15 minutes. Each scan:

1. Fetches new messages from IMAP (incremental, UID-based)
2. Caches to SQLite (parses headers including List-Unsubscribe, SPF, DKIM, X-Spam-Status)
3. Runs pre-classifier tiers (overrides → list-header → heuristics → local model)
4. Classifies remaining messages with full LLM (category + priority)
5. Auto-files to IMAP folders based on category

## Manifest

`discovery/manifests/oap-email.json` — auto-indexed by the discovery service on startup.

### Intent-First Design

The manifest exposes a single `ask` action with one `question` parameter. The LLM passes natural language directly; the service handles the API complexity internally.

```json
{
  "action": "ask",
  "question": "anything urgent today?"
}
```

The service always scans for new IMAP messages first, then returns up to 50 recent messages across all folders (not just INBOX — auto-filed messages are included). Claude interprets the question and filters/formats the response. No keyword parsing inside the service.

**Example questions:**

| What you say | What happens |
|---|---|
| "anything urgent today?" | scan + return today's mail, Claude filters urgent |
| "emails from Amy" | scan + return recent mail, Claude filters by sender |
| "what came in overnight?" | scan + return last 24h, Claude shows overnight window |
| "anything about the invoice?" | scan + return recent mail, Claude searches for invoice |
| "summarize today's email" | scan + return today's mail, Claude summarizes by priority |
| "mark @facebook.com as noise" | `overrides_add` action |
| "reclassify mailing list false positives" | `reclassify_mailing_lists` action |
| "show what changed after reclassify" | `reclassify_diff` action |
| "show sender overrides" | `overrides_list` action |

**Why intent-first?** Operation-first APIs require the LLM to know API internals (which filters exist, what parameter names are). Intent-first APIs expose what you can *ask*, not what operations exist — the same principle that makes conversational interfaces work.
