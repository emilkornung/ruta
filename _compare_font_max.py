"""
Render strip-15 page 2 labeled at several LABEL_FONT_MAX values for visual
comparison of the proportional font sizing. NOT part of the pipeline.
Produces color_labels_max{N}.png for each N.
"""
import fitz
import slicer

PDF, PAGE_INDEX, PNG_SCALE = "strip-15 test.pdf", 1, 10.0
DUMMY_MAP = {
    "#0E6B38": "3560", "#0D5128": "3580", "#B3C8BC": "1010",
    "#72A982": "3020", "#229058": "3050", "#FFFFFF": "Vit",
}

for fmax in (6.0, 8.0, 12.0):
    slicer.LABEL_FONT_MAX = fmax
    page = fitz.open(PDF)[PAGE_INDEX]
    summary = slicer._label_colors_on_page(page, DUMMY_MAP)
    all_fs = [f for v in summary.values() for f in v["font_sizes"]]
    out = page.get_pixmap(matrix=fitz.Matrix(PNG_SCALE, PNG_SCALE), colorspace=fitz.csRGB)
    name = f"color_labels_max{int(fmax)}.png"
    out.save(name)
    n_at_max = sum(1 for f in all_fs if abs(f - fmax) < 1e-6)
    print(f"max={fmax:>4}: {len(all_fs)} labels, fs {min(all_fs):.2f}..{max(all_fs):.2f}, "
          f"{n_at_max} at ceiling -> {name}")
