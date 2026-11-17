"""Publish verified numeric changes without publishing the master price catalog."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile

from game_data_channel.faction_pack import canonical, encode_pack
from game_data_service.catalog import FACTIONS, content_hash, fetch_release, utc_now


def encoded(value):
    return (canonical(value) + "\n").encode("utf-8")


def group_release(release):
    version, chunks, _, _ = release
    factions = {slug: {} for slug in sorted(FACTIONS)}
    for chunk in chunks:
        for row in chunk:
            units = factions[row["faction"]]
            key = row["key"]
            if not re.fullmatch(r"[a-z0-9]+", key) or key in units:
                raise ValueError("Invalid or duplicate unit key")
            units[key] = row["price"]
    if any(not any(price is not None for price in units.values()) for units in factions.values()):
        raise ValueError("Each faction must contain verified prices")
    return version, factions


def version_number(value):
    if not isinstance(value, str) or not re.fullmatch(r"v\d+(?:\.\d+)+", value):
        raise ValueError("Invalid Field Manual version")
    return tuple(map(int, value[1:].split(".")))


def fingerprints(units):
    return {key: None if value is None else content_hash(value) for key, value in units.items()}


def fingerprint_file(directory, slug, digest):
    if slug not in FACTIONS or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Invalid faction reference")
    return directory / "fingerprints" / slug / (digest + ".json")


def read_previous(directory, manifest, version, checked_at):
    if manifest.get("schema") != 1 or set(manifest["factions"]) != FACTIONS:
        raise ValueError("The previous manifest has an unexpected schema or faction set")
    if version_number(version) < version_number(manifest["sourceVersion"]):
        raise ValueError("The Field Manual version regressed")
    if checked_at < manifest["checkedAt"]:
        raise ValueError("A newer check is already present")

    previous = {}
    for slug, ref in manifest["factions"].items():
        state = json.loads(fingerprint_file(directory, slug, ref["sha256"]).read_bytes())
        ids = state["units"]
        if len(ids) != ref["count"] or content_hash(ids) != state["checksum"]:
            raise ValueError(f"Previous {slug} fingerprints failed their integrity check")
        previous[slug] = ids
    return previous


def change_patch(slug, units, ids, old_ids, old_digest, digest, allow_removals):
    previous_verified = {key for key, fingerprint in old_ids.items() if fingerprint is not None}
    now_verified = {key for key, fingerprint in ids.items() if fingerprint is not None}
    lost = len(previous_verified - now_verified)
    if lost >= 3 and lost * 5 > len(previous_verified) and not allow_removals:
        raise ValueError(
            f"{slug} lost {lost} of {len(previous_verified)} verified entries. "
            "Review the official page before accepting the removals.")
    changed = {key: units[key] for key in ids if key not in old_ids or ids[key] != old_ids[key]}
    removed = sorted(old_ids.keys() - ids.keys())
    # Bootstrap and broad rewrites are fetched directly from GW on the device.
    # A public download must never be a substitute for a complete faction.
    if len(changed) * 2 >= len(units) or len(changed) > 32:
        return None
    return {"schema": 1, "faction": slug, "from": old_digest, "to": digest,
            "units": changed, "removed": removed}


def write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_feed(directory, release, checked_at, allow_removals=False):
    directory = Path(directory)
    version, factions = group_release(release)
    version_number(version)
    manifest_path = directory / "manifest.json"
    current = json.loads(manifest_path.read_bytes()) if manifest_path.exists() else {}
    previous = read_previous(directory, current, version, checked_at) if current else {}
    files = {}
    references = {}
    prices_revision = {}
    for slug, units in factions.items():
        # The complete numeric pack exists only in RAM and is never written to the channel.
        digest = sha256(encode_pack(slug, units)).hexdigest()
        reference = {"sha256": digest, "count": len(units)}
        prices_revision[slug] = reference.copy()
        references[slug] = reference
        ids = fingerprints(units)
        files[fingerprint_file(directory, slug, digest)] = encoded({"units": ids, "checksum": content_hash(ids)})
        if not previous:
            continue
        old_ref = current["factions"][slug]
        if digest == old_ref["sha256"]:
            if "patch" in old_ref:
                reference["patch"] = old_ref["patch"]
            continue
        patch = change_patch(slug, units, ids, previous[slug], old_ref["sha256"], digest, allow_removals)
        if patch is not None:
            data = encoded(patch)
            patch_digest = sha256(data).hexdigest()
            files[directory / "patches" / slug / (patch_digest + ".json")] = data
            reference["patch"] = {"from": old_ref["sha256"], "sha256": patch_digest}
    manifest = {"schema": 1, "revision": content_hash([version, prices_revision]),
                "sourceVersion": version, "checkedAt": checked_at, "factions": references}
    # No data is written until all 30 factions and comparisons pass. Only the
    # final manifest makes a new revision visible. Git publishes the same files in one commit.
    for path, data in files.items():
        if not path.exists():
            write_atomic(path, data)
        elif path.read_bytes() != data:
            raise ValueError(f"Immutable game-data file changed: {path.name}")
    write_atomic(manifest_path, encoded(manifest))
    return manifest, sum(price is None for units in factions.values() for price in units.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("game-data"))
    parser.add_argument("--accept-large-removals", action="store_true",
                        help="Accept sharp unit losses when a manual review confirmed the GW change.")
    args = parser.parse_args()
    started_at = utc_now()
    manifest, unresolved = write_feed(args.output, fetch_release(), started_at, args.accept_large_removals)
    refs = manifest["factions"]
    print(f"Checked {manifest['sourceVersion']}: {len(refs)} factions, "
          f"{sum(r['count'] for r in refs.values())} unit entries, {unresolved} unresolved. "
          f"Revision {manifest['revision']}.")


if __name__ == "__main__":
    main()
