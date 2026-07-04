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


def is_by_brand_alias(name):
    return dedupe_key(name) != normalize(name)

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


def merge_duplicates(log=lambda s: None):
    """Collapse duplicate lineup channels into one and delete the extras.

    Two channels are duplicates when they share a guide station id (their own
    gracenote_id or one carried by a present child), have the same normalized
    name ('123 GO!' / '123GO!'), or are a plain name plus its trailing
    'by <brand>' provider alias ('Duck Dynasty' / 'Duck Dynasty by A&E').
    Names that merely look alike ('Fifth Gear' / 'Fifth Gear (UK)') are never
    merged without station-id proof.

    The survivor is the plainest active name (oldest on ties). Children and
    rules are repointed at it, its blank metadata is backfilled from the
    duplicates, and the duplicates are deleted. Runs as the 'dedupe' job
    (button on the Channels/Jobs pages) and inside every refresh unless
    `auto_merge_duplicates` is off.
    """
    channels = db.q("SELECT * FROM channels")
    parent = {c["id"]: c["id"] for c in channels}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    seen = {}
    by_norm = {}
    alias_waiting = {}
    plain_by_key = {}

    def remember_plain(key, cid):
        plain = plain_by_key.setdefault(key, cid)
        union(plain, cid)
        for alias in alias_waiting.pop(key, ()):
            union(plain, alias)

    def remember_alias(key, cid):
        plain = plain_by_key.get(key)
        if plain is None:
            alias_waiting.setdefault(key, []).append(cid)
        else:
            union(plain, cid)

    for c in channels:
        norm = normalize(c["name"])
        if norm:
            union(by_norm.setdefault(norm, c["id"]), c["id"])
        key = dedupe_key(c["name"])
        if key and key != norm:
            remember_alias(key, c["id"])
        elif key:
            remember_plain(key, c["id"])
        if c["gracenote_id"]:
            union(seen.setdefault(("sid", c["gracenote_id"]), c["id"]), c["id"])
    for row in db.q("SELECT channel_id, attrs FROM source_channels WHERE channel_id IS NOT NULL AND present = 1"):
        sid = db.attrs_of(row).get("tvc-guide-stationid")
        if sid:
            union(seen.setdefault(("sid", sid), row["channel_id"]), row["channel_id"])
    for a, b in _programme_lineup_pairs({c["id"] for c in channels}):
        union(a, b)

    clusters = {}
    for c in channels:
        clusters.setdefault(find(c["id"]), []).append(c)
    merged = 0
    for group in clusters.values():
        if len(group) < 2:
            continue
        # keep the plain name over a 'by <brand>' rename, active over inactive, oldest on ties
        group.sort(key=lambda c: (is_by_brand_alias(c["name"]), not c["active"], c["id"]))
        merged += _merge_group(group[0], group[1:], log)
    if merged:
        log(f"dedupe: merged {merged} hard duplicate channels")
    else:
        review_n = len(find_possible_duplicates())
        msg = "dedupe: no hard-merge duplicates found"
        if review_n:
            msg += f"; {review_n} possible duplicate groups need manual review at /dupes"
        log(msg)
    return merged


def _merge_group(keeper_row, losers, log=lambda s: None):
    """Fold losers into keeper: repoint children and rules, backfill the
    keeper's blank metadata, delete the losers."""
    keeper = dict(keeper_row)
    for loser in losers:
        db.execute("UPDATE source_channels SET channel_id = ? WHERE channel_id = ?", (keeper["id"], loser["id"]))
        db.execute("UPDATE rules SET target_channel_id = ? WHERE target_channel_id = ?", (keeper["id"], loser["id"]))
        for f in ("number", "gracenote_id", "tvg_id", "logo", "grp", "description"):
            if not keeper[f] and loser[f]:
                keeper[f] = loser[f]
        if keeper["preferred_source_id"] is None:
            keeper["preferred_source_id"] = loser["preferred_source_id"]
        keeper["attrs"] = json.dumps({**json.loads(loser["attrs"] or "{}"), **json.loads(keeper["attrs"] or "{}")})
        db.execute("DELETE FROM channels WHERE id = ?", (loser["id"],))
        log(f"  merged '{loser['name']}' into '{keeper['name']}'")
    db.execute("UPDATE channels SET number=?, gracenote_id=?, tvg_id=?, logo=?, grp=?, description=?, preferred_source_id=?, attrs=? WHERE id=?",
               tuple(keeper[f] for f in ("number", "gracenote_id", "tvg_id", "logo", "grp", "description", "preferred_source_id", "attrs")) + (keeper["id"],))
    return len(losers)


