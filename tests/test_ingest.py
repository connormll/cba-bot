import json

from cba_bot.ingest.common import Manifest, iso_date, safe_name, state_from_location


def test_state_from_location():
    assert state_from_location("Savannah, GA; Garden City, GA") == "GA"
    assert state_from_location("NATIONAL") == ""


def test_iso_date_from_olms_epoch():
    assert iso_date(983682000000) == "2001-03-04"
    assert iso_date("2035-01-31T06:00:00") == "2035-01-31"
    assert iso_date(None) == ""


def test_safe_name():
    assert "/" not in safe_name("A/B: C?")
    assert safe_name("   ") == "contract"


def test_manifest_merges_by_file(tmp_path):
    manifest = Manifest(tmp_path, "demo")
    manifest.add({"file": "a.pdf", "title": "first", "url": "u1"})
    manifest.add({"file": "a.pdf", "title": "second", "url": "u1"})
    manifest.flush()
    lines = manifest.path.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["title"] == "second"
