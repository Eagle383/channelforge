"""FastChannels API helpers for guide-lineup duplicate detection."""
import re
from urllib.parse import urlsplit

import httpx

from . import db

_FC_SUFFIX = "/api/sources/force-refresh"
_GUIDE_PATHS = ("/feeds/default/epg.xml", "/epg.xml")
_STATION_SLUG_RE = re.compile(r"^\|(\d+)$")
_SAMSUNG_RE = re.compile(r"/stvp-([A-Za-z0-9_-]+)")
_AMAGI_RE = re.compile(r"/playlist/([^/?]+)")


def _base_from_url(value):
    value = (value or "").strip().rstrip("/")
    if not value:
        return ""
    if not value.startswith(("http://", "https://")):
        value = "http://" + value
    if value.endswith(_FC_SUFFIX):
        value = value[:-len(_FC_SUFFIX)]
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def base_urls():
    bases = []
    setting = _base_from_url(db.get_setting("prerefresh_url"))
    if setting:
        bases.append(setting)
    for row in db.q("SELECT url, epg_url FROM sources WHERE active = 1"):
        for field in ("url", "epg_url"):
            base = _base_from_url(row[field])
            if base and base not in bases:
                bases.append(base)
    return bases


def _configured_guide_on_base(base, skip_urls):
    for url in skip_urls:
        if _base_from_url(url) != base:
            continue
        path = urlsplit(url if url.startswith(("http://", "https://")) else "http://" + url).path.rstrip("/")
        if path.endswith("/epg.xml"):
            return True
    return False


def _fetch_guide_blob(base, skip_urls):
    skip = {u.strip().rstrip("/") for u in skip_urls if str(u or "").strip()}
    last_error = None
    with httpx.Client(timeout=300, follow_redirects=True) as client:
        for path in _GUIDE_PATHS:
            url = f"{base}{path}"
            if url.rstrip("/") in skip:
                continue
            try:
                r = client.get(url)
                r.raise_for_status()
                return url, r.content
            except Exception as e:
                last_error = e
    if last_error:
        raise last_error
    raise RuntimeError("no unconfigured FastChannels guide URL to fetch")


def guide_blobs(skip_urls=None, log=lambda s: None):
    """Fetch FastChannels' built-in XMLTV guide for signature indexing.

    This avoids depending on Channels DVR as the guide source. DVR only sees
    channels after ChannelForge has already chosen/merged them, while
    FastChannels has the provider schedules before dedupe decisions are made.
    """
    skip_urls = {str(u or "").strip() for u in (skip_urls or set()) if str(u or "").strip()}
    out = []
    for base in base_urls():
        host = urlsplit(base).netloc or base
        if _configured_guide_on_base(base, skip_urls):
            continue
        try:
            url, blob = _fetch_guide_blob(base, skip_urls)
            out.append(blob)
            log(f"  guide: fetched FastChannels guide from {host} ({urlsplit(url).path})")
        except Exception as e:
            log(f"  guide: FastChannels guide from {host} FAILED ({e})")
    return out


def _get_channels(base):
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        r = client.get(f"{base}/api/channels", params={"per_page": 10000})
        r.raise_for_status()
        data = r.json()
    return data.get("channels", data if isinstance(data, list) else [])


def _provider_id(row):
    provider = (row.get("source_name") or "").strip().lower()
    if not provider:
        return ""
    guide_key = str(row.get("guide_key") or "").strip()
    if guide_key:
        return f"{provider}.{guide_key}"
    stream = str(row.get("stream_url") or "").strip()
    if stream.startswith(("roku://", "tubi://", "localnow://")):
        return f"{provider}.{stream.split('://', 1)[1].split('?', 1)[0].strip('/')}"
    if stream.startswith("xumo://channel/"):
        return f"{provider}.{stream.rsplit('/', 1)[-1].split('?', 1)[0]}"
    if stream.startswith("pluto://"):
        return f"{provider}.{stream.rsplit('/', 1)[-1].split('?', 1)[0]}"
    if provider == "samsung":
        m = _SAMSUNG_RE.search(stream)
        if m:
            return f"{provider}.{m.group(1)}"
    m = _AMAGI_RE.search(stream)
    if m:
        return f"{provider}.{m.group(1)}"
    return ""


def _station_ids(row):
    ids = set()
    gracenote_id = str(row.get("gracenote_id") or "").strip()
    if gracenote_id:
        ids.add(gracenote_id)
    m = _STATION_SLUG_RE.match(str(row.get("slug") or "").strip())
    if m:
        ids.add(m.group(1))
    return ids


def _guide_ids(row):
    ids = set(_station_ids(row))
    for value in (row.get("name"), row.get("number"), row.get("guide_key"), _provider_id(row)):
        value = str(value or "").strip()
        if value:
            ids.add(value)
    return ids


def bridge_signatures(signatures, wanted_ids, log=lambda s: None):
    """Copy provider XMLTV signatures onto FastChannels Gracenote station ids.

    FastChannels can output one row with a Gracenote station id and sibling
    provider rows with XMLTV schedules. Its API marks some provider rows with a
    slug like ``|163942``; use that to attach the provider lineup to station
    ``163942`` before duplicate merging runs.
    """
    wanted = {str(i).strip() for i in wanted_ids if str(i or "").strip()}
    if not wanted:
        return {}
    out = {}
    for base in base_urls():
        try:
            rows = _get_channels(base)
            log(f"  guide: fetched FastChannels metadata from {urlsplit(base).netloc}")
        except Exception as e:
            log(f"  guide: FastChannels metadata from {urlsplit(base).netloc or base} FAILED ({e})")
            continue
        by_station = {}
        for row in rows:
            direct = None
            for guide_id in _guide_ids(row):
                direct = signatures.get(guide_id)
                if direct:
                    break
            if not direct:
                continue
            for station_id in _station_ids(row):
                if station_id:
                    by_station.setdefault(station_id, direct)
        for row in rows:
            inherited = None
            for station_id in _station_ids(row):
                inherited = by_station.get(station_id)
                if inherited:
                    break
            if not inherited:
                continue
            for guide_id in _guide_ids(row):
                if guide_id in wanted and inherited["n"] > signatures.get(guide_id, {}).get("n", 0):
                    out[guide_id] = inherited
    if out:
        log(f"  guide: linked FastChannels programme lineups to {len(out)} station ids")
    return out
