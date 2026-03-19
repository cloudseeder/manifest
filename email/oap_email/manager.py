"""Email Manager — autonomous inbox management.

Phase 1: preference-based archiving and management logging.
Phase 2: unsubscribe via List-Unsubscribe header or body link scanning.
Phase 3: draft reply generation and SMTP send.

Trust boundary: archive/unsubscribe/log freely; never send without explicit human approval.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

log = logging.getLogger("oap.email.manager")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def process_message(msg: dict, db, cfg) -> list[dict]:
    """Run autonomous management on a single message.

    Updates sender relationship unconditionally.
    If manager.enabled, checks preferences and applies matching actions.
    Returns list of action dicts taken.
    """
    actions: list[dict] = []

    # Always track sender relationships
    db.update_sender_relationship(
        msg.get("from_email", ""),
        msg.get("from_name"),
    )

    if not cfg.manager.enabled:
        return actions

    pref = db.get_matching_preference(msg.get("from_email", ""), msg.get("category"))

    if pref:
        action = pref["action"]
        pref_id = pref["id"]
        reason = f"Preference: {pref['pattern']} → {action}"

        if action == "archive" and cfg.manager.archive_enabled:
            result = await _archive(msg, cfg)
            db.log_action(msg["id"], "archived", reason, pref_id)
            actions.append({"action": "archived", "reason": reason, "success": result})

        elif action == "unsubscribe" and cfg.manager.unsubscribe_enabled:
            result = await _unsubscribe(msg, db)
            action_name = "unsubscribed" if result["success"] else "unsubscribe_failed"
            db.log_action(msg["id"], action_name, result.get("reason") or result.get("url", ""), pref_id)
            actions.append({"action": action_name, "reason": reason, **result})

        elif action == "ignore":
            db.log_action(msg["id"], "ignored", reason, pref_id)
            actions.append({"action": "ignored", "reason": reason})

        elif action == "draft_reply" and cfg.manager.draft_reply_enabled:
            draft = await _generate_draft(msg, db, cfg)
            if draft:
                db.log_action(msg["id"], "draft_created", reason, pref_id)
                actions.append({"action": "draft_created", "draft_id": draft["id"], "reason": reason})

    else:
        # No preference match — auto-draft for urgent personal emails only
        # Mailing lists are never acted on without explicit user decision
        if (cfg.manager.draft_reply_enabled
                and msg.get("category") in cfg.manager.draft_reply_categories
                and msg.get("category") != "mailing-list"
                and msg.get("priority") in cfg.manager.draft_reply_priorities):
            draft = await _generate_draft(msg, db, cfg)
            if draft:
                reason = f"Auto-draft: {msg.get('category')}/{msg.get('priority')}"
                db.log_action(msg["id"], "draft_created", reason)
                actions.append({"action": "draft_created", "draft_id": draft["id"], "reason": reason})

    return actions


def _extract_unsubscribe_url(list_unsubscribe_header: str, body_text: str) -> str | None:
    """Find an HTTPS unsubscribe URL from the List-Unsubscribe header or body text.

    Checks header first (RFC 2369: <https://...> entries), then scans body
    for links containing 'unsubscribe'. Only returns HTTPS URLs — never HTTP.
    """
    if list_unsubscribe_header:
        urls = re.findall(r"<(https://[^>]+)>", list_unsubscribe_header, re.IGNORECASE)
        if urls:
            return urls[0]

    if body_text:
        # href="https://..." or href='https://...' containing 'unsubscribe'
        candidates = re.findall(
            r'href=["\']?(https://[^\s"\'<>]+)["\']?',
            body_text,
            re.IGNORECASE,
        )
        for url in candidates:
            if "unsubscribe" in url.lower():
                return url

    return None


async def _unsubscribe(msg: dict, db) -> dict:
    """Attempt to unsubscribe from a mailing list via HTTP GET.

    Safety constraints:
    - HTTPS only, never plain HTTP
    - GET only, no POST
    - 10s timeout, follows redirects to HTTPS only
    - Falls back to body link scanning if List-Unsubscribe header is missing
    """
    list_unsubscribe = msg.get("list_unsubscribe", "")

    # If header not stored (pre-Phase 2 messages), fetch full message for body fallback
    body_text = msg.get("body_text", "")
    if not list_unsubscribe and not body_text:
        full_msg = db.get_message(msg["id"])
        if full_msg:
            list_unsubscribe = full_msg.get("list_unsubscribe", "")
            body_text = full_msg.get("body_text", "")

    url = _extract_unsubscribe_url(list_unsubscribe, body_text)
    if not url:
        return {"success": False, "reason": "No unsubscribe URL found"}

    try:
        import httpx
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=10.0,
            headers={"User-Agent": "OAP-EmailManager/1.0"},
        ) as client:
            response = await client.get(url)
        success = response.status_code < 400
        log.info(
            "Unsubscribe %s: %s → HTTP %d",
            msg.get("from_email"),
            url[:80],
            response.status_code,
        )
        return {"success": success, "url": url, "status_code": response.status_code}
    except Exception as exc:
        log.warning("Unsubscribe failed for %s: %s", url[:80], exc)
        return {"success": False, "url": url, "reason": str(exc)}


async def _archive(msg: dict, cfg) -> bool:
    """Move a message to the Archive IMAP folder."""
    from .imap import move_messages

    folder = msg.get("folder", "INBOX")
    uid = msg.get("uid")
    if not uid:
        log.warning("Cannot archive message %s — no UID", msg.get("id"))
        return False

    target = cfg.manager.archive_folder
    try:
        moved = await move_messages(cfg.imap, [(folder, int(uid), target)])
        if int(uid) in moved:
            log.info("Archived message %s (%s → %s)", msg.get("id"), folder, target)
            return True
        else:
            log.warning("Archive move returned no success for UID %s", uid)
            return False
    except Exception:
        log.exception("Archive failed for message %s", msg.get("id"))
        return False


async def _generate_draft(msg: dict, db, cfg) -> dict | None:
    """Generate a draft reply using the LLM. Returns draft dict or None on failure.

    Calls Claude (escalation) if use_escalation is true, otherwise local Ollama.
    Only fires for personal messages with urgent or important priority.
    """
    # Fetch full message for body context
    full_msg = db.get_message(msg["id"])
    if not full_msg:
        return None

    body = (full_msg.get("body_text") or "")[:2000]
    subject = msg.get("subject", "(no subject)")
    from_name = msg.get("from_name", "")
    from_email = msg.get("from_email", "")
    thread_id = msg.get("thread_id")

    # Fetch thread history for context
    thread_context = ""
    if thread_id:
        thread_msgs = db.get_thread(thread_id)
        if len(thread_msgs) > 1:
            parts = []
            for tm in thread_msgs[-4:]:  # last 4 messages
                parts.append(
                    f"From: {tm.get('from_name', '')} <{tm.get('from_email', '')}>\n"
                    f"Subject: {tm.get('subject', '')}\n\n"
                    f"{(tm.get('body_text') or '')[:500]}"
                )
            thread_context = "\n\n---\n\n".join(parts)

    system_prompt = (
        "You draft concise, professional email replies. "
        "Return ONLY the email body text — no subject line, no 'From:', no sign-off unless natural. "
        "Match the tone of the conversation. Be brief."
    )

    email_context = (
        f"From: {from_name} <{from_email}>\nSubject: {subject}\n\n{body}"
        if not thread_context
        else f"Thread:\n\n{thread_context}"
    )
    user_msg = f"Draft a reply to this email:\n\n{email_context}"

    draft_body = await _call_llm_for_draft(system_prompt, user_msg, cfg)
    if not draft_body:
        return None

    to_addr = {"name": from_name, "email": from_email}
    reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    notes = f"Auto-drafted: {msg.get('category')}/{msg.get('priority')} from {from_email}"

    draft = db.add_draft(
        message_id=msg["id"],
        thread_id=thread_id,
        draft_body=draft_body,
        draft_subject=reply_subject,
        to_addr=to_addr,
        notes=notes,
    )
    log.info("Draft created for message %s → %s", msg["id"], from_email)
    return draft


async def _call_llm_for_draft(system_prompt: str, user_msg: str, cfg) -> str | None:
    """Call Ollama or Claude to generate draft text."""
    import httpx
    import os

    if cfg.manager.use_escalation:
        # Try Claude via Anthropic API directly
        api_key = os.environ.get("OAP_ESCALATION_API_KEY") or os.environ.get("OAP_ANTHROPIC_API_KEY", "")
        if api_key:
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        "https://api.anthropic.com/v1/messages",
                        headers={
                            "x-api-key": api_key,
                            "anthropic-version": "2023-06-01",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": "claude-haiku-4-5-20251001",
                            "max_tokens": 512,
                            "system": system_prompt,
                            "messages": [{"role": "user", "content": user_msg}],
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    for block in data.get("content", []):
                        if block.get("type") == "text":
                            return block["text"].strip()
            except Exception as exc:
                log.warning("Claude draft generation failed, falling back to Ollama: %s", exc)

    # Local Ollama fallback
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{cfg.manager.ollama_url.rstrip('/')}/api/chat",
                json={
                    "model": cfg.manager.ollama_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    "stream": False,
                    "think": False,
                    "options": {"num_ctx": 4096},
                },
            )
            resp.raise_for_status()
            return resp.json().get("message", {}).get("content", "").strip() or None
    except Exception as exc:
        log.warning("Ollama draft generation failed: %s", exc)
        return None


async def run_manage(db, cfg, limit: int = 100) -> dict:
    """Process recent messages through the manager. Called by the manage dispatch action.

    Returns a summary dict: {processed, actions_taken, log_entries}.
    """
    from datetime import timedelta

    since = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    messages = db.list_messages(folder=None, since=since, limit=limit)

    processed = 0
    total_actions: list[dict] = []

    for msg in messages:
        actions = await process_message(msg, db, cfg)
        processed += 1
        total_actions.extend(actions)

    # Surface mailing lists that need a keep/unsubscribe decision
    pending_review = db.get_unreviewed_mailing_lists()

    summary = {
        "processed": processed,
        "actions_taken": len(total_actions),
        "actions": total_actions,
        "mailing_lists_pending_review": pending_review,
        "mailing_lists_count": len(pending_review),
    }
    if pending_review:
        log.info(
            "Manager run complete: processed=%d actions=%d mailing_lists_pending=%d",
            processed, len(total_actions), len(pending_review),
        )
    else:
        log.info("Manager run complete: processed=%d actions=%d", processed, len(total_actions))
    return summary
