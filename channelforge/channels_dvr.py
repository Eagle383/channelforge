"""Channels DVR API client and automations."""
import re

import httpx

from . import db


def base_urls():
    """All configured Channels DVR servers (comma/space-separated setting)."""
    raw = db.get_setting("channels_dvr_url") or ""
    return [u.strip().rstrip("/") for u in re.split(r"[,\s]+", raw) if u.strip()]


def _host(url):
    return url.split("://")[-1]


def _client():
    return httpx.Client(timeout=30, follow_redirects=True)


def ping():
    urls = base_urls()
    if not urls:
        return "not configured"
    parts = []
    for url in urls:
        try:
            with _client() as c:
                r = c.get(f"{url}/status")
                status = f"ok ({r.status_code})" if r.status_code < 400 else f"error {r.status_code}"
        except Exception as e:
            status = f"unreachable: {e}"
        parts.append(status if len(urls) == 1 else f"{_host(url)}: {status}")
    return " | ".join(parts)


def reset_passes(log=lambda s: None):
    """Pause/resume every pass and force all guide lineups to re-download."""
    urls = base_urls()
    if not urls:
        log("reset passes: Channels DVR URL not configured")
        return
    for url in urls:
        with _client() as c:
            passes = c.get(f"{url}/dvr/rules").json()
            ids = [p["ID"] for p in passes if not p.get("Paused")]
            log(f"reset passes: {_host(url)}: cycling {len(ids)} active passes")
            for pid in ids:
                c.put(f"{url}/dvr/rules/{pid}", json={"Paused": True})
            for pid in ids:
                c.put(f"{url}/dvr/rules/{pid}", json={"Paused": False})
            lineups = sorted(set(c.get(f"{url}/dvr/lineups").json().values()))
            log(f"reset passes: {_host(url)}: re-downloading guide for {len(lineups)} lineups")
            for name in lineups:
                r = c.put(f"{url}/dvr/lineups/{name}")
                if r.status_code >= 400:
                    log(f"reset passes: {_host(url)}: lineup {name}: error {r.status_code}")
    log("reset passes: done")


def add_output_sources(base, files):
    """Register every output M3U as a custom channels source on every
    configured Channels DVR server.

    Source names are derived from the filenames, so re-running updates the
    same sources in place instead of duplicating them. Stale CF sources
    (chunks that no longer exist) are removed; other m3u sources are never
    touched.
    """
    urls = base_urls()
    if not urls:
        return "Channels DVR URL not configured (see Settings)"
    m3us = [f for f in files if f.endswith(".m3u")]
    if not m3us:
        return "no output m3u files — run a Refresh first"
    guide = f"{base}/out/cf_guide.xml" if "cf_guide.xml" in files else ""
    msgs = []
    for url in urls:
        ok, pruned, failed = _push_outputs(url, base, m3us, guide)
        msg = f"added/updated {len(ok)} sources"
        if pruned:
            msg += f"; removed stale: {', '.join(pruned)}"
        if failed:
            msg += f"; FAILED: {', '.join(failed)}"
        msgs.append(msg if len(urls) == 1 else f"{_host(url)}: {msg}")
    return " | ".join(msgs)


def _push_outputs(url, base, m3us, guide):
    """Push output m3us to one DVR server, prune its stale CF sources."""
    ok, pruned, failed, current = [], [], [], set()
    with _client() as c:
        for fname in m3us:
            parts = fname[:-4].split("_")          # cf_gracenote_mpeg_ts_01
            kind, fmt, num = parts[1], "_".join(parts[2:-1]), parts[-1]
            fmt = "MPEG-TS" if fmt == "mpeg_ts" else "HLS"
            name = f"CF {'Gracenote' if kind == 'gracenote' else 'EPG'} ({fmt}) [{num}]"
            slug = re.sub(r"[^a-zA-Z0-9]", "", name)
            current.add(slug)
            payload = {
                "name": name, "type": fmt, "source": "URL",
                "url": f"{base}/out/{fname}", "text": "",
                "refresh": "24", "limit": "", "satip": "", "numbering": "", "logos": "",
                "xmltv_url": guide if kind == "epg" else "", "xmltv_refresh": "3600",
            }
            try:
                r = c.put(f"{url}/providers/m3u/sources/{slug}", json=payload)
                if r.status_code < 400:
                    ok.append(name)
                else:
                    failed.append(f"{name} ({r.status_code})")
            except Exception as e:
                failed.append(f"{name} ({e})")
        if current:  # never prune when nothing was pushed
            try:
                for d in c.get(f"{url}/devices").json():
                    did = str(d.get("DeviceID", ""))       # e.g. M3U-CFGracenoteHLS01
                    slug = did[4:]
                    if did.startswith("M3U-CF") and slug not in current:
                        r = c.delete(f"{url}/providers/m3u/sources/{slug}")
                        if r.status_code < 400:
                            pruned.append(d.get("FriendlyName") or slug)
                        else:
                            failed.append(f"remove {slug} ({r.status_code})")
            except Exception as e:
                failed.append(f"prune ({e})")
    return ok, pruned, failed


def refresh_m3u_playlists(log=lambda s: None):
    """Ask every Channels DVR server to re-pull its custom channel (m3u) sources."""
    urls = base_urls()
    if not urls:
        log("refresh m3u: Channels DVR URL not configured")
        return
    for url in urls:
        with _client() as c:
            devices = c.get(f"{url}/devices").json()
            m3us = [d["DeviceID"] for d in devices if str(d.get("DeviceID", "")).startswith("M3U")]
            log(f"refresh m3u: {_host(url)}: refreshing {len(m3us)} m3u sources")
            for device_id in m3us:
                c.post(f"{url}/providers/m3u/sources/{device_id}/refresh")
    log("refresh m3u: done")
