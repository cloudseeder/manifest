# Email Scanner

IMAP email scanner with LLM-powered classification, priority scoring, and auto-filing.

## Architecture

Two-phase design: `POST /scan` fetches from IMAP and caches to SQLite, read endpoints query local cache. UID-based incremental scanning — only new messages are fetched.

```
IMAP Server
  ↓ (POST /scan)
SQLite cache (oap_email.db)
  ↓ (auto-classify after scan)
LLM classifier → category + priority
  ↓ (auto-file)
IMAP folders (Machine/, Mailing-List/, Spam/, Offers/)
```

## Classification

Every message gets both a **category** and a **priority** in a single LLM call using JSON mode.

### Categories

| Category | Description |
|----------|-------------|
| personal | Written by a real person: colleagues, friends, family, clients |
| machine | Automated/system-generated: server alerts, cron, auth codes |
| mailing-list | Newsletters, news digests, industry bulletins |
| spam | Junk, phishing, unsolicited bulk |
| offers | Sales, promotions, deals, subscription renewals |

### Priority Levels

| Priority | Description | Briefing? |
|----------|-------------|-----------|
| urgent | Needs attention now: bank alerts, password resets, direct requests | Yes |
| important | Should see today: CPA, HOA, work, personal from real people | Yes |
| informational | Nice to know: LinkedIn, news, community announcements | No |
| noise | Safe to ignore: Facebook notifications, marketing, Reddit | No |

The morning briefing task filters by `priority=urgent,important` to show only what matters.

### Classifier Options

- **Local LLM** (default): qwen3.5:9b via Ollama, JSON mode output
- **Big LLM**: `classifier.use_escalation: true` routes to Claude/GPT-4 for better accuracy (~$0.001 per email)

### Sender Overrides

Two layers, checked before the LLM:

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

DB overrides take precedence over config. Both take precedence over LLM classification.

Pattern matching: exact email address first, then `@domain` suffix. No regex.

## Auto-Filing

Moves classified messages to IMAP folders based on category:

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

Folders are created on the IMAP server if they don't exist. Messages are COPY+DELETE moved. Filed status tracked in DB to prevent re-processing.

## API

Entry point: `oap-email-api` (:8305)

### Dispatch (for LLM tool calls)

`POST /api` with `{"action": "...", ...params}`

| Action | Description |
|--------|-------------|
| list | Search/filter cached messages (category, priority, query, since) |
| get | Single message by ID |
| thread | All messages in a thread |
| summary | Activity overview with counts and senders |
| scan | Fetch new from IMAP server |
| classify | Run classifier on uncategorized messages |
| reclassify | Reset all categories/priorities and reclassify |
| overrides_list | Show all sender overrides |
| overrides_add | Add/update a sender override |
| overrides_remove | Delete a sender override |

### REST Endpoints

- `POST /scan` — fetch new from IMAP, classify, auto-file
- `GET /messages` — list with filters (folder, since, unread, query, category, priority)
- `GET /messages/{id}` — single message with full body
- `GET /threads/{thread_id}` — thread view
- `GET /summary` — activity overview
- `POST /classify` — run classifier
- `POST /reclassify` — reset and reclassify all
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
  host: "imap.gmail.com"
  port: 993
  username: "you@gmail.com"
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
  use_escalation: false       # use big LLM for better accuracy
  # sender_overrides:         # force category/priority by sender
  #   "@facebookmail.com":
  #     priority: noise

auto_file:
  enabled: true
  folders:
    personal: INBOX
    machine: Machine
    mailing-list: Mailing-List
    spam: Spam
    offers: Offers

# escalation:                 # big LLM config (for use_escalation)
#   enabled: true
#   provider: anthropic
#   model: claude-sonnet-4-6
```

## Scheduled Scanning

The email scan runs via launchd (`com.oap.email-scan`), configured in `setup.sh`. Fires every 15 minutes during the agent's configured hours. Each scan:

1. Fetches new messages from IMAP (incremental, UID-based)
2. Caches to SQLite
3. Classifies uncategorized messages (category + priority)
4. Auto-files to IMAP folders based on category

## Manifest

`discovery/manifests/oap-email.json` — auto-indexed by the discovery service on startup. Enables conversational email access:

- "Check my email" → list action
- "Any urgent emails?" → list with priority=urgent
- "Emails from Amy" → list with query=from:Amy
- "Mark @facebook.com as noise" → overrides_add
