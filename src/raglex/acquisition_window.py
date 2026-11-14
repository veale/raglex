"""Publisher access windows checked at queue admission AND before each request."""
from datetime import datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

DENMARK = ZoneInfo('Europe/Copenhagen')


class AcquisitionWindowClosed(Exception):
    """Defer work without treating a publisher's permitted quiet hours as a failure."""


def danish_window_open(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Acquisition window needs an aware timestamp')
    return 1 <= now.astimezone(DENMARK).hour < 6


def check_request_window(url):
    # Match the publisher host, not merely the adapter name: public PDF fallbacks
    # and the independent portal audit have the same access restriction as KAPI.
    if (urlsplit(str(url)).hostname or '').lower() in {'domsdatabasen.dk', 'www.domsdatabasen.dk'}:
        if not danish_window_open():
            raise AcquisitionWindowClosed('Domsdatabasen waits for 01:00–06:00 Europe/Copenhagen')


def job_window_open(kind, params, catalogue=None):
    danish = (kind == 'audit-domsdatabasen' or
              (kind == 'harvest-source' and params.get('source') == 'dk-domsdatabasen')
              or params.get('_acquisition_window') == 'domsdatabasen')
    if kind == 'run-watch' and catalogue is not None:
        import json
        row = catalogue.conn.execute('SELECT spec_json FROM watches WHERE watch_id=?',
                                     (params.get('watch_id'),)).fetchone()
        if row:
            danish = json.loads(row['spec_json'] or '{}').get('source') == 'dk-domsdatabasen'
    return not danish or danish_window_open()
