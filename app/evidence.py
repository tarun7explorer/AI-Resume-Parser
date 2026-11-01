"""Conservative evidence quotation verification against extracted source text."""
from __future__ import annotations
import re, unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, Sequence
from app.resume_parser import PageText, ParsedDocument

class MatchType(str, Enum): EXACT="exact"; NORMALIZED="normalized"
class PageStatus(str, Enum): VERIFIED="verified"; AMBIGUOUS="ambiguous"; CROSS_PAGE="cross_page"; UNKNOWN="unknown"
class RequirementStatus(str, Enum): FOUND="found"; NOT_FOUND_IN_EXTRACTED_TEXT="not_found_in_extracted_text"; NOT_FOUND_EXTRACTION_INCOMPLETE="not_found_extraction_incomplete"
class RejectionReason(str, Enum): EMPTY_QUOTE="empty_quote"; EMPTY_CLAIM="empty_claim"; QUOTE_TOO_SHORT="quote_too_short"; QUOTE_TOO_LONG="quote_too_long"; QUOTE_NOT_FOUND="quote_not_found"; QUOTE_ELIDED="quote_elided"; NO_SOURCE_TEXT="no_source_text"; DUPLICATE_EVIDENCE="duplicate_evidence"; TERM_NOT_IN_QUOTE="term_not_in_quote"

@dataclass(frozen=True)
class ProposedEvidence:
    claim: str=""; quote: str=""; page_hint: int|None=None; claimed_terms: tuple[str,...]=()
    @classmethod
    def from_mapping(cls, x: Any):
        if not isinstance(x,dict): return cls()
        claim=x.get("claim"); quote=x.get("quote"); page=x.get("page",x.get("page_hint")); terms=x.get("claimed_terms",())
        return cls(claim if isinstance(claim,str) else "", quote if isinstance(quote,str) else "", page if isinstance(page,int) and not isinstance(page,bool) else None, tuple(t for t in terms if isinstance(t,str)) if isinstance(terms,(list,tuple)) else ())

@dataclass(frozen=True)
class VerifiedEvidence:
    claim: str; quote: str; start_offset: int; end_offset: int; page: int|None; page_start: int|None; page_end: int|None; page_status: PageStatus; match_type: MatchType; occurrences: int=1; warnings: tuple[str,...]=()
    @property
    def verified(self): return True
    @property
    def source_quote(self): return self.quote
    @property
    def page_reliable(self): return self.page_status is PageStatus.VERIFIED

@dataclass(frozen=True)
class RejectedEvidence:
    claim: str; quote: str; reason: RejectionReason
    @property
    def verified(self): return False

@dataclass(frozen=True)
class RequirementFinding:
    status: RequirementStatus; page: int|None=None; message: str=""
@dataclass(frozen=True)
class EvidenceReport:
    verified: tuple[VerifiedEvidence,...]; rejected: tuple[RejectedEvidence,...]; evidence_coverage: float; extraction_complete: bool; limitations: tuple[str,...]

def _norm(s: str) -> tuple[str,list[int]]:
    """Normalize for location only and retain a map to each original character."""
    out=[]; mapping=[]; i=0
    while i<len(s):
        ch=s[i]
        if ch in "-‐‑‒–—" and i+1<len(s) and s[i+1] in "\r\n":
            i+=1
            if i<len(s) and s[i]=='\r': i+=1
            if i<len(s) and s[i]=='\n': i+=1
            while i<len(s) and s[i].isspace(): i+=1
            continue
        if ch in "•●▪◦‣∙":
            expanded = " "
        else:
            expanded=unicodedata.normalize("NFKC",ch).casefold()
        expanded=expanded.replace("’", "'").replace("‘", "'").replace('“','"').replace('”','"').replace("–","-").replace("—","-")
        for c in expanded:
            if c.isspace():
                if not out or out[-1]!=" ": out.append(" "); mapping.append(i)
            else: out.append(c); mapping.append(i)
        i+=1
    while out and out[-1]==' ': out.pop(); mapping.pop()
    return ''.join(out),mapping

