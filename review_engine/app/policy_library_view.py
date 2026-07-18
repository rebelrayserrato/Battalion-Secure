"""RAYAAAA-264: the redesigned per-client Policy Library page.

Mirrors the owner's base44 'Aich-R' Policy Library screen (RAYAAAA-191 refs):
a header + "never shared with other clients" subtitle, the
All / Company Policies / State Laws / Compliance Rules tabs with live counts,
a category-chip filter row, a search box, and two modals:

  * **Add Skill** (Add New Policy) — Title / Policy Type / Category / Description
    / Policy Text / Tags -> Create.
  * **AI Search** — a LOCAL-ONLY compliance search (see the scope note below).

Wiring (no backend fork, no scoping bypass):
  * "Company Policy" / "Compliance Rule" skills are written to the EXISTING
    per-client policy library (RAYAAAA-245) via ``svc.save_policy_upload`` — the
    same client-scoped, hard-isolated store the Chat/policy-audit retrieval
    already composes.
  * "State Law" skills are written to the EXISTING jurisdiction law library
    (RAYAAAA-251) via ``svc.save_law_upload`` — provenance is mandatory there, so
    the modal collects it and we DO NOT bypass the guardrail. A State-Law skill
    is stored under the *client's own jurisdiction* (∪ federal), never another
    state's.

The policy metadata the base44 modal collects (type, category, tags, a short
description) has no dedicated columns in the RAYAAAA-245/251 schema, so we encode
it losslessly into the saved document (a machine-readable header) and into a
``SKILL__<type>__<category>__<title>`` filename that survives ``safe_filename``.
That keeps every skill a first-class, retrievable member of the real corpus
(indexed and reachable by the review pipeline) with zero schema change.

**HARD SCOPE GUARD (RAYAAAA-264 / RAYAAAA-193 / RAYAAAA-243):** the base44 "AI
Compliance Search" looks laws up *online*. This build does NOT egress. The
"AI Search" modal here searches the owner's OWN already-uploaded corpus (this
client's policy library + the client-jurisdiction law library) locally and,
when the on-box model is reachable, structures owner-pasted text — no internet,
consistent with the egress seal and Counsel's owner-upload sourcing decision.
"""
from __future__ import annotations

import html
import re

import streamlit as st

from review_engine.app.retrieval import make_client_scoped_retriever, GroundedAnswerer
from review_engine.clients.jurisdictions import UNSPECIFIED_STATE, state_label
from review_engine.law.library import law_jurisdiction_label, resolve_law_jurisdictions

# Category taxonomy from the base44 chip row (RAYAAAA-191 policy-lib ref).
CATEGORIES: tuple[str, ...] = (
    "Terminations",
    "Conduct",
    "Attendance",
    "Performance",
    "Retirement",
    "Benefits",
    "Compensation",
    "Compliance",
    "Safety",
    "Other",
)

# Policy types offered in the Add-Skill modal. Company Policy + Compliance Rule
# both live in the per-client policy library (RAYAAAA-245); State Law routes to
# the jurisdiction law library (RAYAAAA-251, provenance-gated).
POLICY_TYPES: tuple[str, ...] = ("Company Policy", "Compliance Rule", "State Law")
_TYPE_SLUG = {"Company Policy": "company", "Compliance Rule": "compliance", "State Law": "law"}
_SLUG_TYPE = {v: k for k, v in _TYPE_SLUG.items()}

_SKILL_PREFIX = "SKILL"
_SEP = "__"


def _slug(value: str) -> str:
    """Filename-safe slug that survives ``services.safe_filename``.

    ``safe_filename`` keeps only ``[A-Za-z0-9._ -]``; we further restrict to a
    hyphen-joined lowercase token so the ``SKILL__type__cat__title`` scheme parses
    unambiguously (the ``__`` separator can never appear inside a token).
    """
    cleaned = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return cleaned or "untitled"


def skill_filename(policy_type: str, category: str, title: str) -> str:
    """``SKILL__<type>__<category>__<title>.txt`` for an added policy skill."""
    type_slug = _TYPE_SLUG.get(policy_type, "company")
    return _SEP.join([_SKILL_PREFIX, type_slug, _slug(category), _slug(title)]) + ".txt"


