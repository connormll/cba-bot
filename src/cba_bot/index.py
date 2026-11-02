"""Extraction, chunking, SQLite + FTS5 + dense-vector storage, and retrieval."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np

from cba_bot.catalog import Contract

_TESSDATA_COMMIT = "87416418657359cb625c412a48b6e1d6d41c29bd"
_TESSDATA_SHA256 = (
    "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2"
)
_TESSDATA_URL = (
    "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/"
    f"{_TESSDATA_COMMIT}/eng.traineddata"
)
_tessdata_lock = threading.Lock()
_tessdata_ready: bool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    rel TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    union_name TEXT NOT NULL DEFAULT '',
    employer TEXT NOT NULL DEFAULT '',
    sector TEXT NOT NULL DEFAULT '',
    jurisdiction TEXT NOT NULL DEFAULT '',
    effective TEXT NOT NULL DEFAULT '',
    expiry TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'undated',
    expired INTEGER NOT NULL DEFAULT 0,
    url TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    sha TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_documents_sector ON documents(sector);
CREATE INDEX IF NOT EXISTS idx_documents_jurisdiction ON documents(jurisdiction);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    page_start INTEGER,
    page_end INTEGER,
    vec_row INTEGER
);
CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text)
    VALUES ('delete', old.id, old.text);
END;
"""

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+(?=[A-Z0-9(\[\"'])")
_WORD = re.compile(r"[A-Za-z0-9]+(?:['\u2019][A-Za-z]+)?")

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "can",
    "could", "did", "do", "does", "for", "from", "had", "has", "have", "how",
    "if", "in", "into", "is", "it", "its", "may", "might", "not", "of", "on",
    "or", "should", "that", "the", "their", "them", "then", "there", "these",
    "they", "this", "to", "was", "were", "what", "when", "where", "which",
    "who", "why", "will", "with", "would",
}

# ---------------------------------------------------------------- extraction


def _ocr_pdf_page(page) -> str:
    """OCR one rendered page.  Tesseract is already a project dependency."""
    if not shutil.which("tesseract"):
        return ""
    data_dir = _ensure_tessdata()
    with tempfile.TemporaryDirectory(prefix="cba-ocr-") as tmp:
        image = Path(tmp) / "page.png"
        page.get_pixmap(dpi=150).save(image)
        env = dict(os.environ)
        if data_dir:
            env["TESSDATA_PREFIX"] = str(data_dir)
        try:
            result = subprocess.run(
                ["tesseract", str(image), "stdout", "-l", "eng", "--psm", "3"],
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return result.stdout if result.returncode == 0 else ""


def _ensure_tessdata() -> Path | None:
    """Return a tessdata directory that contains English.

    Many distros ship tesseract without ``eng``.  Download the same pinned
    4 MB file llaw-bot uses into the user cache, once, and point tesseract at it.
    """
    global _tessdata_ready
    system = Path("/usr/share/tessdata/eng.traineddata")
    if system.exists():
        return system.parent
    with _tessdata_lock:
        if _tessdata_ready is False:
            return None
        cache = Path(
            os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
        ) / "cba-bot" / "tessdata"
        target = cache / "eng.traineddata"
        if target.exists() and target.stat().st_size > 1_000_000:
            _tessdata_ready = True
            return cache
        cache.mkdir(parents=True, exist_ok=True)
        try:
            import httpx

            digest = hashlib.sha256()
            with httpx.stream("GET", _TESSDATA_URL, follow_redirects=True, timeout=120) as r:
                r.raise_for_status()
                with open(target, "wb") as handle:
                    for block in r.iter_bytes(1 << 16):
                        handle.write(block)
                        digest.update(block)
            if digest.hexdigest() != _TESSDATA_SHA256:
                target.unlink(missing_ok=True)
                _tessdata_ready = False
                return None
        except Exception:
            target.unlink(missing_ok=True)
            _tessdata_ready = False
            return None
        _tessdata_ready = True
        return cache


def extract_pages(
    path: Path,
    *,
    ocr: bool = True,
    ocr_pages: int = 30,
    ocr_seconds: float = 45.0,
) -> list[str]:
    if path.suffix.lower() == ".docx":
        return [_extract_docx(path)]
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("PyMuPDF is required to read PDFs") from exc

    pages: list[str] = []
    ocr_budget = max(0, ocr_pages)
    deadline = time.monotonic() + max(0.0, ocr_seconds)
    with pymupdf.open(path) as pdf:
        for page in pdf:
            text = page.get_text("text")
            if (
                ocr
                and ocr_budget > 0
                and time.monotonic() < deadline
                and len(text.strip()) < 40
                and _looks_scanned(page)
            ):
                ocr_text = _ocr_pdf_page(page)
                ocr_budget -= 1
                if len(ocr_text.strip()) > len(text.strip()):
                    text = ocr_text
            pages.append(text)
    return pages


def _looks_scanned(page) -> bool:
    try:
        return len(page.get_text("words")) < 10
    except Exception:
        return True


def _extract_docx(path: Path) -> str:
    try:
        import docx
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("python-docx is required to read .docx files") from exc
    document = docx.Document(str(path))
    blocks = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            blocks.append(" | ".join(cell.text for cell in row.cells))
    return "\n\n".join(blocks)


def clean_text(text: str) -> str:
    text = (
        text.replace("\x00", " ")
        .replace("\u00ad", "")
        .replace("\ufeff", "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    text = re.sub(r"(\w)-\s*\n\s*([a-z])", r"\1\2", text)
    text = re.sub(r"(?im)^\s*docusign envelope id:.*$\n?", "", text)
    text = re.sub(
        r"(?i)docusign envelope id:\s*[0-9a-f]{8}-[0-9a-f-]{20,}", "", text
    )
    lines = [line.strip() for line in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if not line:
            if out and out[-1] != "":
                out.append("")
            continue
        out.append(line)
    text = "\n".join(out)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ------------------------------------------------------------------ chunking


def _split_long(paragraph: str, target: int) -> list[str]:
    if len(paragraph) <= target:
        return [paragraph]
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(paragraph) if s.strip()]
    if len(sentences) <= 1:
        return [paragraph[i : i + target] for i in range(0, len(paragraph), target)]
    pieces: list[str] = []
    buffer = ""
    for sentence in sentences:
        if len(sentence) > target:
            if buffer:
                pieces.append(buffer)
                buffer = ""
            pieces.extend(
                sentence[i : i + target] for i in range(0, len(sentence), target)
            )
            continue
        if buffer and len(buffer) + 1 + len(sentence) > target:
            pieces.append(buffer)
            buffer = sentence
        else:
            buffer = f"{buffer} {sentence}".strip()
    if buffer:
        pieces.append(buffer)
    return pieces


@dataclass
class Chunk:
    text: str
    page_start: int | None = None
    page_end: int | None = None


def chunk_pages(
    pages: list[str], *, target: int = 1800, overlap: int = 250
) -> list[Chunk]:
    units: list[tuple[int, str]] = []
    for page_number, page_text in enumerate(pages, start=1):
        text = (page_text or "").strip()
        if not text:
            continue
        for paragraph in re.split(r"\n\s*\n", text):
            paragraph = paragraph.strip()
            if paragraph:
                units.extend(
                    (page_number, piece) for piece in _split_long(paragraph, target)
                )

    chunks: list[Chunk] = []
    buffer: list[tuple[int, str]] = []
    length = 0
    start_page: int | None = None
    emitted = False
    for page_number, unit in units:
        if emitted and length + len(unit) + 1 > target:
            chunks.append(
                Chunk(
                    text=" ".join(piece for _, piece in buffer).strip(),
                    page_start=start_page,
                    page_end=buffer[-1][0],
                )
            )
            tail: list[tuple[int, str]] = []
            tail_len = 0
            for page, piece in reversed(buffer):
                if tail_len + len(piece) + 1 > overlap:
                    break
                tail.insert(0, (page, piece))
                tail_len += len(piece) + 1
            if len(tail) == len(buffer):
                tail = []
            buffer = tail
            length = sum(len(piece) + 1 for _, piece in buffer)
            start_page = buffer[0][0] if buffer else None
            emitted = False
        if not buffer:
            start_page = page_number
        buffer.append((page_number, unit))
        length += len(unit) + 1
        emitted = True
    if buffer:
        chunks.append(
            Chunk(
                text=" ".join(piece for _, piece in buffer).strip(),
                page_start=start_page,
                page_end=buffer[-1][0],
            )
        )
    return [chunk for chunk in chunks if len(chunk.text) >= 120]


# ------------------------------------------------------------------ embedding


class Embedder:
    def __init__(self, model_name: str):
        self.model_name = model_name
        self._model = None

    def _ensure(self):
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(model_name=self.model_name)
        return self._model

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        vectors = list(self._ensure().embed(texts, batch_size=32))
        return np.asarray(vectors, dtype=np.float32)

    def embed_query(self, query: str) -> np.ndarray:
        return self.embed([query])[0]


# ---------------------------------------------------------------------- store


@dataclass
class RetrievedChunk:
    chunk_id: int
    contract: Contract
    text: str
    page_start: int | None
    page_end: int | None


@dataclass
class Source:
    index: int
    contract: Contract
    chunks: list[RetrievedChunk] = field(default_factory=list)
    score: float = 0.0

    @property
    def excerpt(self) -> str:
        return self.chunks[0].text if self.chunks else ""


def _contract_from_row(row: sqlite3.Row) -> Contract:
    effective = date.fromisoformat(row["effective"]) if row["effective"] else None
    expiry = date.fromisoformat(row["expiry"]) if row["expiry"] else None
    return Contract(
        id=row["id"],
        path=Path(row["rel"]),
        rel=row["rel"],
        title=row["title"],
        url=row["url"],
        source=row["source"],
        union_name=row["union_name"],
        employer=row["employer"],
        sector=row["sector"],
        jurisdiction=row["jurisdiction"],
        effective=effective,
        expiry=expiry,
        expired_hint=bool(row["expired"]),
    )


def _fts_tokens(query: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for match in _WORD.finditer(query):
        token = match.group(0)
        lowered = token.lower()
        if len(lowered) < 2 or lowered in STOPWORDS or lowered in seen:
            continue
        seen.add(lowered)
        tokens.append(token.replace('"', ""))
        if len(tokens) >= 32:
            break
    return tokens


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "cba.db"
        self.vectors_path = self.root / "vectors.bin"
        self.meta_path = self.root / "vectors.json"
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA cache_size=-65536")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._vector_cache: np.ndarray | None = None
        self._cache_key: tuple[int, int] | None = None
        self._reconcile_vectors()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ------------------------------------------------------------------- docs
    def document_count(self, *, current_only: bool = False) -> int:
        where = " WHERE expired=0" if current_only else ""
        return int(
            self.conn.execute(f"SELECT COUNT(*) FROM documents{where}").fetchone()[0]
        )

    def chunk_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])

    def known_hashes(self) -> dict[str, str]:
        return {
            row["rel"]: row["sha"]
            for row in self.conn.execute("SELECT rel, sha FROM documents")
        }

    def add_document(
        self,
        contract: Contract,
        text: str,
        chunks: list[Chunk],
        vectors: np.ndarray,
        *,
        file_sha: str = "",
    ) -> None:
        vectors = np.asarray(vectors, dtype=np.float32)
        if len(vectors) != len(chunks):
            raise ValueError("vector count does not match chunk count")
        with self._lock:
            base = self.vector_count()
            if len(vectors):
                self._append_vectors(vectors)
            status = contract.status
            with self.conn:
                self.conn.execute("DELETE FROM documents WHERE id=?", (contract.id,))
                self.conn.execute(
                    "INSERT INTO documents"
                    "(id, rel, title, union_name, employer, sector, jurisdiction,"
                    " effective, expiry, status, expired, url, source, sha, text)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        contract.id,
                        contract.rel,
                        contract.title,
                        contract.union_name,
                        contract.employer,
                        contract.sector,
                        contract.jurisdiction,
                        contract.effective.isoformat() if contract.effective else "",
                        contract.expiry.isoformat() if contract.expiry else "",
                        status,
                        1 if status == "expired" else 0,
                        contract.url,
                        contract.source,
                        file_sha or hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        text,
                    ),
                )
                self.conn.executemany(
                    "INSERT INTO chunks"
                    "(document_id, ordinal, text, page_start, page_end, vec_row)"
                    " VALUES(?,?,?,?,?,?)",
                    [
                        (
                            contract.id,
                            ordinal,
                            chunk.text,
                            chunk.page_start,
                            chunk.page_end,
                            base + ordinal,
                        )
                        for ordinal, chunk in enumerate(chunks)
                    ],
                )

    def clear(self) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM documents")
            self.conn.commit()
            for path in (self.vectors_path, self.meta_path):
                path.unlink(missing_ok=True)
            self._vector_cache = None

    def delete_missing(self, rels: set[str]) -> int:
        existing = {row[0] for row in self.conn.execute("SELECT rel FROM documents")}
        missing = existing - set(rels)
        if missing:
            with self.conn:
                self.conn.executemany(
                    "DELETE FROM documents WHERE rel=?", [(rel,) for rel in missing]
                )
        return len(missing)

    def compact_vectors(self) -> int:
        """Drop orphaned vectors (from interrupted or repeated indexing runs)."""
        with self._lock:
            matrix = self._load_vectors()
            if matrix.size == 0:
                return 0
            rows = self.conn.execute(
                "SELECT id, vec_row FROM chunks ORDER BY id"
            ).fetchall()
            valid = [
                (int(row["id"]), int(row["vec_row"]))
                for row in rows
                if row["vec_row"] is not None
                and 0 <= int(row["vec_row"]) < matrix.shape[0]
            ]
            keep = [vec_row for _, vec_row in valid]
            compacted = matrix[keep] if keep else np.zeros((0, matrix.shape[1]))
            tmp = self.vectors_path.with_suffix(".compact")
            np.ascontiguousarray(compacted, dtype=np.float32).tofile(tmp)
            with self.conn:
                self.conn.execute("UPDATE chunks SET vec_row=NULL")
                self.conn.executemany(
                    "UPDATE chunks SET vec_row=? WHERE id=?",
                    [(new_row, chunk_id) for new_row, (chunk_id, _) in enumerate(valid)],
                )
            os.replace(tmp, self.vectors_path)
            self._write_meta(len(compacted), matrix.shape[1])
            self._vector_cache = None
            self._cache_key = None
            return max(0, matrix.shape[0] - len(compacted))

    def fetch_contract(self, document_id: str) -> tuple[Contract, str] | None:
        row = self.conn.execute(
            "SELECT * FROM documents WHERE id=?", (document_id,)
        ).fetchone()
        return (_contract_from_row(row), row["text"]) if row else None

    # ---------------------------------------------------------------- filters
    @staticmethod
    def _filters(
        current_only: bool, sector: str, jurisdiction: str
    ) -> tuple[str, list]:
        clauses: list[str] = []
        params: list = []
        if current_only:
            clauses.append("d.expired=0 AND (d.expiry='' OR d.expiry>=?)")
            params.append(date.today().isoformat())
        if sector and sector != "all":
            clauses.append("d.sector=?")
            params.append(sector)
        if jurisdiction and jurisdiction != "all":
            clauses.append("d.jurisdiction=?")
            params.append(jurisdiction.upper())
        return (" AND " + " AND ".join(clauses) if clauses else ""), params

    # ---------------------------------------------------------------- vectors
    def _read_meta(self) -> dict[str, int]:
        try:
            raw = json.loads(self.meta_path.read_text(encoding="utf-8"))
            return {"count": int(raw.get("count", 0)), "dim": int(raw.get("dim", 0))}
        except (OSError, ValueError):
            return {"count": 0, "dim": 0}

    def _write_meta(self, count: int, dim: int) -> None:
        tmp = self.meta_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"count": count, "dim": dim}), encoding="utf-8")
        os.replace(tmp, self.meta_path)

    def _reconcile_vectors(self) -> dict[str, int]:
        meta = self._read_meta()
        if not self.vectors_path.exists():
            if meta["count"]:
                self._write_meta(0, meta["dim"])
            self._clear_vec_rows()
            return {"count": 0, "dim": meta["dim"]}
        size = self.vectors_path.stat().st_size
        dim = meta["dim"]
        if dim <= 0:
            # The sidecar is gone/corrupt; raw vectors cannot be mapped safely.
            orphan = self.vectors_path.with_suffix(".orphan")
            try:
                os.replace(self.vectors_path, orphan)
            except OSError:
                pass
            self._write_meta(0, 0)
            self._clear_vec_rows()
            return {"count": 0, "dim": 0}
        row_bytes = dim * 4
        rows = size // row_bytes
        remainder = size - rows * row_bytes
        if remainder:
            # A crash mid-append must not shift every later vector row.
            with open(self.vectors_path, "r+b") as handle:
                handle.truncate(rows * row_bytes)
        if rows < meta["count"]:
            self.conn.execute(
                "UPDATE chunks SET vec_row=NULL WHERE vec_row >= ?", (rows,)
            )
            self.conn.commit()
        if rows != meta["count"]:
            self._write_meta(rows, dim)
        return {"count": rows, "dim": dim}

    def _clear_vec_rows(self) -> None:
        self.conn.execute("UPDATE chunks SET vec_row=NULL")
        if self.conn.in_transaction:
            self.conn.commit()

    def vector_count(self) -> int:
        with self._lock:
            return self._reconcile_vectors()["count"]

    def vector_dim(self) -> int:
        return self._read_meta()["dim"]

    def _append_vectors(self, vectors: np.ndarray) -> None:
        vectors = np.ascontiguousarray(vectors, dtype=np.float32)
        if self.vectors_path.exists() and self.vectors_path.stat().st_size > 0:
            if self._read_meta()["dim"] <= 0:
                raise RuntimeError(
                    "vectors.bin exists but vectors.json is missing/corrupt; "
                    "run `cba-bot index --rebuild`"
                )
        meta = self._reconcile_vectors()
        if meta["dim"] and meta["dim"] != vectors.shape[1]:
            raise ValueError(
                f"embedding dimension changed ({meta['dim']} -> {vectors.shape[1]}); "
                "run `cba-bot index --rebuild`"
            )
        with open(self.vectors_path, "ab") as handle:
            handle.write(vectors.tobytes())
        self._write_meta(meta["count"] + len(vectors), vectors.shape[1])

    def _load_vectors(self) -> np.ndarray:
        meta = self._read_meta()
        if not meta["count"] or not meta["dim"]:
            return np.zeros((0, 0), dtype=np.float32)
        stat = self.vectors_path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
        if self._vector_cache is not None and self._cache_key == key:
            return self._vector_cache
        data = np.fromfile(self.vectors_path, dtype=np.float32)
        usable = min(meta["count"], data.size // meta["dim"])
        self._vector_cache = data[: usable * meta["dim"]].reshape(usable, meta["dim"])
        self._cache_key = key
        return self._vector_cache

    # ---------------------------------------------------------------- searches
    def lexical_search(
        self,
        query: str,
        *,
        current_only: bool = False,
        sector: str = "all",
        jurisdiction: str = "all",
        limit: int = 80,
    ) -> list[tuple[int, float]]:
        tokens = _fts_tokens(query)
        if not tokens:
            return []
        match = " OR ".join(f'"{token}"' for token in tokens)
        clause, params = self._filters(current_only, sector, jurisdiction)
        sql = (
            "SELECT c.id, bm25(chunks_fts) AS rank FROM chunks_fts "
            "JOIN chunks c ON c.id=chunks_fts.rowid "
            "JOIN documents d ON d.id=c.document_id "
            f"WHERE chunks_fts MATCH ?{clause} ORDER BY rank LIMIT ?"
        )
        try:
            rows = self.conn.execute(sql, (match, *params, limit)).fetchall()
        except sqlite3.OperationalError as exc:
            if "fts5" in str(exc).lower() or "syntax" in str(exc).lower():
                return []
            raise
        return [(int(row["id"]), -float(row["rank"])) for row in rows]

    def vector_search(
        self,
        query_vector: np.ndarray,
        *,
        current_only: bool = False,
        sector: str = "all",
        jurisdiction: str = "all",
        limit: int = 80,
    ) -> list[tuple[int, float]]:
        query = np.asarray(query_vector, dtype=np.float32).reshape(-1)
        with self._lock:
            clause, params = self._filters(current_only, sector, jurisdiction)
            rows = self.conn.execute(
                "SELECT c.id, c.vec_row FROM chunks c JOIN documents d "
                f"ON d.id=c.document_id WHERE c.vec_row IS NOT NULL{clause}",
                params,
            ).fetchall()
            matrix = self._load_vectors()
            if not rows or matrix.size == 0 or matrix.shape[1] != query.shape[0]:
                return []
            vec_rows = np.asarray([int(r["vec_row"]) for r in rows])
            chunk_ids = np.asarray([int(r["id"]) for r in rows])
            valid = vec_rows < matrix.shape[0]
            scores = (matrix @ query)[vec_rows[valid]]
            ids = chunk_ids[valid]
        take = min(limit, scores.shape[0])
        if take == 0:
            return []
        top = (
            np.argpartition(-scores, take - 1)[:take]
            if take < scores.shape[0]
            else np.arange(scores.shape[0])
        )
        order = top[np.argsort(-scores[top])]
        return [(int(ids[i]), float(scores[i])) for i in order]

    def fetch_chunks(self, chunk_ids: Iterable[int]) -> dict[int, RetrievedChunk]:
        ids = [int(cid) for cid in chunk_ids]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            "SELECT c.id AS chunk_id, c.text AS chunk_text, c.document_id, "
            "c.page_start, c.page_end "
            "FROM chunks c "
            f"WHERE c.id IN ({placeholders})",
            ids,
        ).fetchall()
        document_ids = sorted({row["document_id"] for row in rows})
        docs: dict[str, Contract] = {}
        if document_ids:
            marks = ",".join("?" for _ in document_ids)
            for doc in self.conn.execute(
                f"SELECT * FROM documents WHERE id IN ({marks})", document_ids
            ):
                docs[doc["id"]] = _contract_from_row(doc)
        out: dict[int, RetrievedChunk] = {}
        for row in rows:
            contract = docs.get(row["document_id"])
            if contract is None:
                continue
            out[int(row["chunk_id"])] = RetrievedChunk(
                chunk_id=int(row["chunk_id"]),
                contract=contract,
                text=row["chunk_text"],
                page_start=row["page_start"],
                page_end=row["page_end"],
            )
        return out

    def stats(self) -> dict:
        by_status = {
            row["status"]: int(row["n"])
            for row in self.conn.execute(
                "SELECT status, COUNT(*) n FROM documents GROUP BY status"
            )
        }
        by_sector = {
            row["sector"]: int(row["n"])
            for row in self.conn.execute(
                "SELECT sector, COUNT(*) n FROM documents GROUP BY sector ORDER BY n DESC"
            )
        }
        return {
            "documents": self.document_count(),
            "chunks": self.chunk_count(),
            "vectors": self.vector_count(),
            "vector_dim": self.vector_dim(),
            "status": by_status,
            "sector": by_sector,
            "db_bytes": self.db_path.stat().st_size if self.db_path.exists() else 0,
            "data_dir": str(self.root),
        }


