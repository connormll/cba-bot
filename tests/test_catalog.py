from datetime import date

from cba_bot.catalog import Contract, infer_term


def test_infer_term_ranges():
    start, end = infer_term("SEIU 1000 - UAW Agreement 2024-2028")
    assert start == date(2024, 1, 1)
    assert end == date(2028, 12, 31)

    start, end = infer_term("PNWSU-SEIU2015-CBA-2023-2025-signed")
    assert start == date(2023, 1, 1)
    assert end == date(2025, 12, 31)


def test_infer_term_two_digit_end():
    start, end = infer_term("2021-25 SESU CBA-2")
    assert start == date(2021, 1, 1)
    assert end == date(2025, 12, 31)


def test_infer_term_single_year_and_none():
    start, end = infer_term("MNA 2018-2020")
    assert (start.year, end.year) == (2018, 2020)
    start, end = infer_term("NYSNA USW CBA")
    assert start is None and end is None


def test_status_and_recency():
    today = date.today()
    current = Contract(
        id="a", path=None, rel="a.pdf", title="current",
        effective=date(today.year - 1, 1, 1), expiry=date(today.year + 1, 12, 31),
    )
    expired = Contract(
        id="b", path=None, rel="b.pdf", title="expired",
        effective=date(today.year - 5, 1, 1), expiry=date(today.year - 2, 12, 31),
    )
    assert current.status == "current"
    assert expired.status == "expired"
    assert current.recency_weight() > expired.recency_weight()


def test_manifest_expiry_only_is_current(tmp_path):
    from cba_bot.catalog import infer_contract

    path = tmp_path / "Department of Defense_AFGE_2035.pdf"
    path.write_bytes(b"%PDF-")
    contract = infer_contract(
        path,
        tmp_path,
        {
            "title": "Department of Defense — AFGE",
            "expiry": f"{date.today().year + 5}-01-31",
        },
    )
    assert contract.effective is None
    assert contract.expiry is not None
    assert contract.status == "current"
    assert contract.term.startswith("through")


def test_ona_is_healthcare(tmp_path):
    from cba_bot.catalog import infer_contract

    path = tmp_path / "ONA 2022-2025.pdf"
    path.write_bytes(b"%PDF-")
    contract = infer_contract(path, tmp_path)
    assert contract.sector == "healthcare"
    assert contract.union_name == "ONA"
    assert contract.jurisdiction == "OH"


def test_single_year_assumes_three_year_term():
    start, end = infer_term("Colorado Wins 2026")
    assert start == date(2026, 1, 1)
    assert end == date(2029, 12, 31)
    assert infer_term("AFL-CIO Contract 2023-4-1")[1] == date(2026, 12, 31)


def test_short_us_date():
    start, end = infer_term("ummap_cba_11.1.24-1")
    assert start == date(2024, 11, 1)
    assert end == date(2027, 12, 31)
