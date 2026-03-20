"""IMAP handler — connect, fetch unprocessed messages, move to folders."""

import imaplib
import email
import email.message
from email.header import decode_header
from email.utils import parseaddr
from dataclasses import dataclass, field
from typing import Optional
import logging
import re

log = logging.getLogger(__name__)


@dataclass
class EmailMessage:
    """Lightweight representation of an email for classification."""
    uid: str
    message_id: str
    from_addr: str
    from_domain: str
    to_addr: str
    subject: str
    body_text: str
    headers: dict = field(default_factory=dict)
    raw_size: int = 0

    @property
    def display(self) -> str:
        return f"[{self.uid}] {self.from_addr}: {self.subject[:60]}"


class IMAPHandler:
    def __init__(self, config: dict):
        self.host = config["imap"]["host"]
        self.port = config["imap"]["port"]
        self.username = config["imap"]["username"]
        self.password = config["imap"]["password"]
        self.use_ssl = config["imap"].get("use_ssl", True)
        self.folders = config["folders"]
        self._conn: Optional[imaplib.IMAP4_SSL] = None

    def connect(self):
        """Establish IMAP connection and login."""
        log.info(f"Connecting to {self.host}:{self.port}")
        if self.use_ssl:
            self._conn = imaplib.IMAP4_SSL(self.host, self.port)
        else:
            self._conn = imaplib.IMAP4(self.host, self.port)
        self._conn.login(self.username, self.password)
        log.info("Connected and authenticated")
        self._ensure_folders()

    def disconnect(self):
        """Close connection cleanly."""
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    def _ensure_folders(self):
        """Create quarantine and review folders if they don't exist."""
        for folder_key in ("quarantine", "review"):
            folder_name = self.folders.get(folder_key)
            if not folder_name:
                continue
            status, folders = self._conn.list('""', folder_name)
            if not folders or folders[0] is None:
                log.info(f"Creating folder: {folder_name}")
                self._conn.create(folder_name)
                self._conn.subscribe(folder_name)

    def fetch_unprocessed(self, limit: int = 50) -> list[EmailMessage]:
        """Fetch messages from inbox that haven't been processed yet.

        Uses a custom IMAP flag to track what we've already seen.
        """
        self._conn.select(self.folders["inbox"])

        # Search for messages without our processed flag
        # Fallback: just get UNSEEN or all recent messages
        status, data = self._conn.search(None, "UNFLAGGED")
        if status != "OK" or not data[0]:
            # Try all messages if UNFLAGGED doesn't work
            status, data = self._conn.search(None, "ALL")

        if status != "OK" or not data[0]:
            log.info("No messages to process")
            return []

        uids = data[0].split()
        # Process newest first, limited to batch size
        uids = uids[-limit:]
        log.info(f"Fetching {len(uids)} messages")

        messages = []
        for uid in uids:
            msg = self._fetch_one(uid)
            if msg:
                messages.append(msg)

        return messages

    def _fetch_one(self, uid: bytes) -> Optional[EmailMessage]:
        """Fetch and parse a single message by UID."""
        try:
            status, data = self._conn.fetch(uid, "(RFC822)")
            if status != "OK" or not data[0]:
                return None

            raw = data[0][1]
            msg = email.message_from_bytes(raw)

            # Decode subject
            subject = self._decode_header(msg.get("Subject", ""))

            # Parse sender
            from_name, from_addr = parseaddr(msg.get("From", ""))
            from_domain = from_addr.split("@")[-1].lower() if "@" in from_addr else ""

            # Parse recipient
            _, to_addr = parseaddr(msg.get("To", ""))

            # Extract body text
            body = self._extract_body(msg)

            # Grab key headers for heuristic checks
            headers = {
                "received-spf": msg.get("Received-SPF", ""),
                "dkim-signature": msg.get("DKIM-Signature", ""),
                "authentication-results": msg.get("Authentication-Results", ""),
                "x-spam-status": msg.get("X-Spam-Status", ""),
                "x-mailer": msg.get("X-Mailer", ""),
                "list-unsubscribe": msg.get("List-Unsubscribe", ""),
                "return-path": msg.get("Return-Path", ""),
                "content-type": msg.get("Content-Type", ""),
            }

            return EmailMessage(
                uid=uid.decode() if isinstance(uid, bytes) else str(uid),
                message_id=msg.get("Message-ID", ""),
                from_addr=from_addr.lower(),
                from_domain=from_domain,
                to_addr=to_addr.lower(),
                subject=subject,
                body_text=body,
                headers=headers,
                raw_size=len(raw),
            )

        except Exception as e:
            log.warning(f"Failed to parse message {uid}: {e}")
            return None

    def _decode_header(self, raw: str) -> str:
        """Decode RFC2047 encoded header value."""
        if not raw:
            return ""
        parts = decode_header(raw)
        decoded = []
        for part, charset in parts:
            if isinstance(part, bytes):
                decoded.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                decoded.append(part)
        return " ".join(decoded)

    def _extract_body(self, msg: email.message.Message) -> str:
        """Extract plain text body from a message."""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        return payload.decode(charset, errors="replace")
            # Fallback: try HTML
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        html = payload.decode(charset, errors="replace")
                        # Crude HTML stripping
                        text = re.sub(r'<[^>]+>', ' ', html)
                        text = re.sub(r'\s+', ' ', text).strip()
                        return text
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
        return ""

    def move_to_folder(self, uid: str, folder: str):
        """Move a message to a target folder via COPY + delete."""
        try:
            self._conn.select(self.folders["inbox"])
            result = self._conn.copy(uid.encode(), folder)
            if result[0] == "OK":
                self._conn.store(uid.encode(), "+FLAGS", "\\Deleted")
                self._conn.expunge()
                log.debug(f"Moved {uid} → {folder}")
            else:
                log.warning(f"Copy failed for {uid}: {result}")
        except Exception as e:
            log.error(f"Failed to move {uid} to {folder}: {e}")

    def flag_processed(self, uid: str):
        """Mark a message as processed so we skip it next run."""
        try:
            self._conn.select(self.folders["inbox"])
            self._conn.store(uid.encode(), "+FLAGS", "\\Flagged")
        except Exception as e:
            log.warning(f"Failed to flag {uid}: {e}")
