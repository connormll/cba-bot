"""eScholarship UC IIR labor contracts (public GraphQL API)."""

from __future__ import annotations

import re
from pathlib import Path

from cba_bot.catalog import infer_term
from cba_bot.ingest.common import (
    Manifest,
    clean,
    client,
    fetch_many,
    safe_name,
)

ENDPOINT = "https://escholarship.org/graphql"
UNIT = "iir_library_contracts"
FIREFOX = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
QUERY = """
query($more: String) {
  unit(id: "iir_library_contracts") {
    items(first: 100, more: $more) {
      total
      more
      nodes { id title permalink published contentLink }
    }
  }
}
"""


def _items(http) -> list[dict]:
    items: list[dict] = []
    cursor = None
    while True:
        response = http.post(ENDPOINT, json={"query": QUERY, "variables": {"more": cursor}})
        response.raise_for_status()
        data = response.json().get("data", {}).get("unit", {}).get("items", {})
        items.extend(data.get("nodes") or [])
        cursor = data.get("more")
        if not cursor or len(items) >= int(data.get("total") or 0):
            break
    return items


def _parties(title: str) -> tuple[str, str]:
    match = re.search(
        r"between\s+(.+?)\s+and\s+(.+?)(?:\s*[,(]?\s*(?:19|20)\d{2}|$)",
        title,
        re.IGNORECASE,
    )
    if not match:
        return "", ""
    employer = clean(match.group(1))
    union = clean(match.group(2))
    # Titles often jam the start date onto the union ("AssociationJ uly 1").
    union = re.split(
        r"(?i)\s*(?:j\s*anuary|f\s*ebruary|m\s*arch|a\s*pril|m\s*ay|j\s*une|"
        r"j\s*uly|a\s*ugust|s\s*eptember|o\s*ctober|n\s*ovember|d\s*ecember)\b",
        union,
    )[0]
    for prefix in ("the ", "THE "):
        employer = employer.removeprefix(prefix)
    for prefix in ("the ", "THE "):
        union = union.removeprefix(prefix)
    return clean(union).rstrip(",- "), clean(employer).rstrip(",- ")


def scrape(
    root: Path,
    *,
    limit: int = 250,
    full: bool = False,
    concurrency: int = 6,
    progress=None,
) -> dict:
    root = Path(root)
    manifest = Manifest(root, "escholarship")
    with client(user_agent=FIREFOX) as http:
        items = _items(http)
        items.sort(key=lambda item: str(item.get("published") or ""), reverse=True)
        selected = items if full else items[:limit]
        jobs: list[tuple[str, Path]] = []
        records: dict[Path, dict] = {}
        for item in selected:
            url = item.get("contentLink") or ""
            if not url:
                continue
            title = clean(item.get("title"))
            union, employer = _parties(title)
            effective, expiry = infer_term(title)
            dest = root / "scraped" / "escholarship" / (
                safe_name(title) + f"_{item['id'].rsplit('/', 1)[-1]}.pdf"
            )
            jobs.append((url, dest))
            records[dest] = {
                "file": str(dest.relative_to(root)),
                "title": title,
                "url": item.get("permalink") or url,
                "source": "escholarship",
                "union": union,
                "employer": employer,
                "sector": "public",
                "jurisdiction": "CA",
                "effective": effective.isoformat() if effective else "",
                "expiry": expiry.isoformat() if expiry else "",
                "notes": "eScholarship UC IIR Library Contracts",
            }

        def done(url: str, path: Path, ok: bool) -> None:
            if ok and path in records:
                manifest.add(records[path])

        ok, failed = fetch_many(http, jobs, concurrency=concurrency, on_done=done)
        if progress:
            progress("escholarship", ok, failed, len(jobs))
    manifest.flush()
    return {"listed": len(items), "kept": len(jobs), "downloaded": ok, "failed": failed}
