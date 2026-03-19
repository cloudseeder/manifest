"""FastAPI email scanner API — read-only IMAP access for AI agents."""

from __future__ import annotations

import asyncio
import argparse
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import uvicorn
from fastapi import FastAPI, HTTPException, Query

from .config import Config, load_config
from .db import EmailDB
from .imap import move_messages, scan_folder
from .models import DispatchRequest

log = logging.getLogger("oap.email.api")

_db: EmailDB | None = None
_cfg: Config | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db, _cfg
    config_path = getattr(app, "_config_path", "config.yaml")
    _cfg = load_config(config_path)
    _db = EmailDB(_cfg.db_path)

    # Log cached message count for debugging
    try:
        cached = _db.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        log.info("Email DB: %d cached message(s) in %s", cached, _cfg.db_path)
    except Exception:
        pass

    if not _cfg.imap.host:
        log.warning("No IMAP host configured — scan endpoints will fail")
    else:
        log.info("Email scanner ready — %s → %s:%d folders=%s",
                 _cfg.imap.username, _cfg.imap.host, _cfg.imap.port,
                 _cfg.imap.folders)

    yield

    _db.close()
    log.info("Email scanner stopped")


app = FastAPI(title="OAP Email Scanner", version="0.1.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Scan — fetch new messages from IMAP and cache
# ---------------------------------------------------------------------------

@app.post("/scan")
async def scan():
    """Scan configured IMAP folders for new messages. Caches to SQLite."""
    if not _cfg or not _cfg.imap.host:
        raise HTTPException(status_code=503, detail="IMAP not configured")
    if not _db:
        raise HTTPException(status_code=503, detail="Service unavailable")

    total = 0
    for folder in _cfg.imap.folders:
        since_uid = _db.get_max_uid(folder)
        try:
            messages = await scan_folder(_cfg.imap, folder=folder, since_uid=since_uid)
        except Exception as exc:
            log.error("IMAP scan failed for %s: %s", folder, exc)
            continue
        for msg in messages:
            _db.upsert_message(
                id=msg["id"],
                message_id=msg["message_id"],
                thread_id=msg["thread_id"],
                folder=msg["folder"],
                from_name=msg["from_name"],
                from_email=msg["from_email"],
                to_addrs=msg["to_addrs"],
                cc_addrs=msg["cc_addrs"],
                subject=msg["subject"],
                snippet=msg["snippet"],
                body_text=msg["body_text"],
                received_at=msg["received_at"],
                is_read=msg["is_read"],
                is_flagged=msg["is_flagged"],
                has_attachments=msg["has_attachments"],
                attachments=msg["attachments"],
                uid=msg["uid"],
                list_unsubscribe=msg.get("list_unsubscribe", ""),
            )
        total += len(messages)

    # Cleanup old messages
    pruned = _db.cleanup(_cfg.max_cached)
    if pruned:
        log.info("Pruned %d old cached message(s)", pruned)

    # Classify uncategorized messages — one batch for incremental scans,
    # loop until done for initial ingest (large number of new messages)
    classified = 0
    if _cfg.classifier.enabled:
        from .classifier import classify_uncategorized
        if total > 50:
            # Initial ingest — classify everything
            while True:
                batch = await classify_uncategorized(_cfg.classifier, _db, _cfg.escalation)
                classified += batch
                if batch == 0:
                    break
        else:
            # Incremental — single batch, keeps scan fast
            classified = await classify_uncategorized(_cfg.classifier, _db, _cfg.escalation)

    # Auto-file classified messages
    filed = 0
    if _cfg.auto_file.enabled and _cfg.imap.host:
        result = await file_messages()
        filed = result.get("filed", 0)

    return {"scanned": total, "classified": classified, "filed": filed, "folders": _cfg.imap.folders}


@app.post("/classify")
async def classify():
    """Manually trigger classification of uncategorized messages."""
    if not _db:
        raise HTTPException(status_code=503, detail="Service unavailable")
    if not _cfg or not _cfg.classifier.enabled:
        raise HTTPException(status_code=400, detail="Classifier not enabled")
    from .classifier import classify_uncategorized
    count = await classify_uncategorized(_cfg.classifier, _db, _cfg.escalation)
    return {"classified": count}


@app.post("/file")
async def file_messages():
    """Move classified messages to IMAP folders based on category."""
    if not _db:
        raise HTTPException(status_code=503, detail="Service unavailable")
    if not _cfg or not _cfg.auto_file.enabled:
        raise HTTPException(status_code=400, detail="Auto-file not enabled")
    if not _cfg.imap.host:
        raise HTTPException(status_code=503, detail="IMAP not configured")

    unfiled = _db.get_unfiled(limit=100)
    if not unfiled:
        return {"filed": 0, "skipped": 0}

    folder_map = _cfg.auto_file.folders
    moves: list[tuple[str, int, str]] = []
    skipped = 0

    for msg in unfiled:
        target = folder_map.get(msg["category"])
        if not target or target == msg["folder"]:
            # No mapping or already in target folder — mark as filed
            _db.mark_filed(msg["id"])
            skipped += 1
            continue
        moves.append((msg["folder"], msg["uid"], target))

    filed = 0
    if moves:
        moved_uids = await move_messages(_cfg.imap, moves)
        # Build UID → target folder map for DB update
        uid_to_target = {uid: target for _, uid, target in moves}
        # Mark only successfully moved messages as filed + update folder
        for msg in unfiled:
            if msg["uid"] in moved_uids:
                _db.mark_filed(msg["id"], new_folder=uid_to_target[msg["uid"]])
                filed += 1

    log.info("Auto-filed %d message(s), skipped %d", filed, skipped)
    return {"filed": filed, "skipped": skipped}


@app.post("/refile")
async def refile():
    """Reset all filed flags and re-process. Use after fixing filing issues."""
    if not _db:
        raise HTTPException(status_code=503, detail="Service unavailable")
    reset = _db.reset_filed()
    log.info("Reset %d filed flag(s) for re-filing", reset)
    result = await file_messages()
    return {"reset": reset, **result}


@app.post("/reclassify")
async def reclassify(category: str | None = Query(None)):
    """Reset categories and reclassify. Pass ?category=mailing-list to target only one category."""
    if not _db:
        raise HTTPException(status_code=503, detail="Service unavailable")
    if not _cfg or not _cfg.classifier.enabled:
        raise HTTPException(status_code=400, detail="Classifier not enabled")

    if category:
        reset = _db.reset_category(category)
        log.info("Reset %d '%s' messages for targeted reclassification", reset, category)
    else:
        reset = _db.reset_categories()
        _db.reset_priorities()
        log.info("Reset %d message categories for reclassification", reset)

    from .classifier import classify_uncategorized
    classified = 0
    while True:
        batch = await classify_uncategorized(_cfg.classifier, _db, _cfg.escalation)
        classified += batch
        if batch == 0:
            break
    return {"reset": reset, "classified": classified, "category_filter": category}


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/messages")
async def list_messages(
    folder: str = "INBOX",
    since: str | None = None,
    unread: bool = False,
    query: str | None = None,
    category: str | None = None,
    priority: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    _skip_default_since: bool = False,
):
    """List cached messages."""
    if not _db:
        raise HTTPException(status_code=503, detail="Service unavailable")
    if since is None and not _skip_default_since:
        hours = _cfg.default_scan_hours if _cfg else 24
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    messages = _db.list_messages(folder=folder, since=since, unread=unread, query=query, category=category, priority=priority, limit=limit)
    return {"messages": messages, "total": len(messages)}


@app.get("/messages/{msg_id}")
async def get_message(msg_id: str):
    """Get a single cached message."""
    if not _db:
        raise HTTPException(status_code=503, detail="Service unavailable")
    msg = _db.get_message(msg_id)
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    return msg


@app.get("/threads/{thread_id}")
async def get_thread(thread_id: str):
    """Get all messages in a thread."""
    if not _db:
        raise HTTPException(status_code=503, detail="Service unavailable")
    messages = _db.get_thread(thread_id)
    if not messages:
        raise HTTPException(status_code=404, detail="Thread not found")
    participants = {}
    for m in messages:
        key = m.get("from_email", "")
        if key and key not in participants:
            participants[key] = {"name": m.get("from_name", ""), "email": key}
    return {
        "thread_id": thread_id,
        "subject": messages[0].get("subject", ""),
        "participants": list(participants.values()),
        "messages": messages,
        "message_count": len(messages),
    }


@app.get("/summary")
async def summary(since: str | None = None):
    """Quick summary of recent email activity."""
    if not _db:
        raise HTTPException(status_code=503, detail="Service unavailable")

    now = datetime.now(timezone.utc)
    hours = _cfg.default_scan_hours if _cfg else 24
    if since is None:
        since = (now - timedelta(hours=hours)).isoformat()

    messages = _db.list_messages(folder="INBOX", since=since, limit=100)
    unread = _db.count_unread("INBOX")

    senders = list(dict.fromkeys(
        m.get("from_name") or m.get("from_email", "unknown") for m in messages
    ))
    subjects = list(dict.fromkeys(m.get("subject", "") for m in messages if m.get("subject")))

    return {
        "period_from": since,
        "period_to": now.isoformat(),
        "total_received": len(messages),
        "unread_count": unread,
        "senders": senders[:20],
        "subjects": subjects[:20],
    }


# ---------------------------------------------------------------------------
# Dispatch — single endpoint for OAP tool bridge
# ---------------------------------------------------------------------------

@app.post("/api")
async def dispatch(req: DispatchRequest):
    """Single-endpoint dispatcher for OAP manifests."""
    action = req.action.lower().strip()
    log.info("Dispatch action=%s folder=%s query=%r category=%s since=%s limit=%d",
             action, req.folder, req.query, req.category, req.since, req.limit)

    if action == "ask":
        question = req.question or ""
        if not question:
            raise HTTPException(status_code=400, detail="question required for ask action")
        log.info("Dispatch ask: %r", question)

        # Scan for new messages, then return recent emails — let Claude interpret the question
        scan_result = await scan()
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        list_result = await list_messages(
            folder=None,
            since=since,
            limit=50,
            _skip_default_since=True,
        )
        log.info("Ask → scanned=%d found=%d", scan_result.get("scanned", 0), list_result.get("total", 0))
        return {
            "question": question,
            "scanned": scan_result.get("scanned", 0),
            "classified": scan_result.get("classified", 0),
            **list_result,
        }

    elif action == "scan":
        result = await scan()
        log.info("Dispatch scan → %d message(s) scanned", result.get("scanned", 0))
        return result
    elif action == "classify":
        result = await classify()
        return result
    elif action == "reclassify":
        result = await reclassify()
        return result
    elif action == "reclassify_mailing_lists":
        # Targeted: reset only mailing-list messages, force escalation for accuracy.
        # Runs in background — returns immediately to avoid HTTP timeout.
        if not _cfg or not _cfg.classifier.enabled:
            raise HTTPException(status_code=400, detail="Classifier not enabled")
        import dataclasses, os, asyncio
        from .config import EscalationConfig
        base_esc = _cfg.escalation
        api_key = (
            (base_esc.api_key if base_esc else "")
            or os.environ.get("OAP_ESCALATION_API_KEY", "")
            or os.environ.get("OAP_ANTHROPIC_API_KEY", "")
            or os.environ.get("ANTHROPIC_API_KEY", "")
        )
        if not api_key:
            raise HTTPException(status_code=400, detail="No API key found — set OAP_ANTHROPIC_API_KEY or OAP_ESCALATION_API_KEY")
        escalation = EscalationConfig(
            enabled=True,
            provider=base_esc.provider if base_esc else "anthropic",
            base_url=base_esc.base_url if base_esc else "",
            model="claude-haiku-4-5-20251001",
            api_key=api_key,
            timeout=base_esc.timeout if base_esc else 60,
            max_tokens=256,
        )
        reset = _db.reset_category("mailing-list")
        log.info("Reclassify: reset %d mailing-list messages, running in background (model=%s)", reset, escalation.model)

        from .classifier import classify_uncategorized
        forced_cfg = dataclasses.replace(_cfg.classifier, use_escalation=True)

        async def _bg_reclassify():
            classified = 0
            while True:
                batch = await classify_uncategorized(forced_cfg, _db, escalation)
                classified += batch
                if batch == 0:
                    break
            log.info("Background reclassify complete: %d classified", classified)

        asyncio.create_task(_bg_reclassify())
        return {
            "status": "started",
            "reset": reset,
            "model": escalation.model,
            "message": f"Reclassifying {reset} messages in background. Check 'reclassify_diff' in a minute for results.",
        }

    elif action == "reclassify_diff":
        # Show what changed in the last targeted reclassify
        prev = req.category or "mailing-list"
        changed = _db.get_reclassify_diff(prev, limit=req.limit)
        return {
            "prev_category": prev,
            "changed_count": len(changed),
            "changes": changed,
        }
    elif action == "file":
        result = await file_messages()
        return result
    elif action == "list":
        result = await list_messages(
            folder=req.folder, since=req.since, unread=req.unread,
            query=req.query, category=req.category, priority=req.priority,
            limit=req.limit, _skip_default_since=True,
        )
        log.info("Dispatch list → %d message(s)", result.get("total", 0))
        return result
    elif action == "get":
        if not req.id:
            raise HTTPException(status_code=400, detail="id required for get action")
        return await get_message(req.id)
    elif action == "thread":
        if not req.thread_id:
            raise HTTPException(status_code=400, detail="thread_id required for thread action")
        return await get_thread(req.thread_id)
    elif action == "summary":
        result = await summary(since=req.since)
        log.info("Dispatch summary → %d received, %d unread",
                 result.get("total_received", 0), result.get("unread_count", 0))
        return result
    elif action in ("overrides_list", "overrides"):
        return {"overrides": _db.list_overrides()}
    elif action in ("overrides_add", "override_add", "override"):
        if not req.override_pattern:
            raise HTTPException(status_code=400, detail="override_pattern required")
        if not req.override_category and not req.override_priority:
            raise HTTPException(status_code=400, detail="override_category or override_priority required")
        result = _db.add_override(req.override_pattern, req.override_category, req.override_priority)
        log.info("Override added: %s → category=%s priority=%s",
                 req.override_pattern, req.override_category, req.override_priority)
        return result
    elif action in ("overrides_remove", "override_remove"):
        if not req.override_pattern:
            raise HTTPException(status_code=400, detail="override_pattern required")
        removed = _db.remove_override(req.override_pattern)
        if not removed:
            raise HTTPException(status_code=404, detail="Override not found")
        return {"removed": req.override_pattern}
    # ------------------------------------------------------------------
    # Manager actions
    # ------------------------------------------------------------------
    elif action == "manage":
        from .manager import run_manage
        result = await run_manage(_db, _cfg)
        return result

    elif action in ("mailing_lists", "mailing_list_review"):
        pending = _db.get_unreviewed_mailing_lists()
        return {
            "mailing_lists_pending_review": pending,
            "total": len(pending),
            "instructions": (
                "For each sender, ask the user: keep (saves preference to ignore) "
                "or unsubscribe (saves preference and attempts List-Unsubscribe)."
            ),
        }

    elif action == "preferences_list":
        return {"preferences": _db.list_preferences()}

    elif action in ("preferences_add", "preference_add"):
        pattern = req.pattern or req.override_pattern or ""
        act = req.manager_action or ""
        if not pattern or not act:
            raise HTTPException(status_code=400, detail="'pattern' and 'manager_action' required")
        valid_actions = ("archive", "unsubscribe", "ignore", "flag", "draft_reply")
        if act not in valid_actions:
            raise HTTPException(status_code=400, detail=f"manager_action must be one of {valid_actions}")
        pref = _db.add_preference(pattern, act)
        return pref

    elif action in ("preferences_remove", "preference_remove"):
        pattern = req.pattern or req.override_pattern or ""
        if not pattern:
            raise HTTPException(status_code=400, detail="'pattern' required")
        removed = _db.remove_preference(pattern)
        if not removed:
            raise HTTPException(status_code=404, detail="Preference not found")
        return {"removed": pattern}

    elif action in ("relationships_list", "relationships"):
        return {"relationships": _db.list_relationships(limit=req.limit)}

    elif action in ("manager_log", "log", "management_log"):
        return {"log": _db.list_log(limit=req.limit)}

    elif action == "drafts_list":
        return {"drafts": _db.list_drafts(status="pending")}

    elif action == "drafts_approve":
        draft_id = req.draft_id or req.id
        if not draft_id:
            raise HTTPException(status_code=400, detail="'draft_id' required")
        draft = _db.update_draft_status(draft_id, "approved")
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        return draft

    elif action == "drafts_reject":
        draft_id = req.draft_id or req.id
        if not draft_id:
            raise HTTPException(status_code=400, detail="'draft_id' required")
        draft = _db.update_draft_status(draft_id, "rejected")
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        return draft

    elif action == "drafts_send":
        if not _cfg or not _cfg.smtp.host:
            raise HTTPException(status_code=503, detail="SMTP not configured")
        draft_id = req.draft_id or req.id
        if not draft_id:
            raise HTTPException(status_code=400, detail="'draft_id' required")
        # Fetch and verify status — must be approved
        drafts = _db.list_drafts(status="approved")
        draft = next((d for d in drafts if d["id"] == draft_id), None)
        if not draft:
            raise HTTPException(status_code=400, detail="Draft not found or not approved. Approve it first.")
        from .smtp import send_draft
        try:
            await send_draft(_cfg.smtp, draft)
        except Exception as exc:
            log.error("SMTP send failed for draft %s: %s", draft_id, exc)
            raise HTTPException(status_code=502, detail=f"SMTP send failed: {exc}")
        sent = _db.update_draft_status(draft_id, "sent")
        _db.log_action(draft["message_id"], "draft_sent", f"Sent to {draft.get('to_addr', {}).get('email', '')}")
        return sent

    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    if not _db:
        return {"status": "starting"}
    total = _db.count_unread("INBOX")
    return {
        "status": "ok",
        "imap_configured": bool(_cfg and _cfg.imap.host),
        "unread_inbox": total,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="OAP email scanner API")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    cfg = load_config(args.config)
    host = args.host or cfg.host
    port = args.port or cfg.port

    app._config_path = args.config
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
