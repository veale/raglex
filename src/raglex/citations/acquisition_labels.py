"""Human acquisition hints from citing text; never identity evidence or alias writes."""
from .report_match import extract_preceding_name
from ..core.text import fold


def reference_names(catalogue, textstore, reference, *, text_cache=None):
    cache = text_cache if text_cache is not None else {}
    hints=[]
    for occurrence in catalogue.reference_occurrences(reference,fold(reference),limit=3):
        start=occurrence.get('context_start')
        if start is None or start < 0:continue
        sid=occurrence['src_id']
        if sid not in cache:
            doc=catalogue.get_document(sid)
            try:cache[sid]=textstore.get(doc['payload_hash']) if doc and doc['payload_hash'] else ''
            except OSError:cache[sid]=''
        text=cache[sid]
        name=extract_preceding_name(text[max(0,start-160):start].rstrip(' ,;('))
        if name and not any(x['name']==name for x in hints):
            hints.append({'name':name,'citing_document':sid,'inferred':True})
    return hints
