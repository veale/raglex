"""Danish courts' Domsdatabasen KAPI v2, including its inclusive change feed.

The publisher returns case envelopes with *all* their documents inline. Page
positions count cases, not documents; every sibling gets the case's checkpoint so
a restart cannot skip the remaining siblings. No token or password is persisted
in raw payloads. The initial walk must finish before enabling a change-feed watch.
"""
from __future__ import annotations

import base64
import json
import os
import time
from datetime import date
from typing import Iterator

from ..core.adapter import BaseAdapter, option_int, resume_floor
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub, sha256_bytes
from ..extraction import Extracted, extract_bytes
from ..imports.service import structure_segments

BASE = "https://domsdatabasen.dk/webapi/kapi/v2"
PAGE_SIZE = 25


class DomsdatabasenAdapter(BaseAdapter):
    source = "dk-domsdatabasen"
    min_interval = 0.5

    def __init__(self, *, start_offset: int | str | None = None,
                 client=None, email: str | None = None, password: str | None = None):
        self.start_offset = resume_floor(option_int(start_offset, 0), PAGE_SIZE)
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval,
                                                   timeout=90)
        self._email = email or os.environ.get("DOMSDATABASEN_EMAIL", "")
        self._password = password or os.environ.get("DOMSDATABASEN_PASSWORD", "")
        self._token = None
        self._expires = 0.0

    def _authorize(self):
        if not self._email or not self._password:
            raise FetchError("Domsdatabasen requires DOMSDATABASEN_EMAIL and DOMSDATABASEN_PASSWORD")
        data = self._client.request("POST", f"{BASE}/autoriser",
                                    json={"Email": self._email, "Password": self._password}).json()
        if not isinstance(data, dict) or not data.get("tokenString"):
            raise FetchError("Domsdatabasen authorization returned no token", transient=True)
        self._token = data["tokenString"]
        self._expires = time.monotonic() + 4 * 3600  # issued for five hours

    def _page(self, route, params):
        if not self._token or time.monotonic() >= self._expires:
            self._authorize()
        response = self._client.get(f"{BASE}/{route}", params=params,
                                    headers={"Authorization": f"Bearer {self._token}"},
                                    raise_for_4xx=False)
        if response.status_code == 401:
            self._authorize()
            response = self._client.get(f"{BASE}/{route}", params=params,
                                        headers={"Authorization": f"Bearer {self._token}"})
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError as exc:
            raise FetchError("Domsdatabasen returned invalid JSON", transient=True) from exc
        if not isinstance(data, list):
            raise FetchError("Domsdatabasen listing is not a case array", transient=True)
        return data

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        # Changes include revisions to old judgments; decision dates cannot be the
        # watermark. Pin it to the start of this walk, and re-cover that date next time.
        watermark = date.today().isoformat()
        route = "sagsaendringer" if since else "sager"
        first = self.start_offset // PAGE_SIZE + 1
        seen_pages = set()
        page = first
        while max_pages is None or page - first < max_pages:
            params = {"sideNr": page, "perSide": PAGE_SIZE}
            if since:
                params["fraDato"] = date.fromisoformat(since[:10]).isoformat()
            cases = self._page(route, params)
            if not cases:
                return
            signature = tuple(str(c.get("id")) for c in cases if isinstance(c, dict))
            if not signature or signature in seen_pages:
                raise FetchError("Domsdatabasen repeated or malformed case page", transient=True)
            seen_pages.add(signature)
            for position, case in enumerate(cases):
                if not isinstance(case, dict) or not case.get("id") or not isinstance(case.get("documents"), list):
                    raise FetchError("Domsdatabasen case missing id/documents", transient=True)
                if case.get("revokedDateTime") and not case["documents"]:
                    # Withdrawn cases can lose their document array entirely. Emit an
                    # explicit case tombstone so held siblings are withdrawn without
                    # inventing a document or stopping the rest of the register.
                    prefix = f"dk/domsdatabasen/{case['id']}/"
                    yield Stub(prefix.rstrip("/"), hints={
                        "upstream_withdrawn": True, "withdrawn_prefix": prefix,
                        "watermark": watermark,
                        "resume_offset": (page - 1) * PAGE_SIZE + position})
                    continue
                envelope = {k: v for k, v in case.items() if k != "documents"}
                for document in case["documents"]:
                    if not isinstance(document, dict) or not document.get("id"):
                        raise FetchError("Domsdatabasen document missing id", transient=True)
                    ident = f"dk/domsdatabasen/{case['id']}/{document['id']}"
                    payload = {"case": envelope, "document": document}
                    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
                    withdrawn = bool(case.get("revokedDateTime") or document.get("revokedDateTime")
                                     or document.get("revokedDataTime"))
                    yield Stub(ident, landing_url=f"https://domsdatabasen.dk/#sag/{case['id']}/{document['id']}",
                               title=case.get("headline"), court=case.get("officeName"),
                               hints={"payload": payload, "contenthash": sha256_bytes(raw),
                                      "revision": withdrawn,
                                      "upstream_withdrawn": withdrawn,
                                      "watermark": watermark,
                                      "resume_offset": (page - 1) * PAGE_SIZE + position})
            page += 1  # a short non-empty page is not proof of exhaustion

    def fetch(self, stub: Stub) -> Record:
        payload = stub.hints["payload"]
        case, doc = payload["case"], payload["document"]
        if stub.hints.get("upstream_withdrawn"):
            raise FetchError("Domsdatabasen document withdrawn", status_code=410)
        if doc.get("contentHtml"):
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(doc["contentHtml"], "html.parser")
            for junk in soup.select("head,script,style"):
                junk.decompose()
            # Word-generated spans are inline, not paragraph boundaries. Decode
            # entities and retain actual paragraphs instead of one line per span.
            blocks = [p.get_text(" ", strip=True) for p in soup.select("p,h1,h2,h3,h4,h5,h6,li")
                      if p.find_parent(["p", "li"]) is None]
            text = "\n\n".join(x for x in blocks if x) or soup.get_text(" ", strip=True)
            extracted = Extracted(text=text, engine="domsdatabasen-html", engine_version="1")
        elif doc.get("contentPdf"):
            try:
                pdf = base64.b64decode(doc["contentPdf"], validate=True)
            except ValueError as exc:
                raise FetchError("Domsdatabasen invalid base64 PDF", transient=True) from exc
            if not pdf.startswith(b"%PDF-"):
                raise FetchError("Domsdatabasen PDF lacks PDF signature", transient=True)
            extracted = extract_bytes(pdf, ext="pdf")
        else:
            raise FetchError("Domsdatabasen live document has no body", transient=True)
        if not extracted.text.strip():
            raise FetchError("Domsdatabasen document has no readable text", transient=True)
        kind = doc.get("documentType") or doc.get("documenttype") or {}
        summary = kind.get("enumValue") == 4 or "resum" in str(doc.get("displayTitle", "")).lower()
        dt = DocType.COMMENTARY if summary else DocType.JUDGMENT
        ecli = doc.get("ecli")
        aliases = [ecli] if ecli else []
        # Docket and case IDs belong to the judgment, not its separate summary.
        if not summary and case.get("courtCaseNumber"):
            aliases.append(f"dk/docket/{case['courtCaseNumber']}")
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        when = doc.get("verdictDateTime")
        return Record(source=self.source, stable_id=stub.stable_id, ecli=ecli,
                      doc_type=dt, title=" — ".join(filter(None, [case.get("headline"), doc.get("displayTitle")])),
                      court=case.get("officeName"), language="da", source_language="da",
                      decision_date=date.fromisoformat(when[:10]) if when else None,
                      landing_url=stub.landing_url, raw_bytes=raw, raw_ext="json",
                      text=extracted.text, segments=structure_segments("auto", dt, extracted),
                      extracted_via=ExtractedVia.STRUCTURED,
                      extra={"format": "domsdatabasen-kapi-v2", "case_id": case["id"],
                             "document_id": doc["id"], "case_number": case.get("courtCaseNumber"),
                             "document_type": kind, "aliases": aliases,
                             "contenthash": stub.hints["contenthash"],
                             "case_subjects": case.get("caseSubjects"),
                             "appeal_groups": case.get("verticalCotreatmentGroups"),
                             "references": doc.get("references")})