def parse_skill_filename(name: str) -> dict | None:
    """Recover ``{policy_type, type_slug, category_slug}`` from a skill filename.

    Returns ``None`` for a document that was not created by the Add-Skill flow
    (e.g. a bulk-uploaded PDF) — those are treated as Company Policies / Other.
    """
    stem = name[:-4] if name.lower().endswith(".txt") else name
    parts = stem.split(_SEP)
    if len(parts) < 4 or parts[0] != _SKILL_PREFIX:
        return None
    type_slug = parts[1]
    return {
        "policy_type": _SLUG_TYPE.get(type_slug, "Company Policy"),
        "type_slug": type_slug,
        "category_slug": parts[2],
    }


def skill_document_body(title: str, category: str, description: str, tags: list[str], text: str) -> str:
    """Machine-readable header + the policy content, indexed as one document."""
    header = [
        f"# {title.strip()}",
        f"Policy-Category: {category}",
        f"Tags: {', '.join(tags)}" if tags else "Tags:",
    ]
    if description.strip():
        header.append(f"Description: {description.strip()}")
    return "\n".join(header) + "\n\n" + text.strip() + "\n"


def _doc_meta(name: str) -> dict:
    """Display metadata for one policy document row."""
    parsed = parse_skill_filename(name)
    if parsed is None:
        # Legacy / bulk-uploaded file: a Company Policy with no category.
        title = name.rsplit(".", 1)[0]
        return {"title": title, "policy_type": "Company Policy", "type_slug": "company", "category": "Other"}
    # Recover a human title from the slug tail (best-effort; the header keeps the
    # exact title for the indexed content).
    tail = name[:-4].split(_SEP)[3] if name.lower().endswith(".txt") else name
    title = tail.replace("-", " ").title()
    category = parsed["category_slug"].replace("-", " ").title()
    return {
        "title": title,
        "policy_type": parsed["policy_type"],
        "type_slug": parsed["type_slug"],
        "category": category if category in CATEGORIES else "Other",
    }


def compute_counts(policy_docs: list[dict], law_docs: list[dict]) -> dict:
    """Live tab counts: All / Company Policies / State Laws / Compliance Rules."""
    company = compliance = 0
    for doc in policy_docs:
        if _doc_meta(doc["name"])["type_slug"] == "compliance":
            compliance += 1
        else:
            company += 1
    laws = len(law_docs)
    return {
        "all": company + compliance + laws,
        "company": company,
        "state_law": laws,
        "compliance": compliance,
    }


# ---------------------------------------------------------------------------
# Streamlit rendering
# ---------------------------------------------------------------------------

_PL_CSS = """
<style>
  .pl-head{display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;}
  .pl-sub{color:#64748b;font-size:.95rem;margin-top:-.35rem;}
  .pl-badge{display:inline-block;font-size:.7rem;font-weight:700;padding:.12rem .5rem;
    border-radius:999px;margin-right:.4rem;text-transform:uppercase;letter-spacing:.03em;}
  .pl-badge.company{background:#eef2ff;color:#4f46e5;}
  .pl-badge.compliance{background:#ecfdf5;color:#059669;}
  .pl-badge.law{background:#eff6ff;color:#2563eb;}
  .pl-cat{color:#64748b;font-size:.78rem;font-weight:600;}
  .pl-note{background:#f2fbf9;border:1px solid #cdeee8;border-radius:10px;padding:.7rem .9rem;
    color:#1b2f5b;font-size:.85rem;}
</style>
"""


