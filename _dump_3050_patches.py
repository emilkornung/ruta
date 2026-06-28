"""
Diagnostic dump for the #229058 (code 3050) color on 'strip-15 test.pdf' page 1.

Shows EVERY connected component (down to a small floor), color-coded by what
MIN_PATCH_PX would do, so we can eyeball which sub-40px patches are real vs noise.

  green  = kept   (size >= MIN_PATCH_PX)
  orange = borderline dropped (20 <= size < MIN_PATCH_PX)  <- the ones to judge
  red    = tiny dropped (floor <= size < 20)

Markers are drawn at each patch's deepest interior point (same point the real
labeler would use). Output: 3050_patch_map.png  +  a printed table.
NOT part of the pipeline. Does not touch color_map.json.
"""
import numpy as np
import fitz
from scipy import ndimage

import slicer

PDF        = "strip-15 test.pdf"
PAGE_INDEX = 1
HEXC       = "#229058"
CODE       = "3050"
OUT_PNG    = "3050_patch_map.png"
PNG_SCALE  = 12.0
FLOOR      = 4        # ignore components smaller than this (pure anti-alias dust)

def rgb(h):
    h = h.lstrip("#")
    return np.array([int(h[i:i+2], 16) for i in (0, 2, 4)], dtype=np.int32)

doc  = fitz.open(PDF)
page = doc[PAGE_INDEX]

pix = page.get_pixmap(matrix=fitz.Matrix(slicer.LABEL_RENDER_SCALE, slicer.LABEL_RENDER_SCALE),
                      colorspace=fitz.csRGB)
arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
arr = arr[:, :, :3].astype(np.int32)

target  = rgb(HEXC)
mask    = ((arr - target) ** 2).sum(axis=2) <= slicer.COLOR_MATCH_TOLERANCE ** 2
lbl, n  = ndimage.label(mask)
counts  = np.bincount(lbl.ravel())
objects = ndimage.find_objects(lbl)

rows = []
for comp in range(1, n + 1):
    size = int(counts[comp])
    if size < FLOOR:
        continue
    sl  = objects[comp - 1]
    sub = lbl[sl] == comp
    dt  = ndimage.distance_transform_edt(sub)
    yloc, xloc = np.unravel_index(int(np.argmax(dt)), dt.shape)
    yi, xi = sl[0].start + yloc, sl[1].start + xloc
    # bbox in scale-2 px
    h = sl[0].stop - sl[0].start
    w = sl[1].stop - sl[1].start
    rows.append((size, xi, yi, w, h))

rows.sort(reverse=True)

MIN = slicer.MIN_PATCH_PX
def kind(size):
    if size >= MIN:        return "KEPT",       (0, 0.6, 0)
    if size >= 20:         return "drop(border)",(1, 0.55, 0)
    return "drop(tiny)",   (0.85, 0, 0)

print(f"#229058 (3050) on page {PAGE_INDEX}  | MIN_PATCH_PX={MIN}  "
      f"TOL={slicer.COLOR_MATCH_TOLERANCE}  SCALE={slicer.LABEL_RENDER_SCALE}")
print(f"{'size':>5} {'state':>13} {'bbox(w x h)':>12}   deepest(px@scale2)")
print("-" * 60)
n_kept = n_border = n_tiny = 0
for size, xi, yi, w, h in rows:
    state, _ = kind(size)
    if   state == "KEPT":          n_kept   += 1
    elif state == "drop(border)":  n_border += 1
    else:                          n_tiny   += 1
    print(f"{size:>5} {state:>13}  {w:>4} x {h:<4}   ({xi},{yi})")
print("-" * 60)
print(f"KEPT(>= {MIN}): {n_kept}   drop-border(20..{MIN-1}): {n_border}   "
      f"drop-tiny({FLOOR}..19): {n_tiny}")

# ── Draw markers on the page ────────────────────────────────────────────────────
for size, xi, yi, w, h in rows:
    _, col = kind(size)
    px = (xi + 0.5) / slicer.LABEL_RENDER_SCALE
    py = (yi + 0.5) / slicer.LABEL_RENDER_SCALE
    shape = page.new_shape()
    shape.draw_circle(fitz.Point(px, py), 1.6)
    shape.finish(color=col, width=0.5, fill=col, fill_opacity=0.9)
    shape.commit()
    page.insert_text(fitz.Point(px + 1.8, py + 1.2), str(size), fontsize=3.2, color=col)

out = page.get_pixmap(matrix=fitz.Matrix(PNG_SCALE, PNG_SCALE), colorspace=fitz.csRGB)
out.save(OUT_PNG)
print(f"\nSaved -> {OUT_PNG}  ({out.width}x{out.height})  "
      "green=kept  orange=borderline-dropped  red=tiny-dropped")
