"""M3U parsing and generation (Channels DVR custom-channels dialect)."""
import hashlib
import re

_ATTR_RE = re.compile(r'([a-zA-Z0-9\-]+)="([^"]*)"')
_PROVIDER_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\.")


def provider_of(external_id):
    """Provider prefix of a combined-feed channel id (samsung.x -> samsung).

    Ids that don't start with a letter (e.g. OTA numbers like 3.1) have no
    provider and return "".
    """
    m = _PROVIDER_RE.match(external_id or "")
    return m.group(1).lower() if m else ""

# EXTINF attribute names Channels DVR understands, in output order
CHANNELS_ATTRS = [
    "channel-id", "channel-number", "tvg-id", "tvg-name", "tvg-chno", "tvg-logo",
    "tvg-description", "group-title", "tvc-guide-stationid", "tvc-guide-title",
    "tvc-guide-description", "tvc-guide-art", "tvc-guide-tags", "tvc-guide-genres",
    "tvc-guide-categories", "tvc-guide-placeholders", "tvc-stream-vcodec", "tvc-stream-acodec",
]


def parse(text):
    """Parse M3U text into a list of {'name', 'url', 'attrs': {...}} dicts."""
    entries = []
    current = None
    for raw in text.splitlines():
        line = raw.strip().lstrip("﻿")
        if not line:
            continue
        if line.startswith("#EXTINF"):
            attrs = dict(_ATTR_RE.findall(line))
            name = line.rsplit(",", 1)[1].strip() if "," in line else ""
            current = {"name": name or attrs.get("tvg-name", ""), "attrs": attrs}
        elif line.startswith("#"):
            continue
        elif current is not None:
            current["url"] = line
            entries.append(current)
            current = None
    return entries


def external_id(entry):
    """Stable identity of a channel within its source."""
    for key in ("channel-id", "tvg-id", "tvg-name"):
        v = entry["attrs"].get(key, "").strip()
        if v:
            return v
    if entry["name"]:
        return entry["name"]
    return hashlib.sha1(entry["url"].encode()).hexdigest()[:16]


def generate(rows):
    """rows: iterable of (name, url, attrs-dict). Returns M3U text."""
    out = ["#EXTM3U"]
    for name, url, attrs in rows:
        parts = ["#EXTINF:-1"]
        for key in CHANNELS_ATTRS:
            v = attrs.get(key, "")
            if v:
                parts.append(f'{key}="{v}"')
        out.append(" ".join(parts) + f",{name}")
        out.append(url)
    return "\n".join(out) + "\n"
