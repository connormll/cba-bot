"""Contract metadata: manifests first, filename inference second.

Scrapers write one JSONL manifest per source under ``contracts/manifests/``.
Every record is keyed by the contract's path relative to ``contracts/``.  Files
without a record still get indexed; their term, union, sector, and state are
inferred from the filename and folder.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

DOC_SUFFIXES = {".pdf", ".docx"}
_FOLDER_HINTS = ("expired", "archive")

_UNIONS = (
    "AFSCME", "AFTSU", "AFT", "AAUP", "ACE", "ACLU", "AFGE", "APEA", "BTU",
    "CFT", "CNA", "CSEA", "CWA", "DC 37", "DC37", "IAFF", "IAMAW", "IBEW",
    "IFPTE", "IUOE", "LIUNA", "MNA", "NEA", "NUHW", "NYSNA", "NYSUT", "ONA",
    "ONEA", "OPEIU", "PEF", "PSEA", "SEIU", "UAW", "UFCW", "UFT", "UMMAP",
    "USW", "WSNA", "Teamsters", "Laborers", "NewsGuild", "Guild",
)

# Longest / most specific first.
_STATES = (
    ("new york", "NY"), ("california", "CA"), ("washington", "WA"),
    ("oregon", "OR"), ("colorado", "CO"), ("minnesota", "MN"),
    ("michigan", "MI"), ("connecticut", "CT"), ("massachusetts", "MA"),
    ("illinois", "IL"), ("ohio", "OH"), ("pennsylvania", "PA"),
    ("missouri", "MO"), ("alaska", "AK"), ("new jersey", "NJ"),
    ("nysna", "NY"), ("nysut", "NY"), ("csea", "NY"), ("pef", "NY"),
    ("uup", "NY"), ("dc37", "NY"), ("dc 37", "NY"), ("uft", "NY"),
    ("ona", "OH"), ("onea", "OH"), ("cna", "CA"), ("wsna", "WA"),
    ("mna", "MN"), ("psea", "PA"), ("cft", "CA"), ("btu", "MA"),
    ("nuhew", "CA"), ("nuhw", "CA"), ("seiu 1000", "CA"), ("seiu 521", "CA"),
    ("seiu 721", "CA"), ("seiu 1021", "CA"), ("seiu 1199nw", "WA"),
    ("seiu 503", "OR"), ("seiu 509", "WA"), ("seiu 105", "IL"),
    ("seiu cir", "CA"), ("seiu uhw", "CA"), ("ufcw 3000", "WA"),
    ("ofnhp", "OR"), ("pnwsu", "WA"), ("colorado wins", "CO"),
    ("cowins", "CO"), ("aft michigan", "MI"), ("aft connecticut", "CT"),
    ("aclu-mo", "MO"), ("apea-aft", "AK"), ("ummap", "MI"),
    ("newspaper guild", "WA"), ("washington newspaper guild", "WA"),
    ("seiu usww", "CA"), ("usww", "CA"), ("mjsu", "MO"),
)

_SECTORS = (
    ("federal", ("federal", "opm", "u.s. government", "va ", "dod", "treasury",
                 "irs", "tsa", "usda", "epa", "hhs", "ssa", "gsa", "afge")),
    ("healthcare", ("nurse", "nursing", "health", "hospital", "medical",
                    "clinic", "kaiser", "nysna", "cna", "wsna", "mna", "uhw",
                    "ofnhp", "ona", "onea", "nuhw", "seiu uhw")),
    ("education", ("school", "teacher", "faculty", "university", "college",
                   "education", "aft", "nysut", "psea", "cft", "aaup", "btu",
                   "academic", "student", "ummap")),
    ("public", ("state", "city", "county", "municipal", "public", "government",
                "transit", "police", "fire", "authority", "district", "sesu")),
    ("nonprofit", ("nonprofit", "non-profit", "council", "institute", "museum",
                   "legal aid", "aclu", "association")),
)

_RANGE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})\s*(?:-|–|—|to)\s*((?:19|20)\d{2}|(?:\d{2}))(?!\d)",
    re.IGNORECASE,
)
_FULL_DATE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)"
)
_SHORT_DATE = re.compile(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](\d{2})(?!\d)")
_YEAR = re.compile(r"(?<!\d)((?:19[6-9]|20[0-3])\d)(?!\d)")


@dataclass
class Contract:
    id: str
    path: Path
    rel: str
    title: str
    url: str = ""
    source: str = "local"
    union_name: str = ""
    employer: str = ""
    sector: str = "other"
    jurisdiction: str = ""
    effective: date | None = None
    expiry: date | None = None
    expired_hint: bool = False
    notes: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def status(self) -> str:
        today = date.today()
        if self.expired_hint and (self.expiry is None or self.expiry < today):
            return "expired"
        if self.expiry and self.expiry < today:
            return "expired"
        if self.effective and self.effective > today:
            return "upcoming"
        if self.effective and (self.expiry is None or self.expiry >= today):
            return "current"
        if self.expiry and self.expiry >= today:
            return "current"
        return "undated"

    @property
    def term(self) -> str:
        if self.effective and self.expiry:
            return f"{self.effective.year}–{self.expiry.year}"
        if self.effective:
            return f"{self.effective.year}"
        if self.expiry:
            return f"through {self.expiry.year}"
        return "undated"

    def recency_weight(self, today: date | None = None) -> float:
        """Bounded prior applied to fusion scores.

        Current agreements start near 1.0 and decay slowly; expired agreements
        remain retrievable but must be clearly more on-point to outrank a
        current one.  Undated documents sit in the middle-low band.
        """
        today = today or date.today()
        if self.expiry and self.expiry < today:
            return 0.40
        if self.expired_hint:
            return 0.45
        if self.effective and self.effective > today:
            return 0.90
        if self.effective:
            age_years = max(0.0, (today - self.effective).days / 365.25)
            return 0.75 + 0.25 * (0.5 ** (age_years / 4.0))
        if self.expiry and self.expiry >= today:
            return 0.85
        return 0.62

    def recency_sort_key(self) -> tuple:
        # Current agreements come first regardless of how new an expired
        # agreement's end date is; within a status, newest anchor wins.
        priority = {"current": 3, "upcoming": 2, "undated": 1, "expired": 0}
        anchor = self.effective or self.expiry or date.min
        return (priority.get(self.status, 0), anchor, self.expiry or date.min)


def _clean_title(stem: str) -> str:
    text = stem.replace("_", " ").replace("  ", " ").strip(" -_.")
    return re.sub(r"\s+", " ", text)


def _parse_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    match = re.match(r"((?:19|20)\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            pass
    match = re.match(r"((?:19|20)\d{2})[-/.](\d{1,2})\b", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), 1)
        except ValueError:
            pass
    match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", text)
    if match:
        return date(int(match.group(1)), 1, 1)
    return None


def infer_term(text: str) -> tuple[date | None, date | None]:
    ranges = []
    for match in _RANGE.finditer(text):
        start = int(match.group(1))
        end_raw = match.group(2)
        end = int(end_raw) if len(end_raw) == 4 else int(f"{str(start)[:2]}{end_raw}")
        if 1960 <= start <= 2035 and start <= end <= start + 15:
            ranges.append((start, end))
    if ranges:
        start, end = ranges[-1]
        return date(start, 1, 1), date(end, 12, 31)

    full = _FULL_DATE.search(text)
    if full:
        try:
            start = date(int(full.group(1)), int(full.group(2)), int(full.group(3)))
            # A single date is usually a signing/effective date; assume a
            # conventional three-year term instead of calling it current forever.
            return start, date(start.year + 3, 12, 31)
        except ValueError:
            pass

    short = _SHORT_DATE.search(text)
    if short:
        try:
            month, day, yy = (int(part) for part in short.groups())
            year = 2000 + yy if yy < 70 else 1900 + yy
            start = date(year, month, day)
            return start, date(start.year + 3, 12, 31)
        except ValueError:
            pass

    years = [int(m.group(1)) for m in _YEAR.finditer(text)]
    if years:
        start = date(years[-1], 1, 1)
        return start, date(start.year + 3, 12, 31)
    return None, None


def _infer_union(text: str) -> str:
    upper = text.upper()
    for name in _UNIONS:
        if re.search(rf"(?<![A-Z]){re.escape(name.upper())}(?![A-Z])", upper):
            return name
    return ""


def _infer_state(text: str) -> str:
    lowered = text.lower()
    for token, code in _STATES:
        if token in lowered:
            return code
    return ""


def _infer_sector(text: str, union_name: str) -> str:
    lowered = text.lower()
    for sector, tokens in _SECTORS:
        if any(token in lowered for token in tokens):
            return sector
    if union_name in {"AFGE"}:
        return "federal"
    return "other"


def _infer_expired(rel: str) -> bool:
    parts = {part.lower() for part in Path(rel).parts[:-1]}
    return any(hint in part for part in parts for hint in _FOLDER_HINTS)


def infer_contract(path: Path, root: Path, record: dict | None = None) -> Contract:
    rel = str(path.relative_to(root))
    record = record or {}
    stem = _clean_title(path.stem)
    title = str(record.get("title") or stem).strip()
    union_name = str(
        record.get("union") or record.get("union_name") or _infer_union(stem)
    ).strip()
    sector = str(record.get("sector") or _infer_sector(f"{stem} {union_name}", union_name))
    if "jurisdiction" in record or "state" in record:
        jurisdiction = str(
            record.get("jurisdiction") or record.get("state") or ""
        ).strip().upper()
    else:
        jurisdiction = _infer_state(stem).strip().upper()
    effective = _parse_date(record.get("effective") or record.get("start"))
    expiry = _parse_date(record.get("expiry") or record.get("end"))
    expired_hint = bool(record.get("expired")) or _infer_expired(rel)
    notes = str(record.get("notes") or "")
    if effective is None and expiry is None:
        effective, expiry = infer_term(stem)
    elif expiry is None:
        _, inferred_expiry = infer_term(stem)
        expiry = inferred_expiry
    lowered = stem.lower()
    if "proposal" in lowered or "redline" in lowered:
        note = "proposal/redline, not necessarily an executed agreement"
        notes = f"{notes} · {note}".strip(" ·") if notes else note
    return Contract(
        id=hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16],
        path=path,
        rel=rel,
        title=title,
        url=str(record.get("url") or ""),
        source=str(record.get("source") or "local"),
        union_name=union_name,
        employer=str(record.get("employer") or ""),
        sector=sector or "other",
        jurisdiction=jurisdiction,
        effective=effective,
        expiry=expiry,
        expired_hint=expired_hint,
        notes=notes,
        extra={k: v for k, v in record.items() if k not in _KNOWN_RECORD_KEYS},
    )


_KNOWN_RECORD_KEYS = {
    "file", "path", "title", "url", "source", "union", "union_name", "employer",
    "sector", "jurisdiction", "state", "effective", "start", "expiry", "end",
    "expired", "notes",
}


def load_manifest(root: Path) -> dict[str, dict]:
    """Return manifest records keyed by path relative to ``contracts/``."""
    records: dict[str, dict] = {}
    manifest_dir = root / "manifests"
    if not manifest_dir.exists():
        return records
    for path in sorted(manifest_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            rel = str(record.get("file") or record.get("path") or "").lstrip("./")
            if rel:
                record.setdefault("source", path.stem)
                records[rel] = record
    return records


def discover_contracts(root: Path, limit: int | None = None) -> list[Contract]:
    root = root.resolve()
    records = load_manifest(root)
    paths = [
        path
        for path in root.rglob("*")
        if path.suffix.lower() in DOC_SUFFIXES
        and path.is_file()
        and "manifests" not in path.parts
        and path.stat().st_size > 0
    ]
    contracts = [
        infer_contract(path, root, records.get(str(path.relative_to(root))))
        for path in paths
    ]
    contracts.sort(key=lambda c: c.recency_sort_key(), reverse=True)
    return contracts[:limit] if limit else contracts


def corpus_stats(contracts: list[Contract]) -> dict:
    def tally(attr: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for contract in contracts:
            key = getattr(contract, attr) or "unknown"
            out[key] = out.get(key, 0) + 1
        return dict(sorted(out.items(), key=lambda item: -item[1]))

    return {
        "contracts": len(contracts),
        "status": tally("status"),
        "sector": tally("sector"),
        "jurisdiction": tally("jurisdiction"),
        "sources": tally("source"),
    }
