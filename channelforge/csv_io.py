"""CSV export/import for bulk editing — the round-trip contract:
export, edit in a spreadsheet, import the same file back.

Rows are matched by `id`. Blank id on channels/rules = insert new row.
Columns you leave alone come back unchanged; assignments accept a channel
name OR id in `assigned_channel`, or the word Ignore, or blank to unassign."""
import csv
import io

from . import db

CHANNEL_COLS = ["id", "name", "active", "number", "gracenote_id", "tvg_id", "logo", "grp", "description", "preferred_provider"]
RULE_COLS = ["id", "name", "priority", "active", "source_name", "match_field", "match_type", "pattern", "action", "target_channel", "set_field", "set_value"]
ASSIGN_COLS = ["id", "source_name", "external_id", "name", "assigned_channel", "ignored", "stream_format_override"]


def _csv(cols, rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def export_channels():
    return _csv(CHANNEL_COLS, [dict(r) for r in db.q("SELECT * FROM channels ORDER BY name COLLATE NOCASE")])


def export_rules():
    rows = []
    for r in db.q("""SELECT r.*, s.name AS source_name, c.name AS target_channel
                     FROM rules r LEFT JOIN sources s ON s.id = r.source_id
                     LEFT JOIN channels c ON c.id = r.target_channel_id
                     ORDER BY r.priority, r.id"""):
        d = dict(r)
        d["source_name"] = d.get("source_name") or ""
        d["target_channel"] = d.get("target_channel") or ""
        rows.append(d)
    return _csv(RULE_COLS, rows)


def export_assignments():
    rows = []
    for r in db.q("""SELECT sc.id, s.name AS source_name, sc.external_id, sc.name,
                            c.name AS assigned_channel, sc.ignored, sc.stream_format_override
                     FROM source_channels sc JOIN sources s ON s.id = sc.source_id
                     LEFT JOIN channels c ON c.id = sc.channel_id
                     WHERE sc.present = 1 ORDER BY s.name, sc.name COLLATE NOCASE"""):
        d = dict(r)
        d["assigned_channel"] = d.get("assigned_channel") or ""
        rows.append(d)
    return _csv(ASSIGN_COLS, rows)


def _reader(data):
    return csv.DictReader(io.StringIO(data.decode("utf-8-sig")))


def _channel_lookup():
    by_name = {}
    for c in db.q("SELECT id, name FROM channels"):
        by_name.setdefault(c["name"].strip().casefold(), c["id"])
    return by_name


def _resolve_channel(value, by_name):
    v = (value or "").strip()
    if not v:
        return None, None
    if v.casefold() == "ignore":
        return "ignore", None
    if v.isdigit() and db.q1("SELECT 1 FROM channels WHERE id = ?", (int(v),)):
        return "assign", int(v)
    cid = by_name.get(v.casefold())
    if cid:
        return "assign", cid
    return "missing", None


def import_channels(data):
    n_upd = n_new = 0
    rows = _reader(data)
    has_preferred_provider = "preferred_provider" in (rows.fieldnames or [])
    for r in rows:
        vals = (r.get("name", ""), 1 if str(r.get("active", "1")).strip() in ("1", "On", "true", "True") else 0,
                r.get("number", ""), r.get("gracenote_id", ""), r.get("tvg_id", ""),
                r.get("logo", ""), r.get("grp", ""), r.get("description", ""))
        rid = (r.get("id") or "").strip()
        if rid.isdigit() and db.q1("SELECT 1 FROM channels WHERE id = ?", (int(rid),)):
            if has_preferred_provider:
                db.execute("UPDATE channels SET name=?, active=?, number=?, gracenote_id=?, tvg_id=?, logo=?, grp=?, description=?, preferred_provider=? WHERE id=?",
                           vals + (r.get("preferred_provider", ""), int(rid)))
            else:
                db.execute("UPDATE channels SET name=?, active=?, number=?, gracenote_id=?, tvg_id=?, logo=?, grp=?, description=? WHERE id=?",
                           vals + (int(rid),))
            n_upd += 1
        elif vals[0].strip():
            db.execute("INSERT INTO channels(name, active, number, gracenote_id, tvg_id, logo, grp, description, preferred_provider) VALUES(?,?,?,?,?,?,?,?,?)",
                       vals + (r.get("preferred_provider", ""),))
            n_new += 1
    return f"channels: {n_upd} updated, {n_new} added"


def import_rules(data):
    by_name = _channel_lookup()
    sources = {s["name"].strip().casefold(): s["id"] for s in db.q("SELECT id, name FROM sources")}
    n_upd = n_new = n_skip = 0
    for r in _reader(data):
        action = (r.get("action") or "assign").strip()
        kind, target_id = _resolve_channel(r.get("target_channel", ""), by_name)
        if action == "assign" and kind != "assign":
            n_skip += 1
            continue
        source_id = sources.get((r.get("source_name") or "").strip().casefold())
        vals = (r.get("name", ""), int(r.get("priority") or 100),
                1 if str(r.get("active", "1")).strip() in ("1", "On", "true", "True") else 0,
                source_id, r.get("match_field", "name"), r.get("match_type", "equals"),
                r.get("pattern", ""), action, target_id, r.get("set_field", ""), r.get("set_value", ""))
        rid = (r.get("id") or "").strip()
        if rid.isdigit() and db.q1("SELECT 1 FROM rules WHERE id = ?", (int(rid),)):
            db.execute("UPDATE rules SET name=?, priority=?, active=?, source_id=?, match_field=?, match_type=?, pattern=?, action=?, target_channel_id=?, set_field=?, set_value=? WHERE id=?", vals + (int(rid),))
            n_upd += 1
        elif vals[6].strip():
            db.execute("INSERT INTO rules(name, priority, active, source_id, match_field, match_type, pattern, action, target_channel_id, set_field, set_value) VALUES(?,?,?,?,?,?,?,?,?,?,?)", vals)
            n_new += 1
    return f"rules: {n_upd} updated, {n_new} added, {n_skip} skipped (unknown target channel)"


def import_assignments(data):
    by_name = _channel_lookup()
    n = n_skip = 0
    for r in _reader(data):
        rid = (r.get("id") or "").strip()
        if not rid.isdigit():
            n_skip += 1
            continue
        kind, cid = _resolve_channel(r.get("assigned_channel", ""), by_name)
        if kind == "missing":
            n_skip += 1
            continue
        ignored = 1 if kind == "ignore" or str(r.get("ignored", "0")).strip() in ("1", "On", "true", "True") else 0
        fmt = (r.get("stream_format_override") or "").strip()
        db.execute("UPDATE source_channels SET channel_id = ?, ignored = ?, stream_format_override = ? WHERE id = ?",
                   (cid, ignored, fmt, int(rid)))
        n += 1
    return f"assignments: {n} updated, {n_skip} skipped"