class EvidenceVerifier:
    def __init__(self, document: ParsedDocument, *, min_quote_chars: int=12, max_quote_chars: int=500): self.doc=document; self.min=min_quote_chars; self.max=max_quote_chars
    def verify_one(self, proposed: ProposedEvidence) -> VerifiedEvidence|RejectedEvidence:
        p=proposed if isinstance(proposed,ProposedEvidence) else ProposedEvidence.from_mapping(proposed)
        claim=p.claim.strip(); raw=p.quote.strip()
        def reject(reason): return RejectedEvidence(claim,raw,reason)
        if not claim: return reject(RejectionReason.EMPTY_CLAIM)
        if not raw: return reject(RejectionReason.EMPTY_QUOTE)
        if len(raw)>self.max: return reject(RejectionReason.QUOTE_TOO_LONG)
        if not self.doc.full_text.strip(): return reject(RejectionReason.NO_SOURCE_TEXT)
        if "..." in raw or "…" in raw:
            if re.search(r"\S\s*(?:\.\.\.|…)\s*\S",raw): return reject(RejectionReason.QUOTE_ELIDED)
            raw=raw.strip(" .…")
        nq,_=_norm(raw)
        if len(nq)<self.min: return reject(RejectionReason.QUOTE_TOO_SHORT)
        for term in p.claimed_terms:
            nt,_=_norm(term)
            if nt and not re.search(r"(?<!\w)"+re.escape(nt)+r"(?!\w)",nq): return reject(RejectionReason.TERM_NOT_IN_QUOTE)
        ns,mapping=_norm(self.doc.full_text)
        def find_matches(query):
            found=[]; pos=0
            while True:
                pos=ns.find(query,pos)
                if pos<0: break
                end=pos+len(query)
                word=lambda c: c.isalnum() or c in "_+#"
                if (pos==0 or not word(ns[pos-1]) or not word(query[0])) and (end==len(ns) or not word(ns[end]) or not word(query[-1])):
                    found.append((mapping[pos],mapping[end-1]+1))
                pos+=1
            return found
        matches=find_matches(nq)
        if not matches:
            # Resume PDFs often wrap a word as "high-per-\\nformance" while a
            # proposal quotes the typographic form "high-performance".
            alternate=re.sub(r"(?<=[a-z])-(?=[a-z])","",nq)
            if alternate != nq: matches=find_matches(alternate)
        if not matches: return reject(RejectionReason.QUOTE_NOT_FOUND)
        spans=[]
        for a,b in matches:
            start,end=self.doc.pages_for_span(a,b); spans.append((a,b,start,end))
        if p.page_hint is not None:
            hinted=[x for x in spans if x[2]==x[3]==p.page_hint]
            if hinted: spans=hinted
        a,b,first,last=spans[0]; quote=self.doc.full_text[a:b].replace("\f", "\n")
        if len(spans)>1: page=None; state=PageStatus.AMBIGUOUS
        elif first!=last: page=None; state=PageStatus.CROSS_PAGE
        else: page=first; state=PageStatus.VERIFIED
        warnings=[]
        if p.page_hint is not None and not any(x[2]==x[3]==p.page_hint for x in spans):
            warnings.append("page_hint_out_of_range_ignored" if p.page_hint > self.doc.page_count else "page_hint_did_not_match_ignored")
        return VerifiedEvidence(claim,quote,a,b,page,first,last,state,MatchType.EXACT if quote==raw else MatchType.NORMALIZED,len(spans),tuple(warnings))
    def verify_all(self, items: Sequence[ProposedEvidence|dict]) -> EvidenceReport:
        ok=[]; bad=[]; seen=set()
        for item in items:
            p=item if isinstance(item,ProposedEvidence) else ProposedEvidence.from_mapping(item); out=self.verify_one(p)
            if isinstance(out,VerifiedEvidence):
                key=_norm(out.quote)[0]
                if key in seen: bad.append(RejectedEvidence(out.claim,out.quote,RejectionReason.DUPLICATE_EVIDENCE))
                else: seen.add(key); ok.append(out)
            else: bad.append(out)
        total=len(ok)+len(bad); complete=self.doc.extraction_complete
        return EvidenceReport(tuple(ok),tuple(bad),len(ok)/total if total else 0.0,complete,() if complete else ("One or more image-only pages could not be searched.",))
    def find_requirement(self, term: str) -> RequirementFinding:
        evidence=EvidenceVerifier(self.doc,min_quote_chars=1).verify_one(ProposedEvidence("requirement",term))
        if isinstance(evidence,VerifiedEvidence): return RequirementFinding(RequirementStatus.FOUND,evidence.page,"Found in extracted text.")
        status=RequirementStatus.NOT_FOUND_IN_EXTRACTED_TEXT if self.doc.extraction_complete else RequirementStatus.NOT_FOUND_EXTRACTION_INCOMPLETE
        return RequirementFinding(status,None,"Not found in extracted text; this does not show the candidate lacks the skill.")
    def verify_quote(self, quote: str, pages: Sequence[PageText], *, page_hint: int|None=None):
        d=ParsedDocument.from_page_texts([p.text for p in pages])
        return EvidenceVerifier(d,min_quote_chars=1).verify_one(ProposedEvidence("evidence",quote,page_hint))

def verify_evidence(document: ParsedDocument, items: Sequence[ProposedEvidence|dict]) -> EvidenceReport:
    return EvidenceVerifier(document).verify_all(items)