def merge_channels(keeper_id, loser_ids, log=lambda s: None):
    """Manual merge from the duplicates review page."""
    keeper = db.q1("SELECT * FROM channels WHERE id = ?", (keeper_id,))
    marks = ",".join("?" * len(loser_ids))
    losers = [r for r in db.q(f"SELECT * FROM channels WHERE id IN ({marks})", loser_ids) if r["id"] != keeper_id] if loser_ids else []
    if not keeper or not losers:
        return 0
    n = _merge_group(keeper, losers, log)
    db.executemany("DELETE FROM dupe_dismissed WHERE a = ? OR b = ?", [(r["id"], r["id"]) for r in losers])
    return n


_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP_TOKENS = {
    "the", "a", "an", "and", "with", "by", "of", "en",
    "channel", "free", "hd", "live", "network", "news", "now", "plus", "tv",
}
_GUIDE_ID_FIELDS = ("tvc-guide-stationid", "tvg-id")
_GUIDE_DESC_FIELDS = ("tvc-guide-description", "tvg-description")


def _name_tokens(name):
    s = unicodedata.normalize("NFKD", (name or "")).casefold()
    return _TOKEN_RE.findall(s.replace("'", "").replace("’", ""))


def _guide_text_key(text):
    words = [w for w in _name_tokens(text) if w not in _STOP_TOKENS]
    if len(words) < 6:
        return ""
    return " ".join(words)


def _signature_keys(row):
    try:
        keys = json.loads(row["signature"] or "[]")
    except (TypeError, ValueError):
        return set()
    return {str(k) for k in keys if k}


def _is_subseq(small, big):
    it = iter(big)
    return all(ch in it for ch in small)


def _dupe_confidence(reasons):
    text = "; ".join(reasons)
    if ("same guide programme lineup" in text or "same guide tvg-id" in text
            or "same guide description" in text or "same guide station id" in text
            or "same name after trailing" in text):
        return "strong", 0, "strong guide or alias evidence"
    if "one name's words contained in the other's" in text:
        return "medium", 1, "name overlap"
    return "weak", 2, "weak name hint"


def _programme_lineup_pairs(channel_ids):
    channel_ids = set(channel_ids)
    signatures = {r["tvg_id"]: _signature_keys(r) for r in db.q("SELECT tvg_id, signature FROM guide_signatures WHERE n >= 2")}
    schedule_keys = {}

    def add_schedule(cid, tvg_id):
        if cid not in channel_ids:
            return
        keys = signatures.get((tvg_id or "").strip())
        if keys:
            schedule_keys.setdefault(cid, set()).update(keys)

    for c in db.q("SELECT id, tvg_id FROM channels"):
        add_schedule(c["id"], c["tvg_id"])
    for row in db.q("SELECT channel_id, attrs FROM source_channels WHERE present = 1 AND channel_id IS NOT NULL"):
        add_schedule(row["channel_id"], db.attrs_of(row).get("tvg-id"))

    programme_posting = {}
    for cid, keys in schedule_keys.items():
        for key in keys:
            programme_posting.setdefault(key, set()).add(cid)
    programme_pair_hits = {}
    fanout_cap = 6
    for ids in programme_posting.values():
        ids = sorted(i for i in ids if i in channel_ids)
        if len(ids) < 2 or len(ids) > fanout_cap:
            continue
        for i, cid in enumerate(ids):
            for other in ids[i + 1:]:
                k = (cid, other)
                programme_pair_hits[k] = programme_pair_hits.get(k, 0) + 1
    out = []
    for (cid, other), hits in programme_pair_hits.items():
        smaller = min(len(schedule_keys[cid]), len(schedule_keys[other]))
        min_hits = 2 if smaller <= 3 else 3
        if smaller >= 2 and hits >= min_hits and hits / smaller >= 0.75:
            out.append((cid, other))
    return out


