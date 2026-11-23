"""Offline contract and adversarial regression tests for the static-site audit.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from validate_site import run  # noqa: E402


@pytest.fixture
def site(tmp_path: Path) -> Path:
    """Synthetic local site: all content is original and never downloaded."""
    (tmp_path / "art").mkdir()
    (tmp_path / "data/art_history").mkdir(parents=True)
    (tmp_path / "css").mkdir()
    (tmp_path / "art/study.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 80">'
        '<rect width="120" height="80" fill="#eeeeee"/><circle cx="60" cy="40" r="20" fill="#888888"/>'
        '</svg>', encoding="utf-8"
    )
    (tmp_path / "index.html").write_text(
        '<!doctype html><html><head><link href="css/site.css" rel="stylesheet"></head>'
        '<body id="top"><a href="./">Home</a><a href="privacy.html#rights">Privacy</a>'
        '<a href="support.html?lang=en#contact">Support</a>'
        '<img src="art/study.svg" alt="Original circular study"></body></html>', encoding="utf-8"
    )
    (tmp_path / "privacy.html").write_text('<!doctype html><h1 id="rights">Rights</h1>', encoding="utf-8")
    (tmp_path / "support.html").write_text('<!doctype html><h1 id="contact">Contact</h1>', encoding="utf-8")
    (tmp_path / "css/site.css").write_text('body { background-image:url("../art/study.svg"); }', encoding="utf-8")
    return tmp_path


def sample_catalog() -> dict:
    return {
        "schema_version": 1,
        "catalog_state": "release",
        "default_locale": "en",
        "supported_locales": ["en", "ja"],
        "periods": [{
            "id": "period-alpha", "label": {"en": "Study Period", "ja": "習作時代"},
            "start_year": 1500, "end_year": 1600
        }],
        "movements": [{
            "id": "movement-alpha", "period_id": "period-alpha",
            "label": {"en": "Shape Studies", "ja": "形の研究"},
            "start_year": 1520, "end_year": 1570
        }],
        "artworks": [{
            "id": "composition-alpha",
            "title": {"en": "Circle Study", "ja": "円形の習作"},
            "period_id": "period-alpha", "movement_id": "movement-alpha",
            "start_year": 1540, "end_year": 1550,
            "image": {
                "src": "art/study.svg", "width": 120, "height": 80,
                "alt": {
                    "en": "A grey circle on a pale rectangular field",
                    "ja": "淡い長方形の上に灰色の円がある"
                },
                "caption": {
                    "en": "An original arrangement created for local validation.",
                    "ja": "ローカル検証用に作ったオリジナル構図です"
                },
                "rights": {
                    "status": "owner_original",
                    "origin_note": "A geometric image made solely for this offline fixture."
                }
            }
        }]
    }


def write_catalog(site: Path, data: dict) -> None:
    (site / "data/art_history/catalog.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def codes(site: Path, catalog: dict, *, release: bool = False) -> set[str]:
    write_catalog(site, catalog)
    return {issue.code for issue in run(site, release=release)}


def test_clean_original_catalog_and_site(site: Path) -> None:
    assert codes(site, sample_catalog()) == set()


def test_empty_draft_is_valid_but_release_gate_fails(site: Path) -> None:
    draft = sample_catalog()
    draft.update(catalog_state="draft", periods=[], movements=[], artworks=[])
    assert codes(site, draft) == set()
    assert "RELEASE_EMPTY" in codes(site, draft, release=True)
    draft["catalog_state"] = "release"
    assert "RELEASE_EMPTY" in codes(site, draft)


@pytest.mark.parametrize("target,change,code", [
    ("periods", lambda d: d["periods"][0].update(start_year=1601), "CHRONOLOGY_RANGE"),
    ("movements", lambda d: d["movements"][0].update(end_year=1519), "CHRONOLOGY_RANGE"),
    ("movements", lambda d: d["movements"][0].update(period_id="missing"), "UNKNOWN_PERIOD"),
    ("movements", lambda d: d["movements"][0].update(start_year=1700, end_year=1710), "MOVEMENT_OUTSIDE_PERIOD"),
    ("artworks", lambda d: d["artworks"][0].update(movement_id="missing"), "UNKNOWN_MOVEMENT"),
    ("artworks", lambda d: d["artworks"][0].update(start_year=1590, end_year=1595), "ARTWORK_OUTSIDE_MOVEMENT"),
    ("artworks", lambda d: d["artworks"][0].update(start_year=1700, end_year=1701), "ARTWORK_OUTSIDE_PERIOD"),
    ("artworks", lambda d: d["artworks"].append(deepcopy(d["artworks"][0])), "DUPLICATE_ID"),
    ("artworks", lambda d: d["artworks"][0].update(unapproved="unexpected"), "UNKNOWN_FIELD"),
    ("artworks", lambda d: d["artworks"][0].update(start_year=True), "YEAR"),
    ("artworks", lambda d: d.update(supported_locales=["en", "en"]), "LOCALES"),
])
def test_invalid_catalog_records(site: Path, target: str, change, code: str) -> None:
    data = sample_catalog()
    change(data)
    assert code in codes(site, data)


def test_overlapping_periods_are_allowed(site: Path) -> None:
    data = sample_catalog()
    data["periods"].append({
        "id": "period-beta", "label": {"en": "Parallel Period", "ja": "並行期"},
        "start_year": 1560, "end_year": 1650
    })
    assert codes(site, data) == set()


def test_periods_must_be_in_ascending_chronological_order(site: Path) -> None:
    data = sample_catalog()
    data["periods"].append({
        "id": "period-earlier", "label": {"en": "Prior Period", "ja": "前期"},
        "start_year": 1400, "end_year": 1520
    })
    assert "CHRONOLOGY_ORDER" in codes(site, data)


def test_movements_must_be_in_ascending_chronological_order(site: Path) -> None:
    data = sample_catalog()
    data["movements"].append({
        "id": "movement-earlier", "period_id": "period-alpha",
        "label": {"en": "Earlier Movement", "ja": "前の動向"},
        "start_year": 1510, "end_year": 1530
    })
    assert "CHRONOLOGY_ORDER" in codes(site, data)


@pytest.mark.parametrize("mutate,code", [
    (lambda d: d["artworks"][0]["image"]["alt"].pop("ja"), "LOCALE_MISSING"),
    (lambda d: d["artworks"][0]["image"].update(caption="A caption"), "LOCALE_MAP"),
    (lambda d: d["artworks"][0]["image"]["alt"].update(en="tiny"), "TEXT"),
    (lambda d: d["artworks"][0]["image"]["caption"].update(en=d["artworks"][0]["image"]["alt"]["en"]), "ALT_CAPTION_DUPLICATE"),
    (lambda d: d["artworks"][0]["image"].update(width=0), "IMAGE_DIMENSION"),
    (lambda d: d["artworks"][0]["image"].update(src="https://example.test/remote.svg"), "IMAGE_PATH"),
    (lambda d: d["artworks"][0]["image"].update(src="../../hidden.png"), "IMAGE_PATH"),
    (lambda d: d["artworks"][0]["image"].update(src="art/missing.png"), "IMAGE_MISSING"),
    (lambda d: d["artworks"][0]["image"]["rights"].update(origin_note="short"), "TEXT"),
    (lambda d: d["artworks"][0]["image"]["rights"].update(status="unknown"), "RIGHTS_STATUS"),
])
def test_image_and_accessibility_audit(site: Path, mutate, code: str) -> None:
    data = sample_catalog()
    mutate(data)
    assert code in codes(site, data)


def test_public_domain_requires_full_provenance(site: Path) -> None:
    data = sample_catalog()
    data["artworks"][0]["image"]["rights"] = {"status": "public_domain", "source_url": "http://example.test/work"}
    found = codes(site, data)
    assert {"PROVENANCE_MISSING", "PROVENANCE_URL"} <= found


def test_valid_public_domain_provenance_shape(site: Path) -> None:
    data = sample_catalog()
    data["artworks"][0]["image"]["rights"] = {
        "status": "public_domain", "basis": "term_expired", "jurisdiction": "XX",
        "source_url": "https://example.test/object",
        "rights_evidence_url": "https://example.test/rights-record",
        "reviewed_on": "2026-01-15"
    }
    assert codes(site, data) == set()


def test_public_domain_review_date_and_basis_invalid(site: Path) -> None:
    data = sample_catalog()
    data["artworks"][0]["image"]["rights"] = {
        "status": "public_domain", "basis": "unproven", "jurisdiction": "XX",
        "source_url": "https://example.test/object",
        "rights_evidence_url": "https://example.test/rights",
        "reviewed_on": "2026-02-31"
    }
    assert {"DATE", "PROVENANCE_BASIS"} <= codes(site, data)


def test_cc0_requires_exact_licence_identifier(site: Path) -> None:
    data = sample_catalog()
    data["artworks"][0]["image"]["rights"] = {
        "status": "cc0", "licence_id": "some-other-licence",
        "licence_url": "https://example.test/licence",
        "source_url": "https://example.test/object",
        "rights_evidence_url": "https://example.test/rights-record",
        "reviewed_on": "2026-01-15"
    }
    assert "LICENCE" in codes(site, data)


def test_broken_page_href(site: Path) -> None:
    (site / "index.html").write_text('<a href="missing.html">Broken</a>', encoding="utf-8")
    assert "BROKEN_HREF" in codes(site, sample_catalog())


def test_broken_cross_page_fragment(site: Path) -> None:
    (site / "index.html").write_text('<a href="privacy.html#missing">Broken</a>', encoding="utf-8")
    assert "BROKEN_FRAGMENT" in codes(site, sample_catalog())


def test_duplicate_anchor_fails(site: Path) -> None:
    (site / "index.html").write_text('<a id="again"></a><div id="again"></div>', encoding="utf-8")
    assert "DUPLICATE_ANCHOR" in codes(site, sample_catalog())


@pytest.mark.parametrize("html,expected", [
    ('<img src="absent.svg" alt="Original work">', "MISSING_ASSET"),
    ('<img srcset="art/study.svg 1x, absent.png 2x">', "MISSING_ASSET"),
    ('<script src="https://example.test/a.js"></script>', "REMOTE_ASSET"),
    ('<a href="javascript:alert(1)">Bad</a>', "UNSAFE_REFERENCE"),
    ('<a href="../../confidential.html">Escape</a>', "PATH_ESCAPE"),
    ('<base href="/">', "BASE_URL"),
    ('<link href="missing.css" rel="stylesheet">', "MISSING_ASSET"),
    ('<style>body{background:url("absent.svg")}</style>', "MISSING_ASSET"),
    ('<a href="%2fetc/passwd">Encoded separator</a>', "UNSAFE_REFERENCE"),
])
def test_page_asset_and_link_security(site: Path, html: str, expected: str) -> None:
    (site / "index.html").write_text(html, encoding="utf-8")
    assert expected in codes(site, sample_catalog())


def test_link_root_aliases_and_mail_allowed(site: Path) -> None:
    (site / "index.html").write_text(
        '<main id="intro"><a href="/">Root</a><a href="/#intro">Top</a>'
        '<a href="mailto:info@example.test">Email</a><a href="tel:+10000000000">Call</a>'
        '<a href="https://example.test/somewhere">Elsewhere</a></main>', encoding="utf-8"
    )
    assert codes(site, sample_catalog()) == set()


def test_stylesheet_missing_import(site: Path) -> None:
    (site / "css/site.css").write_text('@import "missing.css";', encoding="utf-8")
    assert "MISSING_ASSET" in codes(site, sample_catalog())


def test_catalog_malformed_json(site: Path) -> None:
    write_catalog(site, sample_catalog())
    (site / "data/art_history/catalog.json").write_text('{broken', encoding="utf-8")
    assert "CATALOG_PARSE" in {i.code for i in run(site)}


def test_cli_success_and_release_gate(site: Path) -> None:
    draft = sample_catalog()
    draft.update(catalog_state="draft", artworks=[], movements=[], periods=[])
    write_catalog(site, draft)
    tool = Path(__file__).resolve().parents[1] / "tools/validate_site.py"
    ok = subprocess.run([sys.executable, str(tool), "--root", str(site)], text=True, capture_output=True, check=False)
    assert ok.returncode == 0
    assert "ALL=true" in ok.stdout
    gate = subprocess.run([sys.executable, str(tool), "--root", str(site), "--release"], text=True, capture_output=True, check=False)
    assert gate.returncode == 1
    assert "RELEASE_EMPTY" in gate.stdout
    assert "ALL=false" in gate.stdout


def test_schema_describes_real_catalog(site: Path) -> None:
    """Optional dependency only for cross-checking the declarative schema."""
    lib = pytest.importorskip("jsonschema")
    schema = json.loads((Path(__file__).resolve().parents[1] / "data/art_history/catalog.schema.json").read_text(encoding="utf-8"))
    lib.Draft202012Validator.check_schema(schema)
    lib.Draft202012Validator(schema, format_checker=lib.FormatChecker()).validate(sample_catalog())


def test_validator_is_independent_of_current_working_directory(site: Path, monkeypatch) -> None:
    monkeypatch.chdir(site / "css")
    write_catalog(site, sample_catalog())
    assert not run(site)


def test_canonical_external_link_is_navigation_not_offline_asset(site: Path) -> None:
    (site / "index.html").write_text(
        '<link rel="canonical" href="https://example.test/guide"><h1>Guide</h1>', encoding="utf-8"
    )
    assert codes(site, sample_catalog()) == set()


def test_kill_switch_off_keeps_page_bytes_identical(site: Path) -> None:
    draft = sample_catalog()
    draft.update(catalog_state="draft", artworks=[], periods=[], movements=[])
    write_catalog(site, draft)
    before = {p.relative_to(site).as_posix(): p.read_bytes() for p in site.rglob("*") if p.is_file()}
    tool = Path(__file__).resolve().parents[1] / "tools/validate_site.py"
    import os
    env = dict(os.environ, SIMPLEART_SITE_VALIDATOR_OFF="1")
    result = subprocess.run([sys.executable, str(tool), "--root", str(site), "--release"],
                            env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0 and "explicitly disabled" in result.stdout
    after = {p.relative_to(site).as_posix(): p.read_bytes() for p in site.rglob("*") if p.is_file()}
    assert before == after
    assert "RELEASE_EMPTY" in codes(site, draft, release=True)


def test_malformed_reference_fields_never_crash(site: Path) -> None:
    data = sample_catalog()
    data["movements"][0]["period_id"] = ["non-hashable"]
    data["artworks"][0]["image"]["src"] = "http://[broken"
    found = codes(site, data)
    assert "ID_FORMAT" in found and "IMAGE_PATH" in found


def test_unset_kill_switch_is_default_on(site: Path) -> None:
    draft = sample_catalog()
    draft.update(catalog_state="draft", artworks=[], periods=[], movements=[])
    write_catalog(site, draft)
    tool = Path(__file__).resolve().parents[1] / "tools/validate_site.py"
    import os
    env = dict(os.environ)
    env.pop("SIMPLEART_SITE_VALIDATOR_OFF", None)
    result = subprocess.run([sys.executable, str(tool), "--root", str(site), "--release"],
                            env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 1 and "RELEASE_EMPTY" in result.stdout


def test_site_image_requires_alt(site: Path) -> None:
    (site / "index.html").write_text('<img src="art/study.svg">', encoding="utf-8")
    assert "HTML_ALT_MISSING" in codes(site, sample_catalog())


def test_site_blank_alt_only_for_explicit_decorative_image(site: Path) -> None:
    (site / "index.html").write_text('<img src="art/study.svg" alt="">', encoding="utf-8")
    assert "HTML_ALT_EMPTY" in codes(site, sample_catalog())
    (site / "index.html").write_text('<img src="art/study.svg" alt="" role="presentation">', encoding="utf-8")
    assert codes(site, sample_catalog()) == set()
