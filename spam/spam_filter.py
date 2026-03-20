#!/usr/bin/env python3
"""
imap-spam-filter — Local IMAP spam classifier using Ollama.

Usage:
    python spam_filter.py                    # Run with config.yaml
    python spam_filter.py --config my.yaml   # Custom config
    python spam_filter.py --dry-run          # Classify but don't move anything
    python spam_filter.py --watch            # Poll continuously
    python spam_filter.py --test-ollama      # Test model connectivity
    python spam_filter.py --demo             # Run with synthetic test messages
"""

import argparse
import logging
import sys
import time
import yaml
from pathlib import Path
from dataclasses import dataclass

from imap_handler import IMAPHandler, EmailMessage
from heuristics import HeuristicFilter, Verdict, HeuristicResult
from classifier import OllamaClassifier, ClassificationResult

log = logging.getLogger("spam_filter")


@dataclass
class FilterDecision:
    """Final decision for a single message."""
    message: EmailMessage
    heuristic: HeuristicResult
    classification: ClassificationResult | None  # None if heuristic was decisive
    action: str  # "quarantine", "review", "pass"
    reason: str


class SpamFilterPipeline:
    def __init__(self, config: dict):
        self.config = config
        self.heuristic_filter = HeuristicFilter(config)
        self.classifier = OllamaClassifier(config)
        self.imap: IMAPHandler | None = None

        classifier_cfg = config.get("classifier", {})
        self.quarantine_threshold = classifier_cfg.get("quarantine_threshold", 0.85)
        self.review_threshold = classifier_cfg.get("review_threshold", 0.5)

        # Stats
        self.stats = {
            "total": 0,
            "heuristic_spam": 0,
            "heuristic_ham": 0,
            "model_spam": 0,
            "model_ham": 0,
            "model_review": 0,
            "errors": 0,
            "total_model_ms": 0.0,
        }

    def process_message(self, msg: EmailMessage) -> FilterDecision:
        """Process a single message through the full pipeline."""
        self.stats["total"] += 1

        # Stage 1: Heuristic pre-filter
        hresult = self.heuristic_filter.check(msg)

        if hresult.verdict == Verdict.SPAM and hresult.confidence >= 0.8:
            self.stats["heuristic_spam"] += 1
            return FilterDecision(
                message=msg,
                heuristic=hresult,
                classification=None,
                action="quarantine",
                reason=f"Heuristic spam ({hresult.confidence:.0%}): {'; '.join(hresult.reasons)}",
            )

        if hresult.verdict == Verdict.HAM and hresult.confidence >= 0.8:
            self.stats["heuristic_ham"] += 1
            return FilterDecision(
                message=msg,
                heuristic=hresult,
                classification=None,
                action="pass",
                reason=f"Heuristic ham ({hresult.confidence:.0%}): {'; '.join(hresult.reasons)}",
            )

        # Stage 2: Model classification for ambiguous messages
        cresult = self.classifier.classify(msg)
        self.stats["total_model_ms"] += cresult.latency_ms

        # Stage 3: Confidence-based routing
        if cresult.label == "spam" and cresult.score >= self.quarantine_threshold:
            self.stats["model_spam"] += 1
            return FilterDecision(
                message=msg,
                heuristic=hresult,
                classification=cresult,
                action="quarantine",
                reason=f"Model: spam ({cresult.score:.0%}, {cresult.latency_ms:.0f}ms)",
            )
        elif cresult.label == "ham" and cresult.score >= self.quarantine_threshold:
            self.stats["model_ham"] += 1
            return FilterDecision(
                message=msg,
                heuristic=hresult,
                classification=cresult,
                action="pass",
                reason=f"Model: ham ({cresult.score:.0%}, {cresult.latency_ms:.0f}ms)",
            )
        else:
            # Low confidence — tag for review, don't auto-quarantine
            self.stats["model_review"] += 1
            return FilterDecision(
                message=msg,
                heuristic=hresult,
                classification=cresult,
                action="review",
                reason=f"Model: {cresult.label} ({cresult.score:.0%}, low confidence, {cresult.latency_ms:.0f}ms)",
            )

    def execute_decision(self, decision: FilterDecision, dry_run: bool = False):
        """Execute a filter decision — move or tag the message."""
        action_symbol = {"quarantine": "🚫", "review": "⚠️ ", "pass": "✅"}
        symbol = action_symbol.get(decision.action, "?")

        log.info(f"{symbol} {decision.message.display} → {decision.action}")
        log.debug(f"   Reason: {decision.reason}")

        if dry_run or not self.imap:
            return

        if decision.action == "quarantine":
            self.imap.move_to_folder(
                decision.message.uid,
                self.config["folders"]["quarantine"],
            )
        elif decision.action == "review":
            self.imap.move_to_folder(
                decision.message.uid,
                self.config["folders"]["review"],
            )
        else:
            # Pass — just flag as processed so we don't re-check
            self.imap.flag_processed(decision.message.uid)

    def run_scan(self, dry_run: bool = False):
        """Run a single scan of the inbox."""
        batch_size = self.config.get("classifier", {}).get("batch_size", 50)

        # Connect to IMAP
        self.imap = IMAPHandler(self.config)
        self.imap.connect()

        try:
            messages = self.imap.fetch_unprocessed(limit=batch_size)
            if not messages:
                log.info("No unprocessed messages found")
                return

            log.info(f"Processing {len(messages)} messages...")
            start = time.monotonic()

            for msg in messages:
                decision = self.process_message(msg)
                self.execute_decision(decision, dry_run=dry_run)

            elapsed = time.monotonic() - start
            self._print_stats(elapsed)

        finally:
            self.imap.disconnect()

    def run_watch(self, dry_run: bool = False):
        """Poll inbox continuously."""
        interval = self.config.get("poll_interval", 300)
        log.info(f"Watch mode — polling every {interval}s (Ctrl+C to stop)")

        while True:
            try:
                self.run_scan(dry_run=dry_run)
            except KeyboardInterrupt:
                log.info("Stopped by user")
                break
            except Exception as e:
                log.error(f"Scan error: {e}")
                self.stats["errors"] += 1

            log.info(f"Sleeping {interval}s...")
            time.sleep(interval)

    def run_demo(self):
        """Run with synthetic test messages to verify the pipeline works."""
        log.info("=== DEMO MODE — synthetic messages ===\n")

        test_messages = [
            EmailMessage(
                uid="1", message_id="<test1>",
                from_addr="prince@spam-kingdom.com",
                from_domain="spam-kingdom.com",
                to_addr="you@example.com",
                subject="CONGRATULATIONS! You WON $5,000,000!!!",
                body_text="Dear friend, you have been selected for a special lottery prize. "
                          "Please wire $500 to claim your winnings. Act immediately!",
                headers={"received-spf": "fail"},
            ),
            EmailMessage(
                uid="2", message_id="<test2>",
                from_addr="notifications@github.com",
                from_domain="github.com",
                to_addr="you@example.com",
                subject="[myrepo] New pull request #42",
                body_text="kevin opened a pull request: Fix authentication handler. "
                          "Changes: src/auth.py — 12 additions, 3 deletions.",
                headers={"received-spf": "pass", "authentication-results": "dkim=pass"},
            ),
            EmailMessage(
                uid="3", message_id="<test3>",
                from_addr="deals@unknown-shop.biz",
                from_domain="unknown-shop.biz",
                to_addr="you@example.com",
                subject="Limited time offer — 80% off everything!",
                body_text="Shop now at http://192.168.1.100/deals for incredible savings. "
                          "Click here to claim your discount before it expires. No credit check required.",
                headers={"received-spf": "softfail"},
            ),
            EmailMessage(
                uid="4", message_id="<test4>",
                from_addr="colleague@company.com",
                from_domain="company.com",
                to_addr="you@example.com",
                subject="Re: Q3 planning meeting notes",
                body_text="Hey, attached are the notes from yesterday's meeting. "
                          "Let me know if you have questions about the timeline. "
                          "Also, can you review the budget spreadsheet by Friday?",
                headers={"received-spf": "pass", "authentication-results": "dkim=pass"},
            ),
            EmailMessage(
                uid="5", message_id="<test5>",
                from_addr="newsletter@techblog.io",
                from_domain="techblog.io",
                to_addr="you@example.com",
                subject="Weekly digest: Top AI stories",
                body_text="This week in AI: New transformer architectures, "
                          "advances in local inference, and the future of edge computing. "
                          "Read more at techblog.io/weekly-42",
                headers={"received-spf": "pass", "list-unsubscribe": "<mailto:unsub@techblog.io>"},
            ),
            EmailMessage(
                uid="6", message_id="<test6>",
                from_addr="security@paypa1.com",
                from_domain="paypa1.com",
                to_addr="you@example.com",
                subject="Urgent: Your account will be suspended",
                body_text="Your account has been limited due to suspicious activity. "
                          "Click here to verify your identity: http://192.168.5.20/verify "
                          "Failure to act immediately will result in permanent suspension.",
                headers={"received-spf": "fail", "authentication-results": "dkim=fail"},
            ),
        ]

        start = time.monotonic()
        ollama_available = self.classifier.is_available()
        if not ollama_available:
            log.warning("Ollama not available — running heuristics only\n")

        for msg in test_messages:
            decision = self.process_message(msg)
            self.execute_decision(decision, dry_run=True)

        elapsed = time.monotonic() - start
        self._print_stats(elapsed)

    def _print_stats(self, elapsed: float):
        """Print processing summary."""
        s = self.stats
        model_calls = s["model_spam"] + s["model_ham"] + s["model_review"]
        avg_model_ms = s["total_model_ms"] / model_calls if model_calls else 0

        print(f"\n{'─' * 50}")
        print(f"Processed {s['total']} messages in {elapsed:.1f}s")
        print(f"  Heuristic catches:  {s['heuristic_spam']} spam, {s['heuristic_ham']} ham")
        print(f"  Model calls:        {model_calls} ({avg_model_ms:.0f}ms avg)")
        print(f"    → spam:           {s['model_spam']}")
        print(f"    → ham:            {s['model_ham']}")
        print(f"    → review:         {s['model_review']}")
        if s["errors"]:
            print(f"  Errors:             {s['errors']}")
        if s["total"] > 0:
            throughput = s["total"] / elapsed
            print(f"  Throughput:         {throughput:.1f} msg/sec")
            est_800 = 800 / throughput
            print(f"  Est. time for 800:  {est_800:.0f}s ({est_800/60:.1f}min)")
        print(f"{'─' * 50}\n")


