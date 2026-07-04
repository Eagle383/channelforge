"""One-time migration from legacy playlist-manager CSV exports
(Playlists, Parents, ChildToParent, StationMappings)."""
import csv
import io
import json

from . import db

LEGACY_FIELD_MAP = {  # legacy mapping field -> our match field
    "title": "name", "tvg_id": "tvg_id", "tvg_name": "tvg_name",
    "group_title": "group", "url": "url", "channel_id": "external_id", "all": "any",
    "tvg_description": "tvg_description", "tvc_guide_description": "tvc_guide_description",
}
LEGACY_COMPARE_MAP = {
    "equal": "equals", "equal_not": "not_equals", "contain": "contains", "contain_not": "not_contains",
    "begin": "starts", "begin_not": "not_starts", "end": "ends", "end_not": "not_ends",
    "regex": "regex", "regex_not": "not_regex",
}


def _rows(file_bytes):
    return list(csv.DictReader(io.StringIO(file_bytes.decode("utf-8-sig"))))


def _channel_lookup():
    out = {"name": {}, "station": {}, "tvg": {}, "number": {}}
    for c in db.q("SELECT id, name, number, gracenote_id, tvg_id FROM channels"):
        if c["name"]:
            out["name"].setdefault(c["name"].strip().casefold(), c["id"])
        if c["gracenote_id"]:
            out["station"].setdefault(c["gracenote_id"].strip(), c["id"])
        if c["tvg_id"]:
            out["tvg"].setdefault(c["tvg_id"].strip().casefold(), c["id"])
        if c["number"]:
            out["number"].setdefault(c["number"].strip(), c["id"])
    return out


def _parent_lookup(parents_bytes):
    return {r["parent_channel_id"]: r for r in _rows(parents_bytes)}


def _resolve_existing_parent(parent_row, channels):
    for legacy_key, lookup_key, fold in (
            ("parent_tvc_guide_stationid_override", "station", False),
            ("parent_tvg_id_override", "tvg", True),
            ("parent_channel_number_override", "number", False),
            ("parent_title", "name", True)):
        value = (parent_row.get(legacy_key) or "").strip()
        if not value:
            continue
        key = value.casefold() if fold else value
        cid = channels[lookup_key].get(key)
        if cid:
            return cid
    return None


def _rule_exists(vals):
    name, priority, active, source_id, match_field, match_type, pattern, action, target_id = vals
    row = db.q1(
        """SELECT 1 FROM rules
           WHERE name = ? AND priority = ? AND active = ? AND source_id IS ?
             AND match_field = ? AND match_type = ? AND pattern = ?
             AND action = ? AND target_channel_id IS ?""",
        (name, priority, active, source_id, match_field, match_type, pattern, action, target_id),
    )
    return row is not None


