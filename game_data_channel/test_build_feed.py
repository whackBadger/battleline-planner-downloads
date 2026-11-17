"""Offline regression checks for the change-only public tester channel."""
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from game_data_channel.faction_pack import build_faction_pack, encode_pack
from game_data_service.catalog import FACTIONS, page_prices, prepare
from game_data_channel.build_feed import fingerprint_file, write_feed


FIRST_CHECK = "2026-11-16T12:00:00.000Z"
NEXT_CHECK = "2026-11-19T12:00:00.000Z"


def pages():
    tier = {"fromCopy": 1, "toCopy": None, "sizes": [{"models": 5, "points": 255}]}
    units = [{"name": f"Squad {n}", "tiers": [tier], "wargear": [{"name": "per Shield", "points": 5}]}
             for n in range(8)]
    return [{"version": "v1.6", "faction": slug, "units": list(units),
             "detachments": [{"note": "Omit this rule text"}]} for slug in sorted(FACTIONS)]


class FeedTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        self.manifest_path = self.directory / "manifest.json"

    def publish(self, sources, when=FIRST_CHECK, **kwargs):
        return write_feed(self.directory, prepare(sources), when, **kwargs)[0]

    def test_bootstrap_contains_only_fingerprints_matches_local_canonical_price_projection(self):
        source = pages()
        page = source[0]
        base = page["units"][0]
        other = {"fromCopy": 3, "toCopy": None, "sizes": [{"models": 5, "points": 275}],
                 "exactSizes": True, "ambiguousSizes": [6]}
        page["units"].append(dict(base, name="C’tan", tiers=[dict(base["tiers"][0], toCopy=2), other],
                                  wargearCostsRemoved=True, notes="Not for publication"))
        page["units"].extend([dict(base, name="Mixed price", tiers=[other]), dict(base, name="Mixed price")])
        first = self.publish(source)
        ref = first["factions"][page["faction"]]
        cached = build_faction_pack(page)
        self.assertEqual(ref, {"count": 10, "sha256": sha256(cached).hexdigest()})
        self.assertEqual(len(first["factions"]), len(FACTIONS))
        units = json.loads(cached)["units"]
        self.assertEqual(units["ctan"]["tiers"][1], other)
        self.assertTrue(units["ctan"]["wargearCostsRemoved"])
        self.assertIsNone(units["mixedprice"])
        for file in self.directory.rglob("*.json"):
            contents = file.read_bytes()
            for disallowed in (b'"tiers"', b'"points"', b'"wargear"', b'per Shield', b'rule text', b'Not for publication'):
                self.assertNotIn(disallowed, contents)
        self.assertFalse((self.directory / "packs").exists())
        self.assertFalse((self.directory / "patches").exists())

    def test_clients_apply_only_the_patch_to_reproduce_official_data_including_removed_units(self):
        source = pages()
        first = self.publish(source)
        page = source[0]
        slug = page["faction"]
        old_units = json.loads(build_faction_pack(page))["units"]
        page["units"][0] = dict(page["units"][0], wargear=[{"name": "per Shield", "points": 10}])
        page["units"].pop()
        page["units"].append(dict(page["units"][1], name="Previously unknown", issue="ambiguous"))
        updated = self.publish(source, NEXT_CHECK)
        self.assertNotEqual(first["revision"], updated["revision"])
        meta = updated["factions"][slug]["patch"]
        path = self.directory / "patches" / slug / (meta["sha256"] + ".json")
        raw = path.read_bytes()
        self.assertEqual(sha256(raw).hexdigest(), meta["sha256"])
        delta = json.loads(raw)
        self.assertEqual(meta["from"], first["factions"][slug]["sha256"])
        self.assertEqual(delta["from"], meta["from"])
        self.assertEqual(delta["to"], updated["factions"][slug]["sha256"])
        self.assertEqual(set(delta["units"]), {"squad0", "previouslyunknown"})
        self.assertIsNone(delta["units"]["previouslyunknown"])
        self.assertEqual(delta["removed"], ["squad7"])
        for key in delta["removed"]:
            del old_units[key]
        old_units.update(delta["units"])
        self.assertEqual(encode_pack(slug, old_units), build_faction_pack(page))
        self.assertEqual(sha256(encode_pack(slug, old_units)).hexdigest(), delta["to"])
        latest = self.publish(source, "2026-11-23T12:00:00.000Z")
        self.assertEqual(latest["revision"], updated["revision"])
        self.assertEqual(latest["factions"][slug]["patch"], meta)
        self.assertNotEqual(latest["checkedAt"], updated["checkedAt"])
        self.assertEqual([k for k in first["factions"] if first["factions"][k] != latest["factions"][k]], [slug])

    def test_large_change_records_new_hash_and_sends_clients_to_gw_instead_of_publishing_prices(self):
        source = pages()
        old = self.publish(source)
        page = source[0]
        page["units"] = [dict(unit, wargear=[]) for unit in page["units"]]
        updated = self.publish(source, NEXT_CHECK)
        slug = page["faction"]
        self.assertNotIn("patch", updated["factions"][slug])
        self.assertNotEqual(old["factions"][slug]["sha256"], updated["factions"][slug]["sha256"])
        self.assertFalse((self.directory / "patches").exists())

    def test_incomplete_stale_and_mixed_checks_keep_previous_manifest(self):
        source = pages()
        self.publish(source)
        before = self.manifest_path.read_bytes()
        with self.assertRaises(ValueError):
            self.publish(source[:-1], NEXT_CHECK)
        source[0]["version"] = "v1.7"
        with self.assertRaises(ValueError):
            self.publish(source, NEXT_CHECK)
        for page in source:
            page["version"] = "v1.5"
        with self.assertRaises(ValueError):
            self.publish(source, NEXT_CHECK)
        with self.assertRaises(ValueError):
            self.publish(pages(), "2026-11-15T12:00:00.000Z")
        with self.assertRaises(ValueError):
            page_prices("<html><body><h1>TEST</h1>", "necrons")
        self.assertEqual(self.manifest_path.read_bytes(), before)

    def test_major_source_layout_loss_needs_review_before_publication(self):
        source = pages()
        self.publish(source)
        before = self.manifest_path.read_bytes()
        page = source[-1]
        page["units"] = page["units"][:1]
        with self.assertRaisesRegex(ValueError, "lost 7"):
            self.publish(source, NEXT_CHECK)
        self.assertEqual(before, self.manifest_path.read_bytes())
        self.publish(source, NEXT_CHECK, allow_removals=True)

    def test_damaged_fingerprints_do_not_replace_last_verified_release(self):
        first = self.publish(pages())
        slug = "necrons"
        path = fingerprint_file(self.directory, slug, first["factions"][slug]["sha256"])
        doc = json.loads(path.read_bytes())
        doc["units"]["squad1"] = None
        path.write_text(json.dumps(doc), encoding="utf-8")
        before = self.manifest_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.publish(pages(), NEXT_CHECK)
        self.assertEqual(before, self.manifest_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
