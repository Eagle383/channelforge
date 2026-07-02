"""Rule engine: maps source channels to lineup channels, ignores junk, rewrites fields.

Exact 'equals' assign/ignore rules are indexed into dictionaries for O(1) matching;
everything else (regex, contains, ...) runs as an ordered scan. This is what keeps
10k channels x thousands of rules fast.
"""
import json
import re

from . import db

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
    """Re-run rules over every present, unassigned, non-ignored source channel."""
    engine = load_engine()
    rows = db.q("SELECT * FROM source_channels WHERE present = 1")
    assigned = ignored_n = changed_n = 0
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
        new_name = sc["name"]
        if changed:
            changed_n += 1
        if new_channel != row["channel_id"] or new_ignored != row["ignored"] or new_name != row["name"]:
            updates.append((new_channel, new_ignored, new_name, row["id"]))

    if updates:
        db.executemany("UPDATE source_channels SET channel_id = ?, ignored = ?, name = ? WHERE id = ?", updates)
    log(f"rules: {assigned} assigned, {ignored_n} ignored, {changed_n} field changes")
    return assigned, ignored_n
