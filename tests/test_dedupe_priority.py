import json
import os
import tempfile
import unittest

from channelforge import db, refresh, rules


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

    def test_merge_duplicates_keeps_loose_name_matches_for_review(self):
        a = self.add_channel("Duck Dynasty by A&E")
        b = self.add_channel("Duck Dynasty by History")

        merged = rules.merge_duplicates()

        self.assertEqual(merged, 0)
        self.assertIsNotNone(db.q1("SELECT 1 FROM channels WHERE id = ?", (a,)))
        self.assertIsNotNone(db.q1("SELECT 1 FROM channels WHERE id = ?", (b,)))

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
