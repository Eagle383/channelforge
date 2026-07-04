import json
import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from channelforge import app as webapp
from channelforge import db, refresh, rules, xmltv


def reset_db(data_dir):
    conn = getattr(db._local, "conn", None)
    if conn is not None:
        conn.close()
        db._local.conn = None
    db.DATA_DIR = data_dir
    db.DB_PATH = os.path.join(data_dir, "channelforge.db")
    db.init()


class DedupePriorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_db(self.tmp.name)

    def tearDown(self):
        conn = getattr(db._local, "conn", None)
        if conn is not None:
            conn.close()
            db._local.conn = None
        self.tmp.cleanup()

    def add_channel(self, name, gracenote_id=""):
        return db.execute(
            "INSERT INTO channels(name, gracenote_id) VALUES(?, ?)",
            (name, gracenote_id),
        ).lastrowid

    def add_source(self, name, priority=100):
        return db.execute(
            "INSERT INTO sources(name, url, priority, active) VALUES(?, ?, ?, 1)",
            (name, f"http://example.test/{name}.m3u", priority),
        ).lastrowid

    def add_child(self, source_id, channel_id, external_id, name, url=None, attrs=None):
        db.execute(
            """INSERT INTO source_channels(source_id, channel_id, external_id, name, url, attrs)
               VALUES(?, ?, ?, ?, ?, ?)""",
            (
                source_id,
                channel_id,
                external_id,
                name,
                url or f"http://example.test/{external_id}.m3u8",
                json.dumps(attrs or {}),
            ),
        )

    def add_signature(self, tvg_id, keys):
        db.execute(
            "INSERT INTO guide_signatures(tvg_id, signature, sample, n, updated) VALUES(?, ?, ?, ?, ?)",
            (tvg_id, json.dumps(keys), "Show A | Show B", len(keys), "now"),
        )

    def test_merge_duplicates_keeps_loose_name_matches_for_review(self):
        a = self.add_channel("Duck Dynasty by A&E")
        b = self.add_channel("Duck Dynasty by History")

        merged = rules.merge_duplicates()

        self.assertEqual(merged, 0)
        self.assertIsNotNone(db.q1("SELECT 1 FROM channels WHERE id = ?", (a,)))
        self.assertIsNotNone(db.q1("SELECT 1 FROM channels WHERE id = ?", (b,)))

    def test_merge_duplicates_log_points_to_manual_review_candidates(self):
        messages = []
        self.add_channel("CNA")
        self.add_channel("CBS News Atlanta")

        merged = rules.merge_duplicates(messages.append)

        self.assertEqual(merged, 0)
        self.assertEqual(len(messages), 1)
        self.assertIn("no hard-merge duplicates found", messages[0])
        self.assertIn("1 possible duplicate groups need manual review", messages[0])

    def test_possible_duplicates_flags_alias_only_groups(self):
        self.add_channel("Duck Dynasty by A&E")
        self.add_channel("Duck Dynasty by History")

        groups = rules.find_possible_duplicates()

        self.assertEqual(len(groups), 1)
        self.assertEqual(
            {c["name"] for c in groups[0]["channels"]},
            {"Duck Dynasty by A&E", "Duck Dynasty by History"},
        )

    def test_possible_duplicates_skips_generic_one_token_groups(self):
        self.add_channel("25 News")
        self.add_channel("Boston 25 News")
        self.add_channel("KXXV 25 News Waco")

        groups = rules.find_possible_duplicates()

        self.assertEqual(groups, [])

    def test_possible_duplicates_keeps_specific_local_station_matches(self):
        self.add_channel("ABC KATU Portland OR")
        self.add_channel("KATU ABC 2 News Portland OR")

        groups = rules.find_possible_duplicates()

        self.assertEqual(len(groups), 1)
        self.assertEqual(
            {c["name"] for c in groups[0]["channels"]},
            {"ABC KATU Portland OR", "KATU ABC 2 News Portland OR"},
        )

    def test_possible_duplicates_uses_matching_guide_tvg_ids(self):
        source = self.add_source("fastchannels")
        a = self.add_channel("Mystery Theater")
        b = self.add_channel("Classic Mystery")
        self.add_child(source, a, "one.mystery", "Mystery Theater", attrs={"tvg-id": "mystery.us"})
        self.add_child(source, b, "two.mystery", "Classic Mystery", attrs={"tvg-id": "mystery.us"})

        groups = rules.find_possible_duplicates()

        self.assertEqual(len(groups), 1)
        self.assertIn("same guide tvg-id", groups[0]["why"])
        self.assertEqual(
            {c["name"] for c in groups[0]["channels"]},
            {"Mystery Theater", "Classic Mystery"},
        )

    def test_possible_duplicates_uses_matching_long_guide_descriptions(self):
        source = self.add_source("fastchannels")
        a = self.add_channel("Oak Island Select")
        b = self.add_channel("Treasure Mysteries")
        desc = "Researchers examine hidden clues and historic maps while searching for buried treasure."
        self.add_child(source, a, "one.oak", "Oak Island Select", attrs={"tvg-description": desc})
        self.add_child(source, b, "two.oak", "Treasure Mysteries", attrs={"tvc-guide-description": desc})

        groups = rules.find_possible_duplicates()

        self.assertEqual(len(groups), 1)
        self.assertIn("same guide description", groups[0]["why"])
        self.assertEqual(
            {c["name"] for c in groups[0]["channels"]},
            {"Oak Island Select", "Treasure Mysteries"},
        )

    def test_possible_duplicates_ignores_short_guide_descriptions(self):
        source = self.add_source("fastchannels")
        a = self.add_channel("Channel One")
        b = self.add_channel("Station Two")
        self.add_child(source, a, "one.live", "Channel One", attrs={"tvg-description": "Watch live TV."})
        self.add_child(source, b, "two.live", "Station Two", attrs={"tvg-description": "Watch live TV."})

        groups = rules.find_possible_duplicates()

        self.assertEqual(groups, [])

    def test_possible_duplicates_uses_matching_programme_lineups(self):
        source = self.add_source("fastchannels")
        a = self.add_channel("Alpha One")
        b = self.add_channel("Zulu Two")
        keys = [f"program-{i}" for i in range(10)]
        self.add_signature("alpha.epg", keys)
        self.add_signature("zulu.epg", keys)
        self.add_child(source, a, "one.alpha", "Alpha One", attrs={"tvg-id": "alpha.epg"})
        self.add_child(source, b, "two.zulu", "Zulu Two", attrs={"tvg-id": "zulu.epg"})

        groups = rules.find_possible_duplicates()

        self.assertEqual(len(groups), 1)
        self.assertIn("same guide programme lineup", groups[0]["why"])
        self.assertEqual(
            {c["name"] for c in groups[0]["channels"]},
            {"Alpha One", "Zulu Two"},
        )

    def test_possible_duplicates_uses_short_high_overlap_programme_lineups(self):
        source = self.add_source("fastchannels")
        a = self.add_channel("Big 12 Network")
        b = self.add_channel("Big 12 Studios")
        keys = ["football-900", "football-1200"]
        self.add_signature("network.epg", keys)
        self.add_signature("studios.epg", keys)
        self.add_child(source, a, "one.big12", "Big 12 Network", attrs={"tvg-id": "network.epg"})
        self.add_child(source, b, "two.big12", "Big 12 Studios", attrs={"tvg-id": "studios.epg"})

        groups = rules.find_possible_duplicates()

        self.assertEqual(len(groups), 1)
        self.assertIn("same guide programme lineup", groups[0]["why"])

    def test_merge_duplicates_merges_matching_programme_lineups(self):
        source = self.add_source("fastchannels")
        keeper = self.add_channel("Big 12 Network")
        loser = self.add_channel("Big 12 Studios")
        keys = ["football-900", "football-1200"]
        self.add_signature("network.epg", keys)
        self.add_signature("studios.epg", keys)
        self.add_child(source, keeper, "one.big12", "Big 12 Network", attrs={"tvg-id": "network.epg"})
        self.add_child(source, loser, "two.big12", "Big 12 Studios", attrs={"tvg-id": "studios.epg"})

        merged = rules.merge_duplicates()

        self.assertEqual(merged, 1)
        self.assertIsNone(db.q1("SELECT 1 FROM channels WHERE id = ?", (loser,)))
        child = db.q1("SELECT channel_id FROM source_channels WHERE external_id = ?", ("two.big12",))
        self.assertEqual(child["channel_id"], keeper)

    def test_xmltv_write_combined_returns_programme_signatures(self):
        out_path = os.path.join(self.tmp.name, "guide.xml")
        xml = b"""<tv>
          <channel id="alpha.epg"><display-name>Alpha</display-name></channel>
          <programme start="20260704010000 +0000" channel="alpha.epg"><title>Show A</title></programme>
          <programme start="20260704020000 +0000" channel="alpha.epg"><title>Show B</title></programme>
          <programme start="20260704030000 +0000" channel="other.epg"><title>Other</title></programme>
        </tv>"""

        kept, signatures = xmltv.write_combined([xml], {"alpha.epg"}, out_path)

        self.assertEqual(kept, 1)
        self.assertEqual(signatures["alpha.epg"]["n"], 2)
        self.assertIn("Show A", signatures["alpha.epg"]["sample"])

    def test_xmltv_programme_signatures_tolerate_nearby_start_minutes(self):
        out_path = os.path.join(self.tmp.name, "guide.xml")
        xml = b"""<tv>
          <channel id="network.epg"><display-name>Network</display-name></channel>
          <channel id="studios.epg"><display-name>Studios</display-name></channel>
          <programme start="20260704210000 +0000" channel="network.epg"><title>BYU vs. Cincinnati Football Full Game Replay</title></programme>
          <programme start="20260704235900 +0000" channel="network.epg"><title>Utah vs. BYU Football Full Game Replay</title></programme>
          <programme start="20260704210000 +0000" channel="studios.epg"><title>BYU vs. Cincinnati Football Full Game Replay</title></programme>
          <programme start="20260705000000 +0000" channel="studios.epg"><title>Utah vs. BYU Football Full Game Replay</title></programme>
        </tv>"""

        _kept, signatures = xmltv.write_combined(
            [xml], set(), out_path, {"network.epg", "studios.epg"})

        self.assertEqual(set(signatures["network.epg"]["signature"]), set(signatures["studios.epg"]["signature"]))

    def test_xmltv_indexes_signature_ids_outside_output_guide(self):
        out_path = os.path.join(self.tmp.name, "guide.xml")
        xml = b"""<tv>
          <channel id="alpha.epg"><display-name>Alpha</display-name></channel>
          <channel id="gracenote-source.epg"><display-name>Gracenote Source</display-name></channel>
          <programme start="20260704010000 +0000" channel="alpha.epg"><title>Show A</title></programme>
          <programme start="20260704010000 +0000" channel="gracenote-source.epg"><title>Show A</title></programme>
        </tv>"""

        kept, signatures = xmltv.write_combined(
            [xml], {"alpha.epg"}, out_path, {"alpha.epg", "gracenote-source.epg"})

        self.assertEqual(kept, 1)
        self.assertIn("alpha.epg", signatures)
        self.assertIn("gracenote-source.epg", signatures)
        with open(out_path, "rb") as fh:
            written = fh.read()
        self.assertIn(b'channel="alpha.epg"', written)
        self.assertNotIn(b'channel="gracenote-source.epg"', written)

    def test_dupes_page_suggests_keeper_by_output_stream_priority(self):
        hi = self.add_source("high", priority=1)
        lo = self.add_source("low", priority=100)
        low_channel = self.add_channel("Alpha Movies")
        high_channel = self.add_channel("Zulu Movies")
        attrs = {"tvg-id": "movies.example"}
        self.add_child(lo, low_channel, "pluto.movies", "Alpha Movies",
                       url="http://low.example/stream.m3u8", attrs=attrs)
        self.add_child(hi, high_channel, "xumo.movies", "Zulu Movies",
                       url="http://high.example/stream.m3u8", attrs=attrs)

        with TestClient(webapp.app) as client:
            response = client.get("/dupes")

        self.assertEqual(response.status_code, 200)
        html = response.text
        checked = f'name="keeper_id" value="{high_channel}" checked'
        unchecked = f'name="keeper_id" value="{low_channel}"'
        self.assertIn(checked, html)
        self.assertLess(html.index(checked), html.index(unchecked))
        self.assertIn("high / xumo", html)
        self.assertIn("source priority 1", html)
        self.assertIn("provider rank unranked", html)

    def test_dupes_page_uses_provider_priority_when_source_priority_ties(self):
        source = self.add_source("fastchannels", priority=10)
        low_provider_channel = self.add_channel("Alpha Movies")
        high_provider_channel = self.add_channel("Zulu Movies")
        attrs = {"tvg-id": "movies.example"}
        self.add_child(source, low_provider_channel, "xumo.movies", "Alpha Movies",
                       url="http://xumo.example/stream.m3u8", attrs=attrs)
        self.add_child(source, high_provider_channel, "pluto.movies", "Zulu Movies",
                       url="http://pluto.example/stream.m3u8", attrs=attrs)
        db.set_setting("provider_order", json.dumps(["pluto", "xumo"]))

        with TestClient(webapp.app) as client:
            response = client.get("/dupes")

        self.assertEqual(response.status_code, 200)
        html = response.text
        checked = f'name="keeper_id" value="{high_provider_channel}" checked'
        unchecked = f'name="keeper_id" value="{low_provider_channel}"'
        self.assertIn(checked, html)
        self.assertLess(html.index(checked), html.index(unchecked))
        self.assertIn("fastchannels / pluto", html)
        self.assertIn("source priority 10, provider rank 1", html)

    def test_dupes_page_filters_by_confidence(self):
        source = self.add_source("fastchannels")
        strong_a = self.add_channel("Alpha One")
        strong_b = self.add_channel("Zulu Two")
        self.add_channel("CNA")
        self.add_channel("CBS News Atlanta")
        keys = [f"program-{i}" for i in range(10)]
        self.add_signature("alpha.epg", keys)
        self.add_signature("zulu.epg", keys)
        self.add_child(source, strong_a, "one.alpha", "Alpha One", attrs={"tvg-id": "alpha.epg"})
        self.add_child(source, strong_b, "two.zulu", "Zulu Two", attrs={"tvg-id": "zulu.epg"})

        with TestClient(webapp.app) as client:
            response = client.get("/dupes?confidence=strong")

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("strong guide/lineup (1)", html)
        self.assertIn("Alpha One", html)
        self.assertIn("Zulu Two", html)
        self.assertNotIn("CBS News Atlanta", html)

    def test_dupes_page_can_bulk_dismiss_visible_weak_groups(self):
        self.add_channel("CNA")
        self.add_channel("CBS News Atlanta")

        with TestClient(webapp.app) as client:
            response = client.get("/dupes?confidence=weak")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Dismiss visible weak groups", response.text)
            self.assertIn('name="group_sets"', response.text)
            client.post("/dupes/dismiss_many", data={"group_sets": "1,2"},
                        headers={"referer": "/dupes?confidence=weak"})

        self.assertIsNotNone(db.q1("SELECT 1 FROM dupe_dismissed WHERE a = 1 AND b = 2"))

    def test_merge_duplicates_merges_plain_name_and_provider_alias(self):
        source = self.add_source("fastchannels")
        plain = self.add_channel("Duck Dynasty")
        alias = self.add_channel("Duck Dynasty by A&E")
        self.add_child(source, alias, "samsung.duck-dynasty", "Duck Dynasty by A&E")

        merged = rules.merge_duplicates()

        self.assertEqual(merged, 1)
        self.assertIsNone(db.q1("SELECT 1 FROM channels WHERE id = ?", (alias,)))
        child = db.q1("SELECT channel_id FROM source_channels WHERE external_id = ?", ("samsung.duck-dynasty",))
        self.assertEqual(child["channel_id"], plain)

    def test_merge_duplicates_merges_shared_station_id(self):
        source = self.add_source("fastchannels")
        keeper = self.add_channel("ABC News Live")
        loser = self.add_channel("ABC News")
        self.add_child(source, loser, "pluto.abc-news", "ABC News", attrs={"tvc-guide-stationid": "12345"})
        self.add_child(source, keeper, "samsung.abc-news-live", "ABC News Live", attrs={"tvc-guide-stationid": "12345"})

        merged = rules.merge_duplicates()

        self.assertEqual(merged, 1)
        self.assertEqual(
            db.q1("SELECT COUNT(*) n FROM source_channels WHERE channel_id = ?", (keeper,))["n"],
            2,
        )

    def test_pick_stream_honors_source_then_provider_priority(self):
        hi = self.add_source("high", priority=1)
        lo = self.add_source("low", priority=100)
        ch = self.add_channel("BBC Earth")
        self.add_child(lo, ch, "pluto.bbc-earth", "BBC Earth", url="http://low.example/stream.m3u8")
        self.add_child(hi, ch, "xumo.bbc-earth", "BBC Earth", url="http://high.example/stream.m3u8")
        db.set_setting("provider_order", json.dumps(["pluto", "xumo"]))

        best, _ = refresh.pick_stream(refresh.assigned_children()[ch], None)

        self.assertEqual(best["url"], "http://high.example/stream.m3u8")

    def test_pick_stream_uses_provider_priority_within_same_source(self):
        source = self.add_source("fastchannels", priority=10)
        ch = self.add_channel("BBC Earth")
        self.add_child(source, ch, "xumo.bbc-earth", "BBC Earth", url="http://xumo.example/stream.m3u8")
        self.add_child(source, ch, "pluto.bbc-earth", "BBC Earth", url="http://pluto.example/stream.m3u8")
        db.set_setting("provider_order", json.dumps(["pluto", "xumo"]))

        best, _ = refresh.pick_stream(refresh.assigned_children()[ch], None)

        self.assertEqual(best["url"], "http://pluto.example/stream.m3u8")


if __name__ == "__main__":
    unittest.main()
