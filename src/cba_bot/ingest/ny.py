"""New York contract sources: SeeThroughNY, OER state contracts, NYC OLR."""

from __future__ import annotations

import re
from pathlib import Path

from bs4 import BeautifulSoup

from cba_bot.ingest.common import (
    Manifest,
    clean,
    client,
    fetch_many,
    safe_name,
)

FIREFOX = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"

# OER's Cloudflare rule blocks Chrome UAs but serves direct files to no/Firefox UA.
OER_CONTRACTS = [
    ("CSEA Administrative Services Unit (ASU)", "CSEA", "State of New York", "2021-2026",
     "https://oer.ny.gov/system/files/documents/2026/10/2021-2026-asu-agreement.pdf"),
    ("CSEA Operational Services Unit (OSU)", "CSEA", "State of New York", "2021-2026",
     "https://oer.ny.gov/system/files/documents/2026/10/2021-2026-osu.pdf"),
    ("CSEA Institutional Services Unit (ISU)", "CSEA", "State of New York", "2021-2026",
     "https://oer.ny.gov/system/files/documents/2023/11/2021-2026-isu-agreement_0.pdf"),
    ("CSEA Division of Military and Naval Affairs (DMNA)", "CSEA", "State of New York", "2021-2026",
     "https://oer.ny.gov/system/files/documents/2023/11/2021-2026-dmna-agreement_0.pdf"),
    ("CWA/GSEU Graduate Student Negotiating Unit (GSNU)", "CWA/GSEU", "State University of New York", "2023-2026",
     "https://oer.ny.gov/system/files/documents/2025/10/2023-2026-graduate-student-negotiating-unit-gsnu-contract-agreement_0.pdf"),
    ("Council 82 Security Services Unit (SSU)", "Council 82", "State of New York", "2016-2023",
     "https://oer.ny.gov/system/files/documents/2024/11/2016-2023-sspu-contract-agreement.pdf"),
    ("DC-37 Rent Regulation Services Unit (RRSU)", "DC 37", "State of New York", "2016-2021",
     "https://oer.ny.gov/system/files/documents/2019/04/rrsu-dc-37-2016-21-final.pdf"),
    ("NYSCOPBA Security Services Unit (SSU)", "NYSCOPBA", "State of New York", "2023-2026",
     "https://oer.ny.gov/system/files/documents/2024/09/2023-2026-security-services-unit-contract-agreement.pdf"),
    ("NYSPIA Bureau of Criminal Investigation (BCI)", "NYSPIA", "State of New York", "2018-2023",
     "https://oer.ny.gov/system/files/documents/2021/10/nyspia-moa_2018-2023.pdf"),
    ("PBA Commissioned and Non-Commissioned Officers", "PBA", "State of New York", "2018-2023",
     "https://oer.ny.gov/system/files/documents/2021/10/pba_co-nco-moa_04.01.18-03.31.23.pdf"),
    ("PBA Troopers", "PBA", "State of New York", "2018-2023",
     "https://oer.ny.gov/system/files/documents/2021/10/troopers-unit-moa_2018-2023.pdf"),
    ("PBANYS Agency Police Services Unit (APSU)", "PBANYS", "State of New York", "2023-2026",
     "https://oer.ny.gov/system/files/documents/2025/09/2023-2026-apsu-contract-agreement.pdf"),
    ("PEF Professional, Scientific and Technical Services (PS&T)", "PEF", "State of New York", "2023-2026",
     "https://oer.ny.gov/system/files/documents/2024/05/2023-2026-cba-final-edition.pdf"),
    ("PEF Professional, Scientific and Technical Services (PS&T), prior term", "PEF", "State of New York", "2019-2023",
     "https://oer.ny.gov/system/files/documents/2022/01/2019-to-2023-state-and-pef.pdf"),
    ("UUP Professional Services Negotiating Unit (PSNU)", "UUP", "State University of New York", "2022-2026",
     "https://oer.ny.gov/system/files/documents/2024/06/2022-26-state.uup-agreement-final_pep-updated-5.30.2024.pdf"),
]


def _record(root: Path, dest: Path, **fields) -> dict:
    record = {"file": str(dest.relative_to(root)), "source": "ny"}
    record.update(fields)
    return record


