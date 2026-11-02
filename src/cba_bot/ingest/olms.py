"""DOL OLMS OPDR CBA search (undocumented servlets, public data)."""

from __future__ import annotations

import json
import ssl
from pathlib import Path

import certifi

from cba_bot.ingest.common import (
    Manifest,
    clean,
    client,
    fetch_many,
    iso_date,
    safe_name,
    state_from_location,
)

BASE = "https://olmsapps.dol.gov/olpdr"


def scrape(
    root: Path,
    *,
    limit: int = 250,
    full: bool = False,
    concurrency: int = 6,
    progress=None,
) -> dict:
    root = Path(root)
    manifest = Manifest(root, "olms")
    # The server omits its Entrust intermediate.  Add the published
    # intermediate to the certifi roots instead of disabling verification.
    context = ssl.create_default_context(cafile=certifi.where())
    context.load_verify_locations(
        cafile=str(Path(__file__).with_name("dol_intermediate.pem"))
    )
    with client(verify=context) as http:
        response = http.post(f"{BASE}/GetCBAFilerListServlet", json={"clearCache": True})
        response.raise_for_status()
        payload = json.loads(response.content.decode("latin-1"))
        rows = payload.get("filerList") or []
        rows.sort(key=lambda row: row.get("expDate") or 0, reverse=True)
        selected = rows if full else rows[:limit]

        jobs: list[tuple[str, Path]] = []
        records: dict[Path, dict] = {}
        for row in selected:
            expiry = iso_date(row.get("expDate"))
            employer = clean(row.get("empName"))
            union = clean(row.get("unionName"))
            if not employer and not union:
                continue
            local = clean(row.get("unionLocalNum"))
            if local and local.lower() not in union.lower():
                union = f"{union} Local {local}"
            title = clean(f"{employer} — {union}")[:180]
            dest = root / "scraped" / "olms" / (
                safe_name(f"{employer}_{union}_{expiry[:4]}") + f"_{row['docId']}.pdf"
            )
            jobs.append((f"{BASE}/GetAttachmentServlet?docId={row['docId']}", dest))
            records[dest] = {
                "file": str(dest.relative_to(root)),
                "title": title,
                "url": f"{BASE}/GetAttachmentServlet?docId={row['docId']}",
                "source": "olms",
                "union": union,
                "employer": employer,
                "sector": "public" if str(row.get("type", "")).upper() == "PUBLIC" else "private",
                "jurisdiction": state_from_location(row.get("location")),
                "expiry": expiry,
                "notes": f"OLMS CBA search · NAICS {row.get('naics', '')}",
            }

        def record(url: str, path: Path, ok: bool) -> None:
            if ok and path in records:
                manifest.add(records[path])

        ok, failed = fetch_many(http, jobs, concurrency=concurrency, on_done=record)
        if progress:
            progress("olms", ok, failed, len(jobs))
    manifest.flush()
    return {
        "listed": len(rows),
        "kept": len(jobs),
        "downloaded": ok,
        "failed": failed,
    }
