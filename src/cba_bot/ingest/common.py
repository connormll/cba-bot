"""Shared scraping helpers: manifests, safe downloads, naming."""

from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

import httpx

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 cba-bot/0.1"
)
STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC", "PR",
}


def safe_name(value: str, *, limit: int = 90) -> str:
    value = re.sub(r"[^\w\s.-]", "", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip(" ._-")
    return (value[:limit].rstrip(" ._-") or "contract")


def clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def state_from_location(value: str | None) -> str:
    for token in re.findall(r"\b([A-Z]{2})\b", value or ""):
        if token in STATE_CODES:
            return token
    return ""


def iso_date(value) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=UTC).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    text = str(value)
    match = re.match(r"((?:19|20)\d{2})-(\d{2})-(\d{2})", text)
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}" if match else ""


class Manifest:
    """One JSONL manifest per source, merged on rerun and rewritten atomically."""

    def __init__(self, root: Path, source: str):
        self.path = Path(root) / "manifests" / f"{source}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.records: dict[str, dict] = {}
        self._dirty = 0
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("file"):
                    self.records[record["file"]] = record

    def add(self, record: dict) -> None:
        with self.lock:
            self.records[record["file"]] = record
            self._dirty += 1
            if self._dirty >= 20:
                self._write()

    def flush(self) -> None:
        with self.lock:
            if self._dirty:
                self._write()

    def _write(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in self.records.values())
            + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.path)
        self._dirty = 0


def client(
    *, timeout: float = 90.0, verify: bool = True, user_agent: str | None = None
) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        verify=verify,
        follow_redirects=True,
        headers={"User-Agent": user_agent or USER_AGENT},
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
    )


def download(
    http: httpx.Client,
    url: str,
    dest: Path,
    *,
    attempts: int = 3,
) -> bool:
    """Download one document, skipping existing files and rejecting HTML errors."""
    if dest.exists() and dest.stat().st_size > 10_000:
        if dest.suffix != ".pdf":
            return True
        try:
            if dest.read_bytes()[:5] == b"%PDF-":
                return True
        except OSError:
            pass
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(attempts):
        try:
            with http.stream("GET", url) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if "text/html" in content_type and dest.suffix == ".pdf":
                    return False
                with open(part, "wb") as handle:
                    handle.writelines(response.iter_bytes(1 << 16))
            if part.stat().st_size < 10_000:
                part.unlink(missing_ok=True)
                continue
            if dest.suffix == ".pdf" and part.read_bytes()[:5] != b"%PDF-":
                part.unlink(missing_ok=True)
                return False
            part.replace(dest)
            return True
        except (httpx.HTTPError, OSError):
            part.unlink(missing_ok=True)
            if attempt == attempts - 1:
                return False
    return False


def fetch_many(
    http: httpx.Client,
    jobs: list[tuple[str, Path]],
    *,
    concurrency: int = 6,
    on_done=None,
) -> tuple[int, int]:
    ok = failed = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(download, http, url, path): (url, path) for url, path in jobs
        }
        for future in as_completed(futures):
            if future.result():
                ok += 1
                if on_done:
                    on_done(*futures[future], True)
            else:
                failed += 1
                if on_done:
                    on_done(*futures[future], False)
    return ok, failed
