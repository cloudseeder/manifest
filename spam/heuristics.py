"""Heuristic pre-filter — fast pattern matching before model inference.

Returns a verdict (SPAM, HAM, UNSURE) without touching the LLM.
Catches 60-70% of obvious cases in microseconds.
"""

import re
import logging
from dataclasses import dataclass
from enum import Enum
from imap_handler import EmailMessage

log = logging.getLogger(__name__)


class Verdict(Enum):
    SPAM = "spam"
    HAM = "ham"
    UNSURE = "unsure"


@dataclass
class HeuristicResult:
    verdict: Verdict
    confidence: float  # 0.0 to 1.0
    reasons: list[str]


class HeuristicFilter:
    def __init__(self, config: dict):
        heuristics = config.get("heuristics", {})
        self.blocked_domains = set(
            d.lower() for d in heuristics.get("blocked_domains", [])
        )
        self.allowlisted_domains = set(
            d.lower() for d in heuristics.get("allowlisted_domains", [])
        )
        self.spam_subject_patterns = [
            re.compile(p) for p in heuristics.get("spam_subject_patterns", [])
        ]
        self.require_spf = heuristics.get("require_spf_pass", False)
        self.require_dkim = heuristics.get("require_dkim_pass", False)

    def check(self, msg: EmailMessage) -> HeuristicResult:
        """Run all heuristic checks on a message. Fast path — no LLM needed."""
        reasons = []
        spam_score = 0.0
        ham_score = 0.0

        # === ALLOWLIST CHECK (instant ham) ===
        if msg.from_domain in self.allowlisted_domains:
            return HeuristicResult(
                verdict=Verdict.HAM,
                confidence=0.95,
                reasons=[f"Allowlisted domain: {msg.from_domain}"],
            )

        # === BLOCKLIST CHECK (instant spam) ===
        if msg.from_domain in self.blocked_domains:
            return HeuristicResult(
                verdict=Verdict.SPAM,
                confidence=0.95,
                reasons=[f"Blocked domain: {msg.from_domain}"],
            )

        # === SUBJECT PATTERN MATCHING ===
        for pattern in self.spam_subject_patterns:
            if pattern.search(msg.subject):
                spam_score += 0.4
                reasons.append(f"Subject matches spam pattern: {pattern.pattern[:40]}")

        # === HEADER ANALYSIS ===

        # SPF check
        spf = msg.headers.get("received-spf", "").lower()
        if "fail" in spf or "softfail" in spf:
            spam_score += 0.2
            reasons.append("SPF fail/softfail")
        elif "pass" in spf:
            ham_score += 0.1

        # Authentication results
        auth = msg.headers.get("authentication-results", "").lower()
        if "dkim=fail" in auth:
            spam_score += 0.2
            reasons.append("DKIM failure")
        elif "dkim=pass" in auth:
            ham_score += 0.1

        # Upstream spam header (some servers add these)
        x_spam = msg.headers.get("x-spam-status", "").lower()
        if x_spam.startswith("yes"):
            spam_score += 0.3
            reasons.append("Upstream X-Spam-Status: Yes")

        # === BODY HEURISTICS ===
        body_lower = msg.body_text.lower()

        # Suspicious URL patterns
        suspicious_url_count = len(re.findall(
            r'https?://(?:\d{1,3}\.){3}\d{1,3}', body_lower
        ))  # IP-based URLs
        if suspicious_url_count > 0:
            spam_score += 0.15 * min(suspicious_url_count, 3)
            reasons.append(f"IP-based URLs found: {suspicious_url_count}")

        # Excessive capitalization in body
        if len(msg.body_text) > 100:
            upper_ratio = sum(1 for c in msg.body_text if c.isupper()) / len(msg.body_text)
            if upper_ratio > 0.4:
                spam_score += 0.15
                reasons.append(f"Excessive caps: {upper_ratio:.0%}")

        # Empty body with attachment indicators
        if len(msg.body_text.strip()) < 20 and "multipart" in msg.headers.get("content-type", "").lower():
            spam_score += 0.1
            reasons.append("Near-empty body with attachments")

        # Known spam phrases in body
        spam_phrases = [
            r"click here to (claim|verify|confirm)",
            r"your account (has been|will be) (suspended|closed|limited)",
            r"wire transfer",
            r"western union",
            r"you have been selected",
            r"million (dollars|usd|pounds)",
            r"act immediately",
            r"risk.?free",
            r"no credit check",
            r"double your",
        ]
        for phrase in spam_phrases:
            if re.search(phrase, body_lower):
                spam_score += 0.2
                reasons.append(f"Spam phrase: {phrase[:30]}")
                break  # One body phrase match is enough

        # === SCORING ===
        net_score = spam_score - ham_score

        if net_score >= 0.6:
            return HeuristicResult(
                verdict=Verdict.SPAM,
                confidence=min(0.5 + net_score * 0.4, 0.95),
                reasons=reasons,
            )
        elif net_score <= -0.2 and not reasons:
            return HeuristicResult(
                verdict=Verdict.HAM,
                confidence=0.6 + ham_score * 0.3,
                reasons=reasons or ["No spam indicators found"],
            )
        else:
            return HeuristicResult(
                verdict=Verdict.UNSURE,
                confidence=0.5,
                reasons=reasons or ["Insufficient signals — needs model"],
            )
