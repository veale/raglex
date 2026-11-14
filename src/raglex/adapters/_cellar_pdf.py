"""CELLAR's official PDF negotiation, including its HTTP 300 item manifests."""
import re
from urllib.parse import quote, urlsplit
from bs4 import BeautifulSoup
from ..core.errors import FetchError


def fetch_pdf(client, url, language='en', *, judgment=False):
    headers={'Accept':'application/pdf','Accept-Language':{'en':'eng','fr':'fra'}.get(language,language)}
    try:
        response=client.get(quote(url,safe=':/%'),headers=headers)
    except FetchError as exc:
        if exc.status_code in {404,410,406}:return None
        raise
    if response.content.startswith(b'%PDF-'):return response.content
    if getattr(response,'status_code',200)!=300:return None
    soup=BeautifulSoup(response.content,'html.parser')
    candidates=[]
    for item in soup.select('li[title="item"]'):
        name=item.select_one('li[title="stream_name"]')
        link=item.find('a',href=True)
        if not name or not link:continue
        label=name.get_text()
        if judgment and not re.search(r'(?:ARRET|JUDGMENT|ORDONNANCE|ORDER)',label,re.I):continue
        if re.search(r'(?:RAPP|REPORT|OPINION|CONCL)',label,re.I):continue
        target=link['href']
        if urlsplit(target).hostname!='publications.europa.eu':continue
        candidates.append(target.replace('http://','https://',1))
    # Never guess which of several parts is the complete instrument.
    if len(candidates)!=1:return None
    response=client.get(candidates[0],headers=headers)
    return response.content if response.content.startswith(b'%PDF-') else None
