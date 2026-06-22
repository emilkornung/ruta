"""
Validation for the ruta_nedre rotate=90 fix. Read-only (uses run_slice).
Run: python _validate_rot90.py

Outputs:
  _v_source.png            - the multi-strip test design
  _v_pageorder_default.png - DEFAULT strip0 pages laid in page order  (ref: R11..R0)
  _v_pageorder_nedre.png   - NEDRE   strip0 pages laid in page order  (R0..R11, continuous)
  _v_recon_nedre.png       - NEDRE strips left-to-right + pages stacked -> reconstructed design
                             (must match source, Rad 1 = leftmost)
  _v_grid_default.png / _v_grid_nedre.png - grid overviews
"""
import io
import fitz
import slicer

PPM = 100
PH  = int(slicer.PAGE_HEIGHT_M * PPM)     # 400
SW  = int(slicer.STRIP_WIDTH_M * PPM)     # 150
WIDTH_M, HEIGHT_M = 3.0, 12.0             # 2 strips (Rad1=left, Rad2=right), 3 full pages
W, H = int(WIDTH_M * PPM), int(HEIGHT_M * PPM)


def make_design():
    doc = fitz.open(); pg = doc.new_page(width=W, height=H)
    pg.draw_rect(pg.rect, fill=(1, 1, 1), color=None)
    for m in range(12):                           # band labels R0 (top) .. R11 (bottom)
        y = m * PPM; sh = 0.20 + 0.06 * m
        pg.draw_rect(fitz.Rect(0, y, W, y + PPM), fill=(sh, 0.45, 0.85), color=None)
        pg.insert_text(fitz.Point(W / 2 - 28, y + 60), f"R{m}", fontsize=34, color=(1, 1, 1))
    # Rad-identifying asymmetric text (left strip vs right strip) + corner marker
    pg.insert_text(fitz.Point(8, 300),  "LEFT-RAD1",  fontsize=20, color=(1, 1, 0), rotate=90)
    pg.insert_text(fitz.Point(W - 30, 300), "RIGHT-RAD2", fontsize=20, color=(0, 1, 1), rotate=90)
    pg.draw_circle(fitz.Point(20, 20), 12, fill=(1, 0, 0), color=None)   # design TOP-LEFT marker
    buf = io.BytesIO(); doc.save(buf); doc.close(); return buf.getvalue()


PDF = make_design()
fitz.open(stream=PDF, filetype="pdf")[0].get_pixmap(matrix=fitz.Matrix(0.5, 0.5)).save("_v_source.png")


def page_order_layout(strip_bytes, outname, scale=0.7, gap=6):
    """Lay a strip's output pages left-to-right in page-number order (the wall layout)."""
    d = fitz.open(stream=strip_bytes, filetype="pdf")
    pms = [p.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB) for p in d]
    tw = sum(p.width for p in pms) + gap * (len(pms) - 1); h = max(p.height for p in pms)
    cv = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, tw, h), 0); cv.clear_with(255)
    x = 0
    for p in pms:
        p.set_origin(x, 0); cv.copy(p, p.irect); x += p.width + gap
    cv.save(outname); d.close()


def reconstruct(res, ruta_nedre, outname, scale=0.5):
    """Place each panel back at its design region (inverse rotation) with strips
    laid left-to-right (strip 0 = leftmost design column) and pages stacked."""
    rec = fitz.open(); rp = rec.new_page(width=W, height=H)
    rp.draw_rect(rp.rect, fill=(0.85, 0.85, 0.85), color=None)
    inv = 270 if ruta_nedre else 90               # inverse of the slice rotation
    for s, strip in enumerate(res["strips"]):
        d = fitz.open(stream=strip["bytes"], filetype="pdf")
        x0 = s * SW; x1 = min((s + 1) * SW, W)
        for k, p in enumerate(d):
            if ruta_nedre:
                y0 = k * PH; y1 = min(H, (k + 1) * PH)
            else:
                y1 = H - k * PH; y0 = max(0, H - (k + 1) * PH)
            cw = y1 - y0
            clip = fitz.Rect(0, 0, cw, x1 - x0)   # content sub-rect (left-aligned)
            rp.show_pdf_page(fitz.Rect(x0, y0, x1, y1), d, p.number, clip=clip, rotate=inv)
        d.close()
    rp.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB).save(outname)


def grid_png(res, outname, scale=0.5):
    d = fitz.open(stream=res["grid_pdf"], filetype="pdf")
    d[0].get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB).save(outname)


res_def = slicer.run_slice(PDF, WIDTH_M, HEIGHT_M, ruta_nedre=False)
res_ned = slicer.run_slice(PDF, WIDTH_M, HEIGHT_M, ruta_nedre=True)

page_order_layout(res_def["strips"][0]["bytes"], "_v_pageorder_default.png")
page_order_layout(res_ned["strips"][0]["bytes"], "_v_pageorder_nedre.png")
reconstruct(res_ned, True,  "_v_recon_nedre.png")
reconstruct(res_def, False, "_v_recon_default.png")
grid_png(res_def, "_v_grid_default.png")
grid_png(res_ned, "_v_grid_nedre.png")
print("done: _v_*.png written")
