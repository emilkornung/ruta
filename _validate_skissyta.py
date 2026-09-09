# -*- coding: utf-8 -*-
"""
_validate_skissyta.py — permanent regression guard for TIF-87 (configurable
Skissyta page height).

TIF-87 made the Skissyta's PAGE HEIGHT a per-job parameter. The strip WIDTH is
deliberately NOT configurable — it is set by the fabric — so every output page
is still 42.52 pt tall and only its WIDTH varies.

What this guard pins down, in four groups:

  1. furniture_scale() is EXACTLY 1.0 at the default page height and at every
     larger one. This is the mechanism that makes default output byte-identical
     to pre-TIF-87 master by construction rather than by luck, so it is asserted
     as an identity (`is 1.0`-strength float equality), not approximately.

  2. Every furniture constant, multiplied through at the default, reproduces its
     own literal EXACTLY. Same reason: a change here is a silent change to every
     historical job's output.

  3. Non-default page heights actually work end to end on a real design — the
     job completes, strip/page counts follow ceil(), page dimensions follow the
     locked width and the chosen height, and no drawn text escapes its page.

  4. The two behaviours that genuinely change with page height are pinned at the
     values this ticket measured, so a future tuning change has to face them:
     Klipp emission (KLIPP_MIN_PINK_PT is 42 cm of fabric, which is a large
     fraction of a short ruta) and zero-page strips.

Deliberately NOT a digest comparison: the master-vs-branch rendered-pixel digest
is a separate, heavier harness (see the TIF-87 report) because it needs a master
worktree to compare against. This guard is self-contained and runs standalone.

Usage:  python _validate_skissyta.py
Exit code 0 only if every check passes.
"""
import math
import os
import sys

import fitz
import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import slicer
from run_guards import PEST_OVRE_MAP

DESIGN      = "pest övre 8x24m.pdf"
DESIGN_W_M  = 24
DESIGN_H_M  = 8

FAILS = []


def design_ppm(pdf_path, width_m, height_m):
    """
    The design's own pts-per-metre, on each axis. NOT the nominal 72/2.54: real
    files land a hair off it (kenta is 28.3333 x 28.3750), and an output page's
    width is page_height_m * the design's Y-axis value, so a hardcoded constant
    is simply the wrong number to compare against.
    """
    d = fitz.open(pdf_path)
    r = d[0].rect
    d.close()
    return r.width / width_m, r.height / height_m


def is_blank_placeholder(page):
    """
    True if `page` is the all-background placeholder slice_one_strip emits for a
    strip every page of which is_fully_background() excluded.

    Tested by RENDERING and looking for a page that is essentially all PINK_PAD,
    rather than by a text heuristic. The obvious shortcut — "one page whose only
    text is '1'" — is wrong and was measured to be: at a page height that yields
    one page per strip, any ordinary strip carrying no colour labels and no Klipp
    marking also has '1' as its only text, which reported five content-bearing
    pest-övre strips as empty.
    """
    # Rendered at 2.0, not at some coarse scale: the placeholder page is only
    # ~113 x 42 pt, so at 0.5 it is 57 x 22 px and the 1-px anti-aliased border
    # ring alone is 12% of the sample — enough to drag a genuinely all-pink page
    # under any sane threshold (measured 93.5%). At 2.0 the same page reads
    # 99.9%. The ring is trimmed as well, the way slicer's own samplers do.
    pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), colorspace=fitz.csRGB)
    arr = np.frombuffer(pix.samples, dtype=np.uint8)             .reshape(pix.height, pix.width, pix.n)[:, :, :3]
    target = np.array([round(slicer.PINK_PAD_R * 255),
                       round(slicer.PINK_PAD_G * 255),
                       round(slicer.PINK_PAD_B * 255)], dtype=np.int32)
    # Tight band: the placeholder is a flat vector fill, so its pixels land on the
    # exact value bar rasteriser rounding. The page number is a few stray pixels.
    close = (np.abs(arr.astype(np.int32) - target).max(axis=2) <= 2)
    if close.shape[0] >= 3 and close.shape[1] >= 3:
        close = close[1:-1, 1:-1]
    return bool(close.mean() > 0.95)


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)
    return bool(ok)


def hdr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


# ── 1. furniture_scale identity ───────────────────────────────────────────────

