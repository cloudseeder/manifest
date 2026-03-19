"""Email Manager — autonomous inbox management.

Phase 1: preference-based archiving and management logging.
Phase 2: unsubscribe via List-Unsubscribe header (httpx GET).
Phase 3: draft reply generation and SMTP send.

Trust boundary: archive/log freely; never send without explicit human approval.
"""

from __future__ import annotations

import logging
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

        elif action == "unsubscribe":
            # Phase 2 — placeholder
            log.debug("Unsubscribe queued for Phase 2: %s", msg.get("from_email"))

        elif action == "ignore":
            db.log_action(msg["id"], "ignored", reason, pref_id)
            actions.append({"action": "ignored", "reason": reason})

    return actions


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
