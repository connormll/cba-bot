from datetime import date

from cba_bot.catalog import Contract
from cba_bot.cli import _select_corpus


def contract(name, source, year):
    return Contract(
        id=name,
        path=None,
        rel=f"{name}.pdf",
        title=name,
        source=source,
        effective=date(year, 1, 1),
        expiry=date(year + 3, 12, 31),
    )


def test_selection_always_keeps_local_and_mixes_sources():
    corpus = [contract(f"local-{i}", "local", 2020 + i) for i in range(5)]
    corpus += [contract(f"opm-{i}", "opm", 2026 + i) for i in range(20)]
    corpus += [contract(f"ny-{i}", "ny", 2024 + i) for i in range(20)]
    selected = _select_corpus(corpus, limit=10)
    ids = {c.id for c in selected}
    assert {f"local-{i}" for i in range(5)} <= ids
    sources = {c.source for c in selected}
    assert {"local", "opm", "ny"} <= sources