# ------------------------------------------------------------------ retrieval

RRF_K = 60
CHUNKS_PER_SOURCE = 3

# Common bargaining topics use vocabulary that differs from the asker's.
# Expand the lexical side only; the dense side already handles paraphrase.
_QUERY_EXPANSIONS = {
    "remote work": ("telework", "work from home", "home office"),
    "telework": ("remote work", "work from home"),
    "grievance": ("complaint", "dispute"),
    "arbitration": ("arbitrator", "impartial"),
    "seniority": ("length of service", "service credit"),
    "health insurance": ("medical", "health care", "benefits"),
    "parental leave": ("family leave", "maternity", "paternity"),
    "layoff": ("reduction in force", "rif", "job security"),
    "overtime": ("hours of work", "premium pay"),
    "union security": ("dues", "membership", "checkoff"),
    "no strike": ("strike", "lockout"),
    "management rights": ("employer rights", "reserved rights"),
}


def expand_query(query: str) -> str:
    lowered = query.lower()
    additions: list[str] = []
    for phrase, synonyms in _QUERY_EXPANSIONS.items():
        if phrase in lowered:
            additions.extend(synonyms)
    return f"{query} {' '.join(additions)}".strip() if additions else query


def _title_key(contract: Contract) -> str:
    title = contract.title.lower()
    title = re.sub(r"\b(?:19|20)\d{2}\b", " ", title)
    title = re.sub(r"\b(?:final|signed|interactive|updated|redline)\b", " ", title)
    title = re.sub(r"[^a-z0-9]+", " ", title).strip()
    if len(title) < 12:
        return ""
    # A generic "memorandum of agreement" must not collapse unrelated MOAs.
    identity = "|".join(
        part.lower() for part in (contract.union_name, contract.employer) if part
    )
    return f"{identity}|{title}" if identity else title