def render_policy_library(svc, clients: list[dict], client_label: dict) -> None:
    st.markdown(_PL_CSS, unsafe_allow_html=True)

    head_l, head_r = st.columns([3, 1.15])
    with head_l:
        st.title("Policy Library")
        st.markdown(
            "<div class='pl-sub'>Your company policies, state laws, and compliance "
            "rules — never shared with other clients.</div>",
            unsafe_allow_html=True,
        )
    if not clients:
        st.info("Create a client in the sidebar first — the policy library is per-client.")
        return

    lib_client = st.selectbox(
        "Client",
        options=[c["id"] for c in clients],
        format_func=lambda cid: client_label.get(cid, cid),
        key="pl_client",
    )
    client_row = next((c for c in clients if c["id"] == lib_client), {})
    client_state = client_row.get("state", UNSPECIFIED_STATE)
    law_jurs = resolve_law_jurisdictions(client_state)

    with head_r:
        st.write("")
        b1, b2 = st.columns(2)
        if b1.button("AI Search", use_container_width=True, key="pl_ai_search_btn"):
            _ai_search_dialog(svc, lib_client, law_jurs)
        if b2.button("Add Skill", type="primary", use_container_width=True, key="pl_add_skill_btn"):
            _add_skill_dialog(svc, lib_client, client_state, law_jurs)

    # --- Load the corpus (policy docs + client-jurisdiction law docs) --------
    policy_docs = svc.db.list_policy_documents(lib_client)
    law_docs: list[dict] = []
    for jur in law_jurs:
        for doc in svc.db.list_law_documents(jur):
            law_docs.append({**doc, "_jurisdiction": jur})
    counts = compute_counts(policy_docs, law_docs)

    # --- Type tabs with live counts (single-select segmented control) --------
    tab_labels = [
        f"All ({counts['all']})",
        f"Company Policies ({counts['company']})",
        f"State Laws ({counts['state_law']})",
        f"Compliance Rules ({counts['compliance']})",
    ]
    tab_keys = ["all", "company", "state_law", "compliance"]
    picked_tab_label = st.segmented_control(
        "Type", tab_labels, default=tab_labels[0], key="pl_type_tab", label_visibility="collapsed"
    )
    active_tab = tab_keys[tab_labels.index(picked_tab_label)] if picked_tab_label else "all"

    # --- Category chip row ----------------------------------------------------
    cat_options = ["All Categories", *CATEGORIES]
    picked_cat = st.pills("Category", cat_options, default="All Categories", key="pl_cat")
    active_cat = picked_cat or "All Categories"

    search = st.text_input(
        "Search skills…", key="pl_search", placeholder="Search skills…",
        label_visibility="collapsed",
    )

    # --- Assemble the filtered, unified skill list ---------------------------
    rows = _assemble_rows(policy_docs, law_docs)
    filtered = _filter_rows(rows, active_tab, active_cat, search)

    st.caption(f"{len(filtered)} of {len(rows)} item(s).")
    if not rows:
        st.markdown(
            "<div class='pl-note'>No policies yet. Use <b>Add Skill</b> to add a "
            "company policy, compliance rule, or state law — or bulk-upload files "
            "in the expander below. Everything stays scoped to this client.</div>",
            unsafe_allow_html=True,
        )
    for row in filtered:
        _render_skill_card(svc, lib_client, row)

    _render_bulk_and_process(svc, lib_client)


