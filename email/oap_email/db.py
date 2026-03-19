"""SQLite message cache for oap-email."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading

log = logging.getLogger("oap.email.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    message_id  TEXT,
    thread_id   TEXT,
    folder      TEXT NOT NULL DEFAULT 'INBOX',
    from_name   TEXT,
    from_email  TEXT,
    to_addrs    TEXT,
    cc_addrs    TEXT,
    subject     TEXT,
    snippet     TEXT,
    body_text   TEXT,
    received_at TEXT,
    is_read     INTEGER DEFAULT 1,
    is_flagged  INTEGER DEFAULT 0,
    has_attachments INTEGER DEFAULT 0,
    attachments TEXT,
    uid         INTEGER,
    cached_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_received ON messages(received_at);
CREATE INDEX IF NOT EXISTS idx_messages_folder ON messages(folder);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_messages_uid ON messages(folder, uid);
"""


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


class EmailDB:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        self._migrate()
        self._lock = threading.Lock()
        log.info("Email DB opened: %s", db_path)

    def _migrate(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "category" not in cols:
            self.conn.execute("ALTER TABLE messages ADD COLUMN category TEXT")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_category ON messages(category)")
            self.conn.commit()
        if "filed" not in cols:
            self.conn.execute("ALTER TABLE messages ADD COLUMN filed INTEGER DEFAULT 0")
            self.conn.commit()
        if "priority" not in cols:
            self.conn.execute("ALTER TABLE messages ADD COLUMN priority TEXT")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_priority ON messages(priority)")
            self.conn.commit()
        # Overrides table for sender-based classification
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS classifier_overrides (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern     TEXT NOT NULL UNIQUE,
                category    TEXT,
                priority    TEXT,
                created_at  TEXT NOT NULL
            );
        """)
        self.conn.commit()
        if "list_unsubscribe" not in cols:
            self.conn.execute("ALTER TABLE messages ADD COLUMN list_unsubscribe TEXT")
            self.conn.commit()
        # Manager tables
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS email_preferences (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern      TEXT NOT NULL UNIQUE,
                action       TEXT NOT NULL,
                condition    TEXT,
                confidence   REAL NOT NULL DEFAULT 1.0,
                created_at   TEXT NOT NULL,
                last_applied TEXT,
                apply_count  INTEGER NOT NULL DEFAULT 0,
                source       TEXT NOT NULL DEFAULT 'explicit'
            );

            CREATE TABLE IF NOT EXISTS email_drafts (
                id           TEXT PRIMARY KEY,
                message_id   TEXT NOT NULL,
                thread_id    TEXT,
                draft_body   TEXT NOT NULL,
                draft_subject TEXT NOT NULL,
                to_addr      TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending',
                created_at   TEXT NOT NULL,
                reviewed_at  TEXT,
                sent_at      TEXT,
                notes        TEXT
            );

            CREATE TABLE IF NOT EXISTS sender_relationships (
                email              TEXT PRIMARY KEY,
                name               TEXT,
                first_seen         TEXT NOT NULL,
                last_seen          TEXT NOT NULL,
                message_count      INTEGER NOT NULL DEFAULT 1,
                reply_count        INTEGER NOT NULL DEFAULT 0,
                notes              TEXT,
                updated_at         TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS management_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id    TEXT NOT NULL,
                action        TEXT NOT NULL,
                reason        TEXT NOT NULL,
                preference_id INTEGER,
                created_at    TEXT NOT NULL
            );
        """)
        self.conn.commit()
        # Normalize received_at to UTC (+00:00) for consistent string comparison
        self._normalize_timestamps()

    def _normalize_timestamps(self):
        """Convert any non-UTC received_at values to UTC +00:00 format."""
        from datetime import datetime, timezone
        rows = self.conn.execute(
            "SELECT id, received_at FROM messages WHERE received_at IS NOT NULL "
            "AND received_at NOT LIKE '%+00:00'"
        ).fetchall()
        if not rows:
            return
        updated = 0
        for row in rows:
            raw = row[1]
            try:
                parsed = datetime.fromisoformat(raw)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                utc = parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
                if utc != raw:
                    self.conn.execute(
                        "UPDATE messages SET received_at = ? WHERE id = ?",
                        (utc, row[0]),
                    )
                    updated += 1
            except (ValueError, TypeError):
                continue
        if updated:
            self.conn.commit()
            log.info("Normalized %d/%d received_at timestamps to UTC", updated, len(rows))

    def close(self):
        self.conn.close()

    def upsert_message(
        self,
        id: str,
        message_id: str,
        thread_id: str,
        folder: str,
        from_name: str,
        from_email: str,
        to_addrs: list[dict],
        cc_addrs: list[dict],
        subject: str,
        snippet: str,
        body_text: str,
        received_at: str,
        is_read: bool,
        is_flagged: bool,
        has_attachments: bool,
        attachments: list[dict],
        uid: int,
        list_unsubscribe: str = "",
    ) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO messages
                   (id, message_id, thread_id, folder, from_name, from_email,
                    to_addrs, cc_addrs, subject, snippet, body_text,
                    received_at, is_read, is_flagged, has_attachments,
                    attachments, uid, cached_at, list_unsubscribe)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                    is_read = excluded.is_read,
                    is_flagged = excluded.is_flagged,
                    cached_at = excluded.cached_at""",
                (
                    id, message_id, thread_id, folder, from_name, from_email,
                    json.dumps(to_addrs), json.dumps(cc_addrs),
                    subject, snippet, body_text,
                    received_at, int(is_read), int(is_flagged),
                    int(has_attachments), json.dumps(attachments),
                    uid, _now(), list_unsubscribe or "",
                ),
            )
            self.conn.commit()

    def list_messages(
        self,
        folder: str | None = "INBOX",
        since: str | None = None,
        unread: bool = False,
        query: str | None = None,
        category: str | None = None,
        priority: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        conditions: list[str] = []
        params: list = []
        if folder:
            conditions.append("folder = ?")
            params.append(folder)
        if since:
            # Normalize Z suffix to +00:00 for consistent string comparison
            if since.endswith("Z"):
                since = since[:-1] + "+00:00"
            conditions.append("received_at >= ?")
            params.append(since)
        if unread:
            conditions.append("is_read = 0")
        if category:
            conditions.append("category = ?")
            params.append(category.lower())
        if priority:
            priorities = [p.strip().lower() for p in priority.split(",")]
            placeholders = ",".join("?" * len(priorities))
            conditions.append(f"priority IN ({placeholders})")
            params.extend(priorities)
        if query:
            query_sql, query_params = self._parse_query(query)
            conditions.append(query_sql)
            params.extend(query_params)

        where = " AND ".join(conditions)
        rows = self.conn.execute(
            f"SELECT * FROM messages WHERE {where} ORDER BY received_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        # Exclude body_text from list results — use get_message for full body
        results = []
        for r in rows:
            d = self._decode(dict(r))
            d.pop("body_text", None)
            results.append(d)
        return results

    def get_message(self, msg_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM messages WHERE id = ?", (msg_id,)
        ).fetchone()
        if not row:
            return None
        return self._decode(dict(row))

    def get_thread(self, thread_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE thread_id = ? ORDER BY received_at ASC",
            (thread_id,),
        ).fetchall()
        return [self._decode(dict(r)) for r in rows]

    def get_max_uid(self, folder: str) -> int:
        """Return the highest cached UID for a folder, or 0."""
        row = self.conn.execute(
            "SELECT MAX(uid) FROM messages WHERE folder = ?", (folder,)
        ).fetchone()
        return row[0] or 0

    def count_unread(self, folder: str = "INBOX") -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE folder = ? AND is_read = 0", (folder,)
        ).fetchone()
        return row[0]

    def cleanup(self, max_per_folder: int = 500) -> int:
        """Keep only the newest max_per_folder messages per folder."""
        folders = [
            r[0] for r in self.conn.execute("SELECT DISTINCT folder FROM messages").fetchall()
        ]
        deleted = 0
        with self._lock:
            for folder in folders:
                cur = self.conn.execute(
                    """DELETE FROM messages WHERE folder = ? AND id NOT IN (
                        SELECT id FROM messages WHERE folder = ?
                        ORDER BY received_at DESC LIMIT ?
                    )""",
                    (folder, folder, max_per_folder),
                )
                deleted += cur.rowcount
            if deleted:
                self.conn.commit()
        return deleted

    def get_uncategorized(self, limit: int = 50) -> list[dict]:
        """Return messages without a category."""
        rows = self.conn.execute(
            "SELECT id, from_name, from_email, subject, snippet FROM messages "
            "WHERE category IS NULL ORDER BY received_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_category(self, msg_id: str, category: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE messages SET category = ? WHERE id = ?",
                (category, msg_id),
            )
            self.conn.commit()

    def get_unfiled(self, limit: int = 50) -> list[dict]:
        """Return classified but unfiled messages."""
        rows = self.conn.execute(
            "SELECT id, folder, uid, category FROM messages "
            "WHERE category IS NOT NULL AND filed = 0 "
            "ORDER BY received_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_filed(self, msg_id: str, new_folder: str | None = None) -> None:
        with self._lock:
            if new_folder:
                self.conn.execute(
                    "UPDATE messages SET filed = 1, folder = ? WHERE id = ?",
                    (new_folder, msg_id),
                )
            else:
                self.conn.execute(
                    "UPDATE messages SET filed = 1 WHERE id = ?", (msg_id,),
                )
            self.conn.commit()

    def reset_filed(self) -> int:
        """Reset all filed flags so messages get re-processed."""
        with self._lock:
            cur = self.conn.execute("UPDATE messages SET filed = 0 WHERE filed = 1")
            self.conn.commit()
            return cur.rowcount

    def reset_categories(self) -> int:
        """Clear all categories so messages get reclassified."""
        with self._lock:
            cur = self.conn.execute("UPDATE messages SET category = NULL WHERE category IS NOT NULL")
            self.conn.commit()
            return cur.rowcount

    def reset_category(self, category: str) -> int:
        """Clear category/priority for messages with a specific category."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE messages SET category = NULL, priority = NULL WHERE category = ?",
                (category,),
            )
            self.conn.commit()
            return cur.rowcount

    def set_classification(self, msg_id: str, category: str | None, priority: str | None) -> None:
        """Set both category and priority in one call."""
        with self._lock:
            self.conn.execute(
                "UPDATE messages SET category = ?, priority = ? WHERE id = ?",
                (category, priority, msg_id),
            )
            self.conn.commit()

    def get_unclassified(self, limit: int = 50) -> list[dict]:
        """Return messages missing category or priority."""
        rows = self.conn.execute(
            "SELECT id, from_name, from_email, subject, snippet FROM messages "
            "WHERE category IS NULL OR priority IS NULL "
            "ORDER BY received_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def reset_priorities(self) -> int:
        """Clear all priorities so messages get reprioritized."""
        with self._lock:
            cur = self.conn.execute("UPDATE messages SET priority = NULL WHERE priority IS NOT NULL")
            self.conn.commit()
            return cur.rowcount

    # --- Classifier overrides ---

    def add_override(self, pattern: str, category: str | None = None, priority: str | None = None) -> dict:
        """Add or update a classifier override. Pattern is email or @domain."""
        now = _now()
        with self._lock:
            self.conn.execute(
                "INSERT INTO classifier_overrides (pattern, category, priority, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(pattern) DO UPDATE SET category=excluded.category, priority=excluded.priority",
                (pattern.lower(), category, priority, now),
            )
            self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM classifier_overrides WHERE pattern = ?", (pattern.lower(),)
        ).fetchone()
        return dict(row)

    def list_overrides(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM classifier_overrides ORDER BY pattern"
        ).fetchall()
        return [dict(r) for r in rows]

    def remove_override(self, pattern: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM classifier_overrides WHERE pattern = ?", (pattern.lower(),)
            )
            self.conn.commit()
        return cur.rowcount > 0

    def get_override(self, from_email: str) -> dict | None:
        """Check for a matching override: exact email first, then @domain."""
        email = from_email.lower()
        row = self.conn.execute(
            "SELECT category, priority FROM classifier_overrides WHERE pattern = ?",
            (email,),
        ).fetchone()
        if row:
            return dict(row)
        # Domain match
        if "@" in email:
            domain = "@" + email.split("@", 1)[1]
            row = self.conn.execute(
                "SELECT category, priority FROM classifier_overrides WHERE pattern = ?",
                (domain,),
            ).fetchone()
            if row:
                return dict(row)
        return None

    # ------------------------------------------------------------------
    # Query parser — supports OR, field prefixes (from:, subject:, body:)
    # ------------------------------------------------------------------

    _FIELD_MAP = {
        "from": ["from_name", "from_email"],
        "sender": ["from_name", "from_email"],
        "to": ["to_addrs"],
        "subject": ["subject"],
        "body": ["body_text"],
    }

    def _parse_query(self, query: str) -> tuple[str, list[str]]:
        """Parse a query string with OR support and field prefixes.

        Examples:
            "Amy Brooks OR Keric Brooks"
            "from:amy@netgate.net"
            "from:Amy subject:car"
            "FROM Amy Brooks OR FROM Kai Brooks"
        """
        import re

        # Split on OR (case-insensitive, surrounded by whitespace)
        or_groups = re.split(r"\s+OR\s+", query, flags=re.IGNORECASE)

        or_clauses = []
        params: list[str] = []

        for group in or_groups:
            group = group.strip()
            if not group:
                continue

            # Extract field-prefixed terms: "from:value" or "FROM value"
            # Also handle "SUBJECT value", "BODY value" etc.
            and_clauses = []
            remaining = group

            # Match prefix:value (colon style)
            for match in re.finditer(r"(\w+):(\S+)", group):
                field_key = match.group(1).lower()
                value = match.group(2)
                columns = self._FIELD_MAP.get(field_key)
                if columns:
                    col_likes = " OR ".join(f"{c} LIKE ?" for c in columns)
                    and_clauses.append(f"({col_likes})")
                    params.extend([f"%{value}%"] * len(columns))
                    remaining = remaining.replace(match.group(0), "", 1)

            # Match PREFIX word... (space style, e.g. "FROM Amy Brooks")
            prefix_match = re.match(
                r"(from|sender|to|subject|body)\s+(.+)",
                remaining.strip(),
                re.IGNORECASE,
            )
            if prefix_match:
                field_key = prefix_match.group(1).lower()
                value = prefix_match.group(2).strip()
                columns = self._FIELD_MAP.get(field_key)
                if columns and value:
                    col_likes = " OR ".join(f"{c} LIKE ?" for c in columns)
                    and_clauses.append(f"({col_likes})")
                    params.extend([f"%{value}%"] * len(columns))
                    remaining = ""

            # Anything left is a general search across all fields
            remaining = remaining.strip()
            if remaining:
                and_clauses.append(
                    "(subject LIKE ? OR from_name LIKE ? OR from_email LIKE ? OR body_text LIKE ?)"
                )
                params.extend([f"%{remaining}%"] * 4)

            if and_clauses:
                or_clauses.append("(" + " AND ".join(and_clauses) + ")")

        if not or_clauses:
            return "1=1", []

        return "(" + " OR ".join(or_clauses) + ")", params

    # ------------------------------------------------------------------
    # Manager — preferences, relationships, log, drafts
    # ------------------------------------------------------------------

    def add_preference(self, pattern: str, action: str, condition: dict | None = None, source: str = "explicit") -> dict:
        now = _now()
        pattern = pattern.lower().strip()
        with self._lock:
            self.conn.execute(
                "INSERT INTO email_preferences (pattern, action, condition, source, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(pattern) DO UPDATE SET action=excluded.action, condition=excluded.condition, source=excluded.source",
                (pattern, action, json.dumps(condition) if condition else None, source, now),
            )
            self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM email_preferences WHERE pattern = ?", (pattern,)
        ).fetchone()
        return dict(row)

    def list_preferences(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM email_preferences ORDER BY pattern"
        ).fetchall()
        return [dict(r) for r in rows]

    def remove_preference(self, pattern: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM email_preferences WHERE pattern = ?", (pattern.lower().strip(),)
            )
            self.conn.commit()
        return cur.rowcount > 0

    def get_matching_preference(self, from_email: str, category: str | None = None) -> dict | None:
        """Find the best matching preference: exact email > @domain > category:X."""
        email_lc = (from_email or "").lower()
        # Exact email match
        row = self.conn.execute(
            "SELECT * FROM email_preferences WHERE pattern = ?", (email_lc,)
        ).fetchone()
        if row:
            return dict(row)
        # Domain match
        if "@" in email_lc:
            domain = "@" + email_lc.split("@", 1)[1]
            row = self.conn.execute(
                "SELECT * FROM email_preferences WHERE pattern = ?", (domain,)
            ).fetchone()
            if row:
                return dict(row)
        # Category match
        if category:
            row = self.conn.execute(
                "SELECT * FROM email_preferences WHERE pattern = ?", (f"category:{category.lower()}",)
            ).fetchone()
            if row:
                return dict(row)
        return None

    def update_sender_relationship(self, email_addr: str, name: str | None) -> None:
        now = _now()
        email_lc = (email_addr or "").lower().strip()
        if not email_lc:
            return
        with self._lock:
            existing = self.conn.execute(
                "SELECT message_count FROM sender_relationships WHERE email = ?", (email_lc,)
            ).fetchone()
            if existing:
                self.conn.execute(
                    "UPDATE sender_relationships SET name=COALESCE(?, name), last_seen=?, "
                    "message_count=message_count+1, updated_at=? WHERE email=?",
                    (name, now, now, email_lc),
                )
            else:
                self.conn.execute(
                    "INSERT INTO sender_relationships (email, name, first_seen, last_seen, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (email_lc, name, now, now, now),
                )
            self.conn.commit()

    def list_relationships(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM sender_relationships ORDER BY last_seen DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def log_action(self, message_id: str, action: str, reason: str, preference_id: int | None = None) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO management_log (message_id, action, reason, preference_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (message_id, action, reason, preference_id, _now()),
            )
            if preference_id is not None:
                self.conn.execute(
                    "UPDATE email_preferences SET last_applied=?, apply_count=apply_count+1 WHERE id=?",
                    (_now(), preference_id),
                )
            self.conn.commit()

    def get_unreviewed_mailing_lists(self, limit: int = 30) -> list[dict]:
        """Return unique mailing-list senders with no preference set yet.

        Excludes senders whose exact email OR @domain already has a preference.
        Groups by from_email, ordered by message count descending.
        """
        rows = self.conn.execute(
            "SELECT from_email, from_name, COUNT(*) as message_count, "
            "MAX(subject) as example_subject, MAX(received_at) as last_received "
            "FROM messages "
            "WHERE category = 'mailing-list' AND from_email IS NOT NULL AND from_email != '' "
            "GROUP BY from_email "
            "ORDER BY message_count DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()

        # Filter out senders that already have a preference (exact or @domain)
        existing = {r["pattern"] for r in self.conn.execute(
            "SELECT pattern FROM email_preferences"
        ).fetchall()}

        results = []
        for r in rows:
            d = dict(r)
            email = d["from_email"].lower()
            domain = "@" + email.split("@", 1)[1] if "@" in email else ""
            if email not in existing and domain not in existing:
                results.append(d)
        return results

    def list_log(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT l.*, m.subject, m.from_name, m.from_email "
            "FROM management_log l "
            "LEFT JOIN messages m ON l.message_id = m.id "
            "ORDER BY l.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_draft(self, message_id: str, thread_id: str | None, draft_body: str,
                  draft_subject: str, to_addr: dict, notes: str | None = None) -> dict:
        import uuid
        draft_id = str(uuid.uuid4())
        now = _now()
        with self._lock:
            self.conn.execute(
                "INSERT INTO email_drafts "
                "(id, message_id, thread_id, draft_body, draft_subject, to_addr, notes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (draft_id, message_id, thread_id, draft_body, draft_subject,
                 json.dumps(to_addr), notes, now),
            )
            self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM email_drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        return dict(row)

    def list_drafts(self, status: str = "pending") -> list[dict]:
        rows = self.conn.execute(
            "SELECT d.*, m.subject as orig_subject, m.from_name, m.from_email "
            "FROM email_drafts d "
            "LEFT JOIN messages m ON d.message_id = m.id "
            "WHERE d.status = ? ORDER BY d.created_at DESC",
            (status,),
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            if d.get("to_addr"):
                try:
                    d["to_addr"] = json.loads(d["to_addr"])
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append(d)
        return results

    def update_draft_status(self, draft_id: str, status: str) -> dict | None:
        now = _now()
        with self._lock:
            self.conn.execute(
                "UPDATE email_drafts SET status=?, reviewed_at=? WHERE id=?",
                (status, now, draft_id),
            )
            self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM email_drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("to_addr"):
            try:
                d["to_addr"] = json.loads(d["to_addr"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    def _decode(self, row: dict) -> dict:
        for field in ("to_addrs", "cc_addrs", "attachments"):
            if row.get(field):
                try:
                    row[field] = json.loads(row[field])
                except (json.JSONDecodeError, TypeError):
                    row[field] = []
        row["is_read"] = bool(row.get("is_read"))
        row["is_flagged"] = bool(row.get("is_flagged"))
        row["has_attachments"] = bool(row.get("has_attachments"))
        return row
