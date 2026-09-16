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
  3. LILAC #F9CDE7 (always runs): a trailing 100%-lilac page is excluded (1c), a
     ~90%-lilac page with a content sliver is kept (1d), and the LILAC_* band is
     disjoint from PINK_* and ORANGE_* over the whole RGB cube (1e).

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
LILAC_HEX = "#F9CDE7"                                   # LILAC_* background band
LILAC_RGB = tuple(int(LILAC_HEX[i:i + 2], 16) / 255 for i in (1, 3, 5))


def _bg_fraction(page):
    """Fraction of a RENDERED output page that is pink/orange background."""
    pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
    a = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3].astype(int)
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    pink = (r > slicer.PINK_R_MIN) & (g > slicer.PINK_G_MIN) & (g < slicer.PINK_G_MAX) & \
           (b > slicer.PINK_B_MIN) & (b < slicer.PINK_B_MAX) & (r > g) & (r > b)
    org = (r > slicer.ORANGE_R_MIN) & (g > slicer.ORANGE_G_MIN) & (g < slicer.ORANGE_G_MAX) & \
          (b < slicer.ORANGE_B_MAX) & (r > g) & (g > b)
    lilac = (r > slicer.LILAC_R_MIN) & (g > slicer.LILAC_G_MIN) & (g < slicer.LILAC_G_MAX) & \
            (b > slicer.LILAC_B_MIN) & (b < slicer.LILAC_B_MAX) & (r > g) & (r > b)
    return float((pink | org | lilac).mean())


def _is_blank_bg_page(page):
    """A blank background page: >= 95% background AND only a page number on it."""
    txt = [sp["text"].strip() for blk in page.get_text("dict")["blocks"]
           for ln in blk.get("lines", []) for sp in ln["spans"]]
    only_number = all(t.isdigit() for t in txt if t)
    return _bg_fraction(page) >= 0.95 and only_number


def _make_trailing_pink_design(bg_rgb=PINK_RGB):
    """One 1.5m strip, 12m tall (3 x 4m pages) at ~28.35 pt/m. Pages 1-2 are black
    content; page 3 is a FULL, 100% background page (pink by default) — a trailing
    fully-background page."""
    ppm = 28.35
    full_w = 1.5 * ppm                 # ~42.5 pt strip
    full_h = 12.0 * ppm                # 3 pages
    page_h = 4.0 * ppm
    doc = fitz.open()
    page = doc.new_page(width=full_w, height=full_h)
    page.draw_rect(fitz.Rect(0, 0, full_w, 2 * page_h),
                   color=(0, 0, 0), fill=(0, 0, 0))          # pages 1-2 content
    page.draw_rect(fitz.Rect(0, 2 * page_h, full_w, full_h),
                   color=bg_rgb, fill=bg_rgb)                # page 3 fully background
    return doc.tobytes()


def _make_sliver_design(bg_rgb):
    """One 1.5m strip, 8m tall (2 x 4m pages). Page 1 is black content; page 2 is
    ~90% background with a ~10% black content sliver — a page that MUST render."""
    ppm = 28.35
    fw = 1.5 * ppm; fh = 8.0 * ppm; ph = 4.0 * ppm      # 2 pages
    d = fitz.open(); pg = d.new_page(width=fw, height=fh)
    pg.draw_rect(fitz.Rect(0, 0, fw, ph), color=(0, 0, 0), fill=(0, 0, 0))       # page 1 content
    pg.draw_rect(fitz.Rect(0, ph, fw, fh), color=bg_rgb, fill=bg_rgb)            # page 2 mostly bg
    pg.draw_rect(fitz.Rect(0, ph, fw, ph + 0.1 * ph),                            # ...with a content sliver
                 color=(0, 0, 0), fill=(0, 0, 0))
    out = d.tobytes(); d.close()
    return out


