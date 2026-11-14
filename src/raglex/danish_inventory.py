"""Reconcile held KAPI identities against the separate public-portal inventory."""
import json

from .core.errors import FetchError
from .core.http import RateLimitedClient


def audit_inventory(facade, *, on_progress=None, cancel_check=None):
    public_cases, public_documents = set(), set()
    total = None
    page = 1
    occurrences = 0
    page_signatures = set()
    with RateLimitedClient("dk-domsdatabasen-audit", min_interval=0.5, timeout=90) as client:
        while True:
            if cancel_check and cancel_check():
                return {"cancelled": True, "public_cases_examined": len(public_cases)}
            if on_progress:
                on_progress(stage="reading independent Danish portal inventory",
                            done=len(public_cases), total=total, item=f"page {page}")
            response = client.request("POST", "https://domsdatabasen.dk/webapi/api/Case/advanced",
                                      params={"page": page, "pageSize": 100,
                                              # Verdict dates have large ties at page boundaries.
                                              # Publication order also keeps new additions at the end.
                                              "sorting": "PublishDateAsc"},
                                      json={"searchExpression": ""})
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
                raise FetchError("Danish portal returned no case inventory", transient=True)
            count = data.get("totalCount")
            if not isinstance(count, int) or (total is not None and count != total):
                raise FetchError("Danish portal inventory changed during audit; retry", transient=True)
            total = count
            cases = data["cases"]
            if not cases:
                break
            signature = tuple((str(c["id"]), tuple(str(d["id"]) for d in c["documents"])) for c in cases)
            if signature in page_signatures:
                raise FetchError("Danish portal repeated an entire inventory page", transient=True)
            page_signatures.add(signature)
            occurrences += len(cases)
            for case in cases:
                cid = str(case["id"])
                public_cases.add(cid)
                for doc in case["documents"]:
                    if not case.get("revokedDateTime") and not doc.get("revokedDateTime"):
                        public_documents.add(f"dk/domsdatabasen/{cid}/{doc['id']}")
            advertised_pages = data.get("pageCount")
            if len(public_cases) == total or (isinstance(advertised_pages, int) and page >= advertised_pages):
                break
            page += 1
    # The portal's advertised total may count records it omits from its pages.
    # Report the discrepancy alongside the document diff; never treat it as proof
    # of completeness or request unadvertised pages to make the count fit.
    inventory_complete = occurrences == total
    if on_progress:
        on_progress(stage="comparing Danish document identities", done=0, total=None,
                    item=f"{len(public_documents)} public documents")
    with facade._open() as (cat, _, ts):
        rows = cat.conn.execute("SELECT stable_id, meta_json, has_text, upstream_status "
                                "FROM documents WHERE source = ?", ("dk-domsdatabasen",)).fetchall()
        held = {r["stable_id"] for r in rows if r["has_text"] and r["upstream_status"] != "withdrawn"}
        held_cases = {str(json.loads(r["meta_json"] or "{}").get("case_id")) for r in rows}
    missing, extra = sorted(public_documents - held), sorted(held - public_documents)
    return {"public_cases": len(public_cases), "public_documents": len(public_documents),
            "publisher_result_count": total, "enumerated_case_occurrences": occurrences,
            "unaccounted_publisher_results": total - occurrences,
            "inventory_complete": inventory_complete,
            "held_cases": len(held_cases), "held_readable_documents": len(held),
            "missing_documents": missing, "held_only_documents": extra,
            "complete": inventory_complete and not missing and not extra,
            "note": "Classify all differences before claiming coverage or importing anything."}
