"""
ENAD full-design validation harness (TIF-27). NOT part of the pipeline. Does
NOT touch the production color_map.json — the ENAD dummy map below is passed
straight into slicer.slice_one_strip, exercising the real production slicing
+ labeling path end to end.

Run:  python _validate_enad_font_sizing.py            # all 41 strips
      python _validate_enad_font_sizing.py 4 16 26    # only these strip numbers

Design under test: ENAD_rutor.pdf (expected untracked in repo root),
61.5 x 20 m ("FRAMÅT MED ENAD KRAFT!"), 28.35 pts/m -> 1 pt ~ 3.5 cm printed.
17 raw vector fills cluster (threshold 25, same as legacy test_colors.py) to
the ~10 pixel-level colors mapped below.

Every labeled strip page is rendered to validation/strip<N>_p<M>.png. The
report aggregates, per color and overall: labels placed, how many were
fit-verified vs overflow fallbacks (floor-size label on a patch thinner than
the text), and how many patches were skipped for collisions (counted by the
labeler, never silently dropped).
"""
import sys

import fitz
import numpy as np

import slicer

PDF       = "ENAD_rutor.pdf"
WIDTH_M   = 61.5
HEIGHT_M  = 20
NUM_STRIPS = 41
NUM_PAGES  = 5
PNG_SCALE = 8.0                  # strip pages are only ~113x42 pts
OUT_DIR   = "validation"

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


def main():
    strips = [int(a) - 1 for a in sys.argv[1:]] or list(range(NUM_STRIPS))

    with open(PDF, "rb") as f:
        pdf_bytes = f.read()

    print(f"Constants: FACTOR={slicer.LABEL_FONT_FACTOR} "
          f"MIN={slicer.LABEL_FONT_MIN} MAX={slicer.LABEL_FONT_MAX} "
          f"MIN_PATCH_PX={slicer.MIN_PATCH_PX} TOL={slicer.COLOR_MATCH_TOLERANCE}")

    totals   = {}   # code -> [placed, fitted, overflow, skipped]
    all_fs   = []
    n_pages  = 0

    real_label = slicer._label_colors_on_page

    def recording_label(page, color_map):
        summary = real_label(page, color_map)
        for hex_c, e in summary.items():
            t = totals.setdefault(color_map[hex_c], [0, 0, 0, 0])
            t[0] += e["count"]
            t[1] += e["placement"].count("fit")
            t[2] += e["overflow"]
            t[3] += e["skipped"]
            all_fs.extend(e["font_sizes"])
        return summary

    slicer._label_colors_on_page = recording_label
    try:
        for s in strips:
            strip_num, strip_bytes = slicer.slice_one_strip(
                (s, pdf_bytes, WIDTH_M, HEIGHT_M, NUM_STRIPS, NUM_PAGES,
                 DUMMY_MAP, False))
            out = fitz.open(stream=strip_bytes, filetype="pdf")
            for pi, page in enumerate(out):
                page.get_pixmap(matrix=fitz.Matrix(PNG_SCALE, PNG_SCALE),
                                colorspace=fitz.csRGB) \
                    .save(f"{OUT_DIR}/strip{strip_num}_p{pi + 1}.png")
            n_pages += len(out)
            out.close()
            print(f"  strip {strip_num}/{NUM_STRIPS} done")
    finally:
        slicer._label_colors_on_page = real_label

    print(f"\n{n_pages} pages rendered to {OUT_DIR}/")
    print("\nPer code:  placed (fitted / overflow)   skipped-for-collision")
    tp = tf = to = ts = 0
    for code in sorted(totals):
        p, f_, o, sk = totals[code]
        tp += p; tf += f_; to += o; ts += sk
        print(f"  {code:>5}: {p:4d}  ({f_} / {o})   {sk}")
    print(f"  TOTAL: {tp}  fitted={tf}  overflow={to}  skipped={ts}")
    if all_fs:
        fss = np.array(all_fs)
        print(f"\nFont sizes: min {fss.min():.2f}  med {np.median(fss):.2f}  "
              f"max {fss.max():.2f}  (n={len(fss)})")


if __name__ == "__main__":
    main()