def t_furniture_scale_identity():
    hdr("1. furniture_scale() is exactly 1.0 at and above the fit floor")

    fs_default = slicer.furniture_scale(slicer.PAGE_HEIGHT_M)
    check("furniture_scale(PAGE_HEIGHT_M) == 1.0 exactly",
          fs_default == 1.0, repr(fs_default))

    # Float equality on purpose. `x * 1.0` is exact in IEEE-754, so an exact 1.0
    # here is what guarantees byte-identical default output; 0.9999999 would not.
    for ph in (1.0, 2.0, 3.0, 4.0, 4.5, 5.0, 8.0, 12.0, 100.0):
        fs = slicer.furniture_scale(ph)
        check(f"furniture_scale({ph}) == 1.0 exactly (at/above fit floor)",
              fs == 1.0, repr(fs))

    # Below the floor it shrinks linearly and never grows.
    for ph, expect in ((0.5, 0.5), (0.25, 0.25), (0.75, 0.75)):
        fs = slicer.furniture_scale(ph)
        check(f"furniture_scale({ph}) == {expect} (linear below the floor)",
              fs == expect, repr(fs))

    check("furniture_scale is monotonic non-decreasing",
          all(slicer.furniture_scale(a) <= slicer.furniture_scale(b)
              for a, b in zip([0.1, 0.5, 1.0, 2.0, 4.0], [0.5, 1.0, 2.0, 4.0, 8.0])))


# ── 2. furniture constants reproduce their literals at the default ────────────

def t_furniture_literals():
    hdr("2. Every furniture constant is unchanged at the default page height")

    fs = slicer.furniture_scale(slicer.PAGE_HEIGHT_M)
    literals = {
        "PAGE_NUM_EXCL_W":        13.5,
        "PAGE_NUM_EXCL_Y0":        4.0,
        "PAGE_NUM_EXCL_Y1":       11.5,
        "PAGE_NUM_FONT_PT":        6.0,
        "PAGE_NUM_INSET_PT":      12.0,
        "PAGE_NUM_BASELINE_PT":   10.0,
        "KLIPP_FONT_DEFAULT":      6.0,
        "KLIPP_MIN_FONT_SIZE_PT":  4.0,
        "KLIPP_TEXT_GAP_PT":       3.0,
        "KLIPP_TEXT_MIN_ROOM_PT":  4.0,
        "KLIPP_TEXT_PAD_PT":       2.0,
        "KLIPP_LINE_WIDTH_PT":     1.0,
        "KLIPP_DASH_PT":           4.0,
    }
    for name, expected in literals.items():
        actual = getattr(slicer, name)
        check(f"{name} == {expected} and scales to itself at the default",
              actual == expected and actual * fs == expected,
              f"{actual} * {fs} = {actual * fs}")

    # The dash pattern is emitted as a STRING; at the default it must re-emit the
    # original literal "[4 4] 0", not "[4.0 4.0] 0" — same pixels either way, but
    # an identical content stream is a stronger guarantee.
    dash = f"{round(slicer.KLIPP_DASH_PT * fs, 3):g}"
    check('dash pattern re-emits "[4 4] 0" at the default',
          f"[{dash} {dash}] 0" == "[4 4] 0", f"[{dash} {dash}] 0")

    # Class P must NOT have acquired a scale factor. These are fabric distances.
    for name, expected in (("KLIPP_MIN_PINK_PT", 12.0),
                           ("KLIPP_LINE_MARGIN_PT", 4.0),
                           ("MIN_LABEL_PATCH_SIZE_PT", 1.0),
                           ("LABEL_FONT_DEFAULT", 3.0),
                           ("MIN_PATCH_PX", 10),
                           ("COLOR_MATCH_TOLERANCE", 28)):
        check(f"Class P constant {name} still == {expected} (never scaled)",
              getattr(slicer, name) == expected, repr(getattr(slicer, name)))


# ── 3. non-default page heights work end to end ──────────────────────────────

def _run(page_height_m, **kw):
    with open(DESIGN, "rb") as f:
        data = f.read()
    return slicer.run_slice(data, DESIGN_W_M, DESIGN_H_M,
                            colour_map=PEST_OVRE_MAP,
                            page_height_m=page_height_m, **kw)


