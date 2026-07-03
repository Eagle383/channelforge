"""Rule engine: maps source channels to lineup channels, ignores junk, rewrites fields.

Exact 'equals' assign/ignore rules are indexed into dictionaries for O(1) matching;
everything else (regex, contains, ...) runs as an ordered scan. This is what keeps
10k channels x thousands of rules fast.
"""
import json
import re
import unicodedata

from . import db

_NORM_RE = re.compile(r"[^a-z0-9]+")
_BY_BRAND_RE = re.compile(r"\s+by\s+.+$", re.IGNORECASE)


def normalize(name):
    """Collapse a name for fuzzy matching: casefold, drop accents/punctuation/spacing.

    '123 GO!', '123 Go' and '123GO!' all become '123go', so providers'
    spelling variants of the same channel land on one key.
    """
    s = unicodedata.normalize("NFKD", name or "").casefold()
    return _NORM_RE.sub("", s)


def dedupe_key(name):
    """normalize() with a trailing ' by <brand>' clause dropped, so provider
    renames like 'Duck Dynasty by A&E' land on 'Duck Dynasty'. Fallback only —
    an exact normalize() match always wins, and a rule can override either."""
    return normalize(_BY_BRAND_RE.sub("", name or ""))

MATCH_FIELDS = ["name", "tvg_id", "tvg_name", "group", "url", "external_id", "any"]
MATCH_TYPES = ["equals", "not_equals", "contains", "not_contains", "starts", "not_starts", "ends", "not_ends", "regex", "not_regex"]
ACTIONS = ["assign", "ignore", "set_field"]
SET_FIELDS = ["name", "group", "logo", "tvg_id"]


def field_value(sc, field):
    attrs = sc["_attrs"]
    if field == "name":
        return sc["name"] or ""
    if field == "tvg_id":
        return attrs.get("tvg-id", "")
    if field == "tvg_name":
        return attrs.get("tvg-name", "")
    if field == "group":
        return attrs.get("group-title", "")
    if field == "url":
        return sc["url"] or ""
    if field == "external_id":
        return sc["external_id"] or ""
    return ""


def _matches(rule, value, compiled):
    t = rule["match_type"]
    p = rule["pattern"]
    if t == "equals":
        return value == p
    if t == "not_equals":
        return value != p
    if t == "contains":
        return p in value
    if t == "not_contains":
        return p not in value
    if t == "starts":
        return value.startswith(p)
    if t == "not_starts":
        return not value.startswith(p)
    if t == "ends":
        return value.endswith(p)
    if t == "not_ends":
        return not value.endswith(p)
    if t == "regex":
        return compiled.search(value) is not None
    if t == "not_regex":
        return compiled.search(value) is None
    return False


class Engine:
    def __init__(self, rule_rows):
        self.exact = {}          # (field, pattern) -> rule, only for equals+assign/ignore, all-sources
        self.scan = []           # (rule, compiled_regex_or_None) in priority order
        rows = sorted(rule_rows, key=lambda r: (r["priority"], r["id"]))
        for r in rows:
            if not r["active"]:
                continue
            if (r["match_type"] == "equals" and r["action"] in ("assign", "ignore")
                    and r["source_id"] is None and r["match_field"] != "any"):
                self.exact.setdefault((r["match_field"], r["pattern"]), r)
            else:
                compiled = None
                if r["match_type"] in ("regex", "not_regex"):
                    try:
                        compiled = re.compile(r["pattern"])
                    except re.error:
                        continue
                self.scan.append((r, compiled))

    def apply(self, sc):
        """sc: dict with name/url/external_id/source_id/_attrs. Returns
        (assign_channel_id | None, ignored: bool, changed_fields: dict)."""
        assign = None
        ignored = False
        changed = {}

        # exact index first (priority among exacts is irrelevant: one pattern -> one rule)
        for field in ("name", "tvg_id", "tvg_name", "group", "external_id"):
            rule = self.exact.get((field, field_value(sc, field)))
            if rule:
                if rule["action"] == "ignore":
                    ignored = True
                elif assign is None:
                    assign = rule["target_channel_id"]

        # ordered scan for everything else
        for rule, compiled in self.scan:
            if rule["source_id"] is not None and rule["source_id"] != sc["source_id"]:
                continue
            fields = MATCH_FIELDS[:-1] if rule["match_field"] == "any" else [rule["match_field"]]
            hit = any(_matches(rule, field_value(sc, f), compiled) for f in fields)
            if not hit:
                continue
            if rule["action"] == "ignore":
                ignored = True
            elif rule["action"] == "assign":
                if assign is None:
                    assign = rule["target_channel_id"]
            elif rule["action"] == "set_field" and rule["set_field"]:
                changed[rule["set_field"]] = rule["set_value"]
                if rule["set_field"] == "name":
                    sc["name"] = rule["set_value"]

        return assign, ignored, changed


