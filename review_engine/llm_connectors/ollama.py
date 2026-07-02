from __future__ import annotations

import json
from urllib.error import URLError
from urllib.request import Request, urlopen


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
