from datetime import date

import numpy as np

from cba_bot.catalog import Contract
from cba_bot.index import Chunk, Store, chunk_pages, retrieve_sources


class FakeEmbedder:
    def embed(self, texts):
        return np.tile(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32), (len(texts), 1))

    def embed_query(self, query):
        return np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)


def make_contract(tmp_path, name, text, effective, expiry):
    path = tmp_path / f"{name}.pdf"
    path.write_bytes(b"%PDF-")
    contract = Contract(
        id=name,
        path=path,
        rel=path.name,
        title=name,
        union_name="SEIU",
        employer="State",
        sector="public",
        jurisdiction="CA",
        effective=effective,
        expiry=expiry,
    )
    chunks = [Chunk(text=text, page_start=1, page_end=1)]
    return contract, chunks


def test_chunk_pages_keeps_pages_and_overlap():
    pages = ["Arbitration clause. " * 120, "Second page text. " * 80]
    chunks = chunk_pages(pages, target=800, overlap=100)
    assert len(chunks) >= 2
    assert chunks[0].page_start == 1
    assert any(chunk.page_start == 2 for chunk in chunks)


def test_store_round_trip_and_recency_ranking(tmp_path):
    store = Store(tmp_path / "index")
    today = date.today()
    text = "The parties shall submit unresolved grievances to binding arbitration."
    current, current_chunks = make_contract(
        tmp_path, "current", text, date(today.year - 1, 1, 1), date(today.year + 1, 12, 31)
    )
    expired, expired_chunks = make_contract(
        tmp_path, "expired", text, date(today.year - 8, 1, 1), date(today.year - 4, 12, 31)
    )
    for contract, chunks in ((current, current_chunks), (expired, expired_chunks)):
        store.add_document(contract, " ".join(text.split()), chunks, np.ones((1, 4), dtype=np.float32))

    assert store.document_count() == 2
    lexical = store.lexical_search("binding arbitration")
    assert lexical
    sources = retrieve_sources(store, FakeEmbedder(), "binding arbitration", top_k=2)
    assert [s.contract.id for s in sources] == ["current", "expired"]
    assert sources[0].contract.status == "current"

    # /fresh removes the expired agreement entirely.
    fresh = retrieve_sources(
        store, FakeEmbedder(), "binding arbitration", top_k=2, current_only=True
    )
    assert [s.contract.id for s in fresh] == ["current"]
    store.close()


def test_store_rebuild(tmp_path):
    store = Store(tmp_path / "index")
    contract, chunks = make_contract(
        tmp_path, "one", "Arbitration and grievance procedure.", date(2024, 1, 1), date(2027, 12, 31)
    )
    store.add_document(contract, "arbitration", chunks, np.ones((1, 4), dtype=np.float32))
    assert store.chunk_count() == 1
    store.clear()
    assert store.document_count() == 0 and store.chunk_count() == 0
    store.close()


def test_query_expansion():
    from cba_bot.index import expand_query

    expanded = expand_query("remote work clause")
    assert "telework" in expanded and "work from home" in expanded
    assert expand_query("arbitration costs") != "arbitration costs"


def test_sector_and_state_filters(tmp_path):
    store = Store(tmp_path / "index")
    contract, chunks = make_contract(
        tmp_path, "ca-public", "Telework is permitted with supervisor approval.",
        date(2024, 1, 1), date(2027, 12, 31),
    )
    store.add_document(contract, "telework", chunks, np.ones((1, 4), dtype=np.float32))
    assert retrieve_sources(store, FakeEmbedder(), "telework", sector="public")
    assert not retrieve_sources(store, FakeEmbedder(), "telework", sector="federal")
    assert retrieve_sources(store, FakeEmbedder(), "telework", jurisdiction="CA")
    assert not retrieve_sources(store, FakeEmbedder(), "telework", jurisdiction="NY")
    store.close()


def test_index_contracts_skips_unchanged_files(tmp_path, monkeypatch):
    import cba_bot.index as index_module
    from cba_bot.index import index_contracts

    monkeypatch.setattr(
        index_module,
        "extract_pages",
        lambda *args, **kwargs: ["Arbitration and grievance procedure text. " * 20],
    )
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF-1.4 test")
    contract = Contract(
        id="sample",
        path=path,
        rel=path.name,
        title="sample",
        effective=date(2024, 1, 1),
        expiry=date(2027, 12, 31),
    )
    store = Store(tmp_path / "index")
    first = index_contracts([contract], store, FakeEmbedder())
    second = index_contracts([contract], store, FakeEmbedder())
    assert first["added"] == 1
    assert second["added"] == 0 and second["skipped"] == 1
    assert store.document_count() == 1
    store.close()


def test_compact_vectors_drops_orphans(tmp_path):
    store = Store(tmp_path / "index")
    contract, chunks = make_contract(
        tmp_path, "one", "Arbitration and grievance procedure.",
        date(2024, 1, 1), date(2027, 12, 31),
    )
    store.add_document(contract, "arbitration", chunks, np.ones((1, 4), dtype=np.float32))
    store._append_vectors(np.ones((3, 4), dtype=np.float32))
    assert store.vector_count() == 4
    removed = store.compact_vectors()
    assert removed == 3
    assert store.vector_count() == 1
    assert store.chunk_count() == 1
    store.close()