def import_station_mapping_rules(parents_bytes, mappings_bytes, log=lambda s: None):
    """Import legacy StationMappings as rules targeting existing channels.

    Unlike the full legacy migration, this does not create channels. The parent
    CSV is used only to translate legacy target ids (``plm_####``) to the
    current channel table by station id, tvg-id, number, then name.
    """
    parents = _parent_lookup(parents_bytes)
    channels = _channel_lookup()
    n = ignored = skipped = duplicates = 0
    missing_targets = []
    for r in _rows(mappings_bytes):
        target = (r.get("target_parent_channel_id") or "").strip()
        match_field = LEGACY_FIELD_MAP.get(r.get("source_field", ""))
        match_type = LEGACY_COMPARE_MAP.get(r.get("source_field_compare_id", ""))
        if match_field is None or match_type is None:
            skipped += 1
            continue
        if r.get("source_m3u_id", "all") != "all":
            skipped += 1
            continue
        if target == "Ignore":
            action, target_id = "ignore", None
        else:
            parent = parents.get(target)
            target_id = _resolve_existing_parent(parent, channels) if parent else None
            if target_id is None:
                skipped += 1
                if target and target not in ("manual", "Make Parent"):
                    missing_targets.append(target)
                continue
            action = "assign"
        vals = (
            r.get("station_mapping_name", ""),
            int(r.get("station_mapping_priority") or 100),
            1 if r.get("station_mapping_active") == "On" else 0,
            None,
            match_field,
            match_type,
            r.get("source_field_string", ""),
            action,
            target_id,
        )
        if _rule_exists(vals):
            duplicates += 1
            continue
        db.execute(
            """INSERT INTO rules(name, priority, active, source_id, match_field, match_type, pattern, action, target_channel_id)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            vals,
        )
        if action == "ignore":
            ignored += 1
        else:
            n += 1
    msg = f"station mappings: {n} assign rules, {ignored} ignore rules imported"
    if duplicates:
        msg += f", {duplicates} duplicates skipped"
    if skipped:
        msg += f", {skipped} skipped"
    if missing_targets:
        msg += f" ({len(set(missing_targets))} targets not found in existing channels)"
    log(msg)
    return n + ignored


def import_legacy(files, log=lambda s: None):
    """files: dict of {'playlists','parents','child_to_parent','station_mappings'} -> bytes (any may be missing)."""
    m3u_to_source = {}
    plm_to_channel = {}

    if files.get("playlists"):
        for r in _rows(files["playlists"]):
            existing = db.q1("SELECT id FROM sources WHERE name = ?", (r["m3u_name"],))
            if existing:
                m3u_to_source[r["m3u_id"]] = existing["id"]
                continue
            cur = db.execute(
                "INSERT INTO sources(name, url, epg_url, stream_format, priority, active, check_streams) VALUES(?,?,?,?,?,?,?)",
                (r["m3u_name"], r["m3u_url"], r.get("epg_xml", "") or "", r.get("stream_format", "HLS") or "HLS",
                 int(r.get("m3u_priority") or 100), 1 if r.get("m3u_active") == "On" else 0,
                 1 if r.get("station_check") == "On" else 0))
            m3u_to_source[r["m3u_id"]] = cur.lastrowid
        log(f"sources: {len(m3u_to_source)} imported")

    if files.get("parents"):
        n = 0
        for r in _rows(files["parents"]):
            extra = {}
            for legacy_key, attr in (("parent_tvc_guide_art_override", "tvc-guide-art"),
                                  ("parent_tvc_guide_tags_override", "tvc-guide-tags"),
                                  ("parent_tvc_guide_genres_override", "tvc-guide-genres"),
                                  ("parent_tvc_guide_categories_override", "tvc-guide-categories"),
                                  ("parent_tvc_guide_placeholders_override", "tvc-guide-placeholders"),
                                  ("parent_tvc_stream_vcodec_override", "tvc-stream-vcodec"),
                                  ("parent_tvc_stream_acodec_override", "tvc-stream-acodec")):
                if r.get(legacy_key):
                    extra[attr] = r[legacy_key]
            preferred = None
            if r.get("parent_preferred_playlist") and r["parent_preferred_playlist"] in m3u_to_source:
                preferred = m3u_to_source[r["parent_preferred_playlist"]]
            cur = db.execute(
                "INSERT INTO channels(name, active, number, gracenote_id, tvg_id, logo, grp, description, preferred_source_id, attrs) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (r["parent_title"], 1 if r.get("parent_active") == "On" else 0,
                 r.get("parent_channel_number_override", "") or "",
                 r.get("parent_tvc_guide_stationid_override", "") or "",
                 r.get("parent_tvg_id_override", "") or "",
                 r.get("parent_tvg_logo_override", "") or "",
                 r.get("parent_group_title_override", "") or "",
                 r.get("parent_tvg_description_override", "") or "",
                 preferred, json.dumps(extra)))
            plm_to_channel[r["parent_channel_id"]] = cur.lastrowid
            n += 1
        log(f"channels: {n} imported")

    if files.get("child_to_parent"):
        assigned = ignored = missing = 0
        for r in _rows(files["child_to_parent"]):
            child_key = r["child_m3u_id_channel_id"]              # e.g. m3u_0008_3.1
            m3u_id = "_".join(child_key.split("_")[:2])
            ext = child_key[len(m3u_id) + 1:]
            source_id = m3u_to_source.get(m3u_id)
            if source_id is None:
                missing += 1
                continue
            parent = r.get("parent_channel_id", "")
            fmt = r.get("stream_format_override", "")
            fmt = "" if fmt in ("None", "") else fmt
            if parent != "Ignore" and parent not in plm_to_channel:
                continue
            # stub row so the assignment survives until the first refresh fills in url/attrs
            db.execute("INSERT OR IGNORE INTO source_channels(source_id, external_id, name, url, present) VALUES(?,?,?,?,0)",
                       (source_id, ext, ext, ""))
            if parent == "Ignore":
                db.execute("UPDATE source_channels SET ignored = 1, stream_format_override = ? WHERE source_id = ? AND external_id = ?",
                           (fmt, source_id, ext))
                ignored += 1
            else:
                db.execute("UPDATE source_channels SET channel_id = ?, stream_format_override = ? WHERE source_id = ? AND external_id = ?",
                           (plm_to_channel[parent], fmt, source_id, ext))
                assigned += 1
        log(f"assignments: {assigned} assigned, {ignored} ignored, {missing} skipped (unknown playlist)")

    if files.get("station_mappings"):
        n = skipped = 0
        for r in _rows(files["station_mappings"]):
            target = r.get("target_parent_channel_id", "")
            match_field = LEGACY_FIELD_MAP.get(r.get("source_field", ""), None)
            match_type = LEGACY_COMPARE_MAP.get(r.get("source_field_compare_id", ""), None)
            if match_field is None or match_type is None:
                skipped += 1
                continue
            if target == "Ignore":
                action, target_id = "ignore", None
            elif target in plm_to_channel:
                action, target_id = "assign", plm_to_channel[target]
            else:
                skipped += 1                      # 'manual', 'Make Parent', unknown targets
                continue
            source_id = m3u_to_source.get(r.get("source_m3u_id", "all"))
            db.execute(
                "INSERT INTO rules(name, priority, active, source_id, match_field, match_type, pattern, action, target_channel_id) VALUES(?,?,?,?,?,?,?,?,?)",
                (r.get("station_mapping_name", ""), int(r.get("station_mapping_priority") or 100),
                 1 if r.get("station_mapping_active") == "On" else 0,
                 source_id, match_field, match_type, r.get("source_field_string", ""), action, target_id))
            n += 1
        log(f"rules: {n} imported, {skipped} skipped (manual/make-parent/unmappable)")

    log("legacy import complete — run a Refresh to fetch sources and apply everything")
