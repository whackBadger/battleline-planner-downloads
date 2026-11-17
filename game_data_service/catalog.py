"""Build immutable, numeric-only price releases from the official faction pages."""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
import urllib.request

from game_data_channel.faction_pack import canonical, faction_units

try:
    from .mfm_points import page_prices  # private scheduled job bundle
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "android/app/src/main/python"))
    from mfm_points import page_prices


FACTIONS = frozenset("""
adepta-sororitas adeptus-custodes adeptus-mechanicus aeldari astra-militarum black-templars
blood-angels chaos-daemons chaos-knights chaos-space-marines chaos-titan-legions dark-angels
death-guard deathwatch drukhari emperors-children genestealer-cults grey-knights imperial-agents
imperial-knights leagues-of-votann necrons orks space-marines space-wolves tau-empire thousand-sons
titan-legions tyranids world-eaters
""".split())


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_faction(slug):
    if slug not in FACTIONS:
        raise ValueError("Unknown Field Manual faction")
    url = "https://mfm.warhammer-community.com/en/" + slug
    request = urllib.request.Request(url, headers={"User-Agent": "BattleLinePlanner-Catalog/1.0"})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=25) as response:
        if response.status != 200 or response.headers.get_content_type() != "text/html":
            raise ValueError(f"Unexpected Field Manual response for {slug}")
        html = response.read(8 * 1024 * 1024 + 1)
    if len(html) > 8 * 1024 * 1024:
        raise ValueError(f"Field Manual response too large for {slug}")
    return page_prices(html.decode("utf-8"), slug)


def prepare(pages):
    """Build a stable, numeric-only release; unresolved equivalents share a pending record."""
    if not pages or Counter(page["faction"] for page in pages) != Counter(FACTIONS):
        raise ValueError("Incomplete or duplicate set of faction pages")
    versions = {page["version"] for page in pages}
    if len(versions) != 1 or not all(re.fullmatch(r"v\d+(?:\.\d+)+", v) for v in versions):
        raise ValueError("Field Manual pages have inconsistent versions")
    version = versions.pop()
    rows = {}
    for page in pages:
        faction = page["faction"]
        for key, price in faction_units(page).items():
            rows[faction, key] = price
    records = [{"faction": f, "key": key, "price": price}
               for (f, key), price in sorted(rows.items())]
    chunks = [records[start:start + 48] for start in range(0, len(records), 48)]
    manifest = [{"hash": content_hash(chunk), "count": len(chunk)} for chunk in chunks]
    return version, chunks, manifest, content_hash([version, manifest])


def content_hash(value):
    return sha256(canonical(value).encode()).hexdigest()


def fetch_release():
    with ThreadPoolExecutor(max_workers=3) as pool:
        pages = list(pool.map(fetch_faction, sorted(FACTIONS)))
    return prepare(pages)
