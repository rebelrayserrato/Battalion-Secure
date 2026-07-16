from __future__ import annotations

import json
import os
from urllib.error import URLError
from urllib.request import Request, urlopen

# RAYAAAA-258: on-box Ollama endpoint + model, resolved from the environment so the
# same code works in local dev (default 127.0.0.1 loopback — where nothing listens,
# so the connector is inert) and in the sealed VPS stack, where the compose service
# sets OLLAMA_BASE_URL=http://ollama:11434 (127.0.0.1 inside the review-engine
# container is NOT the ollama host — it is a separate sealed container on the
# internal net). Defaults preserve the pre-258 behaviour when the vars are unset.
DEFAULT_OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
DEFAULT_OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")


def local_assistant_enabled() -> bool:
    """RAYAAAA-258 feature flag for the local-LLM personal-assistant brain.

    OFF by default. The local model is hosted (Ollama + hermes3:3b) and the connector
    is wired to it, but the assistant surface (sibling issue) must not call the model
    until this flips to "1" — mirrors the MCP connector's MCP_CONNECTOR_ENABLED gate.
    Because the model runs on-box with no external egress, no provider DPA / Ch.V
    legal gate is required (RAYAAAA-191 local-model choice).
    """
    return os.getenv("LOCAL_ASSISTANT_ENABLED", "0") == "1"


# RAYAAAA-232: fixed reply when there is nothing to ground an answer in. Kept as a
# constant so both the connector and the app-layer RAG service return the same
# human-review-first wording, and so tests can assert on it.
GROUNDED_NO_CONTEXT = (
    "The provided documents do not answer this question; requires human review."
)


def build_grounded_prompt(question: str, contexts: list[dict]) -> str:
    """Build the strict, retrieval-only prompt for grounded chat.

    The prompt contains ONLY the question and the numbered retrieved passages —
    no other document text or outside knowledge — so the model cannot introduce
    facts that are not in the retrieved evidence. Each passage keeps its
    source-reference ID so the answer can cite it.
    """
    passages = "\n".join(
        f"[{position}] ({context['source_ref']}) {context['text']}"
        for position, context in enumerate(contexts, start=1)
    )
    return (
        "You answer questions strictly from the numbered source passages below.\n"
        "Rules:\n"
        "- Use ONLY facts stated in the passages. Do not add outside knowledge, "
        "new facts, or legal conclusions.\n"
        "- Cite the source-reference ID (for example SRC-ABC123) in brackets after "
        "each statement you make.\n"
        "- Do not assert that fraud occurred; use 'potential fraud indicator', "
        "'red flag', or 'requires human review' where applicable.\n"
        f"- If the passages do not answer the question, reply exactly: "
        f"\"{GROUNDED_NO_CONTEXT}\"\n\n"
        f"Question: {question}\n\n"
        "Passages:\n"
        f"{passages}"
    )


class OllamaConnector:
    def __init__(self, model: str | None = None, base_url: str | None = None):
        # RAYAAAA-258: fall back to the env-resolved endpoint/model so the same
        # callers work unchanged locally (127.0.0.1 loopback) and in the sealed VPS
        # stack (OLLAMA_BASE_URL=http://ollama:11434, OLLAMA_MODEL=hermes3:3b),
        # without editing any of the existing OllamaConnector() call sites.
        self.model = model or DEFAULT_OLLAMA_MODEL
        self.base_url = (base_url or DEFAULT_OLLAMA_BASE_URL).rstrip("/")

    def available(self) -> bool:
        try:
            with urlopen(f"{self.base_url}/api/tags", timeout=1) as response:
                return response.status == 200
        except (OSError, URLError):
            return False

    def generate(self, prompt: str, timeout: int = 120) -> str:
        """Single grounded completion from the local model (no streaming)."""
        payload = json.dumps(
            {"model": self.model, "prompt": prompt, "stream": False}
        ).encode("utf-8")
        request = Request(
            f"{self.base_url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))["response"].strip()

    def answer_from_context(self, question: str, contexts: list[dict]) -> str:
        """Answer a question using ONLY the retrieved passages (RAYAAAA-232 RAG chat).

        Same guardrails as ``summarize_findings``: no outside facts, no legal
        conclusions, preserve source-reference IDs, defer to human review when the
        passages do not answer. ``contexts`` is the retrieved evidence — nothing
        else is placed in the prompt, so the model has no material to invent from.
        """
        if not contexts:
            return GROUNDED_NO_CONTEXT
        prompt = build_grounded_prompt(question, contexts)
        return self.generate(prompt)

    def summarize_findings(self, findings: list[dict], purpose: str = "executive summary") -> str:
        if not findings:
            return "No source-supported findings are available to summarize."
        allowed = [
            {
                "title": finding["title"],
                "category": finding["category"],
                "explanation": finding["explanation"],
                "confidence": finding["confidence"],
                "sources": finding["supporting_sources"],
            }
            for finding in findings
        ]
        prompt = (
            f"Draft a concise {purpose} using ONLY the JSON findings below. Do not add facts, "
            "findings, legal conclusions, or say fraud occurred. Preserve source reference IDs. "
            "Use 'potential fraud indicator', 'red flag', or 'requires human review' where applicable.\n"
            + json.dumps(allowed)
        )
        payload = json.dumps(
            {"model": self.model, "prompt": prompt, "stream": False}
        ).encode("utf-8")
        request = Request(
            f"{self.base_url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))["response"].strip()