def retrieve_sources(
    store: Store,
    embedder: Embedder,
    query: str,
    *,
    top_k: int = 6,
    pool: int = 80,
    current_only: bool = False,
    sector: str = "all",
    jurisdiction: str = "all",
) -> list[Source]:
    lexical = store.lexical_search(
        expand_query(query),
        current_only=current_only,
        sector=sector,
        jurisdiction=jurisdiction,
        limit=pool,
    )
    vector = store.vector_search(
        embedder.embed_query(query),
        current_only=current_only,
        sector=sector,
        jurisdiction=jurisdiction,
        limit=pool,
    )
    fused: dict[int, float] = {}
    for ranking in (lexical, vector):
        for rank, (chunk_id, _) in enumerate(ranking, start=1):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
    if not fused:
        return []

    ranked = sorted(fused, key=lambda cid: fused[cid], reverse=True)
    fetched = store.fetch_chunks(ranked[: pool * 2])
    groups: dict[str, dict] = {}
    for chunk_id in ranked:
        chunk = fetched.get(chunk_id)
        if chunk is None:
            continue
        group = groups.setdefault(
            chunk.contract.id,
            {"contract": chunk.contract, "chunks": [], "best": 0.0},
        )
        if len(group["chunks"]) < CHUNKS_PER_SOURCE:
            group["chunks"].append((fused[chunk_id], chunk))
        group["best"] = max(group["best"], fused[chunk_id])

    for group in groups.values():
        group["rank"] = group["best"] * group["contract"].recency_weight()

    selected = sorted(
        groups.values(),
        key=lambda g: (g["rank"], g["contract"].recency_sort_key()),
        reverse=True,
    )

    # One version per contract series: if the same title appears with several
    # terms, the recency-weighted ranking already favors the newer one, and
    # keeping only that version avoids six near-identical excerpts.
    seen_titles: set[str] = set()
    sources: list[Source] = []
    for group in selected:
        key = _title_key(group["contract"])
        if key and key in seen_titles:
            continue
        if key:
            seen_titles.add(key)
        sources.append(
            Source(
                index=len(sources) + 1,
                contract=group["contract"],
                chunks=[
                    chunk
                    for _, chunk in sorted(group["chunks"], key=lambda p: -p[0])
                ],
                score=group["rank"],
            )
        )
        if len(sources) >= top_k:
            break
    return sources


