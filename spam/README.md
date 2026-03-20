# IMAP Spam Filter — Local Ollama Classifier

A lightweight, privacy-first IMAP spam filter that runs entirely on your local machine. Uses a tiny language model (0.5B-1.5B params) via Ollama for classification, with fast heuristic pre-filtering to minimize model calls.

## Architecture

```
INBOX → Heuristic Pre-filter → [Obvious spam/ham: instant decision]
                              → [Ambiguous: Ollama classifier]
                                    → Confidence Router
                                        → High confidence spam → Quarantine/
                                        → Low confidence       → Review/
                                        → High confidence ham  → Stay in INBOX
```

**Three-tier speed:**
- **Tier 1 — Heuristics** (~1μs/msg): Blocklists, header checks (SPF/DKIM), regex patterns. Catches 60-70% of obvious cases.
- **Tier 2 — Model** (~100-300ms/msg): Ollama with a 0.5B model classifies ambiguous messages. Truncated body, structured JSON output, constrained generation.
- **Tier 3 — Confidence routing**: High-confidence spam gets quarantined. Low-confidence gets tagged for human review. Never auto-quarantines when unsure.

## Quick Start

### 1. Install Ollama and pull a tiny model

```bash
# Install Ollama (macOS)
brew install ollama
ollama serve   # Start the server (or it may already be running)

# Pull the classifier model (~400MB)
ollama pull qwen2.5:0.5b

# Or for slightly better accuracy (~1GB)
ollama pull qwen2.5:1.5b
```

### 2. Set up the filter

```bash
cd spam-filter
pip install -r requirements.txt

# Copy and edit config
cp config.yaml config.local.yaml
# Edit config.local.yaml with your IMAP credentials
```

### 3. Test it

```bash
# Test Ollama connectivity
python spam_filter.py --test-ollama

# Run demo with synthetic messages (no IMAP needed)
python spam_filter.py --demo

# Dry run against your real inbox (classify but don't move)
python spam_filter.py --dry-run

# Run for real
python spam_filter.py

# Watch mode (poll every 5 minutes)
python spam_filter.py --watch
```

## Performance Expectations (Mac Mini M-series)

| Model | Size | Speed/msg | 800 msgs | Accuracy |
|-------|------|-----------|----------|----------|
| qwen2.5:0.5b | 400MB | ~100ms | ~80s + heuristics | Good for obvious spam |
| qwen2.5:1.5b | 1GB | ~200ms | ~160s + heuristics | Better on edge cases |
| qwen2.5:3b | 2GB | ~400ms | ~320s + heuristics | Near-frontier quality |

With heuristics catching 60-70% of messages, only 30-40% hit the model. So 800 messages with qwen2.5:0.5b: ~240 model calls × 100ms = **~25 seconds total**.

Compare to your current setup: Qwen 3.5 9B doing full generative responses = several hours.

## Configuration

See `config.yaml` for all options. Key settings:

- **`classifier.quarantine_threshold`** (0.85): Only auto-quarantine when the model is very confident. Lower = more aggressive, higher = more conservative.
- **`classifier.max_body_length`** (1500): How much of the email body to send to the model. Spam signals are almost always in the first 1500 chars.
- **`heuristics.allowlisted_domains`**: Domains that always pass through without model classification.
- **`heuristics.blocked_domains`**: Domains that are always quarantined.

## Making It Commercial

To turn this into a product ("Pi-hole for email"), the key additions would be:

1. **Web dashboard** — Review quarantined messages, one-click release, train from corrections
2. **Per-user learning** — Track false positives/negatives, adapt thresholds per user
3. **IMAP IDLE** — Push-based instead of polling for near-realtime filtering
4. **Appliance packaging** — Docker image or macOS app that bundles Ollama + the filter
5. **Digest emails** — Daily summary of what was quarantined with release links
6. **Multi-account** — Run against multiple IMAP accounts from one instance

## License

MIT