def load_config(path: str = "config.yaml") -> dict:
    """Load YAML config, falling back to defaults."""
    config_path = Path(path)

    # Try local override first
    local_path = config_path.with_suffix(".local.yaml")
    if local_path.exists():
        config_path = local_path

    if not config_path.exists():
        log.warning(f"Config not found at {config_path}, using defaults")
        return {
            "imap": {"host": "localhost", "port": 993, "username": "", "password": ""},
            "folders": {"inbox": "INBOX", "quarantine": "Quarantine", "review": "Review"},
            "ollama": {"base_url": "http://localhost:11434", "model": "qwen2.5:0.5b"},
            "classifier": {
                "quarantine_threshold": 0.85,
                "review_threshold": 0.5,
                "max_body_length": 1500,
                "batch_size": 50,
            },
            "heuristics": {
                "blocked_domains": [],
                "allowlisted_domains": [],
                "spam_subject_patterns": [],
            },
        }

    with open(config_path) as f:
        return yaml.safe_load(f)


def setup_logging(config: dict):
    """Configure logging."""
    log_cfg = config.get("logging", {})
    level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)

    fmt = "%(asctime)s %(levelname)-5s %(message)s"
    datefmt = "%H:%M:%S"

    handlers = [logging.StreamHandler(sys.stdout)]
    log_file = log_cfg.get("file")
    if log_file:
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, handlers=handlers)


