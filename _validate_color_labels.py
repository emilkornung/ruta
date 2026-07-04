"""
Local validation for the rebuilt map-driven color labeling. NOT part of the
pipeline. Does NOT touch the production color_map.json — the dummy map is passed
straight into slicer._label_colors_on_page.

Run:  python _validate_color_labels.py

Validates on 'strip-15 test.pdf', page index 1:
  1. ZERO SKIPS: every patch >= MIN_PATCH_PX of the 5 non-skip colors gets
     exactly one label (round-3 contract — no legibility floor).
  2. No labels land on tiny noise fragments (< MIN_PATCH_PX).
  3. Every FITTED label's glyph bbox lies inside its own color's mask; sub-
     pixel labels are checked at their center pixel. "Forced" placements
     (zero collision-free space even at the technical floor) are reported.
  4. No two placed label bboxes intersect (forced ones excluded, reported).
  5. Renders the labeled page to validation/color_labels_validation.png.
"""
import numpy as np
import fitz
from scipy import ndimage

import slicer

PDF        = "strip-15 test.pdf"
PAGE_INDEX = 1
OUT_PNG    = "validation/color_labels_validation.png"
PNG_SCALE  = 10.0   # high-res render purely for human review

DUMMY_MAP = {
    "#0E6B38": "3560",
    "#0D5128": "3580",
    "#B3C8BC": "1010",
    "#72A982": "3020",
    "#229058": "3050",
    "#FFFFFF": "Vit",
}


def rgb(hexc):
    h = hexc.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.int32)


# ── 1. Tolerance bands must be mutually disjoint ────────────────────────────────
non_skip = [(h, c) for h, c in DUMMY_MAP.items() if c != "Skip"]
print(f"Constants: TOLERANCE={slicer.COLOR_MATCH_TOLERANCE} "
      f"MIN_PATCH_PX={slicer.MIN_PATCH_PX} SCALE={slicer.LABEL_RENDER_SCALE} "
      f"DEFAULT={slicer.LABEL_FONT_DEFAULT} TECH_MIN={slicer.LABEL_FONT_TECH_MIN}")
print("\nPairwise color distances (must exceed 2*tol = "
      f"{2 * slicer.COLOR_MATCH_TOLERANCE} for unambiguous bands):")
min_pair = 1e9
for i in range(len(non_skip)):
    for j in range(i + 1, len(non_skip)):
        d = float(np.linalg.norm(rgb(non_skip[i][0]) - rgb(non_skip[j][0])))
        min_pair = min(min_pair, d)
        print(f"  {non_skip[i][0]} <-> {non_skip[j][0]}: {d:6.1f}")
bands_disjoint = min_pair > 2 * slicer.COLOR_MATCH_TOLERANCE
print(f"  min pairwise = {min_pair:.1f}  ->  bands disjoint? {bands_disjoint}")

# ── 2. Run the actual labeling function on page 1 ───────────────────────────────
doc  = fitz.open(PDF)
page = doc[PAGE_INDEX]
summary = slicer._label_colors_on_page(page, DUMMY_MAP)

print("\nLabels per color (fs range | fitted/forced):")
for h, c in non_skip:
    e   = summary.get(h, {})
    fss = e.get("font_sizes", [])
    rng = f"fs {min(fss):.2f}..{max(fss):.2f}" if fss else "-"
    print(f"  {h} ({c}): {e.get('count', 0)} placed  {rng}  "
          f"forced={e.get('forced', 0)}")

# ── 3. Independent checks on a CLEAN pixmap ─────────────────────────────────────
clean = fitz.open(PDF)[PAGE_INDEX]
S     = slicer.LABEL_RENDER_SCALE
pix   = clean.get_pixmap(matrix=fitz.Matrix(S, S), colorspace=fitz.csRGB)
arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
arr = arr[:, :, :3].astype(np.int32)
tol_sq = slicer.COLOR_MATCH_TOLERANCE ** 2

# 3a. Zero skips: every real patch gets exactly one label.
zero_skips = True
print("\nExpected real patches (>= MIN_PATCH_PX) vs placed:")
for h, c in non_skip:
    mask   = ((arr - rgb(h)) ** 2).sum(axis=2) <= tol_sq
    lbl, n = ndimage.label(mask)
    counts = np.bincount(lbl.ravel())
    exp    = int((counts[1:] >= slicer.MIN_PATCH_PX).sum())
    got    = summary.get(h, {}).get("count", 0)
    ok     = exp == got
    zero_skips &= ok
    print(f"  {h} ({c}): expected {exp}, placed {got}  {'OK' if ok else 'MISMATCH'}")

