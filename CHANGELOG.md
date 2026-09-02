# Changelog

## 1.4.0 — 2026-09-02

**JPG/PNG uploads are now accepted alongside PDF (Railway side only; the tifo-databas upload form is a separate follow-up).**

- **New preprocessing step: `slicer.image_to_pdf()`.** A raster upload (detected by magic bytes via `is_raster_source()`, not by filename) is wrapped in a single-page PDF sized `width_m × height_m` at the new `PTS_PER_M` constant, with the image **stretched** to fill the page (`keep_proportion=False`). The entered dimensions win; a mismatched pixel aspect is distorted to match rather than letterboxed or rejected.
- **`PTS_PER_M = 72/2.54` (28.3465 pts/m) is not an arbitrary choice.** Nothing in `slicer.py` ever declared a pts-per-metre — it is derived as `full_w / width_m` from whatever page the source carries. Every real design is drawn at 1:100, so that derivation lands on 28.3465 on every shipped file (measured across `pest övre`, `pest mitten`, `ENAD_rutor`). Since `KLIPP_MIN_PINK_PT`, `KLIPP_LINE_MARGIN_PT`, `MIN_LABEL_PATCH_SIZE_PT`, `LABEL_FONT_DEFAULT` and `PAGE_NUM_EXCL_*` are all absolute PDF-point values calibrated against that scale, a raster page built at any other size would silently change what every one of them means physically.
- **Banderoll mode swaps the page dimensions before conversion**, so `rotate_pdf_90()` reconciles a raster source exactly as it does a landscape vector source.
- **Colour labeling is forced OFF for raster sources**, unconditionally, whatever `skip_colors` the request carried. The map describes the discrete paints a *vector* design was drawn with; raster pixels carry no such guarantee, and resampling, JPEG ringing or photographic gradients can drift a region into a mapped colour's tolerance band by accident. This is not self-enforcing: `_label_colors_on_page` reads the *rendered* page, so unlike the vector-only `extract_pdf_colors` it labels raster artwork happily unless stopped — and it was doing exactly that. There is no mixed vector/raster job to worry about: `is_raster_source` tests the whole upload and `image_to_pdf` replaces it with a single-page PDF.
- **PNG transparency is flattened onto `PINK_PAD`** (`#F490B5`) — the constant already used to pad partial pages — not white. This is mechanical, not cosmetic: that colour falls inside the `PINK_*` band `_background_mask` and `is_fully_background` classify as background, so transparent regions are read as "not real content" by the Klipp content-boundary scan and by trailing-page exclusion, exactly as a design's own painted leftover would be. Flattened onto white they would read as content, and `#FFFFFF` is the real paint code `Vit`.
  - Done as an underlay composited by the PDF renderer, not by hand on the source pixels: MuPDF pixmap samples are **premultiplied**, so correct manual compositing is `rgb + bg*(1-a)` and the intuitive `rgb*a + bg*(1-a)` silently darkens every partially-transparent edge.
  - The underlay is emitted **only** for images that actually carry alpha. It is a vector fill, so emitting it unconditionally made `extract_pdf_colors()` report a phantom `#F490B5` on every raster job. JPEG has no alpha in any variant and short-circuits without decoding.
- **Everything downstream is unchanged and verified so, not assumed so.** Rotation (default *and* `ruta_nedre`), strip/page counts and dimensions, Klipp cut detection, TIF-68 trailing-background exclusion, and page numbering were each exercised on real JPG and PNG input through the real `run_slice()` entry point — see the new `_validate_raster_source.py` (14/14 groups). The Klipp payoff from alpha flattening is verified rather than argued: a transparent design and a control that *paints* the same pink produce identical Klipp decisions and identical page-exclusion decisions, differing only on the 1-px border ring and the single transparency-edge row — the partial-coverage artifact class the pipeline already trims before deciding anything. The vector path is pixel-identical to 1.3.0.

### New field: `colors_analyzed`

`run_slice()` and the `/slice` response gained a `colors_analyzed` boolean.