def main():
    parser = argparse.ArgumentParser(description="IMAP Spam Filter with Ollama")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--dry-run", action="store_true", help="Classify without moving")
    parser.add_argument("--watch", action="store_true", help="Poll continuously")
    parser.add_argument("--demo", action="store_true", help="Run with test messages")
    parser.add_argument("--test-ollama", action="store_true", help="Test Ollama connection")
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config)

    pipeline = SpamFilterPipeline(config)

    if args.test_ollama:
        print(f"Testing Ollama at {config['ollama']['base_url']}...")
        print(f"Model: {config['ollama']['model']}")
        if pipeline.classifier.is_available():
            print("✅ Ollama is available and model is loaded")

            # Quick test classification
            test_msg = EmailMessage(
                uid="test", message_id="<test>",
                from_addr="test@test.com", from_domain="test.com",
                to_addr="you@test.com",
                subject="Buy cheap watches now!!!",
                body_text="Amazing deal click here http://192.168.1.1/buy",
                headers={},
            )
            result = pipeline.classifier.classify(test_msg)
            print(f"Test result: {result.label} ({result.score:.0%}) in {result.latency_ms:.0f}ms")
        else:
            print(f"❌ Ollama not available or model not loaded")
            print(f"   Run: ollama pull {config['ollama']['model']}")
        return

    if args.demo:
        pipeline.run_demo()
        return

    if args.watch:
        pipeline.run_watch(dry_run=args.dry_run)
    else:
        pipeline.run_scan(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
