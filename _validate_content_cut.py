"""
_validate_content_cut.py — permanent guard for the CONTENT-driven "Klipp" cut
marking (TIF-57). Drives the REAL pipeline (run_slice), not an isolated harness.

WHY THIS EXISTS
---------------
The cut marking used to be triggered by is_partial — a purely GEOMETRIC test:
"does the source clip fail to cover the full output page?" That is blind to the
case that actually ships. pest-mitten is 24 m tall with 4 m pages and 31.5 m wide
with 1.5 m strips, so it divides EVENLY into 21 strips x 6 full pages: no page is
ever partial, is_partial never fires, and no cut line was drawn anywhere on the
design — even though the artwork stops partway into page 6 and everything past it
is painted background (#EEA8CB, mapped to "Skip"), i.e. fabric to be cut off.

Both prior guards missed this by construction:
  - _validate_klipp_partial.py (TIF-57) FORCES partial pages by picking heights
    that do not divide evenly, so it only ever exercises the geometric trigger.
  - the font-sizing harnesses use the even dimensions and assert on labels, never
    on the cut marking.
So the one configuration that matters in production — even dimensions, content
ending mid-page inside painted background — had zero coverage. That is the gap
this guard closes.

WHAT IT ASSERTS (on the real pest-mitten design, through run_slice)
  1. NOT A SINGLE page is geometrically partial (no pink pad anywhere). This is
     the point: it proves the old is_partial trigger is inert on this design, so
     any Klipp found below is attributable to content scanning alone.
  2. At most ONE Klipp marking per strip — the boundary is a property of the
     whole strip, not of each page.
  3. Strip 21's Klipp lands on page 6, mid-page (not flush with either edge).
  4. Every Klipp's dashed line sits at the TRUE content boundary, recomputed here
     INDEPENDENTLY from the source PDF (own render, own classifier) rather than
     trusted from the code under test.
  5. The "Klipp" text is on-page and clear of the page-number glyph (TIF-57's
     invariant, which must survive the retrigger).

Run:  python _validate_content_cut.py
"""
import math
import sys

import fitz
import numpy as np
from scipy import ndimage

import slicer

PDF        = "pest mitten 24x31,5m.pdf"
WIDTH_M    = 31.5
HEIGHT_M   = 24.0
RUTA_NEDRE = True
CUT_STRIP  = 21          # 1-based; the strip Emil reported
CUT_PAGE   = 6           # 1-based; where the artwork ends on that strip

# The real production map (Supabase source='manual'), incl. the Skip entry that
# marks the not-to-be-cut background. This is what api.py hands run_slice.
COLOUR_MAP = {
    "#EEA8CB": "Skip", "#392C1B": "8010", "#231F20": "8010", "#6A4F32": "6020",
    "#E5C699": "1515", "#172816": "7020", "#030505": "S",    "#947042": "5030",
    "#BD9164": "3030", "#1C5429": "5540", "#90908F": "4500", "#D4D2CE": "1500",
    "#313131": "8500", "#1F763B": "3560", "#B2B0AF": "3000", "#737372": "6000",
    "#515150": "7500", "#FFFFFF": "V",
}

PINK_PAD = (slicer.PINK_PAD_R, slicer.PINK_PAD_G, slicer.PINK_PAD_B)
TOL_PT   = 1.0   # placement must match the independent boundary to within this


def _spans(page):
    return [sp for blk in page.get_text("dict")["blocks"]
            for ln in blk.get("lines", []) for sp in ln["spans"]]


def _pink_pads(page):
    return [d for d in page.get_drawings()
            if d.get("fill") and all(abs(a - b) < 0.03
                                     for a, b in zip(d["fill"], PINK_PAD))]


def _dashed_lines(page):
    return [d for d in page.get_drawings()
            if d.get("dashes") and d["dashes"] not in ("[] 0", "[]0")
            and any(it[0] == "l" for it in d["items"])]