def find_possible_duplicates():
    """Groups of active channels whose names look related: one name's words
    contained in another's ('Antiques Roadshow' / 'PBS Antiques Roadshow',
    'Family Feud' / 'Family Feud Classic') or a leading acronym matching
    another name's initials ('AFV ...' / \"America's Funniest Home Videos\").

    Review-only evidence — many hits are genuinely distinct feeds (Tastemade /
    Tastemade Travel), so nothing here is ever merged automatically; the
    Duplicates page shows the groups for a human merge/dismiss verdict.
    Pairs dismissed there (dupe_dismissed) stop being reported."""
    channels = db.q("SELECT * FROM channels WHERE active = 1")
    dismissed = {(r["a"], r["b"]) for r in db.q("SELECT a, b FROM dupe_dismissed")}
    toks = {}       # id -> frozenset of non-stopword tokens
    initials = {}   # id -> first letters of every token, in order
    for c in channels:
        words = _name_tokens(c["name"])
        t = frozenset(w for w in words if w not in _STOP_TOKENS)
        if t:
            toks[c["id"]] = t
            initials[c["id"]] = "".join(w[0] for w in words)

    posting = {}    # token -> ids of channels whose name carries it
    for cid, t in toks.items():
        for w in t:
            posting.setdefault(w, set()).add(cid)

    pairs = {}      # (small id, big id) -> why
    def edge(a, b, why):
        key = (min(a, b), max(a, b))
        if key not in dismissed:
            pairs.setdefault(key, why)

    fanout_cap = 6   # a name contained in more channels than this is generic ('FOX', 'Comedy'), not a duplicate
    for cid, t in toks.items():
        if len(t) < 2 or not any(len(w) >= 4 and not w.isdigit() for w in t):
            continue
        rarest = min(t, key=lambda w: len(posting[w]))
        supers = [o for o in posting[rarest] if o != cid and t <= toks[o]]
        if len(supers) <= fanout_cap:
            for other in supers:
                edge(cid, other, "one name's words contained in the other's")
    by_id = {c["id"]: c for c in channels}
    acros = {}   # leading all-caps token -> ids of channels starting with it
    for c in channels:
        first = (c["name"] or "").split()[0] if (c["name"] or "").split() else ""
        acro = "".join(ch for ch in first if ch.isalpha())
        if acro.isupper() and 3 <= len(acro) <= 6:
            acros.setdefault(acro, []).append(c["id"])
    for acro, ids in acros.items():
        if len(ids) > 3:   # borne by many channels = a network prefix (ABC 7, NBC News...), not an abbreviation
            continue
        a = acro.casefold()
        # near-exact expansions only: same first letter, at most one extra word's initial
        targets = [o for o, ini in initials.items()
                   if o not in ids and ini[:1] == a[0] and len(a) <= len(ini) <= len(a) + 1
                   and _is_subseq(a, ini)]
        if len(targets) > 3:   # expands to half the lineup = coincidence
            continue
        for cid in ids:
            for other in targets:
                edge(cid, other, f"'{acro}' could abbreviate '{by_id[other]['name']}'")

    alias_groups = {}
    for c in channels:
        if is_by_brand_alias(c["name"]):
            alias_groups.setdefault(dedupe_key(c["name"]), []).append(c["id"])
    for key, ids in alias_groups.items():
        if not key or len(ids) < 2 or len(ids) > fanout_cap:
            continue
        for i, cid in enumerate(ids):
            for other in ids[i + 1:]:
                edge(cid, other, "same name after trailing 'by <brand>' is removed")

    guide_groups = {}
    def guide_edge(kind, value, cid):
        if not value:
            return
        guide_groups.setdefault((kind, value), set()).add(cid)

    for c in channels:
        guide_edge("guide station id", (c["gracenote_id"] or "").strip(), c["id"])
        guide_edge("guide tvg-id", (c["tvg_id"] or "").strip().casefold(), c["id"])
        guide_edge("guide description", _guide_text_key(c["description"]), c["id"])
    for row in db.q("SELECT channel_id, attrs FROM source_channels WHERE present = 1 AND channel_id IS NOT NULL"):
        cid = row["channel_id"]
        if cid not in by_id:
            continue
        attrs = db.attrs_of(row)
        for field in _GUIDE_ID_FIELDS:
            guide_edge("guide station id" if field == "tvc-guide-stationid" else "guide tvg-id",
                       (attrs.get(field) or "").strip().casefold(), cid)
        for field in _GUIDE_DESC_FIELDS:
            guide_edge("guide description", _guide_text_key(attrs.get(field)), cid)
    for (kind, _value), ids in guide_groups.items():
        ids = sorted(ids)
        if len(ids) < 2 or len(ids) > fanout_cap:
            continue
        for i, cid in enumerate(ids):
            for other in ids[i + 1:]:
                edge(cid, other, f"same {kind}")

    for cid, other in _programme_lineup_pairs(by_id):
        edge(cid, other, "same guide programme lineup")

    parent = {c["id"]: c["id"] for c in channels}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b in pairs:
        parent[max(find(a), find(b))] = min(find(a), find(b))

    groups = {}
    for a, b in pairs:
        groups.setdefault(find(a), set()).update((a, b))
    out = []
    for ids in groups.values():
        members = sorted((by_id[i] for i in ids), key=lambda c: (len(toks.get(c["id"], ())), c["name"].casefold()))
        why = sorted({w for k, w in pairs.items() if k[0] in ids or k[1] in ids})
        confidence, rank, hint = _dupe_confidence(why)
        out.append({"channels": members, "why": "; ".join(why),
                    "confidence": confidence, "confidence_rank": rank,
                    "confidence_hint": hint})
    out.sort(key=lambda g: (g["confidence_rank"], g["channels"][0]["name"].casefold()))
    return out


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
