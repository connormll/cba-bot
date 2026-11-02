"""OPM federal-sector CBA database (JSON API)."""

from __future__ import annotations

from pathlib import Path

from cba_bot.ingest.common import (
    Manifest,
    clean,
    client,
    fetch_many,
    safe_name,
)

API = "https://www.opm.gov/cba/api/documents/published"


def _search(http, page: int, size: int = 100) -> dict:
    response = http.post(
        API,
        json={
            "sortBy": "ExpirationDateDesc",
            "agencyIds": [],
            "subAgencyNames": [],
            "activityOfficeRegions": [],
            "laborUnionNames": [],
            "locals": [],
            "busCodes": [],
            "currentPage": page,
            "recsPerPage": size,
            "searchString": "",
        },
    )
    response.raise_for_status()
    return response.json()


def scrape(
    root: Path,
    *,
    limit: int = 250,
    full: bool = False,
    concurrency: int = 6,
    progress=None,
) -> dict:
    root = Path(root)
    manifest = Manifest(root, "opm")
    with client() as http:
        first = _search(http, 1)
        rows = list(first.get("results") or [])
        total = int(first.get("rowCount") or len(rows))
        pages = int(first.get("pageCount") or 1)
        wanted = total if full else min(limit, total)
        page = 2
        while len(rows) < wanted and page <= pages:
            rows.extend(_search(http, page).get("results") or [])
            page += 1
        rows = rows[:wanted]

        jobs: list[tuple[str, Path]] = []
        records: dict[Path, dict] = {}
        for row in rows:
            agency = clean(row.get("agencyName"))
            component = clean(row.get("subAgencyOrComponent"))
            activity = clean(row.get("activityOfficeRegion"))
            union = clean(row.get("laborUnionName"))
            local = clean(row.get("local"))
            if local and local.lower() not in union.lower():
                union = f"{union} Local {local}"
            employer = " — ".join(part for part in (agency, component, activity) if part)
            expiry = str(row.get("expirationDate") or "")[:10]
            title = clean(f"{employer} — {union}")[:180]
            dest = root / "scraped" / "opm" / (
                safe_name(f"{agency}_{union}_{expiry[:4]}") + f"_{row['id'][:6]}.pdf"
            )
            jobs.append((row["fileUrl"], dest))
            records[dest] = {
                "file": str(dest.relative_to(root)),
                "title": title,
                "url": row.get("fileUrl", ""),
                "source": "opm",
                "union": union,
                "employer": employer,
                "sector": "federal",
                "jurisdiction": "",
                "expiry": expiry,
                "notes": f"OPM federal CBA database · {row.get('fileName', '')}",
            }

        def record(url: str, path: Path, ok: bool) -> None:
            if ok and path in records:
                manifest.add(records[path])

        ok, failed = fetch_many(http, jobs, concurrency=concurrency, on_done=record)
        if progress:
            progress("opm", ok, failed, len(jobs))
    manifest.flush()
    return {
        "listed": total,
        "kept": len(jobs),
        "downloaded": ok,
        "failed": failed,
    }