Unknown-colour detection reads **vector** fills (`extract_pdf_colors` → `get_drawings`). A raster source has none, so it always returned `unknown_colors: []` — not because every colour matched, but because nothing was examined. Verified directly: the same two-colour design with one unmapped colour reports `['#FF00FF']` as a PDF and `[]` as a PNG. `api.py` then logged *"All design colors matched a colour_map entry."*, a false clean bill of health, and exactly the silent-unlabeled-shipping failure TIF-60 was raised for.

`[]` remains the right value (listing every distinct pixel value on a photo would be thousands of useless entries), but callers can now distinguish *nothing unknown* from *not checked*. `api.py` logs the distinction instead of claiming a match. **Consumers must not present an empty `unknown_colors` as "all colours matched" when `colors_analyzed` is `false`.**

Since raster sources are never labeled at all, `colors_analyzed` now simply tracks `effective_skip`: it is `false` for `skip_colors`, for `ENABLE_COLOR_LABELS=False`, and for every raster upload. Raster uploads need no unknown-colour detection — there is nothing to warn about when nothing is labeled.

## 1.3.0 — 2026-07-11

**Fixes the colour-map wipe that shipped `pest mitten 24x31,5m` (ruta_jobs `5ac70b43`) with zero labels (TIF-60, Urgent).**

- **`run_slice()` no longer discards the entire colour map when a design contains an unknown color.** The old code ran `if unknown_colors: color_map = {}`, so a single unmapped color anywhere in a design threw away *every other* mapping and the strips shipped completely unlabeled. Unknown colors are now reported and left unlabeled **individually**; every mapped color still gets its label. This matches the map-driven principle the rest of TIF-27 was built on.
- **The unknown-color test now uses the labeler's own tolerance semantics.** The old gate was exact hex membership (`c not in color_map`) while the labeler (`_code_masks`) claims pixels within `COLOR_MATCH_TOLERANCE`. Rendered vector fills routinely land 1–2 RGB units off the curated hex (the PDF's `#737272` vs the map's `#737271`), so the exact test reported colors as unknown that the labeler matches perfectly — **11 reported unknowns on pest-mitten where only 1 was real**. New `slicer.find_unknown_colors()` shares `_map_reps()` with `_code_masks()` so the two can never drift apart again.
  - *Consumer-visible:* the `unknown_colors` field in the `/slice` response keeps its shape but changes meaning — it now lists only colors with **no** map entry within tolerance. Expect far fewer entries. This is why the bump is MINOR rather than PATCH.
- **Unknown colors are logged.** `api.py` now emits a `WARNING` naming every unknown color, or an `INFO` confirming a full match. The original failure was invisible in the logs; it can't be again.
- **New permanent regression guard `_validate_unknown_colors.py`.** All six prior validation rounds drove `_label_colors_on_page()` / `_code_masks()` directly and bypassed `run_slice()` entirely, so the wipe branch had no coverage and could not fail a test. This guard exercises the real `run_slice()` entry point, and additionally pins down that multiple independent `"Skip"` entries (pink + orange) coexist without interfering with each other or with any mapped color. Verified to fail 0/4 against the pre-fix code and pass 4/4 after.

Re-verified against the real design and the exact 16-entry Supabase map production sent: **2,194 labels across all 21 strips**, where production drew **0**. `unknown_colors` correctly reports the single genuinely unmappable color (`#EEA8CA`).

> Persisting `unknown_colors` onto the `ruta_jobs` row requires a migration and a `createRutaJob` change in the **TifoDatabas** repo — see the TIF-60 report. Not included here.

## 1.2.1 — 2026-07-11

- **Color labeling is now live in production.** `ENABLE_COLOR_LABELS` flipped from `False` to `True` in `slicer.py` (TIF-27, final step). The labeling engine (rounds 1-5: zero-skip sizing, collision-aware placement, page-number exclusion zone) and the TIF-57 Klipp partial-page fix are both merged to `master` and validated. Per-page colour code labels will now render on sliced output by default.

## 1.2.0 and earlier

No changelog entries recorded prior to this release.
