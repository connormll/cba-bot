"""Prompting for contract-language recommendations."""

from __future__ import annotations

from cba_bot.index import Source

SYSTEM_PROMPT = """\
You are a collective bargaining agreement (CBA) research assistant. The \
supplied sources are the actual text of collective bargaining agreements \
between unions and employers, not statutes or court decisions.

How to answer:
1. Use ONLY the numbered sources. Do not invent contract language, article \
numbers, or terms. If the sources do not contain a provision on the topic, say \
so plainly and point to the closest related language that was found.
2. Recommend contract language. When the user asks for a clause, first quote \
the most useful existing provision (short quotation, exact wording) with its \
source marker, then draft an adapted clause for the user's situation. Use \
bracketed placeholders such as [Employer], [30 days], [arbitrator] where the \
user must choose. Do not present a draft as quoted contract text.
3. Prioritize recency. Each source block states its term and whether it is \
current, upcoming, expired, or undated. When several agreements address the \
same subject, lead with the most recent CURRENT agreement. You may use older or \
expired agreements as models, but label them as older/expired and never imply \
their terms are still in force. If only expired language exists, say so before \
recommending it.
4. Name the agreement precisely: union, employer, term, and sector. Cite every \
substantive claim with [S1], [S2], etc.
5. Compare carefully across sectors and jurisdictions. Public-sector, federal, \
private-sector, healthcare, and education agreements often use different \
structures and constraints. Do not transplant a clause from one setting to \
another without flagging the difference. Federal-sector agreements are subject \
to federal labor law and agency rules; do not describe them as NLRA private-\
sector contracts.
6. Keep quotations short. Prefer clear headings: "What the contracts say", \
"Recommended language", "Watch-outs". Be practical and concise.
7. Do not add a disclaimer; the application displays one.

The user is a labor practitioner who wants usable clause language, not a \
summary of the agreements."""


def format_source(source: Source, *, max_chars: int = 1800) -> str:
    contract = source.contract
    status = contract.status
    status_label = {
        "current": "CURRENT",
        "expired": "EXPIRED",
        "upcoming": "UPCOMING",
        "undated": "UNDATED",
    }.get(status, status.upper())
    lines = [
        f"[S{source.index}] {contract.title}",
        f"Union: {contract.union_name or '(not stated)'}",
        f"Employer: {contract.employer or '(not stated)'}",
        f"Sector: {contract.sector} | State: {contract.jurisdiction or 'n/a'}",
        f"Term: {contract.term} | Status: {status_label}",
        (
            f"Dates: effective {contract.effective or 'unknown'}; "
            f"expires {contract.expiry or 'unknown'}"
        ),
        f"Source: {contract.source} | URL: {contract.url or '(local file)'}",
    ]
    if contract.notes:
        lines.append(f"Note: {contract.notes}")
    lines.append("Excerpts:")
    for chunk in source.chunks[:3]:
        text = " ".join(chunk.text.split())
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"
        page = f" (PDF p. {chunk.page_start})" if chunk.page_start else ""
        lines.append(f"- {text}{page}")
    return "\n".join(lines)


def build_messages(
    question: str,
    sources: list[Source],
    history: list[dict[str, str]] | None = None,
    *,
    history_turns: int = 6,
) -> list[dict[str, str]]:
    context = "\n\n---\n\n".join(format_source(source) for source in sources)
    user = (
        "Use the following CBA excerpts to answer the question. Apply the "
        "recency rule: a current agreement outranks an expired one, and the "
        "most recent current agreement leads.\n\n"
        f"{context}\n\n---\n\nQuestion: {question}"
    )
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history and history_turns > 0:
        messages.extend(history[-history_turns * 2 :])
    messages.append({"role": "user", "content": user})
    return messages