def load_engine():
    return Engine(db.q("SELECT * FROM rules"))


def apply_all(log=lambda s: None):
    """Re-run rules over every present, unassigned, non-ignored source channel.

    After the rules, source channels still unassigned can fall through to
    guide-station matching (same tvc-guide-stationid = same channel, whatever
    the providers call it), then normalized-name matching against existing
    channels (and optionally auto-create a channel when nothing matches).
    Ignore rules always win.

    Assignments the rules didn't produce (fallthrough matches, imports,
    pre-rule history) are materialized as `auto:` equals rules
    (`auto_rule_on_match`, default on) so every mapping is visible and
    tweakable on the Rules page.
    """
    engine = load_engine()
    auto_match = db.get_setting("auto_assign_normalized") != "0"
    auto_create = db.get_setting("auto_create_channels") == "1"
    auto_rule = db.get_setting("auto_rule_on_match") != "0"
    by_norm, by_alias, by_station = {}, {}, {}
    if auto_match:
        # prefer curated (numbered) channels, then oldest, when variants collide
        for ch in db.q("SELECT id, name, gracenote_id FROM channels ORDER BY (number != '') DESC, id"):
            by_norm.setdefault(normalize(ch["name"]), ch["id"])
            by_alias.setdefault(dedupe_key(ch["name"]), ch["id"])
            if ch["gracenote_id"]:
                by_station.setdefault(ch["gracenote_id"], ch["id"])
    rows = db.q("SELECT * FROM source_channels WHERE present = 1")
    new_rules = {}     # child name -> channel id, materialized as `auto:` rules
    sid_of_name = {}   # normalized name -> station id, so a sid-less EPG child
    if auto_match:     # can borrow the sid its gracenote-feed twin carries
        for row in rows:
            sid = db.attrs_of(row).get("tvc-guide-stationid")
            if not sid:
                continue
            sid_of_name.setdefault(normalize(row["name"]), sid)
            if row["channel_id"] is not None and not row["ignored"]:
                by_station.setdefault(sid, row["channel_id"])
    assigned = ignored_n = changed_n = matched = created = 0
    updates = []
    for row in rows:
        sc = dict(row)
        sc["_attrs"] = json.loads(sc["attrs"] or "{}")
        assign, ignored, changed = engine.apply(sc)

        new_channel = sc["channel_id"]
        if assign is not None and sc["channel_id"] is None:
            new_channel = assign
            assigned += 1
        new_ignored = sc["ignored"]
        if ignored and not sc["ignored"] and sc["channel_id"] is None:
            new_ignored = 1
            ignored_n += 1
        if auto_match and new_channel is None and not new_ignored:
            key = normalize(sc["name"])
            sid = sc["_attrs"].get("tvc-guide-stationid", "") or sid_of_name.get(key, "")
            cid = by_station.get(sid) if sid else None
            if cid is None and key:
                cid = by_norm.get(key)
                if cid is None:
                    cid = by_alias.get(dedupe_key(sc["name"]))
            if cid is None and auto_create and key:
                cid = db.execute("INSERT INTO channels(name) VALUES(?)", (sc["name"],)).lastrowid
                by_norm[key] = cid
                by_alias.setdefault(dedupe_key(sc["name"]), cid)
                created += 1
            if cid is not None:
                if sid:
                    by_station.setdefault(sid, cid)
                new_channel = cid
                matched += 1
        # assignment exists but no rule produced it (fallthrough / import /
        # pre-rule history) -> pin it as a visible, editable equals rule
        if (auto_rule and new_channel is not None and not new_ignored and assign is None
                and sc["name"] and ("name", sc["name"]) not in engine.exact):
            new_rules.setdefault(sc["name"], new_channel)

        new_name = sc["name"]
        if changed:
            changed_n += 1
        if new_channel != row["channel_id"] or new_ignored != row["ignored"] or new_name != row["name"]:
            updates.append((new_channel, new_ignored, new_name, row["id"]))

    if updates:
        db.executemany("UPDATE source_channels SET channel_id = ?, ignored = ?, name = ? WHERE id = ?", updates)
    if new_rules:
        db.executemany(
            "INSERT INTO rules(name, priority, active, match_field, match_type, pattern, action, target_channel_id) VALUES(?,100,1,'name','equals',?,'assign',?)",
            [(f"auto: {n}", n, cid) for n, cid in new_rules.items()])
    msg = f"rules: {assigned} assigned, {ignored_n} ignored, {changed_n} field changes"
    if auto_match:
        msg += f"; name-match: {matched} assigned, {created} channels created"
    if new_rules:
        msg += f"; {len(new_rules)} auto rules pinned"
    log(msg)
    return assigned + matched, ignored_n