def expected_boundary(src_doc, strip_idx):
    """
    Recompute the cut boundary from the SOURCE pdf, independently of slicer's
    _find_cut_boundary — own render scale, own background test — so this guard
    cannot pass merely because the code agrees with itself.

    Returns (page_num0, cut_x) or None. Deliberately mirrors the seq() contract:
        seq = design_y (ruta_nedre);  page = floor(seq / page_h);  x = seq % page_h
    """
    p = src_doc[0]
    full_w, full_h = p.rect.width, p.rect.height
    strip_w = slicer.STRIP_WIDTH_M * (full_w / WIDTH_M)
    page_h  = slicer.PAGE_HEIGHT_M * (full_h / HEIGHT_M)
    x0 = strip_idx * strip_w
    x1 = min((strip_idx + 1) * strip_w, full_w)

    S = 4.0   # NOT slicer's LABEL_RENDER_SCALE — independent resolution
    pix = p.get_pixmap(matrix=fitz.Matrix(S, S), clip=fitz.Rect(x0, 0, x1, full_h),
                       colorspace=fitz.csRGB)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)[:, :, :3].astype(np.int32)

    # Independent background test: straight Euclidean distance to the Skip hexes,
    # no nearest-rep argmin, no legacy colour box.
    skip_rgb = [np.array([int(h[i:i + 2], 16) for i in (1, 3, 5)], dtype=np.int32)
                for h, code in COLOUR_MAP.items() if code == "Skip"]
    bg = np.zeros(arr.shape[:2], dtype=bool)
    for rgb in skip_rgb:
        bg |= ((arr - rgb) ** 2).sum(axis=2) <= slicer.COLOR_MATCH_TOLERANCE ** 2

    content = ~bg
    # The 1-pixel border of the render is partial-coverage raster artifact (the
    # clip's edge blended with what lies outside it), not artwork — see
    # slicer._find_cut_boundary. That is a physical fact about rasterizing a clip,
    # so this independent implementation must account for it too; everything else
    # here (scale, classifier, patch threshold) stays deliberately different.
    if content.shape[0] >= 3 and content.shape[1] >= 3:
        content[0, :] = content[-1, :] = False
        content[:, 0] = content[:, -1] = False

    lbl, n = ndimage.label(content)
    if n == 0:
        return None
    counts  = np.bincount(lbl.ravel())
    # MIN_PATCH_PX is calibrated at LABEL_RENDER_SCALE; area scales as S^2.
    min_px  = slicer.MIN_PATCH_PX * (S / slicer.LABEL_RENDER_SCALE) ** 2
    keep    = counts >= min_px
    keep[0] = False
    content = keep[lbl]

    rows = np.flatnonzero(content.any(axis=1))
    if rows.size == 0:
        return None

    r_last       = int(rows[-1])                 # ruta_nedre: seq rises with y
    boundary_seq = (r_last + 1) / S
    seq_mid      = (r_last + 0.5) / S
    num_pages    = math.ceil(HEIGHT_M / slicer.PAGE_HEIGHT_M)
    pn     = min(max(int(seq_mid // page_h), 0), num_pages - 1)
    b_star = boundary_seq - pn * page_h
    # TIF-67 (rev): one upfront gate — total pink <= KLIPP_MIN_PINK_PT suppresses the
    # whole marking — then the line sits KLIPP_LINE_MARGIN_PT into the pink. Mirror
    # both here so this independent recomputation predicts the SAME line the code
    # draws (still an independent render/classifier — only the geometry matches).
    if page_h - b_star <= slicer.KLIPP_MIN_PINK_PT:
        return None
    return pn, b_star + slicer.KLIPP_LINE_MARGIN_PT


def run():
    try:
        with open(PDF, "rb") as f:
            pdf_bytes = f.read()
    except FileNotFoundError:
        print(f"SKIP: {PDF} not found in repo root")
        return True

    num_strips = math.ceil(WIDTH_M / slicer.STRIP_WIDTH_M)
    print(f"{PDF}  {WIDTH_M}x{HEIGHT_M} m  ruta_nedre={RUTA_NEDRE}")
    print(f"divides evenly: {WIDTH_M / slicer.STRIP_WIDTH_M:.0f} strips x "
          f"{HEIGHT_M / slicer.PAGE_HEIGHT_M:.0f} pages -> NO page is ever partial, "
          f"so is_partial can never fire on this design")
    print("driving the real pipeline: run_slice()\n")

    result = slicer.run_slice(pdf_bytes, WIDTH_M, HEIGHT_M,
                              ruta_nedre=RUTA_NEDRE, colour_map=COLOUR_MAP)
    src_doc = fitz.open(PDF)

    fails    = 0
    n_pads   = 0
    n_klipp  = 0
    checked  = 0

    for entry in result["strips"]:
        strip_num = int(entry["filename"].split("-")[1].split(".")[0])
        doc = fitz.open(stream=entry["bytes"], filetype="pdf")

        klipp_pages = []
        for i, pg in enumerate(doc):
            pads = _pink_pads(pg)
            n_pads += len(pads)
            if pads:
                print(f"  FAIL strip {strip_num} p{i+1}: pink pad present — this "
                      f"design has no partial pages; is_partial must never fire")
                fails += 1
            sp    = _spans(pg)
            klipp = next((s for s in sp if s["text"].strip().startswith("Kli")), None)
            if klipp:
                klipp_pages.append((i, pg, klipp))

        n_klipp += len(klipp_pages)

        # (2) at most one per strip
        if len(klipp_pages) > 1:
            print(f"  FAIL strip {strip_num}: {len(klipp_pages)} Klipp markings "
                  f"(pages {[i+1 for i, _, _ in klipp_pages]}) — must be at most 1")
            fails += 1

        exp = expected_boundary(src_doc, strip_num - 1)

        if exp is None:
            if klipp_pages:
                print(f"  FAIL strip {strip_num}: Klipp drawn but independent scan "
                      f"finds no boundary (content runs to the edge)")
                fails += 1
            continue

        exp_pn, exp_x = exp
        if not klipp_pages:
            print(f"  FAIL strip {strip_num}: independent scan finds a boundary on "
                  f"page {exp_pn+1} at x={exp_x:.2f} but no Klipp was drawn")
            fails += 1
            continue

        i, pg, klipp = klipp_pages[0]
        checked += 1
        bad = []

        # (4) right page, and the dashed line at the TRUE boundary
        if i != exp_pn:
            bad.append(f"on page {i+1}, independent scan says page {exp_pn+1}")
        lines = _dashed_lines(pg)
        if not lines:
            bad.append("dashed cut line missing")
        else:
            lx = lines[0]["rect"].x0
            if abs(lx - exp_x) > TOL_PT:
                bad.append(f"line at x={lx:.2f}, independent boundary x={exp_x:.2f} "
                           f"(off by {abs(lx - exp_x):.2f} pt > {TOL_PT})")

        # (3)/(5) mid-page, on-page, clear of the page number
        w  = pg.rect.width
        kb = klipp["bbox"]
        if kb[0] < -0.5 or kb[2] > w + 0.5:
            bad.append(f'"Klipp" off-page: x[{kb[0]:.1f}..{kb[2]:.1f}] width {w:.1f}')
        # (6) TIF-67 HARD RULE: the text ALWAYS sits to the RIGHT of the line, never
        # left. Any left-of-line placement is a bug (this is the invariant TIF-67
        # tightened over TIF-57's now-removed left-side fallback).
        if lines and kb[0] < lines[0]["rect"].x0 - 0.5:
            bad.append(f'"Klipp" is LEFT of the cut line (x0={kb[0]:.1f} < line '
                       f'{lines[0]["rect"].x0:.1f}) — TIF-67 forbids this')
        pnum = next((s for s in _spans(pg) if s["text"].strip().isdigit()), None)
        if pnum:
            pb = pnum["bbox"]
            if not (kb[2] < pb[0] or pb[2] < kb[0]):
                bad.append(f'"Klipp" collides with page number "{pnum["text"].strip()}"')

        status = "OK  " if not bad else "FAIL"
        print(f"  {status} strip {strip_num:2d} p{i+1}: cut at x={exp_x:6.2f} / "
              f"{w:.2f}pt ({exp_x/w:.0%} across)")
        for m in bad:
            print(f"        - {m}")
            fails += 1
        doc.close()

    # (3) the reported scenario, specifically
    print()
    tgt = next(e for e in result["strips"]
               if int(e["filename"].split("-")[1].split(".")[0]) == CUT_STRIP)
    doc = fitz.open(stream=tgt["bytes"], filetype="pdf")
    hit = [i + 1 for i, pg in enumerate(doc)
           if any(s["text"].strip().startswith("Kli") for s in _spans(pg))]
    if hit == [CUT_PAGE]:
        pg = doc[CUT_PAGE - 1]
        lx = _dashed_lines(pg)[0]["rect"].x0
        print(f"REPORTED SCENARIO: strip {CUT_STRIP} Klipp on page {CUT_PAGE} at "
              f"x={lx:.2f} of {pg.rect.width:.2f}pt ({lx/pg.rect.width:.0%} across) — OK")
    else:
        print(f"REPORTED SCENARIO FAIL: strip {CUT_STRIP} Klipp on pages {hit}, "
              f"expected exactly [{CUT_PAGE}]")
        fails += 1
    doc.close()
    src_doc.close()

    print(f"\nStrips: {num_strips}   Klipp markings: {n_klipp}   "
          f"boundaries cross-checked: {checked}   pink pads (must be 0): {n_pads}")
    ok = fails == 0
    print("CONTENT-CUT CHECK:", "PASS" if ok else f"FAIL ({fails})")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
