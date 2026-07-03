"""
ENAD font-sizing tuning harness (TIF-27). NOT part of the pipeline. Does NOT
touch the production color_map.json — the ENAD dummy map below is passed
straight into slicer.slice_one_strip, exercising the real production slicing
+ labeling path end to end.

Run:  python _validate_enad_font_sizing.py

Design under test: ENAD_rutor.pdf, 61.5 x 20 m ("FRAMÅT MED ENAD KRAFT!").
17 raw vector fills cluster (threshold 25, same as legacy test_colors.py) to
the ~10 pixel-level colors mapped below. Three representative strips cover the
three sizing regimes:

  strip  4 (s=3)  — giant white letters at the bottom (the "Vit far too big"
                    failure mode from the strip-15 known-bad run)
  strip 16 (s=15) — dense crowd figures: many small/medium green patches
  strip 26 (s=25) — large flat orange background areas (biggest patches)

For each font-sizing config it slices those strips, records every label's
font size and its patch's inscribed-circle radius, and renders each page to
enad_fs_<config>_strip<N>_p<M>.png for visual review. The "modesty" metric:
cap-height (~0.72*fs) as a fraction of the patch's inscribed-circle DIAMETER —
the known-bad giant labels were >100%; unclamped FACTOR=1.0 gives ~36%.
"""
import numpy as np
import fitz

import slicer

PDF       = "ENAD_rutor.pdf"
WIDTH_M   = 61.5
HEIGHT_M  = 20
STRIPS    = [3, 15, 25]          # 0-based s → strip numbers 4, 16, 26
PNG_SCALE = 8.0                  # strip pages are only ~113x42 pts

# Cluster representatives from ENAD_rutor.pdf pixel analysis. Codes for the
# green/sage/white family reuse the strip-15 known-bad dummy map so results
# are comparable; orange/yellow get new dummy codes. Same format as
# color_map.json ("Skip" = acknowledged, never labeled).
DUMMY_MAP = {
    "#F7931D": "0505",   # orange background — the giant-patch stress case
    "#FECB03": "0580",   # yellow (crest, glow outlines)
    "#0D5129": "3580",   # darkest green
    "#0F6B37": "3560",   # dark green
    "#268E58": "3050",   # mid green
    "#72A982": "3020",   # sage
    "#B3C8BB": "1010",   # pale sage
    "#FFFFFF": "Vit",    # white letters / shirts
    "#EBECE8": "Skip",   # off-white highlight tint
    "#2E251E": "Skip",   # near-black outlines
}

# (name, LABEL_FONT_FACTOR, LABEL_FONT_MIN, LABEL_FONT_MAX)
CONFIGS = [
    ("max6_f1.0",  1.0, 3.0, 6.0),   # current committed values
    ("max8_f1.0",  1.0, 3.0, 8.0),   # one step up — is 6 too timid?
    ("max6_f0.6",  0.6, 3.0, 6.0),   # sublinear feel: big patches hit the
                                     # ceiling later, mid patches shrink
]


