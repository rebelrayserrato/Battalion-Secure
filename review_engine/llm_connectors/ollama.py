from __future__ import annotations

import json
from urllib.error import URLError
from urllib.request import Request, urlopen

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
    def __init__(self, model: str = "llama3.2", base_url: str = "http://127.0.0.1:11434"):
        self.model = model
        self.base_url = base_url.rstrip("/")

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
