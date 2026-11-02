"""Ingest protocols register themselves here."""

from __future__ import annotations

from cba_bot.ingest import cornell, escholarship, ny, olms, opm, states

SOURCES = {
    "cornell": cornell.scrape,
    "escholarship": escholarship.scrape,
    "olms": olms.scrape,
    "opm": opm.scrape,
    "ny": ny.scrape,
    "states": states.scrape,
}


def available() -> list[str]:
    return sorted(SOURCES)
