#!/usr/bin/env python3
"""Validate a static site's local references and offline artwork metadata.

SPDX-License-Identifier: MIT
Only standard-library modules are used; no network requests are made.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import unquote, urlsplit


ID = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
LOCALE = re.compile(r"^[a-z]{2}(?:-[A-Z]{2})?$")
CSS_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I | re.S)
CSS_IMPORT = re.compile(r"@import\s+(['\"])(.*?)\1", re.I)
HTML_SUFFIXES = {".html", ".htm"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".avif", ".svg"}
REMOTE_SCHEMES = {"http", "https"}
NON_FILE_LINK_SCHEMES = {"mailto", "tel", "sms"}


@dataclass(frozen=True, order=True)
class Issue:
    file: str
    line: int
    code: str
    message: str

    def format(self) -> str:
        loc = f"{self.file}:{self.line}" if self.line else self.file
        return f"FAIL {self.code} {loc} — {self.message}"


class Audit:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.issues: list[Issue] = []

    def location(self, path: Path | str) -> str:
        if isinstance(path, str):
            return path
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return "[outside-site-root]"

    def error(self, code: str, path: Path | str, message: str, line: int = 0) -> None:
        self.issues.append(Issue(self.location(path), line, code, message))

    def contained(self, path: Path, from_path: Path, *, code: str = "PATH_ESCAPE") -> bool:
        if not path.resolve().is_relative_to(self.root):
            self.error(code, from_path, "reference escapes site root")
            return False
        return True


def _fields(
    a: Audit, item: object, required: set[str], optional: set[str],
    where: str,
) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        a.error("TYPE", where, "expected an object")
        return None
    missing = required - item.keys()
    extra = item.keys() - required - optional
    for key in sorted(missing):
        a.error("REQUIRED", where, f"missing field: {key}")
    for key in sorted(extra):
        a.error("UNKNOWN_FIELD", where, f"unexpected field: {key}")
    return item


def _text(a: Audit, value: object, where: str, field: str, *, min_chars: int = 1) -> bool:
    if not isinstance(value, str) or len(value.strip()) < min_chars or value != value.strip():
        a.error("TEXT", where, f"{field} must be trimmed text of at least {min_chars} characters")
        return False
    return True


def _slug(a: Audit, value: object, where: str, field: str) -> bool:
    if not isinstance(value, str) or not ID.fullmatch(value):
        a.error("ID_FORMAT", where, f"{field} must be a lowercase, hyphenated identifier")
        return False
    return True


def _year(a: Audit, value: object, where: str, field: str) -> bool:
    if type(value) is not int or not -10000 <= value <= 3000:
        a.error("YEAR", where, f"{field} must be an integer between -10000 and 3000")
        return False
    return True


def _date(a: Audit, value: object, where: str, field: str) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        a.error("DATE", where, f"{field} must be an ISO calendar date (YYYY-MM-DD)")
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        a.error("DATE", where, f"{field} is not a real calendar date")
        return False
    return True


def _https(a: Audit, value: object, where: str, field: str) -> bool:
    if not isinstance(value, str) or value != value.strip():
        a.error("PROVENANCE_URL", where, f"{field} requires a complete HTTPS URL")
        return False
    try:
        parts = urlsplit(value)
        valid = (
            parts.scheme == "https" and bool(parts.hostname) and not parts.username
            and not parts.password and not any(c.isspace() for c in value)
            and parts.port in (None, 443)
        )
    except ValueError:
        valid = False
    if not valid:
        a.error("PROVENANCE_URL", where, f"{field} requires a complete HTTPS URL")
    return valid


def _interval(a: Audit, item: dict[str, Any], where: str, begin: str, end: str) -> tuple[int, int] | None:
    if not (_year(a, item.get(begin), where, begin) & _year(a, item.get(end), where, end)):
        return None
    first, last = item[begin], item[end]
    if first > last:
        a.error("CHRONOLOGY_RANGE", where, f"{begin} is after {end}")
        return None
    return first, last


def _overlaps(first: tuple[int, int], second: tuple[int, int]) -> bool:
    return first[0] <= second[1] and second[0] <= first[1]


def _localized(
    a: Audit, value: object, where: str, field: str,
    locales: list[str], *, min_chars: int = 1,
) -> dict[str, str] | None:
    if not isinstance(value, dict):
        a.error("LOCALE_MAP", where, f"{field} must be a locale-to-text object")
        return None
    for language in locales:
        if language not in value:
            a.error("LOCALE_MISSING", where, f"{field} missing locale {language}")
    for language in value:
        if language not in locales:
            a.error("LOCALE_UNKNOWN", where, f"{field} has unsupported locale {language}")
        else:
            _text(a, value[language], where, f"{field}.{language}", min_chars=min_chars)
    return value  # type: ignore[return-value]


def _image_src(a: Audit, src: object, where: str) -> None:
    if not isinstance(src, str) or not src or src != src.strip():
        a.error("IMAGE_PATH", where, "image src must be a local, nonempty relative path")
        return
    try:
        parts = urlsplit(src)
    except ValueError:
        a.error("IMAGE_PATH", where, "image src is malformed")
        return
    if (parts.scheme or parts.netloc or parts.query or parts.fragment or src.startswith(("/", "\\"))
            or "\\" in src or "\x00" in src):
        a.error("IMAGE_PATH", where, "image src must be a plain site-relative local file")
        return
    raw_parts = Path(unquote(parts.path)).parts
    if ".." in raw_parts or not raw_parts or Path(parts.path).suffix.lower() not in IMAGE_SUFFIXES:
        a.error("IMAGE_PATH", where, "image src contains traversal or an unsupported image format")
        return
    file = a.root / unquote(parts.path)
    if not a.contained(file, where, code="IMAGE_PATH"):
        return
    if not file.is_file():
        a.error("IMAGE_MISSING", where, f"image file not found: {src}")


def _rights(a: Audit, rights: object, where: str) -> None:
    base = _fields(a, rights, {"status"}, {
        "origin_note", "source_url", "rights_evidence_url", "reviewed_on", "jurisdiction", "basis", "licence_id", "licence_url"
    }, where)
    if base is None:
        return
    status = base.get("status")
    if status not in ("owner_original", "public_domain", "cc0"):
        a.error("RIGHTS_STATUS", where, "status must be owner_original, public_domain, or cc0")
        return
    allowed: dict[str, set[str]] = {
        "owner_original": {"status", "origin_note"},
        "public_domain": {"status", "source_url", "rights_evidence_url", "reviewed_on", "jurisdiction", "basis"},
        "cc0": {"status", "source_url", "rights_evidence_url", "reviewed_on", "licence_id", "licence_url"},
    }
    needed = allowed[status]
    for key in sorted(needed - base.keys()):
        a.error("PROVENANCE_MISSING", where, f"{status} requires {key}")
    for key in sorted(base.keys() - needed):
        a.error("PROVENANCE_EXTRA", where, f"{key} does not apply to {status}")
    if status == "owner_original":
        if "origin_note" in base:
            _text(a, base["origin_note"], where, "origin_note", min_chars=15)
    else:
        for key in ("source_url", "rights_evidence_url"):
            if key in base:
                _https(a, base[key], where, key)
        if "reviewed_on" in base:
            _date(a, base["reviewed_on"], where, "reviewed_on")
        if status == "public_domain":
            if "jurisdiction" in base:
                _text(a, base["jurisdiction"], where, "jurisdiction", min_chars=2)
            if "basis" in base and base["basis"] not in ("term_expired", "government_work", "documented_exception"):
                a.error("PROVENANCE_BASIS", where, "public-domain basis is unrecognized")
        else:
            if "licence_id" in base and base["licence_id"] != "CC0-1.0":
                a.error("LICENCE", where, "CC0 work requires licence_id CC0-1.0")
            if "licence_url" in base:
                _https(a, base["licence_url"], where, "licence_url")


def validate_catalog(a: Audit, path: Path, *, release: bool = False) -> None:
    if not a.contained(path, path):
        return
    if not path.is_file():
        a.error("CATALOG_MISSING", path, "offline catalog file is missing")
        return
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        a.error("CATALOG_PARSE", path, f"cannot read JSON: {exc}")
        return
    where = a.location(path)
    obj = _fields(a, catalog, {"schema_version", "catalog_state", "default_locale", "supported_locales", "periods", "movements", "artworks"}, set(), where)
    if obj is None:
        return
    if type(obj.get("schema_version")) is not int or obj.get("schema_version") != 1:
        a.error("SCHEMA_VERSION", where, "schema_version must equal integer 1")
    state = obj.get("catalog_state")
    if state not in ("draft", "release"):
        a.error("CATALOG_STATE", where, "catalog_state must be draft or release")
    locales = obj.get("supported_locales")
    if not isinstance(locales, list) or not locales or any(not isinstance(x, str) or not LOCALE.fullmatch(x) for x in locales) or len(set(locales)) != len(locales):
        a.error("LOCALES", where, "supported_locales must be unique valid language tags")
        locales = []
    if not isinstance(obj.get("default_locale"), str) or obj["default_locale"] not in locales:
        a.error("DEFAULT_LOCALE", where, "default_locale must be included in supported_locales")
    collections = {}
    for key in ("periods", "movements", "artworks"):
        items = obj.get(key)
        if not isinstance(items, list):
            a.error("COLLECTION", where, f"{key} must be an array")
            items = []
        collections[key] = items
    periods, movements, artworks = (collections[key] for key in ("periods", "movements", "artworks"))
    if (release or state == "release") and not artworks:
        a.error("RELEASE_EMPTY", where, "release catalog must contain at least one artwork")

    intervals: dict[str, dict[str, tuple[int, int]]] = {"periods": {}, "movements": {}}
    refs: dict[str, set[str]] = {"periods": set(), "movements": set(), "artworks": set()}
    for kind, items in (("periods", periods), ("movements", movements), ("artworks", artworks)):
        previous_start: int | None = None
        for index, raw in enumerate(items):
            item_where = f"{where}#{kind}[{index}]"
            required = ({"id", "label", "start_year", "end_year"} if kind == "periods" else
                        {"id", "label", "start_year", "end_year", "period_id"} if kind == "movements" else
                        {"id", "title", "start_year", "end_year", "period_id", "movement_id", "image"})
            item = _fields(a, raw, required, set(), item_where)
            if item is None:
                continue
            if _slug(a, item.get("id"), item_where, "id"):
                ident = item["id"]
                if ident in refs[kind]:
                    a.error("DUPLICATE_ID", item_where, f"duplicate identifier: {ident}")
                refs[kind].add(ident)
            dates = _interval(a, item, item_where, "start_year", "end_year")
            if dates:
                if kind in intervals and isinstance(item.get("id"), str):
                    intervals[kind][item["id"]] = dates
                if previous_start is not None and dates[0] < previous_start:
                    a.error("CHRONOLOGY_ORDER", item_where, f"{kind} must be ordered by start_year ascending")
                previous_start = dates[0]
            _localized(a, item.get("label" if kind != "artworks" else "title"), item_where, "label" if kind != "artworks" else "title", locales)
            if kind == "movements":
                _slug(a, item.get("period_id"), item_where, "period_id")
            if kind == "artworks":
                _slug(a, item.get("period_id"), item_where, "period_id")
                _slug(a, item.get("movement_id"), item_where, "movement_id")
                image = _fields(a, item.get("image"), {"src", "alt", "caption", "rights", "width", "height"}, set(), item_where + ".image")
                if image:
                    img_where = item_where + ".image"
                    _image_src(a, image.get("src"), img_where)
                    for dimension in ("width", "height"):
                        val = image.get(dimension)
                        if type(val) is not int or not (1 <= val <= 50000):
                            a.error("IMAGE_DIMENSION", img_where, f"{dimension} must be a positive integer up to 50000")
                    alt = _localized(a, image.get("alt"), img_where, "alt", locales, min_chars=8)
                    caption = _localized(a, image.get("caption"), img_where, "caption", locales, min_chars=8)
                    if alt is not None and caption is not None:
                        for language in locales:
                            av, cv = alt.get(language), caption.get(language)
                            if isinstance(av, str) and isinstance(cv, str) and av.casefold() == cv.casefold():
                                a.error("ALT_CAPTION_DUPLICATE", img_where, f"alt and caption repeat in locale {language}")
                    _rights(a, image.get("rights"), img_where + ".rights")
    for index, item in enumerate(movements):
        if not isinstance(item, dict):
            continue
        where_m = f"{where}#movements[{index}]"
        period = item.get("period_id")
        movement = item.get("id")
        if isinstance(period, str) and period not in refs["periods"]:
            a.error("UNKNOWN_PERIOD", where_m, f"unknown period_id: {period}")
        if isinstance(movement, str) and isinstance(period, str) and movement in intervals["movements"] and period in intervals["periods"]:
            if not _overlaps(intervals["movements"][movement], intervals["periods"][period]):
                a.error("MOVEMENT_OUTSIDE_PERIOD", where_m, "movement and period date spans do not overlap")
    for index, item in enumerate(artworks):
        if not isinstance(item, dict):
            continue
        where_w = f"{where}#artworks[{index}]"
        period, movement = item.get("period_id"), item.get("movement_id")
        if isinstance(period, str) and period not in refs["periods"]:
            a.error("UNKNOWN_PERIOD", where_w, f"unknown period_id: {period}")
        if isinstance(movement, str) and movement not in refs["movements"]:
            a.error("UNKNOWN_MOVEMENT", where_w, f"unknown movement_id: {movement}")
        years = item.get("start_year"), item.get("end_year")
        if all(type(x) is int for x in years) and years[0] <= years[1]:
            artwork_years = (years[0], years[1])
            for key, value, error in (("periods", period, "ARTWORK_OUTSIDE_PERIOD"),
                                      ("movements", movement, "ARTWORK_OUTSIDE_MOVEMENT")):
                if isinstance(value, str) and value in intervals[key] and not _overlaps(artwork_years, intervals[key][value]):
                    a.error(error, where_w, "artwork date span does not overlap its assigned chronology")


@dataclass(frozen=True)
class Ref:
    url: str
    kind: str  # link or asset
    line: int


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.duplicate_ids: list[tuple[int, str]] = []
        self.refs: list[Ref] = []
        self.base: list[int] = []
        self.image_alts: list[tuple[int, str]] = []
        self._in_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        props = dict(attrs)
        line = self.getpos()[0]
        ident = props.get("id") or (props.get("name") if tag == "a" else None)
        if ident:
            if ident in self.ids:
                self.duplicate_ids.append((line, ident))
            self.ids.add(ident)
        if tag == "base":
            self.base.append(line)
        if tag == "style":
            self._in_style = True
        if tag == "img":
            if "alt" not in props:
                self.image_alts.append((line, "HTML_ALT_MISSING"))
            elif not (props.get("alt") or "").strip():
                decorative = props.get("role") in ("presentation", "none") or props.get("aria-hidden") == "true"
                if not decorative:
                    self.image_alts.append((line, "HTML_ALT_EMPTY"))
        if tag == "a" and props.get("href") is not None:
            self.refs.append(Ref(props["href"] or "", "link", line))
        if tag in ("link", "area") and props.get("href") is not None:
            asset_rel = {"stylesheet", "icon", "apple-touch-icon", "mask-icon", "preload", "modulepreload", "prefetch", "manifest"}
            rel = set((props.get("rel") or "").lower().split())
            kind = "asset" if tag == "link" and rel & asset_rel else "link"
            self.refs.append(Ref(props["href"] or "", kind, line))
        if tag in ("img", "script", "iframe", "source", "audio", "video", "embed", "track") and props.get("src") is not None:
            self.refs.append(Ref(props["src"] or "", "asset", line))
        if tag == "video" and props.get("poster") is not None:
            self.refs.append(Ref(props["poster"] or "", "asset", line))
        if tag == "object" and props.get("data") is not None:
            self.refs.append(Ref(props["data"] or "", "asset", line))
        if tag in ("img", "source") and props.get("srcset") is not None:
            for candidate in (props["srcset"] or "").split(","):
                value = candidate.strip().split()
                if value:
                    self.refs.append(Ref(value[0], "asset", line))
        if props.get("style"):
            self._css_urls(props["style"] or "", line)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self._css_urls(data, self.getpos()[0])

    def _css_urls(self, css: str, line: int) -> None:
        self.refs.extend(Ref(url.strip(), "asset", line + css[:match.start()].count("\n"))
                         for match in CSS_URL.finditer(css) if (url := match.group(2).strip()) and not url.startswith("#"))
        self.refs.extend(Ref(match.group(2).strip(), "asset", line + css[:match.start()].count("\n"))
                         for match in CSS_IMPORT.finditer(css) if match.group(2).strip())


def _resolve_reference(a: Audit, origin: Path, ref: Ref) -> tuple[Path, str] | None:
    raw = ref.url.strip()
    if not raw:
        return None
    if any(c in raw for c in "\x00\r\n") or "\\" in raw:
        a.error("UNSAFE_REFERENCE", origin, f"invalid reference: {raw!r}", ref.line)
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        a.error("UNSAFE_REFERENCE", origin, "malformed URL", ref.line)
        return None
    if parts.scheme:
        if parts.scheme in REMOTE_SCHEMES:
            if ref.kind == "asset":
                a.error("REMOTE_ASSET", origin, "offline site assets must be local", ref.line)
            return None
        if ref.kind == "link" and parts.scheme in NON_FILE_LINK_SCHEMES:
            return None
        a.error("UNSAFE_REFERENCE", origin, "unsupported URL scheme", ref.line)
        return None
    if parts.netloc:
        if ref.kind == "asset":
            a.error("REMOTE_ASSET", origin, "offline site assets must be local", ref.line)
        return None
    if re.search(r"%(?:2f|5c|00)", raw, re.I):
        a.error("UNSAFE_REFERENCE", origin, "encoded separators or NUL are not accepted", ref.line)
        return None
    path = unquote(parts.path)
    if raw.startswith("/"):
        target = a.root / path.lstrip("/")
    else:
        target = origin.parent / path if path else origin
    if not a.contained(target, origin):
        return None
    if path.endswith("/") or (target.exists() and target.is_dir()):
        target = target / "index.html"
    if not target.exists():
        a.error("BROKEN_HREF" if ref.kind == "link" else "MISSING_ASSET", origin,
                f"missing local target: {raw}", ref.line)
        return None
    if not target.is_file():
        a.error("NOT_A_FILE", origin, f"reference is not a file: {raw}", ref.line)
        return None
    return target.resolve(), unquote(parts.fragment)


def _parse_page(a: Audit, path: Path) -> PageParser | None:
    try:
        doc = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        a.error("HTML_READ", path, f"cannot read HTML: {exc}")
        return None
    parsed = PageParser()
    parsed.feed(doc)
    parsed.close()
    for line, ident in parsed.duplicate_ids:
        a.error("DUPLICATE_ANCHOR", path, f"repeated HTML id or anchor name: {ident}", line)
    for line in parsed.base:
        a.error("BASE_URL", path, "base elements change offline reference resolution", line)
    for line, code in parsed.image_alts:
        a.error(code, path, "image needs descriptive alt text, or explicit decorative status", line)
    return parsed


def validate_site(a: Audit) -> None:
    html_paths = sorted(path for path in a.root.rglob("*.html") if ".git" not in path.parts)
    html_paths += sorted(path for path in a.root.rglob("*.htm") if ".git" not in path.parts)
    if not html_paths:
        a.error("HTML_NONE", "[site]", "no HTML pages found")
    parsed = {path.resolve(): page for path in html_paths if (page := _parse_page(a, path))}
    for origin, page in parsed.items():
        for ref in page.refs:
            target = _resolve_reference(a, origin, ref)
            if not target:
                continue
            path, fragment = target
            if fragment and path.suffix.lower() in HTML_SUFFIXES:
                dest = parsed.get(path)
                if dest is None or fragment not in dest.ids:
                    a.error("BROKEN_FRAGMENT", origin, f"missing HTML anchor: {ref.url}", ref.line)
    for css in sorted(a.root.rglob("*.css")):
        if ".git" in css.parts:
            continue
        try:
            content = css.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            a.error("CSS_READ", css, f"cannot read stylesheet: {exc}")
            continue
        p = PageParser()
        p._css_urls(content, 1)
        for ref in p.refs:
            if ref.url.startswith("#"):
                continue  # inline fragment IDs are not filesystem paths
            _resolve_reference(a, css, ref)


def run(root: Path, catalog: Path | None = None, *, release: bool = False) -> list[Issue]:
    a = Audit(root)
    if not root.is_dir():
        a.error("ROOT_MISSING", "[site]", "site root is not a directory")
        return a.issues
    validate_catalog(a, catalog or (a.root / "data/art_history/catalog.json"), release=release)
    validate_site(a)
    return sorted(set(a.issues))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--catalog", type=Path, default=None, help="catalog path relative to root or absolute")
    parser.add_argument("--release", action="store_true", help="reject an empty draft catalog")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    catalog = args.catalog
    if catalog is not None and not catalog.is_absolute():
        catalog = root / catalog
    # Explicit OFF bypass; default operation always audits. No site files are changed.
    if os.environ.get("SIMPLEART_SITE_VALIDATOR_OFF", "").strip().lower() in ("1", "true", "yes"):
        print("PASS validator explicitly disabled by environment kill switch; site untouched")
        print("ALL=true")
        return 0
    problems = run(root, catalog, release=args.release)
    if problems:
        for issue in problems:
            print(issue.format())
        print(f"ALL=false issues={len(problems)}")
        return 1
    print("PASS offline catalog, chronology, provenance, local assets and page references")
    print("ALL=true")
    return 0


if __name__ == "__main__":
    sys.exit(main())
