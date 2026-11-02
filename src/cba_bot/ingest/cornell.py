"""Cornell eCommons (DSpace 7) CBA collections."""

from __future__ import annotations

import time
from pathlib import Path

from cba_bot.ingest.common import (
    Manifest,
    clean,
    client,
    fetch_many,
    iso_date,
    safe_name,
    state_from_location,
)

API = "https://ecommons.cornell.edu/server/api"
COLLECTIONS = {
    "perb": "7603db13-cd5d-46ce-abd3-05c6f3b41b0b",
    "dol": "11bcf4ba-664b-4b6d-8bb5-5039c826f883",
    "retail-education": "eb22d707-dbb0-4269-96d0-9f16e71c225c",
}
SECTOR_MAP = {
    "public sector": "public",
    "private industry": "private",
    "federal": "federal",
    "health care": "healthcare",
    "education": "education",
}


def _value(metadata: dict, key: str) -> str:
    values = metadata.get(key) or []
    if not values:
        return ""
    first = values[0]
    return clean(first.get("value", "") if isinstance(first, dict) else str(first))


def _listing(http, collection: str) -> list[dict]:
    items: list[dict] = []
    page = 0
    while True:
        for attempt in range(5):
            try:
                response = http.get(
                    f"{API}/discover/search/objects",
                    params={"scope": collection, "size": 100, "page": page},
                )
                response.raise_for_status()
                break
            except Exception:
                if attempt == 4:
                    raise
                # DSpace starts returning 429 after a burst of listing calls.
                time.sleep(8 * (attempt + 1))
        result = response.json()["_embedded"]["searchResult"]
        for entry in result["_embedded"]["objects"]:
            items.append(entry["_embedded"]["indexableObject"])
        if len(items) >= int(result["page"]["totalElements"]) or not result[
            "_embedded"
        ]["objects"]:
            break
        page += 1
        time.sleep(0.4)
    return items


def _pdf_url(http, item_id: str) -> str:
    response = http.get(f"{API}/core/items/{item_id}/bundles")
    response.raise_for_status()
    bundles = response.json().get("_embedded", {}).get("bundles", [])
    original = next((b for b in bundles if b.get("name") == "ORIGINAL"), None)
    if not original:
        return ""
    response = http.get(original["_links"]["bitstreams"]["href"])
    response.raise_for_status()
    bitstreams = response.json().get("_embedded", {}).get("bitstreams", [])
    for bitstream in bitstreams:
        link = bitstream.get("_links", {}).get("content", {}).get("href", "")
        name = bitstream.get("name", "").lower()
        if link and (name.endswith(".pdf") or bitstream.get("bundleName") == "ORIGINAL"):
            return link
    return ""


def scrape(
    root: Path,
    *,
    limit: int = 120,
    full: bool = False,
    concurrency: int = 6,
    progress=None,
) -> dict:
    root = Path(root)
    manifest = Manifest(root, "cornell")
    totals = {"listed": 0, "kept": 0, "downloaded": 0, "failed": 0}
    seen: set[str] = set()

    with client() as http:
        for label, collection in COLLECTIONS.items():
            items = _listing(http, collection)
            totals["listed"] += len(items)
            candidates = []
            for item in items:
                metadata = item.get("metadata") or {}
                if "collective labor agreement" not in _value(
                    metadata, "dc.type"
                ).lower():
                    continue
                effective = iso_date(_value(metadata, "cla.date.effective"))
                issued = iso_date(_value(metadata, "dc.date.issued"))
                title = _value(metadata, "dc.title") or item.get("name", "")
                if not title:
                    continue
                candidates.append((effective or issued or "1900-01-01", item))
            candidates.sort(key=lambda pair: pair[0], reverse=True)

            keep = candidates if full else candidates[:limit]
            jobs: list[tuple[str, Path]] = []
            records: dict[Path, dict] = {}
            for _, item in keep:
                if item["id"] in seen:
                    continue
                seen.add(item["id"])
                metadata = item.get("metadata") or {}
                effective = iso_date(_value(metadata, "cla.date.effective")) or iso_date(
                    _value(metadata, "dc.date.issued")
                )
                expiry = iso_date(_value(metadata, "cla.date.expiration"))
                union = _value(metadata, "cla.union")
                local = _value(metadata, "cla.union.local")
                if local and local.lower() not in union.lower():
                    union = f"{union} Local {local}"
                employer = _value(metadata, "cla.employer")
                title = _value(metadata, "dc.title") or item.get("name", "")
                suffix = effective[:4] if effective else "undated"
                filename = safe_name(f"{union or 'cba'}_{employer or title}_{suffix}")
                filename = f"{filename}_{item['id'][:6]}.pdf"
                dest = root / "scraped" / "cornell" / label / filename
                try:
                    pdf_url = _pdf_url(http, item["id"])
                except Exception:
                    totals["failed"] += 1
                    continue
                if not pdf_url:
                    totals["failed"] += 1
                    continue
                jobs.append((pdf_url, dest))
                records[dest] = {
                    "file": str(dest.relative_to(root)),
                    "title": clean(title),
                    "url": _value(metadata, "dc.identifier.uri"),
                    "source": "cornell",
                    "union": union,
                    "employer": employer,
                    "sector": SECTOR_MAP.get(
                        _value(metadata, "cla.sector").lower(),
                        "public" if label == "perb" else "other",
                    ),
                    "jurisdiction": state_from_location(_value(metadata, "cla.location")),
                    "effective": effective,
                    "expiry": expiry,
                    "notes": f"Cornell eCommons · {label}",
                }
            totals["kept"] += len(jobs)

            def record(url: str, path: Path, ok: bool, records=records) -> None:
                if ok and path in records:
                    manifest.add(records[path])

            ok, failed = fetch_many(
                http, jobs, concurrency=concurrency, on_done=record
            )
            totals["downloaded"] += ok
            totals["failed"] += failed
            if progress:
                progress(label, ok, failed, len(jobs))
    manifest.flush()
    return totals