# ------------------------------------------------------------------- indexing


def file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def index_contracts(
    contracts: list[Contract],
    store: Store,
    embedder: Embedder,
    *,
    rebuild: bool = False,
    progress=None,
    max_chunks: int = 700,
    ocr: bool = True,
    ocr_pages: int = 30,
    ocr_seconds: float = 45.0,
    prune: bool = False,
) -> dict:
    rebuild_pending = rebuild
    if prune and not rebuild:
        store.delete_missing({contract.rel for contract in contracts})
    known = store.known_hashes()
    added = skipped = failed = 0
    for number, contract in enumerate(contracts, start=1):
        if progress:
            progress(number, len(contracts), contract.title)
        try:
            sha = file_sha(contract.path)
            if not rebuild_pending and known.get(contract.rel) == sha:
                skipped += 1
                continue
            pages = extract_pages(
                contract.path, ocr=ocr, ocr_pages=ocr_pages, ocr_seconds=ocr_seconds
            )
            textless = sum(1 for page in pages if len(page.strip()) < 40)
            if pages and textless > max(3, len(pages) // 5):
                note = "partial OCR/text layer"
                contract.notes = f"{contract.notes} · {note}".strip(" ·")
            text = clean_text("\n\f\n".join(pages))
            if len(text) < 80:
                failed += 1
                continue
            chunks = chunk_pages(pages)[:max_chunks]
            if not chunks:
                failed += 1
                continue
            vectors = embedder.embed([chunk.text for chunk in chunks])
            if rebuild_pending:
                # Clear only after a document has been extracted and embedded,
                # so a missing model or PDF stack cannot leave an empty index.
                store.clear()
                known = {}
                rebuild_pending = False
            store.add_document(contract, text, chunks, vectors, file_sha=sha)
            added += 1
        except Exception as exc:  # keep the corpus build going
            failed += 1
            if progress:
                progress(number, len(contracts), f"{contract.title} — {exc}")
    return {"added": added, "skipped": skipped, "failed": failed}