def _render_pages(pdf_bytes, W, H, nedre, strip):
    ns = math.ceil(W / slicer.STRIP_WIDTH_M)
    npg = math.ceil(H / slicer.PAGE_HEIGHT_M)
    _, out = slicer.slice_one_strip((strip, pdf_bytes, W, H, ns, npg, {}, nedre, True,
                                     slicer.PAGE_HEIGHT_M))
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
    doc = _render_pages(_make_sliver_design(PINK_RGB), 1.5, 8.0, True, 0)
    n_sliver = doc.page_count
    doc.close()
    print(f"1b CONTENT-SLIVER page (~10% content, ~90% pink): "
          f"{n_sliver} pages rendered (expect 2 — the sliver page must be KEPT)")
    if n_sliver != 2:
        fails.append(f"1b CONTENT-SLIVER: expected 2 pages, got {n_sliver} — a page "
                     f"with a real content sliver was wrongly excluded (over-exclusion)")

    # ── 1c. LILAC #F9CDE7: trailing full page must not render ─────────────────
    # Same fixture as 1, background swapped for the LILAC_* band's colour. Proven
    # meaningful the same way: on the parent commit (no LILAC_* band) page 3 renders.
    doc = _render_pages(_make_trailing_pink_design(LILAC_RGB), 1.5, 12.0, True, 0)
    n_lilac = doc.page_count
    blank = [i + 1 for i, pg in enumerate(doc) if _is_blank_bg_page(pg)]
    doc.close()
    print(f"1c SYNTHETIC trailing-lilac page (3 pages, page 3 = full 100% {LILAC_HEX}):")
    print(f"    {n_lilac} pages rendered, blank-bg pages: {blank}")
    if blank or n_lilac != 2:
        fails.append(f"1c LILAC: expected 2 content pages and no blank page, got "
                     f"{n_lilac} pages, blank {blank} — is_fully_background failed to "
                     f"exclude a 100% {LILAC_HEX} page")

    # ── 1d. LILAC content sliver must be KEPT (over-exclusion guard) ─────────
    doc = _render_pages(_make_sliver_design(LILAC_RGB), 1.5, 8.0, True, 0)
    n_lsliver = doc.page_count
    doc.close()
    print(f"1d CONTENT-SLIVER page (~10% content, ~90% lilac): "
          f"{n_lsliver} pages rendered (expect 2 — the sliver page must be KEPT)")
    if n_lsliver != 2:
        fails.append(f"1d LILAC CONTENT-SLIVER: expected 2 pages, got {n_lsliver} — a "
                     f"lilac page with a real content sliver was wrongly excluded")

    # ── 1e. LILAC is DISJOINT from PINK and ORANGE ───────────────────────────
    # Exhaustive over all 256^3 triples. An overlap would let a design's intentional
    # pink read as lilac background (or vice versa). Also pins that the known real
    # pinks stay pink-only.
    v = np.arange(256, dtype=np.int32)
    R, G, B = v[:, None, None], v[None, :, None], v[None, None, :]
    in_pink = (R > slicer.PINK_R_MIN) & (G > slicer.PINK_G_MIN) & (G < slicer.PINK_G_MAX) & \
              (B > slicer.PINK_B_MIN) & (B < slicer.PINK_B_MAX) & (R > G) & (R > B)
    in_org = (R > slicer.ORANGE_R_MIN) & (G > slicer.ORANGE_G_MIN) & (G < slicer.ORANGE_G_MAX) & \
             (B < slicer.ORANGE_B_MAX) & (R > G) & (G > B)
    in_lilac = (R > slicer.LILAC_R_MIN) & (G > slicer.LILAC_G_MIN) & (G < slicer.LILAC_G_MAX) & \
               (B > slicer.LILAC_B_MIN) & (B < slicer.LILAC_B_MAX) & (R > G) & (R > B)
    n_pl, n_ol = int((in_pink & in_lilac).sum()), int((in_org & in_lilac).sum())
    print(f"1e BAND DISJOINTNESS over 256^3: pink&lilac={n_pl}  orange&lilac={n_ol}")
    if n_pl or n_ol:
        fails.append(f"1e BANDS OVERLAP: pink&lilac={n_pl} orange&lilac={n_ol} RGB triples")
    for hx in ("#F490B5", PINK_HEX, "#EEA8CA"):
        t = tuple(int(hx[i:i + 2], 16) for i in (1, 3, 5))
        if not in_pink[t] or in_lilac[t]:
            fails.append(f"1e {hx}: expected pink-only, got pink={bool(in_pink[t])} "
                         f"lilac={bool(in_lilac[t])}")

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
    print("BACKGROUND-PAGE EXCLUSION CHECK (TIF-68 + LILAC):", "PASS" if ok else f"FAIL ({len(fails)})")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
