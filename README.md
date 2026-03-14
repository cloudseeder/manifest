# Manifest

A companion chat app with autonomous task execution, powered by local AI.

Manifest combines a conversational agent with cron-scheduled background tasks, tool discovery via OAP manifests, and local voice I/O — all running on a Mac Mini with no cloud dependencies (except optional big-LLM escalation).

## What's Inside

| Service | Port | Description |
|---------|------|-------------|
| Discovery | 8300 | Manifest discovery, tool bridge, experience cache, Ollama proxy |
| Agent | 8303 | Chat UI + task scheduler (serves both API and Vite SPA) |
| Reminder | 8304 | SQLite-backed reminders with recurrence |
| Email | 8305 | IMAP scanner with LLM classification and auto-filing |

## Prerequisites

- **macOS** (Apple Silicon recommended — tested on M4 Mac Mini, 16GB)
- **Homebrew**: `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"`
- **Python 3.12**: `brew install python@3.12`
- **Ollama**: Download from [ollama.com/download](https://ollama.com/download)

Pull the required models:

```bash
ollama pull qwen3:8b
ollama pull nomic-embed-text
```

## Setup

```bash
git clone https://github.com/cloudseeder/manifest.git ~/manifest
cd ~/manifest

# Create venv and install all services
$(brew --prefix python@3.12)/bin/python3.12 -m venv ~/.oap-venv
source ~/.oap-venv/bin/activate
pip install --upgrade pip setuptools
pip install -e discovery
pip install -e agent
pip install -e reminder
pip install -e email

# Copy config examples
cp discovery/config.yaml.example discovery/config.yaml
cp agent/config.yaml.example agent/config.yaml
cp email/config.yaml.example email/config.yaml

# Start everything (generates secret, creates launchd plists, runs health checks)
./setup.sh
```

Open **http://localhost:8303** — the chat UI.

## Configuration

### API Keys (for tool bridge)

Copy `discovery/credentials.example.yaml` to `discovery/credentials.yaml` and add your keys:

```yaml
www.alphavantage.co:
  api_key: "YOUR_KEY"
newsapi.org:
  api_key: "YOUR_KEY"
```

### Email Scanner

Edit `email/config.yaml` with your IMAP credentials:

```yaml
imap:
  host: "imap.example.com"
  port: 993
  username: "you@example.com"
  password: "app-specific-password"
```

### Voice (Optional)

**TTS** — download a Piper voice:

```bash
mkdir -p agent/piper-voices
cd agent/piper-voices
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json
```

Set the path in `agent/config.yaml`:

```yaml
voice:
  tts_enabled: true
  tts_model_path: /Users/YOU/manifest/agent/piper-voices/en_US-lessac-medium.onnx
  tts_models_dir: /Users/YOU/manifest/agent/piper-voices
```

**STT** — faster-whisper is installed automatically. The model downloads on first use.

See [docs/PIPER.md](docs/PIPER.md) for more voice options.

### Big LLM Escalation (Optional)

Uncomment the `escalation` section in `discovery/config.yaml`:

```yaml
escalation:
  enabled: true
  provider: anthropic
  model: claude-sonnet-4-6
```

Set `OAP_ANTHROPIC_API_KEY` (or `OAP_OPENAI_API_KEY`, `OAP_GOOGLEAI_API_KEY`) in your environment or in the launchd plist.

## Troubleshooting

**Check service logs:**
```bash
tail -f /tmp/com.oap.discovery.log
tail -f /tmp/com.oap.agent.log
tail -f /tmp/com.oap.reminder.log
tail -f /tmp/com.oap.email.log
```

**Restart a service:**
```bash
launchctl unload ~/Library/LaunchAgents/com.oap.agent.plist
launchctl load ~/Library/LaunchAgents/com.oap.agent.plist
```

**Re-run setup** (idempotent — reuses existing secret):
```bash
./setup.sh
```

**Health checks:**
```bash
SECRET=$(cat ~/.oap-secret)
curl -H "X-Backend-Token: $SECRET" http://localhost:8300/health
curl http://localhost:8303/v1/agent/health
curl http://localhost:8304/reminders
curl http://localhost:8305/health
```

## Cron Jobs

**Email scan + auto-file** (every 15 minutes):
```
*/15 * * * * curl -s -X POST http://localhost:8305/scan && curl -s -X POST http://localhost:8305/file
```

**Reminder cleanup** (monthly):
```
0 0 1 * * curl -s -X POST "http://localhost:8304/reminders/cleanup?older_than_days=90"
```

## Architecture

See [docs/AGENT.md](docs/AGENT.md) for the full architecture: chat routing, task scheduler, escalation, memory system.

See [docs/SECURITY.md](docs/SECURITY.md) for the defense-in-depth security model.

## License

CC0 1.0 (Public Domain)
