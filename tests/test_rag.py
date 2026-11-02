from cba_bot.rag import linkify_citations


def test_linkify_citations():
    text = "Costs are shared [S1], but [S9] is out of range."
    linked = linkify_citations(text, answer_id=3, source_count=2)
    assert "[[S1]](cba-source:3:1)" in linked
    assert "[S9]" in linked