def _assemble_rows(policy_docs: list[dict], law_docs: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for doc in policy_docs:
        meta = _doc_meta(doc["name"])
        rows.append({
            "kind": "policy", "name": doc["name"], "title": meta["title"],
            "policy_type": meta["policy_type"], "type_slug": meta["type_slug"],
            "category": meta["category"], "processed": doc.get("processed_at"),
        })
    for doc in law_docs:
        title = doc["name"].rsplit(".", 1)[0]
        rows.append({
            "kind": "law", "name": doc["name"], "title": title,
            "policy_type": "State Law", "type_slug": "law", "category": "Compliance",
            "jurisdiction": doc["_jurisdiction"], "source": doc.get("source_name"),
            "processed": doc.get("processed_at"),
        })
    return rows


def _filter_rows(rows: list[dict], tab: str, category: str, search: str) -> list[dict]:
    out = []
    q = (search or "").strip().lower()
    for row in rows:
        if tab == "company" and row["type_slug"] != "company":
            continue
        if tab == "compliance" and row["type_slug"] != "compliance":
            continue
        if tab == "state_law" and row["type_slug"] != "law":
            continue
        if category != "All Categories" and row["category"] != category:
            continue
        if q and q not in row["title"].lower() and q not in row["name"].lower():
            continue
        out.append(row)
    return out


def _render_skill_card(svc, client_id: str, row: dict) -> None:
    with st.container(border=True):
        badge_cls = {"company": "company", "compliance": "compliance", "law": "law"}[row["type_slug"]]
        meta_line = (
            f"<span class='pl-badge {badge_cls}'>{html.escape(row['policy_type'])}</span>"
            f"<span class='pl-cat'>{html.escape(row['category'])}</span>"
        )
        if row["kind"] == "law":
            jur = law_jurisdiction_label(row.get("jurisdiction", ""))
            meta_line += f" <span class='pl-cat'>· {html.escape(jur)}</span>"
        st.markdown(
            f"**{html.escape(row['title'])}**<br>{meta_line}", unsafe_allow_html=True
        )
        cols = st.columns([6, 1])
        indexed = "Indexed" if row.get("processed") else "Not indexed yet"
        cols[0].caption(indexed)
        if cols[1].button("Delete", key=f"pl_del_{row['kind']}_{row['name']}"):
            if row["kind"] == "policy":
                svc.delete_policy_document(client_id, row["name"])
            else:
                svc.delete_law_document(row["jurisdiction"], row["name"])
            st.rerun()


def _render_bulk_and_process(svc, client_id: str) -> None:
    # Preserve the RAYAAAA-245 bulk file-upload + process/reindex flow so no
    # existing capability is lost (task guard: don't remove working features).
    with st.expander("Bulk-upload policy files / re-index"):
        uploads = st.file_uploader(
            "Upload policy documents",
            type=["pdf", "docx", "txt", "csv", "xlsx", "png", "jpg", "jpeg", "zip"],
            accept_multiple_files=True, key="pl_bulk_uploader",
            help="Stored under this client's local policy library; not sent for model training.",
        )
        if st.button("Save policy files", disabled=not uploads, key="pl_bulk_save"):
            for uploaded in uploads:
                svc.save_policy_upload(client_id, uploaded.name, uploaded.getvalue())
            st.success(f"Saved {len(uploads)} policy file(s).")
            st.rerun()
        if st.button("Process / re-index this library", type="primary", key="pl_process"):
            with st.spinner("Extracting and indexing this client's policies…"):
                result = svc.process_policy_library(client_id)
            if result["errors"]:
                st.warning("\n".join(result["errors"]))
            st.success(
                f"Indexed {result['processed']} policy document(s) into "
                f"{result['chunks']} source chunks."
            )


# ---------------------------------------------------------------------------
# Modals (st.dialog)
# ---------------------------------------------------------------------------


@st.dialog("Add New Skill", width="large")
def _add_skill_dialog(svc, client_id: str, client_state: str, law_jurs: list[str]) -> None:
    title = st.text_input("Title", placeholder="e.g. Attendance Policy", key="as_title")
    policy_type = st.selectbox("Policy Type", POLICY_TYPES, key="as_type")
    category = st.selectbox("Category", CATEGORIES, index=CATEGORIES.index("Other"), key="as_cat")
    description = st.text_input("Description", placeholder="Brief summary of this policy", key="as_desc")
    text = st.text_area("Policy Text", placeholder="Enter the full policy content here…", height=200, key="as_text")

    # Tags: keep a session-scoped list; add via the button (Streamlit has no
    # native "press Enter to add" for a text_input, so mirror it with a button).
    tags: list[str] = st.session_state.setdefault("as_tags", [])
    tcol1, tcol2 = st.columns([4, 1])
    new_tag = tcol1.text_input("Tags", placeholder="Add a tag and press Add", key="as_tag_input", label_visibility="collapsed")
    if tcol2.button("Add", key="as_tag_add") and new_tag.strip():
        if new_tag.strip() not in tags:
            tags.append(new_tag.strip())
        st.rerun()
    if tags:
        st.caption("Tags: " + ", ".join(tags))

    # State Law goes to the provenance-gated law library (RAYAAAA-251): collect
    # the mandatory provenance rather than bypass the guardrail.
    provenance = {}
    law_jur = None
    if policy_type == "State Law":
        law_choices = law_jurs  # {client state} ∪ {federal} only — never another state
        law_jur = st.selectbox(
            "Jurisdiction", law_choices, format_func=law_jurisdiction_label, key="as_law_jur",
        )
        st.caption(
            "State-law skills are stored in the jurisdiction law library and "
            "require official-source provenance (RAYAAAA-243/251). "
            f"Scoped to {state_label(client_state)} ∪ federal only."
        )
        pc = st.columns(2)
        provenance["source_name"] = pc[0].text_input("Source / official publisher", key="as_src_name")
        provenance["source_url"] = pc[1].text_input("Source URL", key="as_src_url")
        provenance["effective"] = pc[0].text_input("Effective date / version", key="as_eff")
        provenance["retrieved"] = pc[1].text_input("Retrieval date (YYYY-MM-DD)", key="as_ret")

    c1, c2 = st.columns(2)
    if c1.button("Cancel", use_container_width=True, key="as_cancel"):
        st.session_state.pop("as_tags", None)
        st.rerun()
    if c2.button("Create Skill", type="primary", use_container_width=True, key="as_create"):
        if not title.strip() or not text.strip():
            st.error("Title and Policy Text are required.")
            return
        body = skill_document_body(title, category, description, tags, text).encode("utf-8")
        try:
            if policy_type == "State Law":
                if not all(v.strip() for v in provenance.values()):
                    st.error("All four provenance fields are required for a state law.")
                    return
                fname = f"{_slug(title)}.txt"
                svc.save_law_upload(
                    law_jur, fname, body,
                    source_name=provenance["source_name"], source_url=provenance["source_url"],
                    effective=provenance["effective"], retrieved=provenance["retrieved"],
                )
                svc.process_law_library(law_jur)
            else:
                fname = skill_filename(policy_type, category, title)
                svc.save_policy_upload(client_id, fname, body)
                svc.process_policy_library(client_id)
        except ValueError as exc:
            st.error(str(exc))
            return
        st.session_state.pop("as_tags", None)
        st.success(f"Created skill “{title.strip()}”.")
        st.rerun()


@st.dialog("AI Compliance Search", width="large")
def _ai_search_dialog(svc, client_id: str, law_jurs: list[str]) -> None:
    st.caption(
        "Searches your **own** uploaded library locally — this client's policies "
        "plus the law reference corpus for its jurisdiction. **No internet:** the "
        "review engine is egress-sealed, and laws are owner-uploaded from official "
        "sources (RAYAAAA-193/243), so this never fetches from the web."
    )
    query = st.text_input("Search your library", placeholder="e.g. Arizona employment law, OSHA…", key="ai_q")
    examples = ["OSHA", "HIPAA", "SOX", "ADA", "FMLA", "GDPR", "Overtime", "Termination"]
    picked = st.pills("Quick examples", examples, key="ai_examples")
    effective_query = (query or "").strip() or (picked or "")

    if st.button("Search", type="primary", key="ai_go", disabled=not effective_query):
        with st.spinner("Searching your local library…"):
            rows = _local_library_search(svc, client_id, law_jurs, effective_query)
            answer = None
            answerer = GroundedAnswerer(retriever=make_client_scoped_retriever(svc.db))
        if not rows:
            st.info(
                "Nothing in your local library matched. Add it with **Add Skill**, "
                "or upload the official-source document in the Law reference library."
            )
        for row in rows:
            with st.container(border=True):
                st.markdown(f"**{html.escape(row['citation'])}**")
                st.write(row["text"][:600] + ("…" if len(row["text"]) > 600 else ""))
                st.caption(f"Source: {row['source']} · {row['origin']}")


def _local_library_search(svc, client_id: str, law_jurs: list[str], query: str) -> list[dict]:
    """Search the client's policy index + the jurisdiction law indexes locally."""
    out: list[dict] = []
    try:
        for r in svc.policy_search(client_id, query, 5):
            out.append({
                "citation": r.get("citation", r.get("source_ref", "")),
                "text": r.get("text", ""), "source": "Policy library", "origin": "policy",
            })
    except Exception:
        pass
    for jur in law_jurs:
        try:
            for r in svc.law_search(jur, query, 5):
                out.append({
                    "citation": r.get("citation", r.get("source_ref", "")),
                    "text": r.get("text", ""),
                    "source": f"Law library · {law_jurisdiction_label(jur)}", "origin": "law",
                })
        except Exception:
            pass
    return out
