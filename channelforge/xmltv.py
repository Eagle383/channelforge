"""Combine XMLTV guides from multiple sources into one file, filtered to the
channel ids in use. Streams to disk — memory stays bounded no matter how many
days of programmes the source guides carry."""
import gzip
import hashlib
import io
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

_WORD_RE = re.compile(r"[a-z0-9]+")
_SIG_LIMIT = 96


def _gunzip(blob):
    if blob[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(blob)
        except OSError:
            return b""
    return blob


def _norm_text(text):
    return " ".join(_WORD_RE.findall((text or "").casefold()))


def _start_datetime(start):
    if isinstance(start, (int, float)):
        ts = start / 1000 if start > 100000000000 else start
        return datetime.fromtimestamp(ts, timezone.utc)
    text = str(start or "").strip()
    if not text:
        return None
    if text.isdigit() and len(text) <= 13:
        ts = int(text)
        ts = ts / 1000 if ts > 100000000000 else ts
        return datetime.fromtimestamp(ts, timezone.utc)
    m = re.match(r"^(\d{14}|\d{12})(?:\s*([+-]\d{4}))?", text)
    if not m:
        return None
    raw, offset = m.groups()
    try:
        dt = datetime.strptime(raw[:12], "%Y%m%d%H%M")
    except ValueError:
        return None
    if offset:
        sign = 1 if offset[0] == "+" else -1
        delta = timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5]))
        dt = dt.replace(tzinfo=timezone(sign * delta)).astimezone(timezone.utc)
    return dt


def _rounded_start_key(start):
    dt = _start_datetime(start)
    if dt is None:
        return ""
    dt += timedelta(minutes=(2 if dt.minute % 5 >= 3 else 0))
    dt -= timedelta(minutes=dt.minute % 5)
    return dt.strftime("%Y%m%d%H%M")


def programme_signature_key(title, start):
    title = _norm_text(title)
    if not title:
        return ""
    start = _rounded_start_key(start)
    if not start:
        return ""
    return hashlib.sha1(f"{start}|{title}".encode("utf-8")).hexdigest()[:16]


def _programme_key(elem):
    key = programme_signature_key(elem.findtext("title"), elem.get("start"))
    if not key:
        return "", ""
    return key, elem.findtext("title") or ""


def _add_signature_item(signatures, samples, channel_id, elem):
    if channel_id not in signatures or len(signatures[channel_id]) >= _SIG_LIMIT:
        return
    key, title = _programme_key(elem)
    if not key:
        return
    signatures[channel_id].append(key)
    if title and len(samples[channel_id]) < 4 and title not in samples[channel_id]:
        samples[channel_id].append(title)


def _channel_signature_aliases(blob, signature_ids):
    """Map XMLTV channel ids to requested signature ids carried by display-name."""
    aliases = {}
    src_root = None
    try:
        for event, elem in ET.iterparse(io.BytesIO(blob), events=("start", "end")):
            if event == "start":
                if src_root is None:
                    src_root = elem
                continue
            if elem.tag == "channel":
                channel_id = (elem.get("id") or "").strip()
                if channel_id:
                    if channel_id in signature_ids:
                        aliases.setdefault(channel_id, set()).add(channel_id)
                    for display in elem.findall("display-name"):
                        name = (display.text or "").strip()
                        if name in signature_ids:
                            aliases.setdefault(channel_id, set()).add(name)
                src_root.clear()
            elif elem.tag in ("channel", "programme"):
                src_root.clear()
    except ET.ParseError:
        pass
    return aliases


def _stream_matching(blob, tag, wanted_ids, id_attr, out, seen=None, signatures=None, samples=None, signature_aliases=None):
    """Write every <tag> element whose id attribute is wanted straight to `out`."""
    src_root = None
    try:
        for event, elem in ET.iterparse(io.BytesIO(blob), events=("start", "end")):
            if event == "start":
                if src_root is None:
                    src_root = elem
                continue
            if elem.tag == tag:
                key = elem.get(id_attr, "")
                if key in wanted_ids and (seen is None or key not in seen):
                    if seen is not None:
                        seen.add(key)
                    out.write(ET.tostring(elem, encoding="utf-8"))
                if tag == "programme" and signatures is not None and samples is not None:
                    for signature_id in (signature_aliases or {}).get(key, (key,)):
                        _add_signature_item(signatures, samples, signature_id, elem)
                src_root.clear()
            elif elem.tag in ("channel", "programme"):
                src_root.clear()
    except ET.ParseError:
        pass


def write_combined(xml_blobs, wanted_ids, out_path, signature_ids=None):
    """xml_blobs: list of raw bytes (possibly gzipped) XMLTV documents.
    Writes the merged, filtered guide to out_path. Returns
    (number of channels kept, programme signatures by tvg-id)."""
    wanted_ids = set(wanted_ids)
    signature_ids = set(wanted_ids if signature_ids is None else signature_ids)
    blobs = [_gunzip(b) for b in xml_blobs if b]
    seen_channels = set()
    signatures = {i: [] for i in signature_ids}
    samples = {i: [] for i in signature_ids}
    signature_aliases = {}
    for blob in blobs:
        for channel_id, aliases in _channel_signature_aliases(blob, signature_ids).items():
            signature_aliases.setdefault(channel_id, set()).update(aliases)
    with open(out_path, "wb") as out:
        out.write(b'<?xml version="1.0" encoding="utf-8"?>\n')
        out.write(b'<tv generator-info-name="channelforge">\n')
        for blob in blobs:                # channels first, per the XMLTV convention
            _stream_matching(blob, "channel", wanted_ids, "id", out, seen_channels)
        for blob in blobs:
            _stream_matching(blob, "programme", wanted_ids, "channel", out,
                             signatures=signatures, samples=samples, signature_aliases=signature_aliases)
        out.write(b"</tv>\n")
    signatures = {
        tvg_id: {"signature": keys, "sample": " | ".join(samples[tvg_id]), "n": len(keys)}
        for tvg_id, keys in signatures.items()
        if keys
    }
    return len(seen_channels), signatures
