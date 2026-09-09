# Changelog

## 1.5.0 — 2026-09-08

**The Skissyta's page height is now a per-job parameter (TIF-87). Default output is unchanged — byte-identical, and proven so.**

- **New `page_height_m` on `run_slice()` and `POST /slice`** (optional, default `4.0`). It is how tall **one printed ruta** is. Threaded through `run_slice → _slice_pdf → slice_one_strip → generate_grid_pdf`.
- **Strip width is deliberately NOT configurable.** `STRIP_WIDTH_M` stays locked at 1.5 m because it is set by the fabric, not by the job, so every output page is still 42.52 pt tall and only its width varies.

### The ticket's scaling premise was wrong, and measuring it is what settled it

TIF-87 was raised on the belief that a smaller Skissyta means a smaller scale — that at "half scale" a `KLIPP_LINE_MARGIN_PT` of 2 pt would silently come to mean 14 cm of fabric instead of 7 cm — and that every calibrated constant therefore had to be made scale-relative.

It does not work that way. Nothing in `slicer.py` declares a pts-per-metre; it is **derived** as `full_w / width_m` from the **design**, which is drawn at 1:100. Measured across page heights from 1.0 m to 8.0 m on pest-mitten, the output is **28.3493 pts/m at every one of them**, and 4 pt is 14.11 cm of fabric at every one of them. A shorter ruta is a *smaller page at the same scale*, not a smaller scale.

So the auto-scale as originally specified would have **introduced** the drift it was meant to prevent. Constants split into three classes instead:

- **Physical (fabric-referenced) — unchanged, and must stay unchanged.** `KLIPP_MIN_PINK_PT`, `KLIPP_LINE_MARGIN_PT`, `MIN_LABEL_PATCH_SIZE_PT`, `MIN_PATCH_PX`, `LABEL_FONT_DEFAULT`, the label collision `MARGIN`. Verified on real designs: running pest-övre at 1.0–8.0 m page heights leaves the font-size distribution's shape intact, which is what "the calibration still holds" looks like.
- **Colourimetric — unchanged.** `COLOR_MATCH_TOLERANCE` stays at 28. Not merely "because it isn't a length": the render drift it absorbs is a function of `LABEL_RENDER_SCALE × pts_per_m`, which is 56.7 px per fabric metre at *every* page height, so its input provably does not change. And TIF-55 left ~2.0 units of headroom before the tightest distinct-colour pair (8010/8500 at 23.94) collides — there is nothing to spend. The reasoning is now recorded on the constant itself so it stops being re-asked.
- **Page furniture (sheet-referenced) — scaled, but only for FIT.** The page number, the "Klipp" word, cut-line stroke weight and dash period. Strip PDFs are viewed and printed at true scale, so a 6 pt page number *should* stay 6 pt on every job; furniture is shrunk only where it physically stops fitting on a shorter page.

### `furniture_scale()` — byte-identity by construction

```python
def furniture_scale(page_height_m):
    return min(1.0, page_height_m / FURNITURE_FIT_FLOOR_M)   # floor = 1.0 m
```

Anchored at the **measured fit floor** (~0.97 m, where "Klipp" stops fitting at any font size), not at the 4.0 m default. Above the floor it returns **exactly `1.0`**, and `x * 1.0` is exact in IEEE-754 — so the default, and every larger page height, reproduces pre-1.5.0 output *by construction* rather than by verification. Measured floors, for the record: 0.476 m (the page-number exclusion zone is wider than the page), 0.659 m (a 2-digit page number overflows), 0.966 m ("Klipp" cannot fit).

Verified anyway, with the TIF-73 rendered-pixel digest approach. With `page_height_m` left defaulted, the full matrix was run — ENAD / pest-mitten / pest-övre × both rotation modes × both banderoll states, 12 configurations — and master and this branch are **identical in every one**. Passing `4.0` explicitly was checked on pest-övre's four configurations only, not the full matrix; it reaches `run_slice` as the same float that the default binds, so it exercises the same path rather than a second one.

### Also in this release

- **Six previously hardcoded page-furniture literals are now named constants**: `PAGE_NUM_FONT_PT`, `PAGE_NUM_INSET_PT`, `PAGE_NUM_BASELINE_PT`, `KLIPP_TEXT_PAD_PT`, `KLIPP_LINE_WIDTH_PT`, `KLIPP_DASH_PT`. The page number's size and position were inline `width - 12` / `10` / `fontsize=6` while `PAGE_NUM_EXCL_*` existed specifically to reserve the box they occupy — the two were only kept consistent by hand.
- **An all-background strip no longer crashes the whole job.** A strip whose every page `is_fully_background()` excludes left `out_doc` with no pages, and `save()` raised `"cannot save with zero pages"`, propagating out of the thread pool and failing the entire slice (kenta strip 14). It now emits **one blank pink page**, so `strip-NN` numbering stays dense for an operator counting strips against the grid. This was **not** a TIF-87 regression and is measured not to be — it fires identically at every page height from 1.0 to 8.0 m, the 4.0 m default included — but it is a real crash and the code was open. `run_guards.py` no longer excludes kenta strip 14.
- **New permanent guard `_validate_skissyta.py`**, covering the `furniture_scale` identity, every furniture constant reproducing its literal at the default, end-to-end geometry at non-default page heights, a Skissyta taller than the whole design, Klipp emission across page heights, and the all-background strip.

### Known behaviour at short page heights

`KLIPP_MIN_PINK_PT` is 12 pt = **42.3 cm of blank fabric**, a fixed physical requirement. On a short ruta that is a large fraction of the page, so cut markings legitimately become rarer — measured on pest-övre: 11 Klipp markings at 4.0 m, 5 at 2.0 m, **0 at 1.0 m**. Below ~0.97 m the "Klipp" text cannot fit at any font size. The web app warns below 1.0 m and outside 2–8 m; input stays free by design. These thresholds are **technically derived** — from what the slicer measurably does — not from any known fabric or printing constraint, and should be revisited once that constraint is written down.

> The `page_height_m` column on `ruta_jobs`, the `createRutaJob` change and the "Skissyta" form section live in the **TifoDatabas** repo.

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
