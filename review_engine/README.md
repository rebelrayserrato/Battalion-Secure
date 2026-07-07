<!-- ============================================================= -->
<!-- RAYAAAA-194 DEPLOY BANNER — READ BEFORE UPLOADING ANYTHING     -->
<!-- ============================================================= -->
> ## ⚠️ SYNTHETIC / OWNER-INTERNAL DATA ONLY
>
> This service is deployed as an **internal-only** tool on the RAYSERR VPS, reachable
> **only** through the owner-portal-authenticated admin route `/admin/review-engine`
> (nginx `auth_request` → admin session check). It is **not** publicly routable.
>
> **Do NOT upload real client case files.** The GDPR erasure/retention sweep
> (RAYAAAA-182 pattern) does **not** yet reach this tool's data store
> (`data/review_engine.sqlite3` + `data/uploads/<matter_id>/` + Chroma vectors in
> `data/indexes/<matter_id>/` — embeddings are personal data). Until the erasure
> cascade (RAYAAAA-196) + retention purge job + DPIA sign-off (RAYAAAA-198) are live
> and the CTO/QualityGuard sec/QA gate has passed, use **synthetic or the owner's own
> internal documents only**. Real client PII is a one-way door gated on Counsel's
> B1–B5 (see RAYAAAA-192/195).
>
> Findings here are **risk indicators to guide human review — not legal or fraud
> determinations.**
<!-- ============================================================= -->

# Local Evidence Review Engine v0.1

A local-first evidence review platform for document sets involving HR, legal-risk, and
potential fraud indicator screening. It extracts source-linked evidence, builds a
searchable local index, detects rule-based review flags and data anomalies, and creates
DOCX/PDF reports for human review.

This is not a chatbot-first application. It does not make final legal conclusions and
does not determine that fraud occurred.

## Privacy and local operation

- Original files and extracted data stay in `data/` on the local machine.
- No outside API is called by default.
- Uploaded documents are not used for model training.
- Embedding uses a locally cached `all-MiniLM-L6-v2` model when present. If it is not
  already cached, the app uses a deterministic local hashing fallback and does not
  download it.
- Ollama is optional, disabled by default, and addressed only at `127.0.0.1`. It may
  summarize existing findings but cannot create findings.

## Setup

Python 3.11 is recommended.

```powershell
cd "C:\Users\Raymundo Serrato\Documents\RAYSERR Solutions\review_engine"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## Run

```powershell
python run_app.py
```

Streamlit prints the local browser address, usually `http://localhost:8501`.

## Workflow

1. Create a matter and enter the jurisdiction if known.
2. Upload PDF, DOCX, TXT, CSV, or XLSX files in **Documents**.
3. Save the originals, then select **Process documents**.
4. Search extracted evidence by question or keyword.
5. Run the HR/legal-risk and/or potential fraud indicator reviews.
6. Inspect the timeline and every finding's cited source.
7. Export a DOCX or PDF report.
8. Review the audit log.

Matter metadata, chunks, entities, findings, and audit events are stored in
`data/review_engine.sqlite3`. Originals are stored in `data/uploads/<matter_id>/`;
Chroma indexes are stored in `data/indexes/<matter_id>/`.

## Optional Ollama

Install and start Ollama separately, make the desired model available locally, then
enable the option in **Export report**. The connector sends only already-created
findings to the local Ollama server. Its output remains a draft and must be reviewed.

## Tests

```powershell
python -m pytest -q
```

## Limitations

- v0.1 uses simple extraction, regular expressions, keywords, and column-name
  heuristics. OCR for image-only PDFs is not included.
- DOCX page numbers are not reliably available; sections are cited instead.
- Spreadsheet row references assume row 1 is the header.
- Contradiction detection does not yet perform robust entity resolution.
- Missing-document flags mean a referenced document was not found in processed text;
  they do not prove the document does not exist.
- Isolation Forest scores are relative to the uploaded numeric dataset and are not
  proof of misconduct.
- A local hashing embedding fallback is less semantically capable than a transformer.
- Jurisdiction-dependent issues are marked `Jurisdiction required` when unknown.
- No authentication, encryption-at-rest, malware scanning, OCR, or retention policy is
  included in this local MVP.

## Legal disclaimer

This software supports document organization and issue spotting. It does not provide
legal advice, determine rights or liability, or replace review by qualified counsel.
All flags require validation against the cited evidence and applicable jurisdiction.

## Fraud disclaimer

The software identifies only potential fraud indicators and red flags. A match,
duplicate, anomaly, relationship, or inconsistent record does not establish that fraud
occurred. Findings require human review and corroborating evidence.

## Roadmap

- OCR and richer table extraction
- Matter access control, encryption, retention, and secure deletion
- Configurable jurisdiction-specific rule packs reviewed by counsel
- Entity resolution and event clustering
- Cross-document invoice/vendor reconciliation
- Native evidence viewer with highlighted source spans
- Model and rule versioning, reviewer decisions, and finding disposition
- Signed report manifests and stronger audit integrity
