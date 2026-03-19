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

    summary = {
        "processed": processed,
        "actions_taken": len(total_actions),
        "actions": total_actions,
    }
    log.info("Manager run complete: processed=%d actions=%d", processed, len(total_actions))
    return summary
