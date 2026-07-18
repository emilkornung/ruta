"""
Shared validation runner for TIF-27 color labeling. NOT part of the pipeline.
Does NOT touch the production color_map.json — each design's dummy map is
passed straight into slicer.slice_one_strip, exercising the real production
slicing + labeling path end to end.

Used by _validate_enad_font_sizing.py and _validate_kenta_font_sizing.py.

Renders every labeled strip page to validation/<prefix>_strip<N>_p<M>.png.
Strips listed in pdf_strips are additionally exported as labeled vector PDFs
(validation/<prefix>_strip<N>.pdf — the exact bytes production would upload),
so output can be reviewed at real print scale rather than as raster.
Reports, per code and overall: labels placed, fitted vs forced (a "forced"
placement means even sub-pixel text had no collision-free pixel — worth
knowing about, it flags a zero-space cluster), the font-size distribution,
and how many labels sat at the technical floor LABEL_FONT_TECH_MIN.

The zero-skip contract (TIF-69 revision): placed must equal the
independently recounted number of patches that are BOTH >= MIN_PATCH_PX
AND at/above MIN_LABEL_PATCH_SIZE_PT's inscribed-circle radius. Patches
below that size floor are EXPECTED to be skipped — that is no longer a
failure — and are reported separately (not folded into "expected") so a
before/after skip count is visible per run.
"""
import math
import os
import time

import fitz
import numpy as np
from scipy import ndimage

import slicer

PNG_SCALE = 8.0
OUT_DIR   = "validation"


def run_design(pdf_path, width_m, height_m, dummy_map, prefix, strips=None,
               pdf_strips=(), ruta_nedre=False, out_dir=OUT_DIR):
    num_strips = math.ceil(width_m / slicer.STRIP_WIDTH_M)
    num_pages  = math.ceil(height_m / slicer.PAGE_HEIGHT_M)
    strips     = strips if strips is not None else list(range(num_strips))

    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    print(f"{prefix}: {pdf_path} {width_m}x{height_m} m -> "
          f"{num_strips} strips x {num_pages} pages (running {len(strips)})")
    print(f"Constants: DEFAULT={slicer.LABEL_FONT_DEFAULT} "
          f"SHRINK={slicer.LABEL_FONT_SHRINK} TECH_MIN={slicer.LABEL_FONT_TECH_MIN} "
          f"MIN_PATCH_PX={slicer.MIN_PATCH_PX} TOL={slicer.COLOR_MATCH_TOLERANCE} "
          f"MIN_LABEL_PATCH_SIZE_PT={slicer.MIN_LABEL_PATCH_SIZE_PT}")

    totals  = {}   # code -> [placed, fitted, forced]
    all_fs  = []
    n_pages = 0
    expected_total = placed_total = skipped_small_total = 0
    t0 = time.time()

    real_label = slicer._label_colors_on_page
    S = slicer.LABEL_RENDER_SCALE

    def recording_label(page, color_map):
        nonlocal expected_total, placed_total, skipped_small_total
        # Independent recount of labelable patches on the clean page (before
        # any text lands on it) — this is what "zero skips" is checked against.
        # Uses the same nearest-color / per-code mask semantics as the labeler,
        # AND the same distance-transform inscribed-circle metric (r_pts) the
        # labeler's own sizing ladder uses, split by the TIF-69 size gate:
        #   expected_total       -- patches at/above MIN_LABEL_PATCH_SIZE_PT.
        #                           These must all get labeled (checked below).
        #   skipped_small_total  -- patches below it. Expected to be skipped —
        #                           reported, not folded into "expected".
        pix = page.get_pixmap(matrix=fitz.Matrix(S, S), colorspace=fitz.csRGB)
        arr = np.frombuffer(pix.samples, dtype=np.uint8) \
                .reshape(pix.height, pix.width, pix.n)[:, :, :3].astype(np.int32)
        for code, mask in slicer._code_masks(arr, color_map).items():
            lbl, num = ndimage.label(mask)
            if num == 0:
                continue
            counts  = np.bincount(lbl.ravel())
            objects = ndimage.find_objects(lbl)
            for comp in range(1, num + 1):
                if counts[comp] < slicer.MIN_PATCH_PX:
                    continue
                sl       = objects[comp - 1]
                sub_mask = lbl[sl] == comp
                dt = ndimage.distance_transform_edt(np.pad(sub_mask, 1))[1:-1, 1:-1]
                r_pts = float(dt.max()) / S
                if r_pts < slicer.MIN_LABEL_PATCH_SIZE_PT:
                    skipped_small_total += 1
                else:
                    expected_total += 1

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
                 dummy_map, ruta_nedre, False))   # skip_labels=False — labels are the point
            os.makedirs(out_dir, exist_ok=True)
            if strip_num in pdf_strips:
                # Actual labeled vector output (what production would upload),
                # for reviewing text at real print scale instead of raster.
                with open(f"{out_dir}/{prefix}_strip{strip_num}.pdf", "wb") as fh:
                    fh.write(strip_bytes)
            out = fitz.open(stream=strip_bytes, filetype="pdf")
            for pi, pg in enumerate(out):
                pg.get_pixmap(matrix=fitz.Matrix(PNG_SCALE, PNG_SCALE),
                              colorspace=fitz.csRGB) \
                  .save(f"{out_dir}/{prefix}_strip{strip_num}_p{pi + 1}.png")
            n_pages += len(out)
            out.close()
            print(f"  strip {strip_num}/{num_strips} done ({time.time() - t0:.0f}s)")
    finally:
        slicer._label_colors_on_page = real_label

    print(f"\n{n_pages} pages rendered to {out_dir}/ in {time.time() - t0:.0f}s")
    print("\nPer code:  placed (fitted / forced)")
    tp = tf = tfo = 0
    for code in sorted(totals):
        p, f_, fo = totals[code]
        tp += p; tf += f_; tfo += fo
        print(f"  {code:>5}: {p:4d}  ({f_} / {fo})")
    print(f"  TOTAL: {tp}  fitted={tf}  forced={tfo}")

    zero_skips = expected_total == placed_total
    print(f"\nZERO-SKIP CHECK (patches >= {slicer.MIN_LABEL_PATCH_SIZE_PT}pt): "
          f"expected {expected_total}, placed {placed_total}  "
          f"-> {'OK' if zero_skips else 'MISMATCH'}")
    print(f"Skipped as sub-{slicer.MIN_LABEL_PATCH_SIZE_PT}pt dust (TIF-69, "
          f"EXPECTED, not a failure): {skipped_small_total}")

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

    # before/after (TIF-69): "before" = what the OLD (no size-gate) contract
    # would have placed, i.e. every patch that clears MIN_PATCH_PX regardless
    # of inscribed size -- exactly placed_total + skipped_small_total, since
    # zero_skips (when true) means expected_total == placed_total already.
    before = placed_total + skipped_small_total
    print(f"\nTIF-69 before/after: before(no size gate)={before}  "
          f"after(>= {slicer.MIN_LABEL_PATCH_SIZE_PT}pt)={placed_total}  "
          f"skipped={skipped_small_total}")

    return {
        "zero_skips": zero_skips,
        "expected": expected_total,
        "placed": placed_total,
        "skipped_small": skipped_small_total,
        "before": before,
    }
