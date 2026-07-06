"""
Shared validation runner for TIF-27 color labeling. NOT part of the pipeline.
Does NOT touch the production color_map.json — each design's dummy map is
passed straight into slicer.slice_one_strip, exercising the real production
slicing + labeling path end to end.

Used by _validate_enad_font_sizing.py and _validate_kenta_font_sizing.py.

Renders every labeled strip page to validation/<prefix>_strip<N>_p<M>.png and
reports, per code and overall: labels placed, fitted vs forced (a "forced"
placement means even sub-pixel text had no collision-free pixel — worth
knowing about, it flags a zero-space cluster), the font-size distribution,
and how many labels sat at the technical floor LABEL_FONT_TECH_MIN.
The zero-skip contract is asserted: placed must equal the independently
recounted number of patches >= MIN_PATCH_PX.
"""
import math
import time

import fitz
import numpy as np
from scipy import ndimage

import slicer

PNG_SCALE = 8.0
OUT_DIR   = "validation"


def run_design(pdf_path, width_m, height_m, dummy_map, prefix, strips=None):
    num_strips = math.ceil(width_m / slicer.STRIP_WIDTH_M)
    num_pages  = math.ceil(height_m / slicer.PAGE_HEIGHT_M)
    strips     = strips if strips is not None else list(range(num_strips))

    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    print(f"{prefix}: {pdf_path} {width_m}x{height_m} m -> "
          f"{num_strips} strips x {num_pages} pages (running {len(strips)})")
    print(f"Constants: DEFAULT={slicer.LABEL_FONT_DEFAULT} "
          f"SHRINK={slicer.LABEL_FONT_SHRINK} TECH_MIN={slicer.LABEL_FONT_TECH_MIN} "
          f"MIN_PATCH_PX={slicer.MIN_PATCH_PX} TOL={slicer.COLOR_MATCH_TOLERANCE}")

    totals  = {}   # code -> [placed, fitted, forced]
    all_fs  = []
    n_pages = 0
    expected_total = placed_total = 0
    t0 = time.time()

    real_label = slicer._label_colors_on_page
    S = slicer.LABEL_RENDER_SCALE

    def recording_label(page, color_map):
        nonlocal expected_total, placed_total
        # Independent recount of labelable patches on the clean page (before
        # any text lands on it) — this is what "zero skips" is checked against.
        # Uses the same nearest-color / per-code mask semantics as the labeler.
        pix = page.get_pixmap(matrix=fitz.Matrix(S, S), colorspace=fitz.csRGB)
        arr = np.frombuffer(pix.samples, dtype=np.uint8) \
                .reshape(pix.height, pix.width, pix.n)[:, :, :3].astype(np.int32)
        for code, mask in slicer._code_masks(arr, color_map).items():
            lbl, _ = ndimage.label(mask)
            counts = np.bincount(lbl.ravel())
            expected_total += int((counts[1:] >= slicer.MIN_PATCH_PX).sum())

        summary = real_label(page, color_map)
        for code, e in summary.items():
            t = totals.setdefault(code, [0, 0, 0])
            t[0] += e["count"]
            t[1] += e["placement"].count("fit")
            t[2] += e["forced"]
            all_fs.extend(e["font_sizes"])
            placed_total += e["count"]
        return summary

    slicer._label_colors_on_page = recording_label
    try:
        for s in strips:
            strip_num, strip_bytes = slicer.slice_one_strip(
                (s, pdf_bytes, width_m, height_m, num_strips, num_pages,
                 dummy_map, False))
            out = fitz.open(stream=strip_bytes, filetype="pdf")
            for pi, pg in enumerate(out):
                pg.get_pixmap(matrix=fitz.Matrix(PNG_SCALE, PNG_SCALE),
                              colorspace=fitz.csRGB) \
                  .save(f"{OUT_DIR}/{prefix}_strip{strip_num}_p{pi + 1}.png")
            n_pages += len(out)
            out.close()
            print(f"  strip {strip_num}/{num_strips} done ({time.time() - t0:.0f}s)")
    finally:
        slicer._label_colors_on_page = real_label

    print(f"\n{n_pages} pages rendered to {OUT_DIR}/ in {time.time() - t0:.0f}s")
    print("\nPer code:  placed (fitted / forced)")
    tp = tf = tfo = 0
    for code in sorted(totals):
        p, f_, fo = totals[code]
        tp += p; tf += f_; tfo += fo
        print(f"  {code:>5}: {p:4d}  ({f_} / {fo})")
    print(f"  TOTAL: {tp}  fitted={tf}  forced={tfo}")

    zero_skips = expected_total == placed_total
    print(f"\nZERO-SKIP CHECK: expected patches {expected_total}, "
          f"placed {placed_total}  -> {'OK' if zero_skips else 'MISMATCH'}")

    if all_fs:
        fss = np.array(all_fs)
        at_default = int((np.abs(fss - slicer.LABEL_FONT_DEFAULT) < 1e-6).sum())
        at_floor   = int((fss <= slicer.LABEL_FONT_TECH_MIN + 1e-6).sum())
        sub_1pt    = int((fss < 1.0).sum())
        print(f"Font sizes: min {fss.min():.2f}  med {np.median(fss):.2f}  "
              f"max {fss.max():.2f}  (n={len(fss)})")
        print(f"  at default ({slicer.LABEL_FONT_DEFAULT}): {at_default}   "
              f"below 1pt: {sub_1pt}   AT TECH FLOOR ({slicer.LABEL_FONT_TECH_MIN}): {at_floor}"
              f"{'   <-- REPORT: near-zero-space patches!' if at_floor else ''}")
    return zero_skips
