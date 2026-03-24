"""Configuration loader for oap-email."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class IMAPConfig:
    host: str = ""
    port: int = 993
    username: str = ""
    password: str = ""
    use_ssl: bool = True
    # Folders to scan (IMAP folder names)
    folders: list[str] = field(default_factory=lambda: ["INBOX"])


_DEFAULT_CATEGORIES: dict[str, str] = {
    "personal": (
        "written by a real individual person: colleagues, friends, family, clients, "
        "neighbors, community members. Strong signals: sent from a personal email "
        "address (gmail, yahoo, icloud, hotmail, or any personal domain), has a "
        "real person's name as the sender, conversational or direct tone. "
        "HOA/community group emails where a real person is writing also count. "
        "When in doubt between personal and mailing-list, prefer personal."
    ),
    "machine": (
        "automated/system-generated with no human author: server alerts, "
        "cron output, cPanel, disk space warnings, security scans, WordPress updates, "
        "CI/CD, monitoring, settlement reports, auth codes"
    ),
    "mailing-list": (
        "informational newsletters, news digests, editorial content, "
        "industry bulletins (CISA advisories, tech newsletters, curated content). "
        "NOT social notifications about people you know (those are personal). "
        "NOT promotional offers (those are offers)"
    ),
    "spam": "junk, phishing, unsolicited bulk email, adult content",
    "offers": (
        "selling something: sales, promotions, deals, coupons, discounts, "
        "event tickets, subscription renewals, product launches, service upgrades"
    ),
}


_DEFAULT_PRIORITIES: dict[str, str] = {
    "urgent": (
        "needs attention now: bank/financial alerts, password resets, "
        "security notices, direct requests from known people asking for "
        "a timely response, appointment confirmations for today"
    ),
    "important": (
        "should see today: emails from CPA/accountant/lawyer, HOA notices, "
        "work correspondence, personal messages from real people, "
        "bills or invoices, appointment reminders"
    ),
    "informational": (
        "nice to know but no action needed: LinkedIn updates, news digests, "
        "community announcements (PLUG, meetups), industry newsletters, "
        "social media summaries about people you know"
    ),
    "noise": (
        "safe to ignore: Facebook/Instagram/Reddit notifications, marketing emails, "
        "promotional offers, subscription renewals, automated social media alerts, "
        "bulk newsletters from companies"
    ),
}


@dataclass
class ClassifierConfig:
    enabled: bool = False
    ollama_url: str = "http://localhost:11434"
    model: str = "qwen3.5:latest"
    timeout: int = 120
    categories: dict[str, str] = field(default_factory=lambda: dict(_DEFAULT_CATEGORIES))
    priorities: dict[str, str] = field(default_factory=lambda: dict(_DEFAULT_PRIORITIES))
    sender_overrides: dict[str, dict[str, str]] = field(default_factory=dict)
    use_escalation: bool = False  # use big LLM instead of local model


@dataclass
class EscalationConfig:
    enabled: bool = False
    provider: str = "anthropic"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    timeout: int = 60
    max_tokens: int = 256


@dataclass
class AutoFileConfig:
    enabled: bool = False
    # Map category → IMAP folder name (created if missing)
    folders: dict[str, str] = field(default_factory=lambda: {
        "personal": "INBOX",
        "machine": "machine",
        "mailing-list": "mailing-list",
        "spam": "spam",
        "offers": "offers",
    })


@dataclass
class SpamFilterConfig:
    """Pre-classifier spam tiers — run before the expensive LLM.

    Tier order: blocked_domains → x_spam_status header → local model.
    Only messages that pass all tiers reach the full category+priority LLM.
    """
    enabled: bool = False
    # Small local model for fast spam/ham binary check (qwen3:2b recommended)
    local_model: str = "qwen3:1.7b"
    # Confidence threshold — only auto-classify as spam above this score
    spam_threshold: float = 0.85
    # Domain blocklist — instant spam with no model call (e.g. ["spam-kingdom.com"])
    blocked_domains: list[str] = field(default_factory=list)
    # Minimum SA score to trust X-Spam-Status unconditionally (skip LLM).
    # Set higher on a fresh/untrained SA install to avoid false positives.
    sa_score_threshold: float = 10.0


@dataclass
class SALearnConfig:
    url: str = ""        # https://mail.netgate.net/salearn/train
    api_key: str = ""    # X-Api-Key header value


@dataclass
class SMTPConfig:
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = ""
    use_tls: bool = True


@dataclass
class ManagerConfig:
    enabled: bool = False
    archive_enabled: bool = True
    unsubscribe_enabled: bool = True
    draft_reply_enabled: bool = False
    learning_enabled: bool = True
    archive_folder: str = "archive"
    draft_reply_categories: list[str] = field(default_factory=lambda: ["personal"])
    draft_reply_priorities: list[str] = field(default_factory=lambda: ["urgent", "important"])
    discovery_url: str = "http://localhost:8300"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:8b"
    use_escalation: bool = False  # use big LLM for draft generation


@dataclass
class Config:
    imap: IMAPConfig = field(default_factory=IMAPConfig)
    smtp: SMTPConfig = field(default_factory=SMTPConfig)
    salearn: SALearnConfig = field(default_factory=SALearnConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    auto_file: AutoFileConfig = field(default_factory=AutoFileConfig)
    escalation: EscalationConfig = field(default_factory=EscalationConfig)
    manager: ManagerConfig = field(default_factory=ManagerConfig)
    spam_filter: SpamFilterConfig = field(default_factory=SpamFilterConfig)
    db_path: str = "oap_email.db"
    host: str = "127.0.0.1"
    port: int = 8305
    # Max messages to cache per folder
    max_cached: int = 500
    # Default scan window (hours) when no 'since' provided
    default_scan_hours: int = 24


def load_config(path: str | None = None) -> Config:
    path = path or os.environ.get("OAP_EMAIL_CONFIG", "config.yaml")
    cfg = Config()
    p = Path(path)
    if p.exists():
        raw = yaml.safe_load(p.read_text()) or {}

        # IMAP settings
        imap = raw.get("imap", {})
        cfg.imap.host = imap.get("host", cfg.imap.host)
        cfg.imap.port = imap.get("port", cfg.imap.port)
        cfg.imap.username = imap.get("username", cfg.imap.username)
        cfg.imap.password = os.environ.get("OAP_EMAIL_PASSWORD", imap.get("password", ""))
        cfg.imap.use_ssl = imap.get("use_ssl", cfg.imap.use_ssl)
        if "folders" in imap:
            cfg.imap.folders = imap["folders"]

        # Database
        db = raw.get("database", {})
        if "path" in db:
            cfg.db_path = db["path"]

        # API
        api = raw.get("api", {})
        cfg.host = api.get("host", cfg.host)
        cfg.port = api.get("port", cfg.port)

        cfg.max_cached = raw.get("max_cached", cfg.max_cached)
        cfg.default_scan_hours = raw.get("default_scan_hours", cfg.default_scan_hours)

        # Classifier
        cl = raw.get("classifier", {})
        cfg.classifier.enabled = cl.get("enabled", cfg.classifier.enabled)
        cfg.classifier.ollama_url = cl.get("ollama_url", cfg.classifier.ollama_url)
        cfg.classifier.model = cl.get("model", cfg.classifier.model)
        cfg.classifier.timeout = cl.get("timeout", cfg.classifier.timeout)
        if "categories" in cl:
            # Merge user categories into defaults — user can override or add
            cfg.classifier.categories.update(cl["categories"])
        if "priorities" in cl:
            cfg.classifier.priorities.update(cl["priorities"])
        if "sender_overrides" in cl:
            cfg.classifier.sender_overrides = {
                k.lower(): v for k, v in cl["sender_overrides"].items()
            }
        cfg.classifier.use_escalation = cl.get("use_escalation", cfg.classifier.use_escalation)

        # Escalation (for classifier big LLM option)
        esc = raw.get("escalation", {})
        cfg.escalation.enabled = esc.get("enabled", cfg.escalation.enabled)
        cfg.escalation.provider = esc.get("provider", cfg.escalation.provider)
        cfg.escalation.base_url = esc.get("base_url", cfg.escalation.base_url)
        cfg.escalation.model = esc.get("model", cfg.escalation.model)
        cfg.escalation.api_key = os.environ.get(
            "OAP_ESCALATION_API_KEY",
            os.environ.get(
                f"OAP_{cfg.escalation.provider.upper()}_API_KEY",
                esc.get("api_key", ""),
            ),
        )
        cfg.escalation.timeout = esc.get("timeout", cfg.escalation.timeout)
        cfg.escalation.max_tokens = esc.get("max_tokens", cfg.escalation.max_tokens)

        # Auto-file
        af = raw.get("auto_file", {})
        cfg.auto_file.enabled = af.get("enabled", cfg.auto_file.enabled)
        if "folders" in af:
            cfg.auto_file.folders.update(af["folders"])

        # SMTP
        smtp = raw.get("smtp", {})
        cfg.smtp.host = smtp.get("host", cfg.smtp.host)
        cfg.smtp.port = smtp.get("port", cfg.smtp.port)
        cfg.smtp.username = smtp.get("username", cfg.smtp.username)
        cfg.smtp.password = os.environ.get("OAP_SMTP_PASSWORD", smtp.get("password", ""))
        cfg.smtp.use_tls = smtp.get("use_tls", cfg.smtp.use_tls)

        # SA-learn
        sal = raw.get("salearn", {})
        cfg.salearn.url = sal.get("url", cfg.salearn.url)
        cfg.salearn.api_key = os.environ.get("OAP_SALEARN_API_KEY", sal.get("api_key", ""))

        # Spam pre-filter
        sf = raw.get("spam_filter", {})
        cfg.spam_filter.enabled = sf.get("enabled", cfg.spam_filter.enabled)
        cfg.spam_filter.local_model = sf.get("local_model", cfg.spam_filter.local_model)
        cfg.spam_filter.spam_threshold = sf.get("spam_threshold", cfg.spam_filter.spam_threshold)
        cfg.spam_filter.sa_score_threshold = sf.get("sa_score_threshold", cfg.spam_filter.sa_score_threshold)
        if "blocked_domains" in sf:
            cfg.spam_filter.blocked_domains = [d.lower() for d in sf["blocked_domains"]]

        # Manager
        mg = raw.get("manager", {})
        cfg.manager.enabled = mg.get("enabled", cfg.manager.enabled)
        cfg.manager.archive_enabled = mg.get("archive_enabled", cfg.manager.archive_enabled)
        cfg.manager.unsubscribe_enabled = mg.get("unsubscribe_enabled", cfg.manager.unsubscribe_enabled)
        cfg.manager.draft_reply_enabled = mg.get("draft_reply_enabled", cfg.manager.draft_reply_enabled)
        cfg.manager.learning_enabled = mg.get("learning_enabled", cfg.manager.learning_enabled)
        cfg.manager.archive_folder = mg.get("archive_folder", cfg.manager.archive_folder)
        cfg.manager.discovery_url = mg.get("discovery_url", cfg.manager.discovery_url)
        cfg.manager.ollama_url = mg.get("ollama_url", cfg.manager.ollama_url)
        cfg.manager.ollama_model = mg.get("ollama_model", cfg.manager.ollama_model)
        cfg.manager.use_escalation = mg.get("use_escalation", cfg.manager.use_escalation)

        # Resolve relative DB path against config file directory
        db_path = Path(cfg.db_path)
        if not db_path.is_absolute():
            cfg.db_path = str(p.parent.resolve() / db_path)
    else:
        db_path = Path(cfg.db_path)
        if not db_path.is_absolute():
            cfg.db_path = str(Path(__file__).resolve().parent.parent / db_path)
    return cfg