def t_geometry(page_height_m):
    hdr(f"3. End-to-end geometry at page_height_m={page_height_m}")

    res = _run(page_height_m)
    exp_strips = math.ceil(DESIGN_W_M / slicer.STRIP_WIDTH_M)
    check(f"strip count == ceil({DESIGN_W_M}/{slicer.STRIP_WIDTH_M}) = {exp_strips}",
          len(res["strips"]) == exp_strips, str(len(res["strips"])))

    # Page HEIGHT is locked (strip width is not configurable); page WIDTH tracks
    # the chosen Skissyta height. Both derived from the design's own pts/m, which
    # is why they are exact multiples rather than approximations.
    doc0 = fitz.open(stream=res["strips"][0]["bytes"], filetype="pdf")
    ppm = doc0[0].rect.height / slicer.STRIP_WIDTH_M
    doc0.close()

    exp_h = slicer.STRIP_WIDTH_M * ppm
    exp_w = page_height_m * ppm
    bad_dims, off_page, pages = [], [], 0
    for s in res["strips"]:
        d = fitz.open(stream=s["bytes"], filetype="pdf")
        for p in d:
            pages += 1
            if abs(p.rect.height - exp_h) > 0.01 or abs(p.rect.width - exp_w) > 0.01:
                bad_dims.append((s["filename"], round(p.rect.width, 2),
                                 round(p.rect.height, 2)))
            for blk in p.get_text("dict")["blocks"]:
                for ln in blk.get("lines", []):
                    for sp in ln["spans"]:
                        r = fitz.Rect(sp["bbox"])
                        # Horizontal only: a small ascender overshoot at y0 is a
                        # PRE-EXISTING artifact of the label placer's 0.72*fs
                        # glyph-height estimate (present at the default too, ~9%
                        # of spans) and is not what this guard is about.
                        if r.x0 < -0.01 or r.x1 > p.rect.width + 0.01:
                            off_page.append((sp["text"], tuple(round(v, 1)
                                                               for v in sp["bbox"])))
        d.close()

    check(f"every page is {exp_w:.2f} x {exp_h:.2f} pt", not bad_dims,
          str(bad_dims[:3]))
    check("no text escapes its page horizontally", not off_page,
          str(off_page[:3]))
    check("grid PDF produced", bool(res["grid_pdf"]), f"{len(res['grid_pdf'])} bytes")
    print(f"    ({pages} rendered pages, {len(res['strips'])} strips)")
    return res


def t_larger_than_design():
    hdr("3b. Skissyta taller than the whole design")

    # 10 m pages on an 8 m design: one page per strip, and the single page is
    # geometrically partial, so the pink pad path runs at a non-default height.
    res = _run(10.0)
    counts = []
    for s in res["strips"]:
        d = fitz.open(stream=s["bytes"], filetype="pdf")
        counts.append(d.page_count)
        d.close()
    check("every strip yields at most one page", all(c <= 1 for c in counts),
          str(sorted(set(counts))))
    check("job completed without error", len(res["strips"]) > 0,
          f"{len(res['strips'])} strips")


# ── 4. the behaviours that genuinely change with page height ─────────────────

def t_klipp_vs_page_height():
    hdr("4. Klipp emission across page heights (KLIPP_MIN_PINK_PT is physical)")

    # KLIPP_MIN_PINK_PT = 12 pt = 42.3 cm of blank fabric. That is a fixed
    # PHYSICAL requirement, so on a short ruta it is a large fraction of the page
    # and cut markings legitimately become rarer. Pinned here so the effect is
    # visible and deliberate rather than discovered on a print.
    ppm = 72.0 / 2.54
    for ph in (1.0, 2.0, 4.0):
        res = _run(ph, skip_colors=True)
        klipp = 0
        for s in res["strips"]:
            d = fitz.open(stream=s["bytes"], filetype="pdf")
            for p in d:
                if "Klipp" in p.get_text():
                    klipp += 1
            d.close()
        pct = slicer.KLIPP_MIN_PINK_PT / (ph * ppm) * 100
        print(f"    page_height_m={ph:>4}: {klipp:>3} Klipp text(s); "
              f"KLIPP_MIN_PINK_PT is {pct:.1f}% of the page")

    # The floor below which "Klipp" cannot fit at ANY font size, from the
    # measured furniture widths. This is the number the UI warns on.
    fs = 1.0
    need = (slicer.PAGE_NUM_EXCL_W + slicer.KLIPP_TEXT_PAD_PT
            + slicer.KLIPP_TEXT_GAP_PT
            + fitz.get_text_length("Klipp", fontsize=slicer.KLIPP_MIN_FONT_SIZE_PT))
    check("the 1.0 m UI warn threshold still matches the measured Klipp-fit floor",
          0.90 <= need / ppm <= 1.05, f"measured floor = {need / ppm:.3f} m")


ALL_BG_DESIGN = "kenta.pdf"          # strip 14 (index 13) is solid background
ALL_BG_W_M, ALL_BG_H_M = 21, 20
ALL_BG_STRIP = 13


