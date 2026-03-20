"""Ollama classifier — optimized for fast spam/ham classification.

Key optimizations over naive LLM classification:
1. Structured prompt that constrains output to a single JSON line
2. Truncated/normalized email body (no need for full content)
3. Uses the smallest viable model (0.5B-1.5B params)
4. Extracts features into a compact representation before sending
"""

import json
import logging
import time
from dataclasses import dataclass
import requests
from imap_handler import EmailMessage

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an email spam classifier. You MUST respond with ONLY a JSON object on a single line, nothing else.

Classification rules:
- "spam": Unsolicited commercial email, phishing, scams, malware delivery, fake notifications
- "ham": Legitimate email the recipient wants — personal, work, subscriptions they signed up for, transactional (receipts, shipping, password resets)
- "suspect": Could be either — marketing from known companies, borderline newsletters

Respond ONLY with: {"label":"spam|ham|suspect","score":0.0-1.0}
The score is your confidence (1.0 = certain).

Do NOT explain. Do NOT add any text outside the JSON."""

CLASSIFY_TEMPLATE = """From: {from_addr}
Subject: {subject}
Body preview: {body_preview}

Classify this email."""


@dataclass
class ClassificationResult:
    label: str        # "spam", "ham", "suspect"
    score: float      # 0.0 to 1.0
    latency_ms: float
    model: str
    error: str = ""


class OllamaClassifier:
    def __init__(self, config: dict):
        ollama_cfg = config.get("ollama", {})
        self.base_url = ollama_cfg.get("base_url", "http://localhost:11434")
        self.model = ollama_cfg.get("model", "qwen2.5:0.5b")
        self.timeout = ollama_cfg.get("timeout", 30)

        classifier_cfg = config.get("classifier", {})
        self.max_body_length = classifier_cfg.get("max_body_length", 1500)

    def is_available(self) -> bool:
        """Check if Ollama is running and the model is loaded."""
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=5)
            if resp.status_code == 200:
                models = [m["name"] for m in resp.json().get("models", [])]
                # Check for exact match or prefix match (e.g., "qwen2.5:0.5b" matches "qwen2.5:0.5b")
                available = any(
                    self.model in m or m.startswith(self.model.split(":")[0])
                    for m in models
                )
                if not available:
                    log.warning(
                        f"Model {self.model} not found. Available: {models}. "
                        f"Run: ollama pull {self.model}"
                    )
                return available
        except requests.ConnectionError:
            log.error(f"Ollama not reachable at {self.base_url}")
        return False

    def classify(self, msg: EmailMessage) -> ClassificationResult:
        """Classify a single email message."""
        # Build compact prompt
        body_preview = self._truncate_body(msg.body_text)
        prompt = CLASSIFY_TEMPLATE.format(
            from_addr=msg.from_addr,
            subject=msg.subject[:200],
            body_preview=body_preview,
        )

        start = time.monotonic()

        try:
            resp = requests.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": self.model,
                    "system": SYSTEM_PROMPT,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": 0.1,    # Low temp for consistent classification
                        "top_p": 0.9,
                        "num_predict": 50,     # We only need ~30 tokens for the JSON
                        "stop": ["\n", "```"], # Stop at first newline
                    },
                },
                timeout=self.timeout,
            )
            elapsed_ms = (time.monotonic() - start) * 1000

            if resp.status_code != 200:
                return ClassificationResult(
                    label="suspect", score=0.5, latency_ms=elapsed_ms,
                    model=self.model, error=f"HTTP {resp.status_code}"
                )

            raw_response = resp.json().get("response", "").strip()
            return self._parse_response(raw_response, elapsed_ms)

        except requests.Timeout:
            elapsed_ms = (time.monotonic() - start) * 1000
            log.warning(f"Timeout classifying {msg.display}")
            return ClassificationResult(
                label="suspect", score=0.5, latency_ms=elapsed_ms,
                model=self.model, error="timeout"
            )
        except Exception as e:
            elapsed_ms = (time.monotonic() - start) * 1000
            log.error(f"Classification error: {e}")
            return ClassificationResult(
                label="suspect", score=0.5, latency_ms=elapsed_ms,
                model=self.model, error=str(e)
            )

    def classify_batch(self, messages: list[EmailMessage]) -> list[ClassificationResult]:
        """Classify a batch of messages sequentially.

        Note: Ollama doesn't support true batching, but sequential calls
        to a small model are still fast. A 0.5B model at ~100ms/msg
        processes 800 messages in ~80 seconds.
        """
        results = []
        for i, msg in enumerate(messages):
            result = self.classify(msg)
            results.append(result)
            if (i + 1) % 25 == 0:
                avg_ms = sum(r.latency_ms for r in results) / len(results)
                log.info(
                    f"Progress: {i+1}/{len(messages)} "
                    f"(avg {avg_ms:.0f}ms/msg, "
                    f"ETA {avg_ms * (len(messages) - i - 1) / 1000:.0f}s)"
                )
        return results

    def _truncate_body(self, body: str) -> str:
        """Truncate and clean email body for classification.

        We don't need the full email — spam signals are almost always
        in the first ~1500 chars. This dramatically reduces prompt size.
        """
        if not body:
            return "[empty body]"

        # Collapse whitespace
        text = " ".join(body.split())

        # Truncate
        if len(text) > self.max_body_length:
            text = text[:self.max_body_length] + "..."

        return text

    def _parse_response(self, raw: str, latency_ms: float) -> ClassificationResult:
        """Parse the model's JSON response, handling common failure modes."""
        # Strip markdown fences if present
        raw = raw.strip().strip("`").strip()
        if raw.startswith("json"):
            raw = raw[4:].strip()

        # Try direct JSON parse
        try:
            data = json.loads(raw)
            label = data.get("label", "suspect").lower().strip()
            score = float(data.get("score", 0.5))

            # Normalize label
            if label not in ("spam", "ham", "suspect"):
                if "spam" in label:
                    label = "spam"
                elif "ham" in label or "legit" in label or "safe" in label:
                    label = "ham"
                else:
                    label = "suspect"

            # Clamp score
            score = max(0.0, min(1.0, score))

            return ClassificationResult(
                label=label, score=score, latency_ms=latency_ms,
                model=self.model
            )
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

        # Fallback: look for keywords in raw output
        raw_lower = raw.lower()
        if "spam" in raw_lower:
            return ClassificationResult(
                label="spam", score=0.6, latency_ms=latency_ms,
                model=self.model, error="parsed-fallback"
            )
        elif "ham" in raw_lower or "legitimate" in raw_lower:
            return ClassificationResult(
                label="ham", score=0.6, latency_ms=latency_ms,
                model=self.model, error="parsed-fallback"
            )

        log.warning(f"Unparseable response: {raw[:100]}")
        return ClassificationResult(
            label="suspect", score=0.5, latency_ms=latency_ms,
            model=self.model, error=f"unparseable: {raw[:50]}"
        )
