"""
Local validation for the ruta_nedre slicing mode. NOT part of the pipeline.

Run:  python _validate_nedre.py

1. Regression: the default path (ruta_nedre=False) must be pixel-identical to the
   pre-change committed slicer.py (baseline commit below).
2. Behavior: ruta_nedre=True must move the partial/pink page to the design's
   bottom and put the pink on the opposite (free-edge) side.

The baseline is fetched from git so this keeps working after the change is
committed. Bump BASELINE_REF if you re-baseline.
"""
import hashlib
import importlib.util
import io
import subprocess
import tempfile

import fitz

import slicer as new_slicer

BASELINE_REF = "bc36eb2"   # last commit before the ruta_nedre change


def load_baseline():
    src = subprocess.check_output(["git", "show", f"{BASELINE_REF}:slicer.py"])
    tmp = tempfile.NamedTemporaryFile("wb", suffix=".py", delete=False)
    tmp.write(src)
    tmp.close()
    spec = importlib.util.spec_from_file_location("slicer_orig", tmp.name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


orig_slicer = load_baseline()

WIDTH_M, HEIGHT_M = 3.0, 10.0          # 2 strips, 3 pages (last = partial 2m)
PPM = 100                               # pts per meter
W, H = int(WIDTH_M * PPM), int(HEIGHT_M * PPM)


def make_test_pdf():
    doc = fitz.open()
    page = doc.new_page(width=W, height=H)
    page.draw_rect(page.rect, fill=(1, 1, 1), color=None)
    # Horizontal bands every 1m; y=0 is design TOP, y=H is design BOTTOM.
    for m in range(int(HEIGHT_M)):
        y0 = m * PPM
        shade = 0.3 + 0.06 * m
        page.draw_rect(fitz.Rect(0, y0, W, y0 + PPM),
                       fill=(shade, 0.4, 0.8), color=None)
        page.insert_text(fitz.Point(10, y0 + 60), f"{m}-{m+1}m",
                         fontsize=28, color=(1, 1, 1))
    page.insert_text(fitz.Point(5, H - 10), "LEFTcol", fontsize=20, color=(0, 0, 0))
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def page_hashes(strip_bytes, scale=1.0):
    d = fitz.open(stream=strip_bytes, filetype="pdf")
    hs = []
    for p in d:
        pix = p.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB)
        hs.append(hashlib.sha256(pix.samples).hexdigest())
    d.close()
    return hs


def pink_side(strip_bytes, page_index):
    """Which horizontal half ('left'/'right'/None) holds the pink pad."""
    d = fitz.open(stream=strip_bytes, filetype="pdf")
    if page_index >= len(d):
        d.close()
        return None
    p = d[page_index]
    pix = p.get_pixmap(matrix=fitz.Matrix(0.25, 0.25), colorspace=fitz.csRGB)
    w, h, s = pix.width, pix.height, pix.samples
    left = right = 0
    for yi in range(h):
        for xi in range(w):
            i = (yi * w + xi) * 3
            r, g, b = s[i], s[i+1], s[i+2]
            if r > 230 and 120 < g < 170 and 160 < b < 205:   # ~#F490B5
                if xi < w / 2:
                    left += 1
                else:
                    right += 1
    d.close()
    if left == 0 and right == 0:
        return None
    return "left" if left > right else "right"


pdf = make_test_pdf()
orig = orig_slicer.run_slice(pdf, WIDTH_M, HEIGHT_M)
new_def = new_slicer.run_slice(pdf, WIDTH_M, HEIGHT_M, ruta_nedre=False)
new_ned = new_slicer.run_slice(pdf, WIDTH_M, HEIGHT_M, ruta_nedre=True)

# ── 1. Regression: default path identical to baseline ────────────────────────
all_ok = True
for o, n in zip(orig["strips"], new_def["strips"]):
    same = page_hashes(o["bytes"]) == page_hashes(n["bytes"])
    all_ok &= same
    print(f"  {o['filename']}: default==baseline? {same}")
all_ok &= page_hashes(orig["grid_pdf"]) == page_hashes(new_def["grid_pdf"])
print(f"REGRESSION default==baseline (strips + grid): {all_ok}")
assert all_ok, "REGRESSION FAILED: default path differs from baseline"

# ── 2. Behavior: partial/pink page location & side per mode ───────────────────
print("\nPartial-page pink location (strip-01):")
for label, res in (("default", new_def), ("ruta_nedre", new_ned)):
    sb = res["strips"][0]["bytes"]
    n = len(fitz.open(stream=sb, filetype="pdf"))
    found = next(((i, pink_side(sb, i)) for i in range(n) if pink_side(sb, i)), None)
    print(f"  {label:11s}: pages={n}  pink page_index={found[0] if found else None} "
          f"(last={n-1})  side={found[1] if found else None}")

print("\nExpected: default pink on RIGHT (top free edge); "
      "ruta_nedre pink on LEFT (bottom free edge).")