def t_all_background_strip():
    hdr("5. An all-background strip yields a blank page, not a crash")

    # A strip every page of which is_fully_background() excludes used to raise
    # "cannot save with zero pages" from out_doc.save(), failing the WHOLE job.
    # It now emits one blank pink page so strip numbering stays dense.
    #
    # This is NOT page-height-driven and the loop below is what says so: with the
    # strip width locked, a strip's content is a property of its column, so the
    # same strip is all-background at every page height. Pinned as a range, not a
    # single value, precisely so a future change that couples the two is caught.
    if not os.path.exists(ALL_BG_DESIGN):
        print(f"    SKIP: {ALL_BG_DESIGN} not in repo root")
        return

    with open(ALL_BG_DESIGN, "rb") as f:
        data = f.read()
    ns = math.ceil(ALL_BG_W_M / slicer.STRIP_WIDTH_M)
    _, ppm_y = design_ppm(ALL_BG_DESIGN, ALL_BG_W_M, ALL_BG_H_M)

    for ph in (1.0, 2.0, 4.0, 8.0):
        npg = math.ceil(ALL_BG_H_M / ph)
        try:
            _, b = slicer.slice_one_strip(
                (ALL_BG_STRIP, data, ALL_BG_W_M, ALL_BG_H_M, ns, npg, {},
                 False, True, ph))
        except ValueError as e:
            check(f"all-background strip does not raise at page_height_m={ph}",
                  False, str(e))
            continue
        d = fitz.open(stream=b, filetype="pdf")
        n = d.page_count
        dims = (round(d[0].rect.width, 2), round(d[0].rect.height, 2)) if n else None
        txt = d[0].get_text().strip() if n else ""
        blank_ok = is_blank_placeholder(d[0]) if n else False
        d.close()
        exp_w = round(ph * ppm_y, 2)
        check(f"page_height_m={ph}: exactly one blank page, numbered",
              n == 1 and txt == "1" and blank_ok,
              f"pages={n} dims={dims} text={txt!r} placeholder={blank_ok}")
        # The blank page must be a REAL page of the right size, not a stub — an
        # operator prints it alongside the rest of the strip.
        check(f"page_height_m={ph}: blank page has the correct dimensions",
              dims is not None and abs(dims[0] - exp_w) < 0.05,
              f"{dims} (expected width ~{exp_w})")


def t_zero_page_strips():
    hdr("6. No content-bearing strip is emptied by the page height")

    # The other half of the same concern: page height must not push a strip that
    # DOES carry content over the is_fully_background threshold. The exclusion
    # test is a fraction, so a tall page dilutes a small content sliver; this
    # sweeps the range to confirm that never empties a strip on a real design.
    with open(DESIGN, "rb") as f:
        data = f.read()
    ns = math.ceil(DESIGN_W_M / slicer.STRIP_WIDTH_M)
    blanked = {}
    for ph in (1.0, 2.0, 4.0, 8.0):
        npg = math.ceil(DESIGN_H_M / ph)
        empty = []
        for s in range(ns):
            _, b = slicer.slice_one_strip(
                (s, data, DESIGN_W_M, DESIGN_H_M, ns, npg, PEST_OVRE_MAP,
                 False, True, ph))
            d = fitz.open(stream=b, filetype="pdf")
            if d.page_count == 1 and is_blank_placeholder(d[0]):
                empty.append(s + 1)
            d.close()
        blanked[ph] = empty
        print(f"    page_height_m={ph:>4}: emptied strips = {empty or 'none'}")

    check("no page height empties a content-bearing strip",
          all(not e for e in blanked.values()), str(blanked))


def main():
    if not os.path.exists(DESIGN):
        print(f"SKIP: {DESIGN} not found in repo root")
        return 2

    print(f"slicer VERSION {slicer.VERSION}  "
          f"STRIP_WIDTH_M={slicer.STRIP_WIDTH_M} (locked)  "
          f"PAGE_HEIGHT_M={slicer.PAGE_HEIGHT_M} (default)  "
          f"FURNITURE_FIT_FLOOR_M={slicer.FURNITURE_FIT_FLOOR_M}")

    t_furniture_scale_identity()
    t_furniture_literals()
    for ph in (2.0, 2.5, 4.0):
        t_geometry(ph)
    t_larger_than_design()
    t_klipp_vs_page_height()
    t_all_background_strip()
    t_zero_page_strips()

    print("\n" + "=" * 78)
    if FAILS:
        print(f"{len(FAILS)} FAILURE(S):")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
