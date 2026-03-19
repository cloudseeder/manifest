"""Email classifier — categorizes and prioritizes messages using LLM."""

from __future__ import annotations

import json
import logging
import os
import re

import httpx

from .config import ClassifierConfig, EscalationConfig

log = logging.getLogger("oap.email.classifier")

_client: httpx.AsyncClient | None = None
_client_timeout: int = 0


def _get_client(cfg) -> httpx.AsyncClient:
    """Return a reusable async HTTP client, creating one if needed."""
    global _client, _client_timeout
    if _client is None or _client.is_closed or _client_timeout != cfg.timeout:
        _client = httpx.AsyncClient(timeout=cfg.timeout)
        _client_timeout = cfg.timeout
    return _client


# Legacy category mapping for pre-existing cached responses
_LEGACY = {"inbox": "personal", "transactional": "machine", "marketing": "mailing-list"}


def _build_system_prompt(categories: dict[str, str], priorities: dict[str, str]) -> str:
    """Build classifier system prompt for combined category + priority."""
    lines = ["Classify this email into exactly one category AND one priority level.\n"]
    lines.append("Categories:")
    for name, description in categories.items():
        lines.append(f"  {name} — {description}")
    lines.append("\nPriority levels:")
    for name, description in priorities.items():
        lines.append(f"  {name} — {description}")
    lines.append('\nRespond with ONLY a JSON object: {"category": "...", "priority": "..."}')
    return "\n".join(lines)


def _check_overrides(
    from_email: str,
    config_overrides: dict[str, dict[str, str]],
    db_override: dict | None,
) -> dict[str, str | None] | None:
    """Check DB override first, then config overrides.

    Returns {"category": ..., "priority": ...} or None.
    """
    if db_override:
        return db_override

    email_lower = from_email.lower()
    if email_lower in config_overrides:
        return config_overrides[email_lower]
    if "@" in email_lower:
        domain = "@" + email_lower.split("@", 1)[1]
        if domain in config_overrides:
            return config_overrides[domain]

    return None


async def classify_message(
    cfg: ClassifierConfig,
    from_name: str,
    from_email: str,
    subject: str,
    snippet: str,
) -> dict[str, str] | None:
    """Classify a single email via local LLM. Returns {"category": ..., "priority": ...}."""
    user_msg = f"From: {from_name} <{from_email}>\nSubject: {subject}\n\n{snippet}"
    system_prompt = _build_system_prompt(cfg.categories, cfg.priorities)

    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
        "format": "json",
        "options": {"num_ctx": 2048},
        "think": False,
    }

    try:
        client = _get_client(cfg)
        resp = await client.post(
            f"{cfg.ollama_url.rstrip('/')}/api/chat",
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("Classification failed for %s <%s>: %s", from_name, from_email, exc)
        return None

    content = data.get("message", {}).get("content", "").strip()
    # Strip thinking tags if present
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        # Fall back to text parsing for non-JSON responses
        content_lower = content.lower()
        category = None
        for cat in cfg.categories:
            if cat in content_lower:
                category = cat
                break
        if not category:
            for old, new in _LEGACY.items():
                if old in content_lower and new in cfg.categories:
                    category = new
                    break
        return {"category": category or "personal", "priority": "informational"} if category else None

    category = parsed.get("category", "").lower().replace(" ", "-")
    priority = parsed.get("priority", "").lower()

    # Validate against known values
    if category not in cfg.categories:
        for cat in cfg.categories:
            if cat in category:
                category = cat
                break
        else:
            category = "personal"
    if priority not in cfg.priorities:
        priority = "informational"

    return {"category": category, "priority": priority}


async def classify_message_escalated(
    cfg: ClassifierConfig,
    escalation: EscalationConfig,
    from_name: str,
    from_email: str,
    subject: str,
    snippet: str,
) -> dict[str, str] | None:
    """Classify via big LLM (Claude/GPT-4). Returns {"category": ..., "priority": ...}."""
    api_key = escalation.api_key or os.environ.get(
        "OAP_ESCALATION_API_KEY",
        os.environ.get(f"OAP_{escalation.provider.upper()}_API_KEY", ""),
    )
    if not api_key:
        log.warning("Escalation classification skipped — no API key")
        return None

    user_msg = f"From: {from_name} <{from_email}>\nSubject: {subject}\n\n{snippet}"
    system_prompt = _build_system_prompt(cfg.categories, cfg.priorities)

    try:
        if escalation.provider == "anthropic":
            base_url = escalation.base_url or "https://api.anthropic.com"
            async with httpx.AsyncClient(timeout=escalation.timeout) as client:
                resp = await client.post(
                    f"{base_url.rstrip('/')}/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": escalation.model,
                        "max_tokens": escalation.max_tokens,
                        "system": system_prompt,
                        "messages": [{"role": "user", "content": user_msg}],
                        "temperature": 0,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("type") == "error":
                    raise ValueError(f"Anthropic API error: {data.get('error', {}).get('message', data)}")
                content = ""
                for block in data.get("content", []):
                    if block.get("type") == "text":
                        content = block["text"]
                        break
                if not content:
                    log.warning("Empty content from Claude — stop_reason=%s blocks=%s for %s",
                                data.get("stop_reason"), [b.get("type") for b in data.get("content", [])],
                                from_email)
        else:
            base_url = escalation.base_url or "https://api.openai.com/v1"
            async with httpx.AsyncClient(timeout=escalation.timeout) as client:
                resp = await client.post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": escalation.model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_msg},
                        ],
                        "temperature": 0,
                        "max_tokens": escalation.max_tokens,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]

        parsed = json.loads(content.strip())
        category = parsed.get("category", "personal").lower()
        priority = parsed.get("priority", "informational").lower()
        if category not in cfg.categories:
            category = "personal"
        if priority not in cfg.priorities:
            priority = "informational"
        return {"category": category, "priority": priority}

    except Exception as exc:
        log.warning("Escalated classification failed: %s", exc)
        return None


async def classify_uncategorized(
    cfg: ClassifierConfig,
    db,
    escalation: EscalationConfig | None = None,
) -> int:
    """Classify all unclassified messages. Returns count."""
    rows = db.get_unclassified(limit=50)
    if not rows:
        return 0

    classified = 0
    for row in rows:
        from_email = row.get("from_email", "")

        # Check overrides first (DB, then config)
        db_override = db.get_override(from_email) if from_email else None
        override = _check_overrides(from_email, cfg.sender_overrides, db_override)

        if override:
            category = override.get("category")
            priority = override.get("priority")
            # Fill in missing values with defaults
            if not category:
                category = "personal"
            if not priority:
                priority = "informational"
            db.set_classification(row["id"], category, priority)
            classified += 1
            log.info("%-13s %-13s [override] %s — %s",
                     category, priority, from_email, row.get("subject", "")[:50])
            continue

        # LLM classification
        if cfg.use_escalation and escalation and escalation.enabled:
            result = await classify_message_escalated(
                cfg, escalation,
                from_name=row.get("from_name", ""),
                from_email=from_email,
                subject=row.get("subject", ""),
                snippet=row.get("snippet", ""),
            )
        else:
            result = await classify_message(
                cfg,
                from_name=row.get("from_name", ""),
                from_email=from_email,
                subject=row.get("subject", ""),
                snippet=row.get("snippet", ""),
            )

        if result:
            db.set_classification(row["id"], result["category"], result["priority"])
            classified += 1
            log.info("%-13s %-13s %s — %s",
                     result["category"], result["priority"],
                     from_email, row.get("subject", "")[:50])

    log.info("Classified %d/%d message(s)", classified, len(rows))
    return classified