def _seethroughny(http, root: Path, manifest: Manifest, limit: int, concurrency: int) -> dict:
    endpoint = "https://seethroughny.net/index.php/tools/reports/contract?action=get"
    rows: list[dict] = []
    page = 0
    result_id = "0"
    while len(rows) < limit:
        response = http.post(
            endpoint,
            data={
                "SortBy": "EndDate DESC",
                "current_page": page,
                "result_id": result_id,
            },
        )
        response.raise_for_status()
        payload = response.json()
        result_id = payload.get("result_id", result_id)
        soup = BeautifulSoup(payload.get("html", ""), "lxml")
        result_rows = soup.select("tr[id^=resultRow]")
        if not result_rows:
            break
        for result_row in result_rows:
            cells = result_row.find_all("td")
            if len(cells) < 6:
                continue
            expand = soup.select_one(f"#expandRow{result_row['id'][len('resultRow'):]}")
            unit = start = ""
            if expand:
                for label in expand.select("strong"):
                    value = label.find_parent("div")
                    value = value.find_next_sibling("div") if value else None
                    if not value:
                        continue
                    if "Bargaining Unit" in label.get_text():
                        unit = clean(value.get_text(" "))
                    elif "Start Date" in label.get_text():
                        start = clean(value.get_text(" "))
            match = re.search(r"window\.open\('([^']+\.pdf)'\)", str(result_row))
            if not match:
                continue
            employer = clean(cells[2].get_text(" "))
            contract_type = clean(cells[3].get_text(" "))
            end = clean(cells[5].get_text(" "))
            rows.append(
                {
                    "employer": employer,
                    "unit": unit,
                    "type": contract_type,
                    "start": start,
                    "end": end,
                    "url": match.group(1),
                }
            )
            if len(rows) >= limit:
                break
        page += 1

    jobs: list[tuple[str, Path]] = []
    records: dict[Path, dict] = {}
    for row in rows:
        end_year = row["end"][-4:] if row["end"] else ""
        dest = root / "scraped" / "ny" / "seethroughny" / (
            safe_name(f"{row['employer']}_{row['unit'] or row['type']}_{end_year}") + ".pdf"
        )
        jobs.append((row["url"], dest))
        effective = ""
        if row["start"]:
            month, day, year = (row["start"].split("/") + ["", "", ""])[:3]
            if month and day and year:
                effective = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        expiry = ""
        if row["end"]:
            month, day, year = (row["end"].split("/") + ["", "", ""])[:3]
            if month and day and year:
                expiry = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        sector = "education" if row["type"].lower() in {
            "teacher", "superintendent", "chancellor"
        } else "healthcare" if row["type"].lower() == "nurses" else "public"
        records[dest] = _record(
            root,
            dest,
            title=clean(f"{row['employer']} — {row['unit'] or row['type']}"),
            url=row["url"],
            union=row["unit"],
            employer=row["employer"],
            sector=sector,
            jurisdiction="NY",
            effective=effective,
            expiry=expiry,
            notes=f"SeeThroughNY · {row['type']}",
        )

    def done(url: str, path: Path, ok: bool) -> None:
        if ok and path in records:
            manifest.add(records[path])

    ok, failed = fetch_many(http, jobs, concurrency=concurrency, on_done=done)
    return {"listed": len(rows), "downloaded": ok, "failed": failed}


def _oer(http, root: Path, manifest: Manifest, concurrency: int) -> dict:
    jobs: list[tuple[str, Path]] = []
    records: dict[Path, dict] = {}
    for title, union, employer, term, url in OER_CONTRACTS:
        dest = root / "scraped" / "ny" / "oer" / (
            safe_name(f"{union}_{title}_{term}") + ".pdf"
        )
        jobs.append((url, dest))
        start, end = term.split("-")
        records[dest] = _record(
            root,
            dest,
            title=title,
            url=url,
            union=union,
            employer=employer,
            sector="public",
            jurisdiction="NY",
            effective=f"{start}-01-01",
            expiry=f"{end}-12-31",
            notes="NY OER state union contract",
        )

    def done(url: str, path: Path, ok: bool) -> None:
        if ok and path in records:
            manifest.add(records[path])

    ok, failed = fetch_many(http, jobs, concurrency=concurrency, on_done=done)
    manifest.flush()
    return {"listed": len(jobs), "downloaded": ok, "failed": failed}


def _nyc(http, root: Path, manifest: Manifest, concurrency: int, limit: int) -> dict:
    response = http.get("https://www.nyc.gov/site/olr/labor/labor-recent-agreements.page")
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")
    jobs: list[tuple[str, Path]] = []
    records: dict[Path, dict] = {}
    for anchor in soup.select('a[href$=".pdf"]')[:limit]:
        href = anchor["href"]
        url = href if href.startswith("http") else "https://www.nyc.gov" + href
        title = clean(anchor.get_text(" ")) or Path(href).stem
        year = re.findall(r"(20\d{2})", title)
        effective = f"{year[0]}-01-01" if year else ""
        expiry = f"{year[-1]}-12-31" if len(year) > 1 else ""
        dest = root / "scraped" / "ny" / "nyc" / (safe_name(title) + ".pdf")
        jobs.append((url, dest))
        records[dest] = _record(
            root,
            dest,
            title=title,
            url=url,
            union=title.split(",")[0],
            employer="City of New York",
            sector="public",
            jurisdiction="NY",
            effective=effective,
            expiry=expiry,
            notes="NYC OLR recent agreements",
        )

    def done(url: str, path: Path, ok: bool) -> None:
        if ok and path in records:
            manifest.add(records[path])

    ok, failed = fetch_many(http, jobs, concurrency=concurrency, on_done=done)
    manifest.flush()
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
    manifest = Manifest(root, "ny")
    summary = {"listed": 0, "downloaded": 0, "failed": 0}
    stny_limit = 100_000 if full else limit
    with client(user_agent=FIREFOX) as http:
        for name, fn in (
            ("seethroughny", lambda: _seethroughny(http, root, manifest, stny_limit, concurrency)),
            ("oer", lambda: _oer(http, root, manifest, concurrency)),
            ("nyc", lambda: _nyc(http, root, manifest, concurrency, 100_000 if full else limit)),
        ):
            result = fn()
            for key in summary:
                summary[key] += result.get(key, 0)
            if progress:
                progress(name, result.get("downloaded", 0), result.get("failed", 0),
                         result.get("listed", 0))
    return summary
