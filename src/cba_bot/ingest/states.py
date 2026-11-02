"""Additional state collections with static PDF listings (Washington OFM)."""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup

from cba_bot.catalog import infer_term
from cba_bot.ingest.common import (
    Manifest,
    clean,
    client,
    fetch_many,
    safe_name,
)

WA_PAGE = "https://ofm.wa.gov/hr-policies/labor-relations/collective-bargaining-agreements/"


def _washington(http, root: Path, manifest: Manifest, concurrency: int, limit: int) -> dict:
    response = http.get(WA_PAGE)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")
    jobs: list[tuple[str, Path]] = []
    records: dict[Path, dict] = {}
    seen: set[str] = set()
    for item in soup.select(".item"):
        anchor = item.select_one('a[href$=".pdf"]')
        if not anchor:
            continue
        url = anchor["href"]
        if "ofm.wa.gov" not in url:
            continue
        title = clean(item.get_text(" ")).replace("Download agreement", "").strip()
        if not title or url in seen or "FAQ" in title:
            continue
        seen.add(url)
        effective, expiry = infer_term(title)
        dest = root / "scraped" / "wa" / (safe_name(title) + ".pdf")
        jobs.append((url, dest))
        records[dest] = {
            "file": str(dest.relative_to(root)),
            "title": title,
            "url": url,
            "source": "wa",
            "union": "",
            "employer": "State of Washington",
            "sector": "public",
            "jurisdiction": "WA",
            "effective": effective.isoformat() if effective else "",
            "expiry": expiry.isoformat() if expiry else "",
            "notes": "Washington OFM collective bargaining agreements",
        }
        if len(jobs) >= limit:
            break

    def done(url: str, path: Path, ok: bool) -> None:
        if ok and path in records:
            manifest.add(records[path])

    ok, failed = fetch_many(http, jobs, concurrency=concurrency, on_done=done)
    return {"listed": len(jobs), "downloaded": ok, "failed": failed}


def scrape(
    root: Path,
    *,
    limit: int = 250,
    full: bool = False,
    concurrency: int = 6,
    progress=None,
) -> dict:
    root = Path(root)
    manifest = Manifest(root, "states")
    with client() as http:
        result = _washington(http, root, manifest, concurrency, 10_000 if full else limit)
        if progress:
            progress("washington", result["downloaded"], result["failed"], result["listed"])
    manifest.flush()
    return result
