# Offline catalog contract (private draft)

This additive module does not rewrite the existing static pages. It defines one canonical metadata format at `data/art_history/catalog.json` and one validation entry point at `tools/validate_site.py`. The initial catalog is intentionally **empty**: the supplied archive contains no licensed artwork, provenance records, or verified art-history chronology. Do not represent test examples as real art history.

## Fields and chronology

- `schema_version`: integer `1`; `catalog_state`: `draft` or `release`.
- `supported_locales`: unique language tags, including `default_locale`. Every artwork title, image `alt`, and image `caption` must be supplied in every supported locale.
- `periods`: historical date-span records, with unique slug `id`, localized `label`, inclusive `start_year` and `end_year`.
- `movements`: the same chronological fields plus an existing `period_id`.
- `artworks`: unique `id`, localized `title`, inclusive dates, existing `period_id` and `movement_id`, and an `image` object.
- Date arrays for periods, movements and artworks must be sorted ascending by `start_year`. Dates may overlap; the validator requires each movement to intersect its period and each artwork to intersect its assigned movement and period, rather than imposing a false universal non-overlap or strict nesting rule.
- Years use integer astronomical numbering, including `0` for astronomical 1 BCE. To avoid ambiguous BCE conversion, check dates carefully during editorial review.
- The catalog is a private draft by default. `--release` or `catalog_state: release` rejects a catalog with no artwork; **this does not publish anything**.

## Image descriptions and attribution

Each artwork's `image` requires `src`, `width`, `height`, locale maps `alt` and `caption`, and `rights`. `src` must refer to a real local image; remote content, path traversal and missing images fail. Only common browser image formats are allowed. `alt` describes visible form; `caption` offers distinct contextual information. Both must be trimmed and at least eight characters long in every locale, and must not be identical. Length and non-equality are mechanical checks, **not** guarantees of accuracy, access, or inclusive language.

`rights.status` is one of:

- `owner_original`: `origin_note` explaining the original source of the work (at least 15 characters).
- `public_domain`: HTTPS `source_url`, HTTPS `rights_evidence_url`, `reviewed_on` (`YYYY-MM-DD`), `jurisdiction`, and `basis` (`term_expired`, `government_work` or `documented_exception`). **Public-domain claims depend on the jurisdiction and specific image, not just the underlying artwork.**
- `cc0`: HTTPS `source_url`, HTTPS `rights_evidence_url`, `reviewed_on`, exact `licence_id: CC0-1.0`, and HTTPS `licence_url`.

The validator checks **presence, shape and local-file existence**, not whether a remote rights statement is authentic or whether rights are cleared worldwide. A human must verify each provenance link, any reproduction-specific rights, and artwork dates before a release. The validator intentionally **never fetches** remote URLs, so it is usable offline. No externally sourced art or example public-domain assertions are bundled.

## Commands

Run from repository root:

```sh
python tools/validate_site.py --root .
python tools/validate_site.py --root . --release
python -m pytest -q tests/test_site_validator.py
```

The validator is **ON by default** whenever invoked. Set `SIMPLEART_SITE_VALIDATOR_OFF=1` for the explicit audit kill switch (it skips validation and leaves the site untouched); unset it to restore the default gate. The first command checks the current draft metadata and all local HTML, CSS and referenced assets. The second is a stricter content-readiness gate (it fails while the catalog is empty). The test suite uses temporary synthetic sites and wholly original geometric examples. No network access is required for either command.

## Local-link rules

The reference checker scans static `.html` / `.htm` pages, local link destinations, HTML fragment targets, images/scripts/media, `srcset`, linked stylesheets, `url(...)` and imported CSS resources. It supports relative and site-root links, directory indexes, fragments and query strings. Missing paths, duplicate HTML anchors, unlabelled nondecorative HTML images, assets hosted remotely, encoded separators, path escapes, and embedded code schemes fail. Explicit external navigation links and email/telephone links are allowed **but are not network-checked**. The checker is file-system based, not a browser/JavaScript runtime or deployment smoke test.

### Deployment scope

No page, server, external link, publishing setting or live branch is changed by this archive. Apply the patch to a private checkout only after reviewing it. The source patch adds files rather than altering existing pages; existing pages therefore retain their previous rendered output until an owner manually integrates a catalog display.