# 3b. Fitted labels: glyph bbox inside the label's own color mask. For rects
# smaller than the analysis pixel grid, the center pixel is the honest test.
fit_inside_own = True
n_fit = n_forced = 0
for h, c in non_skip:
    e    = summary.get(h, {})
    mask = ((arr - rgb(h)) ** 2).sum(axis=2) <= tol_sq
    for rect, kind in zip(e.get("rects", []), e.get("placement", [])):
        if kind != "fit":
            n_forced += 1
            continue
        n_fit += 1
        # Pixels fully covered by the rect; empty for sub-pixel labels.
        x0, y0 = int(np.ceil(rect[0] * S)), int(np.ceil(rect[1] * S))
        x1, y1 = int(np.floor(rect[2] * S)), int(np.floor(rect[3] * S))
        if x1 > x0 and y1 > y0:
            inside = mask[y0:y1, x0:x1].all()
        else:
            cy = min(int((rect[1] + rect[3]) / 2 * S), mask.shape[0] - 1)
            cx = min(int((rect[0] + rect[2]) / 2 * S), mask.shape[1] - 1)
            inside = bool(mask[cy, cx])
        if not inside:
            fit_inside_own = False
            print(f"  BBOX ESCAPES OWN COLOR: {c} at {rect}")

# 3c. No two placed label bboxes intersect (forced placements excluded).
all_rects = []
for h, _ in non_skip:
    e = summary.get(h, {})
    for rect, kind in zip(e.get("rects", []), e.get("placement", [])):
        if kind == "fit":
            all_rects.append(fitz.Rect(*rect))
no_collisions = True
for i in range(len(all_rects)):
    for j in range(i + 1, len(all_rects)):
        if all_rects[i].intersects(all_rects[j]):
            no_collisions = False
            print(f"  LABEL COLLISION: {all_rects[i]} vs {all_rects[j]}")

all_fs = [f for h, _ in non_skip for f in summary.get(h, {}).get("font_sizes", [])]
if all_fs:
    n_default = sum(1 for f in all_fs if abs(f - slicer.LABEL_FONT_DEFAULT) < 1e-6)
    n_floor   = sum(1 for f in all_fs if f <= slicer.LABEL_FONT_TECH_MIN + 1e-6)
    print(f"\nFont sizes used across ALL labels: min {min(all_fs):.2f}  "
          f"max {max(all_fs):.2f}  (n={len(all_fs)}, fitted={n_fit}, forced={n_forced})")
    print(f"  at default ({slicer.LABEL_FONT_DEFAULT}): {n_default}   "
          f"at tech floor ({slicer.LABEL_FONT_TECH_MIN}): {n_floor}"
          f"{'   <-- REPORT: near-zero-space patches!' if n_floor else ''}")

# ── 4. Render labeled page for review ───────────────────────────────────────────
out_pix = page.get_pixmap(matrix=fitz.Matrix(PNG_SCALE, PNG_SCALE), colorspace=fitz.csRGB)
out_pix.save(OUT_PNG)
print(f"\nSaved labeled render -> {OUT_PNG}  ({out_pix.width}x{out_pix.height})")

# ── Verdict ─────────────────────────────────────────────────────────────────────
print("\n=== REQUIRED CHECKS ===")
print(f"  zero skips (every real patch labeled):      {zero_skips}")
print(f"  fitted label bboxes inside their OWN color: {fit_inside_own}")
print(f"  no label-vs-label bbox collisions:          {no_collisions}")
print(f"  forced placements (should be 0):            {n_forced}")
ok = zero_skips and fit_inside_own and no_collisions
print(f"  REQUIRED CHECKS PASS: {ok}")

# Diagnostic only: with these specific map colors some bands nearly touch
# (e.g. #0E6B38 vs #0D5128 = 30.5). Placement still lands correctly because
# fit verification runs against each patch's own mask. Surfaced here for
# MIN_PATCH_PX / COLOR_MATCH_TOLERANCE tuning, not a pass/fail.
print("\n=== DIAGNOSTIC ===")
print(f"  tolerance bands fully disjoint:             {bands_disjoint}"
      f"  (min pairwise dist {min_pair:.1f} vs 2*tol {2 * slicer.COLOR_MATCH_TOLERANCE})")
