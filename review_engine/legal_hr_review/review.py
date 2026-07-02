from __future__ import annotations
from review_engine.extraction.models import SourceChunk

RULES = [
("Termination risk", ("terminated","termination","fired"), "Termination-related language requires review of documentation, consistency, and process."),
("Protected leave red flag", ("fmla","medical leave","parental leave","protected leave"), "Protected-leave language appears near employment events and requires jurisdiction-specific review."),
("Accommodation red flag", ("accommodation","disability","interactive process"), "Accommodation-related language requires review of the interactive process and supporting records."),
("Discrimination red flag", ("discrimination","race","religion","gender","age discrimination"), "Potential discrimination-related language appears and requires human review."),
("Retaliation red flag", ("retaliation","retaliated","after reporting"), "Potential retaliation-related language appears and requires timeline review."),
("Harassment red flag", ("harassment","harassed","hostile work environment"), "Harassment-related language appears and requires review of reports and response steps."),
("Final pay checklist", ("final pay","final paycheck","last paycheck"), "Final-pay language appears; timing and required payments depend on jurisdiction."),]

def _matching(chunks, terms): return [c for c in chunks if any(t in c.text.lower() for t in terms)]
def _candidate(title, explanation, sources, confidence="Medium"):
    return {"title":title,"category":"HR Legal Risk","explanation":explanation,"sources":sources[:5],"confidence":confidence,"confidence_reason":"Rule terms are directly present; legal significance is not determined.","human_review_required":True}

def run_hr_legal_review(chunks: list[SourceChunk], jurisdiction=""):
    out=[]
    for title,terms,explanation in RULES:
        matches=_matching(chunks,terms)
        if matches: out.append(_candidate(title, explanation + ("" if jurisdiction else " Jurisdiction required."), matches))
    text=" ".join(c.text.lower() for c in chunks)
    term=_matching(chunks,("terminated","termination","fired")); investigation=_matching(chunks,("investigation","complaint"))
    if investigation and not any(t in text for t in ("investigation findings","investigation report","interview notes")):
        out.append(_candidate("Investigation gap","An investigation or complaint is referenced, but no findings, report, or interview notes were identified.",investigation))
    trigger=term or investigation
    if trigger and "witness statement" not in text and "witness interview" not in text:
        out.append({"title":"Missing witness statements","category":"Missing Document","explanation":"A termination or investigation is referenced, but no witness statement or witness interview was identified.","sources":trigger[:3],"confidence":"Medium","confidence_reason":"The triggering event is sourced; absence is based only on processed documents.","human_review_required":True})
    if term:
        if not any(t in text for t in ("policy","handbook","procedure")):
            out.append({"title":"Missing policy reference","category":"Missing Document","explanation":"Termination evidence is present, but no policy, handbook, or procedure reference was identified.","sources":term[:3],"confidence":"Medium","confidence_reason":"The triggering event is sourced; document-set completeness requires confirmation.","human_review_required":True})
        out.append(_candidate("Attorney review required","A termination-related event was identified. This is a review flag, not a legal conclusion.",term,"High"))
    return out
