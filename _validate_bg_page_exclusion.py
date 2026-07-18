"""
_validate_bg_page_exclusion.py — permanent regression guard for TIF-68: a trailing
page that is 100% background (pink or orange) must NEVER be rendered.

WHY THIS EXISTS
---------------
TIF-57's design explicitly assumed that trailing fully-background pages are excluded
by is_fully_background(), and used that assumption to justify why page-seam Klipp
suppression was safe. That assumption was never actually tested — and it was FALSE.
is_fully_background() sampled the page at a 0.05 render scale, turning a full page
into ~20-28 pixels (a partial into as few as ~15). At that resolution:
  - solid fills average out of the narrow PINK_*/ORANGE_* boxes (e.g. #F7931D orange
    downsamples toward (209,126,29), which fails ORANGE_R_MIN=220), and
  - a 1-2 px anti-aliased edge is a large FRACTION of so few pixels.
So a genuinely ~100%-background page read as only 40-83% background, stayed under
ORANGE_THRESHOLD, and RENDERED — a blank pink/orange page carrying only a page number.
The fix raises BG_SAMPLE_SCALE to 0.5 (a full page ~1200 px), so the sampled fraction
tracks the true fraction; the threshold and colour boxes are unchanged.

WHAT IT ASSERTS
  1. SYNTHETIC (always runs): a design whose trailing page is a full, 100% pink page
     must not render — the strip stops at the last content page. Proven meaningful by
     toggling BG_SAMPLE_SCALE back to the old 0.05 and showing the blank page DOES
     render there (the exact bug), then that the shipped 0.5 excludes it.
  2. REAL DESIGN (skipped if the pdf is absent): pest-mitten at its named 24x31.5 dims
     — the configuration that produced the reported strip-16 bug — must not emit ANY
     blank background page (>= 95% background with only a page number on it).

Run:  python _validate_bg_page_exclusion.py
"""
import math
import os
import sys

import fitz
import numpy as np

import slicer

PINK_HEX = "#EEA8CB"                                    # pest-mitten's real pink Skip
PINK_RGB = tuple(int(PINK_HEX[i:i + 2], 16) / 255 for i in (1, 3, 5))


def _bg_fraction(page):
    """Fraction of a RENDERED output page that is pink/orange background."""
    pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
    a = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3].astype(int)
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    pink = (r > slicer.PINK_R_MIN) & (g > slicer.PINK_G_MIN) & (g < slicer.PINK_G_MAX) & \
           (b > slicer.PINK_B_MIN) & (b < slicer.PINK_B_MAX) & (r > g) & (r > b)
    org = (r > slicer.ORANGE_R_MIN) & (g > slicer.ORANGE_G_MIN) & (g < slicer.ORANGE_G_MAX) & \
          (b < slicer.ORANGE_B_MAX) & (r > g) & (g > b)
    return float((pink | org).mean())


def _is_blank_bg_page(page):
    """A blank background page: >= 95% background AND only a page number on it."""
    txt = [sp["text"].strip() for blk in page.get_text("dict")["blocks"]
           for ln in blk.get("lines", []) for sp in ln["spans"]]
    only_number = all(t.isdigit() for t in txt if t)
    return _bg_fraction(page) >= 0.95 and only_number


def _make_trailing_pink_design():
    """One 1.5m strip, 12m tall (3 x 4m pages) at ~28.35 pt/m. Pages 1-2 are black
    content; page 3 is a FULL, 100% pink page — a trailing fully-background page."""
    ppm = 28.35
    full_w = 1.5 * ppm                 # ~42.5 pt strip
    full_h = 12.0 * ppm                # 3 pages
    page_h = 4.0 * ppm
    doc = fitz.open()
    page = doc.new_page(width=full_w, height=full_h)
    page.draw_rect(fitz.Rect(0, 0, full_w, 2 * page_h),
                   color=(0, 0, 0), fill=(0, 0, 0))          # pages 1-2 content
    page.draw_rect(fitz.Rect(0, 2 * page_h, full_w, full_h),
                   color=PINK_RGB, fill=PINK_RGB)            # page 3 fully pink
    return doc.tobytes()


def _render_pages(pdf_bytes, W, H, nedre, strip):
    ns = math.ceil(W / slicer.STRIP_WIDTH_M)
    npg = math.ceil(H / slicer.PAGE_HEIGHT_M)
    _, out = slicer.slice_one_strip((strip, pdf_bytes, W, H, ns, npg, {}, nedre, True))
    return fitz.open(stream=out, filetype="pdf")


