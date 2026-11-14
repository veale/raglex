"""Nightly bootstrap until independent coverage verification enables the daily watch."""
import json
import os
from datetime import datetime, timezone
from .acquisition_window import DENMARK, danish_window_open

LABEL = 'nightly Danish full acquisition and inventory verification'


def schedule_acquisition(facade, jobs, *, now=None):
    now = now or datetime.now(timezone.utc)
    if not danish_window_open(now):
        return {'waiting_for_window': True}
    if not os.environ.get('DOMSDATABASEN_EMAIL') or not os.environ.get('DOMSDATABASEN_PASSWORD'):
        return {'credentials_missing': True}
    # Once verified, the ordinary enabled watch consumes the inclusive change feed.
    if any(w.get('enabled') and w.get('spec', {}).get('source') == 'dk-domsdatabasen'
           for w in facade.list_watches()):
        return {'daily_watch_enabled': True}
    with facade._open() as (cat, _, __):
        for row in [*cat.running_jobs(), *cat.queued_jobs()]:
            if row['kind'] == 'harvest-source' and json.loads(row['params_json'] or '{}').get('source') == 'dk-domsdatabasen':
                return {'job_id': row['job_id'], 'already_running': True}
        row = cat.conn.execute("SELECT started_at FROM jobs WHERE kind='harvest-source' AND label=? "
                               "ORDER BY started_at DESC LIMIT 1", (LABEL,)).fetchone()
        if row and datetime.fromisoformat(row['started_at']).astimezone(DENMARK).date() == now.astimezone(DENMARK).date():
            return {'already_started_today': True}
    return jobs.start('harvest-source', LABEL,
                      {'source': 'dk-domsdatabasen', 'backfill': True, 'max_pages': None,
                       'verify_inventory': True, 'use_llm': False,
                       'options': {'max_ocr_pages': 500}}, queue=True)