def run_config(name, factor, fmin, fmax, pdf_bytes):
    slicer.LABEL_FONT_FACTOR = factor
    slicer.LABEL_FONT_MIN    = fmin
    slicer.LABEL_FONT_MAX    = fmax

    # Wrap the real labeler to also capture patch radii for the modesty metric.
    stats = []  # (strip_num, code, fs, inscribed_radius_pts)
    real_label = slicer._label_colors_on_page

    for s in STRIPS:
        strip_ctx = {"n": s + 1}

        def recording_label(page, color_map, _ctx=strip_ctx):
            # Re-derive radii exactly like the labeler does, then call it.
            pix = page.get_pixmap(
                matrix=fitz.Matrix(slicer.LABEL_RENDER_SCALE, slicer.LABEL_RENDER_SCALE),
                colorspace=fitz.csRGB)
            arr = np.frombuffer(pix.samples, dtype=np.uint8) \
                    .reshape(pix.height, pix.width, pix.n)[:, :, :3].astype(np.int32)
            from scipy import ndimage
            tol_sq = slicer.COLOR_MATCH_TOLERANCE ** 2
            radii = {}   # code -> list of inscribed radii (pts), patch order
            for hex_c, code in color_map.items():
                if not code or code == "Skip":
                    continue
                target = slicer._hex_to_rgb255(hex_c)
                mask = ((arr - target) ** 2).sum(axis=2) <= tol_sq
                if not mask.any():
                    continue
                lbl, num = ndimage.label(mask)
                counts   = np.bincount(lbl.ravel())
                objects  = ndimage.find_objects(lbl)
                for comp in range(1, num + 1):
                    if counts[comp] < slicer.MIN_PATCH_PX:
                        continue
                    # Same 1px pad as slicer: page/bbox edge = patch boundary.
                    dt = ndimage.distance_transform_edt(
                        np.pad(lbl[objects[comp - 1]] == comp, 1))
                    radii.setdefault(code, []).append(
                        float(dt.max()) / slicer.LABEL_RENDER_SCALE)

            summary = real_label(page, color_map)
            for hex_c, info in summary.items():
                code = color_map[hex_c]
                for fs, r in zip(info["font_sizes"], radii.get(code, [])):
                    stats.append((_ctx["n"], code, fs, r))
            return summary

        slicer._label_colors_on_page = recording_label
        try:
            strip_num, strip_bytes = slicer.slice_one_strip(
                (s, pdf_bytes, WIDTH_M, HEIGHT_M, 41, 5, DUMMY_MAP, False))
        finally:
            slicer._label_colors_on_page = real_label

        out = fitz.open(stream=strip_bytes, filetype="pdf")
        for pi, page in enumerate(out):
            png = f"enad_fs_{name}_strip{strip_num}_p{pi + 1}.png"
            page.get_pixmap(matrix=fitz.Matrix(PNG_SCALE, PNG_SCALE),
                            colorspace=fitz.csRGB).save(png)
        print(f"  strip {strip_num}: {len(out)} pages rendered")
        out.close()

    # Modesty metric per config.
    fss   = np.array([x[2] for x in stats])
    ratio = np.array([0.72 * x[2] / (2 * x[3]) for x in stats if x[3] > 0])
    print(f"  labels={len(stats)}  fs min/med/max = "
          f"{fss.min():.2f}/{np.median(fss):.2f}/{fss.max():.2f}  "
          f"at-floor={int((np.abs(fss - fmin) < 1e-6).sum())}  "
          f"at-ceiling={int((np.abs(fss - fmax) < 1e-6).sum())}")
    print(f"  cap-height / inscribed-diameter: med {np.median(ratio):.2f}  "
          f"p90 {np.percentile(ratio, 90):.2f}  max {ratio.max():.2f}  "
          f"(known-bad was >1.0; unclamped f=1.0 target ~0.36)")
    return stats


if __name__ == "__main__":
    with open(PDF, "rb") as f:
        pdf_bytes = f.read()

    orig = (slicer.LABEL_FONT_FACTOR, slicer.LABEL_FONT_MIN, slicer.LABEL_FONT_MAX)
    print(f"Committed constants: FACTOR={orig[0]} MIN={orig[1]} MAX={orig[2]}")
    print(f"Design: {WIDTH_M}x{HEIGHT_M} m -> "
          f"{fitz.open(PDF)[0].rect.width / WIDTH_M:.2f} pts/m "
          f"(1 pt ~ {100 * WIDTH_M / fitz.open(PDF)[0].rect.width:.1f} cm printed)\n")

    for name, factor, fmin, fmax in CONFIGS:
        print(f"=== {name}  (factor={factor} min={fmin} max={fmax}) ===")
        run_config(name, factor, fmin, fmax, pdf_bytes)
        print()

    slicer.LABEL_FONT_FACTOR, slicer.LABEL_FONT_MIN, slicer.LABEL_FONT_MAX = orig
