from __future__ import annotations
import json
from urllib.error import URLError
from urllib.request import Request, urlopen


class OllamaConnector:
    def __init__(self, model="llama3.2", base_url="http://127.0.0.1:11434"):
        self.model, self.base_url = model, base_url.rstrip("/")

    def available(self):
        try:
            with urlopen(f"{self.base_url}/api/tags", timeout=1) as response:
                return response.status == 200
        except (OSError, URLError): return False

    def summarize_findings(self, findings, purpose="executive summary"):
        if not findings: return "No source-supported findings are available to summarize."
        allowed = [{"title": f["title"], "category": f["category"], "explanation": f["explanation"], "confidence": f["confidence"], "sources": f["supporting_sources"]} for f in findings]
        prompt = f"Draft a concise {purpose} using ONLY the JSON findings below. Do not add facts, findings, legal conclusions, or say fraud occurred. Preserve source reference IDs. Use 'potential fraud indicator', 'red flag', or 'requires human review' where applicable.\n" + json.dumps(allowed)
        request = Request(f"{self.base_url}/api/generate", data=json.dumps({"model": self.model, "prompt": prompt, "stream": False}).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode())["response"].strip()
