"""SMTP send for approved email drafts."""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from .config import SMTPConfig

log = logging.getLogger("oap.email.smtp")


def _send_sync(cfg: SMTPConfig, draft: dict, from_addr: str) -> None:
    """Send a draft via SMTP (synchronous — run in thread)."""
    to = draft.get("to_addr", {})
    to_name = to.get("name", "") if isinstance(to, dict) else ""
    to_email = to.get("email", "") if isinstance(to, dict) else str(to)

    if not to_email:
        raise ValueError("Draft has no to_addr email")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = draft["draft_subject"]
    msg["From"] = from_addr
    msg["To"] = f"{to_name} <{to_email}>" if to_name else to_email
    msg["In-Reply-To"] = draft.get("thread_id", "")
    msg.attach(MIMEText(draft["draft_body"], "plain"))

    if cfg.use_tls:
        server = smtplib.SMTP(cfg.host, cfg.port, timeout=30)
        server.ehlo()
        server.starttls()
        server.ehlo()
    else:
        server = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=30)

    try:
        server.login(cfg.username, cfg.password)
        server.sendmail(from_addr, [to_email], msg.as_string())
        log.info("Sent draft %s → %s", draft["id"], to_email)
    finally:
        server.quit()


async def send_draft(cfg: SMTPConfig, draft: dict) -> None:
    """Async wrapper — sends an approved draft via SMTP."""
    from_addr = cfg.username
    await asyncio.to_thread(_send_sync, cfg, draft, from_addr)
