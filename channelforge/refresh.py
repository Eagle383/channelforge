"""Fetch sources, sync source_channels, run rules, regenerate outputs."""
import datetime
import json
import os

import httpx

from . import channels_dvr, db, m3u, rules, xmltv

OUT_DIR_NAME = "outputs"


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def out_dir():
    d = os.path.join(db.DATA_DIR, OUT_DIR_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def fetch_text(url, timeout=120):
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.text


def fetch_bytes(url, timeout=300):
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.content


def sync_source(source, log):
    try:
        text = fetch_text(source["url"])
    except Exception as e:
        db.execute("UPDATE sources SET last_status = ? WHERE id = ?", (f"fetch failed: {e}", source["id"]))
        log(f"  {source['name']}: FETCH FAILED ({e}); keeping previous channels")
        return 0

    entries = m3u.parse(text)
    if not entries:
        db.execute("UPDATE sources SET last_status = 'empty playlist' WHERE id = ?", (source["id"],))
        log(f"  {source['name']}: empty playlist; keeping previous channels")
        return 0

    existing = {r["external_id"]: r for r in db.q("SELECT * FROM source_channels WHERE source_id = ?", (source["id"],))}
    seen = set()
    inserts, updates = [], []
    for e in entries:
        ext = m3u.external_id(e)
        if ext in seen:
            continue
        seen.add(ext)
        attrs_json = json.dumps(e["attrs"], sort_keys=True)
        row = existing.get(ext)
        if row is None:
            inserts.append((source["id"], ext, e["name"], e["url"], attrs_json))
        elif row["url"] != e["url"] or row["attrs"] != attrs_json or not row["present"]:
            updates.append((e["url"], attrs_json, row["id"]))

    gone = [r["id"] for ext, r in existing.items() if ext not in seen and r["present"]]

    if inserts:
        db.executemany("INSERT INTO source_channels(source_id, external_id, name, url, attrs) VALUES(?,?,?,?,?)", inserts)
    if updates:
        db.executemany("UPDATE source_channels SET url = ?, attrs = ?, present = 1 WHERE id = ?", updates)
    if gone:
        db.executemany("UPDATE source_channels SET present = 0 WHERE id = ?", [(i,) for i in gone])
    db.execute("UPDATE sources SET last_fetched = ?, last_status = ? WHERE id = ?",
               (now(), f"ok: {len(seen)} channels (+{len(inserts)} new, -{len(gone)} gone)", source["id"]))
    log(f"  {source['name']}: {len(seen)} channels (+{len(inserts)} new, ~{len(updates)} changed, -{len(gone)} gone)")
    return len(seen)


def assigned_children():
    """channel_id -> its children from active sources, in pick order: source
    priority first, then the drag-and-drop provider order for combined feeds
    where one source carries many providers (unranked providers last), then id.
    """
    children = db.q("""
        SELECT sc.*, s.priority AS src_priority, s.stream_format AS src_format, s.name AS src_name
        FROM source_channels sc JOIN sources s ON s.id = sc.source_id
        WHERE sc.channel_id IS NOT NULL AND s.active = 1
    """)
    rank = {p: i for i, p in enumerate(json.loads(db.get_setting("provider_order") or "[]"))}
    unranked = len(rank)
    by_channel = {}
    for c in sorted(children, key=lambda c: (
            c["src_priority"], rank.get(m3u.provider_of(c["external_id"]), unranked), c["id"])):
        by_channel.setdefault(c["channel_id"], []).append(c)
    return by_channel


def pick_stream(children_rows, preferred_source_id):
    """children_rows sorted by source priority; return best (row, format)."""
    candidates = [c for c in children_rows if c["present"] and not c["ignored"]]
    healthy = [c for c in candidates if c["healthy"]]
    pool = healthy or candidates
    if not pool:
        return None, None
    if preferred_source_id:
        for c in pool:
            if c["source_id"] == preferred_source_id:
                return c, c["stream_format_override"] or c["src_format"]
    c = pool[0]
    return c, c["stream_format_override"] or c["src_format"]


def build_outputs(log=lambda s: None):
    """Generate m3u files + combined XMLTV into the outputs dir."""
    max_per = int(db.get_setting("output_max_per_m3u", "1200") or 1200)
    channels = db.q("SELECT * FROM channels WHERE active = 1 ORDER BY name COLLATE NOCASE")
    by_channel = assigned_children()

    lineups = {("gracenote", "HLS"): [], ("gracenote", "MPEG-TS"): [], ("epg", "HLS"): [], ("epg", "MPEG-TS"): []}
    wanted_tvg_ids = set()
    skipped = 0

    # auto-numbering: hand out numbers from output_start_number upward, skipping
    # ones already taken, and persist each onto its channel so they stay stable
    start = (db.get_setting("output_start_number") or "").strip()
    auto_no, num_updates = None, []
    if start.isdigit():
        taken = [int(r["number"]) for r in db.q("SELECT number FROM channels WHERE number != ''") if r["number"].isdigit()]
        auto_no = max([int(start) - 1] + [n for n in taken if n >= int(start)]) + 1

    for ch in channels:
        best, fmt = pick_stream(by_channel.get(ch["id"], []), ch["preferred_source_id"])
        if best is None:
            skipped += 1
            continue
        child_attrs = json.loads(best["attrs"] or "{}")
        overrides = json.loads(ch["attrs"] or "{}")
        attrs = dict(child_attrs)
        attrs["channel-id"] = f"cf-{ch['id']}"
        number = ch["number"]
        inherited = (m3u.ota_number_of(best["external_id"])
                     or child_attrs.get("channel-number", "") or child_attrs.get("tvg-chno", ""))
        if not number:
            if auto_no is not None and "." not in inherited:   # dotted = real OTA number, keep it
                number = str(auto_no)
                auto_no += 1
            else:
                number = inherited
            if number:   # persist whatever the channel actually uses so it's visible and editable
                num_updates.append((number, ch["id"]))
        if number:
            attrs["channel-number"] = number
            attrs["tvg-chno"] = number
        if ch["logo"]:
            attrs["tvg-logo"] = ch["logo"]
        if ch["grp"]:
            attrs["group-title"] = ch["grp"]
        if ch["description"]:
            attrs["tvg-description"] = ch["description"]
        attrs.update({k: v for k, v in overrides.items() if v})

        gracenote = ch["gracenote_id"] or child_attrs.get("tvc-guide-stationid", "")
        if gracenote:
            attrs["tvc-guide-stationid"] = gracenote
            attrs.pop("tvg-id", None)
            lineups[("gracenote", fmt)].append((ch["name"], best["url"], attrs))
        else:
            tvg = ch["tvg_id"] or child_attrs.get("tvg-id", "")
            attrs.pop("tvc-guide-stationid", None)
            if tvg:
                attrs["tvg-id"] = tvg
                wanted_tvg_ids.add(tvg)
            lineups[("epg", fmt)].append((ch["name"], best["url"], attrs))

    if num_updates:
        db.executemany("UPDATE channels SET number = ? WHERE id = ?", num_updates)
        log(f"outputs: auto-numbered {len(num_updates)} channels ({num_updates[0][0]}-{num_updates[-1][0]})")

    d = out_dir()
    for f in os.listdir(d):
        if f.endswith((".m3u", ".xml", ".xml.gz")):
            os.remove(os.path.join(d, f))

    files = []
    for (kind, fmt), rows in lineups.items():
        if not rows:
            continue
        fmt_slug = fmt.lower().replace("-", "_")
        chunks = [rows[i:i + max_per] for i in range(0, len(rows), max_per)]
        for i, chunk in enumerate(chunks, 1):
            fname = f"cf_{kind}_{fmt_slug}_{i:02d}.m3u"
            with open(os.path.join(d, fname), "w", encoding="utf-8") as fh:
                fh.write(m3u.generate(chunk))
            files.append(f"{fname} ({len(chunk)} channels)")

    # combined guide for the epg lineups, streamed to disk to bound memory
    if wanted_tvg_ids:
        blobs = []
        for s in db.q("SELECT * FROM sources WHERE active = 1 AND epg_url != ''"):
            try:
                blobs.append(fetch_bytes(s["epg_url"]))
                log(f"  guide: fetched {s['name']}")
            except Exception as e:
                log(f"  guide: {s['name']} FAILED ({e})")
        log(f"  guide: merging {len(blobs)} guides for {len(wanted_tvg_ids)} station ids...")
        kept = xmltv.write_combined(blobs, wanted_tvg_ids, os.path.join(d, "cf_guide.xml"))
        files.append(f"cf_guide.xml ({kept} channels)")

    log(f"outputs: {', '.join(files) if files else 'nothing generated'}")
    if skipped:
        log(f"outputs: {skipped} active channels had no available stream")


def run_refresh(log=lambda s: None):
    log("refreshing sources...")
    total = 0
    for source in db.q("SELECT * FROM sources WHERE active = 1 ORDER BY priority"):
        total += sync_source(source, log)
    log(f"total channels across sources: {total}")
    rules.apply_all(log)
    build_outputs(log)
    if db.get_setting("push_outputs_to_dvr") == "1":
        base = (db.get_setting("base_url") or "").rstrip("/")
        if base:
            files = sorted(f for f in os.listdir(out_dir()) if not f.startswith("."))
            log("dvr: " + channels_dvr.add_output_sources(base, files))
        else:
            log("dvr: push skipped — set the external base URL in Settings first")
    log("refresh complete")