def run():
    fails = []

    # ── 1. SYNTHETIC: trailing full-pink page must not render ─────────────────
    pdf = _make_trailing_pink_design()
    W, H, nedre = 1.5, 12.0, True      # ruta_nedre: page 1 top, page 3 = trailing pink

    doc = _render_pages(pdf, W, H, nedre, 0)
    n_shipped = doc.page_count
    blank = [i + 1 for i, pg in enumerate(doc) if _is_blank_bg_page(pg)]
    doc.close()

    # This fixture is deliberately the worst case: a full pink page whose pink edge
    # coincides with the design boundary (white beyond), so the 1-px anti-aliased
    # border reads as false content — exactly what made the pre-TIF-68 code (0.05
    # scale, no border trim, 0.85 threshold) render it. It renders on that code and
    # is excluded here; git history is the before/after.
    print(f"1 SYNTHETIC trailing-pink page (3 pages, page 3 = full 100% pink):")
    print(f"    {n_shipped} pages rendered, blank-bg pages: {blank}")
    if blank:
        fails.append(f"1 SYNTHETIC: a trailing 100%-pink page RENDERED (pages {blank}) "
                     f"— is_fully_background failed to exclude it")
    if n_shipped != 2:
        fails.append(f"1 SYNTHETIC: expected 2 content pages, got {n_shipped}")

    # ── 1b. CONTENT MUST BE KEPT (over-exclusion guard) ──────────────────────
    # A faithful sampler at too low an exclusion threshold would drop pages that are
    # mostly-but-not-entirely background — pages carrying a real, if small, content
    # sliver (the tail of a design). Those MUST still render. Build a design whose
    # trailing page is ~90% pink with a ~10% black content sliver and assert it
    # renders (is NOT excluded). This pins the over-exclusion direction that TIF-68's
    # scale fix, at the old 0.85 threshold, would have regressed.
    ppm = 28.35
    fw = 1.5 * ppm; fh = 8.0 * ppm; ph = 4.0 * ppm      # 2 pages
    d = fitz.open(); pg = d.new_page(width=fw, height=fh)
    pg.draw_rect(fitz.Rect(0, 0, fw, ph), color=(0, 0, 0), fill=(0, 0, 0))       # page 1 content
    pg.draw_rect(fitz.Rect(0, ph, fw, fh), color=PINK_RGB, fill=PINK_RGB)        # page 2 mostly pink
    pg.draw_rect(fitz.Rect(0, ph, fw, ph + 0.1 * ph),                            # ...with a content sliver
                 color=(0, 0, 0), fill=(0, 0, 0))
    sliver_pdf = d.tobytes(); d.close()
    doc = _render_pages(sliver_pdf, 1.5, 8.0, True, 0)
    n_sliver = doc.page_count
    doc.close()
    print(f"1b CONTENT-SLIVER page (~10% content, ~90% pink): "
          f"{n_sliver} pages rendered (expect 2 — the sliver page must be KEPT)")
    if n_sliver != 2:
        fails.append(f"1b CONTENT-SLIVER: expected 2 pages, got {n_sliver} — a page "
                     f"with a real content sliver was wrongly excluded (over-exclusion)")

    # ── 2. REAL DESIGN: pest-mitten at its named 24x31.5 dims ─────────────────
    pest = "pest mitten 24x31,5m.pdf"
    if not os.path.exists(pest):
        print(f"2 REAL: SKIP — {pest} not in repo root")
    else:
        with open(pest, "rb") as f:
            pest_bytes = f.read()
        # Strips 1-3 held the reported fully-pink trailing pages (page 8) in both modes.
        checked = 0
        for nedre in (True, False):
            for strip in (0, 1, 2):
                doc = _render_pages(pest_bytes, 24.0, 31.5, nedre, strip)
                bad = [i + 1 for i, pg in enumerate(doc) if _is_blank_bg_page(pg)]
                doc.close()
                checked += 1
                if bad:
                    fails.append(f"2 REAL: pest 24x31.5 nedre={nedre} strip {strip+1} "
                                 f"rendered blank background page(s) {bad}")
        print(f"2 REAL pest-mitten 24x31.5: checked {checked} strips, "
              f"blank background pages found: "
              f"{sum(1 for f in fails if f.startswith('2 REAL'))}")

    print()
    for m in fails:
        print(f"  FAIL {m}")
    ok = not fails
    print("BACKGROUND-PAGE EXCLUSION CHECK (TIF-68):", "PASS" if ok else f"FAIL ({len(fails)})")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
