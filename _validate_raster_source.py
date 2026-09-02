"""
_validate_raster_source.py — TIF raster (JPG/PNG) source support.

Drives the REAL production entry point, slicer.run_slice(), on genuine JPG and
PNG uploads and verifies, explicitly and one at a time, that every stage after
the new image->PDF preprocessing step behaves on a raster-sourced page:
conversion geometry, the stretch-to-fill rule, rotation (default AND ruta_nedre),
strip/page counts and dimensions, Klipp cut detection, page numbering, and colour
labeling / unknown_colors.

All artifacts land in raster_test/, which is wiped at the start of every run.
"""

import io
import os
import shutil

import fitz
import numpy as np

import slicer
from slicer import (run_slice, PTS_PER_M, image_to_pdf, extract_pdf_colors,
                    _hex_to_rgb255)

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "raster_test")

# The design's declared background. "Skip" is what _background_mask keys the
# Klipp content scan off, exactly as a real vector design declares it.
PINK_SKIP = "#EEA8CB"

COLOUR_MAP = {
    PINK_SKIP: "Skip",
    "#1E7534": "3560",
    "#D4D1CD": "1500",
    "#392C1B": "8010",
    "#FFFFFF": "v",
    "#000000": "S",
}

# A marker colour deliberately NOT in COLOUR_MAP, used to prove stretch geometry
# and to probe unknown-colour reporting.
MARKER = (1.0, 0.0, 1.0)   # magenta


# ── Test image generation ─────────────────────────────────────────────────────

def _hexf(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def make_design_image(path, px_w, px_h, content_frac=0.75, fmt=None, marker=True):
    """
    Render a design-like raster: mapped-colour shapes over the BOTTOM
    `content_frac` of the image, declared-Skip pink over the top remainder (the
    leftover fabric a real design leaves for the Klipp cut).

    A magenta marker square sits in the top-left 10% of the image so stretch
    geometry can be measured independently of the artwork.
    """
    doc = fitz.open()
    page = doc.new_page(width=px_w, height=px_h)
    sh = page.new_shape()

    split = px_h * (1 - content_frac)
    sh.draw_rect(fitz.Rect(0, 0, px_w, split))
    sh.finish(fill=_hexf(PINK_SKIP), color=None)

    sh.draw_rect(fitz.Rect(0, split, px_w, px_h))
    sh.finish(fill=_hexf("#D4D1CD"), color=None)
    sh.commit()

    # Blobs in mapped colours, spread across the content area.
    sh = page.new_shape()
    for i, hx in enumerate(["#1E7534", "#392C1B", "#FFFFFF", "#000000"]):
        for j in range(4):
            x = px_w * (0.06 + 0.23 * j)
            y = split + (px_h - split) * (0.10 + 0.22 * i)
            sh.draw_rect(fitz.Rect(x, y, x + px_w * 0.16, y + (px_h - split) * 0.16))
            sh.finish(fill=_hexf(hx), color=None)
    sh.commit()

    # Stretch marker: top-left, 10% of each dimension. Optional, because it sits
    # inside the Skip background and is therefore real content there — which is
    # exactly what a trailing-background-exclusion test must not have.
    if marker:
        sh = page.new_shape()
        sh.draw_rect(fitz.Rect(0, 0, px_w * 0.10, px_h * 0.10))
        sh.finish(fill=MARKER, color=None)
        sh.commit()

    pix = page.get_pixmap(colorspace=fitz.csRGB)
    pix.save(path, output=fmt or os.path.splitext(path)[1].lstrip("."))
    doc.close()
    return path


def make_photo_image(path, px_w, px_h, fmt=None):
    """
    A full-bleed 'photo': a smooth gradient with no declared-Skip background
    anywhere. This is the raster case that has NO designed leftover margin.
    """
    doc = fitz.open()
    page = doc.new_page(width=px_w, height=px_h)
    sh = page.new_shape()
    bands = 40
    for i in range(bands):
        t = i / (bands - 1)
        y0 = px_h * i / bands
        y1 = px_h * (i + 1) / bands
        sh.draw_rect(fitz.Rect(0, y0, px_w, y1 + 1))
        sh.finish(fill=(0.15 + 0.7 * t, 0.35, 0.85 - 0.6 * t), color=None)
    sh.commit()
    pix = page.get_pixmap(colorspace=fitz.csRGB)
    pix.save(path, output=fmt or os.path.splitext(path)[1].lstrip("."))
    doc.close()
    return path


def make_alpha_png(path, px_w, px_h):
    """A PNG with a genuinely transparent region (right half)."""
    doc = fitz.open()
    page = doc.new_page(width=px_w, height=px_h)
    sh = page.new_shape()
    sh.draw_rect(fitz.Rect(0, 0, px_w * 0.5, px_h))
    sh.finish(fill=_hexf("#1E7534"), color=None)
    sh.commit()
    pix = page.get_pixmap(colorspace=fitz.csRGB, alpha=True)
    pix.save(path, output="png")
    doc.close()
    return path


# ── Inspection helpers ────────────────────────────────────────────────────────

def page_texts(pdf_bytes):
    """[(page_index, [text spans])] for an output strip PDF."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    out = []
    for p in doc:
        words = [w[4] for w in p.get_text("words")]
        out.append(words)
    doc.close()
    return out


def page_dims(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    d = [(round(p.rect.width, 2), round(p.rect.height, 2)) for p in doc]
    doc.close()
    return d


def has_dashed_line(pdf_bytes, page_idx):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    found = False
    if page_idx < len(doc):
        for d in doc[page_idx].get_drawings():
            if d.get("dashes") and d["dashes"] not in ("[] 0", ""):
                found = True
    doc.close()
    return found


def sample_rgb(pdf_bytes, page_idx, fx, fy, scale=2.0):
    """RGB at fractional position (fx, fy) of a rendered output page."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    p = doc[page_idx]
    pix = p.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
    px = min(int(fx * pix.width), pix.width - 1)
    py = min(int(fy * pix.height), pix.height - 1)
    doc.close()
    return tuple(int(v) for v in arr[py, px])


def save_result(tag, result):
    d = os.path.join(OUT, tag)
    os.makedirs(d, exist_ok=True)
    for s in result["strips"]:
        with open(os.path.join(d, s["filename"]), "wb") as f:
            f.write(s["bytes"])
    with open(os.path.join(d, "grid.pdf"), "wb") as f:
        f.write(result["grid_pdf"])
    return d


def hdr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    return bool(ok)


# ── Tests ─────────────────────────────────────────────────────────────────────

RESULTS = []


def t_conversion_geometry():
    hdr("STEP 1 — image->PDF conversion geometry and the stretch rule")
    ok = True

    # Aspect-MATCHED source: 600x1200 px for a 6x12 m design (both 0.5).
    m = make_design_image(os.path.join(OUT, "src_matched.png"), 600, 1200)
    # Aspect-MISMATCHED source: 1600x400 px (4:1) for the same 6x12 m (1:2).
    x = make_design_image(os.path.join(OUT, "src_mismatched.png"), 1600, 400)

    for tag, src in (("matched", m), ("mismatched", x)):
        with open(src, "rb") as f:
            pdf = image_to_pdf(f.read(), 6.0, 12.0)
        doc = fitz.open(stream=pdf, filetype="pdf")
        r = doc[0].rect
        ok &= check(f"{tag}: page is {6.0*PTS_PER_M:.2f} x {12.0*PTS_PER_M:.2f} pts",
                    abs(r.width - 6.0 * PTS_PER_M) < 0.01 and abs(r.height - 12.0 * PTS_PER_M) < 0.01,
                    f"got {r.width:.2f} x {r.height:.2f}")
        ok &= check(f"{tag}: derived pts_per_m matches the 1:100 design convention",
                    abs(r.width / 6.0 - 28.3465) < 0.01 and abs(r.height / 12.0 - 28.3465) < 0.01,
                    f"x={r.width/6.0:.4f} y={r.height/12.0:.4f}")
        doc.close()

        # Stretch proof: the marker occupies the image's top-left 10%x10%.
        # Under fill-stretch it must still be at 10%x10% of the PAGE regardless of
        # the source aspect; under aspect-preserving placement the mismatched
        # source would letterbox and the marker would land somewhere else.
        inside = sample_rgb(pdf, 0, 0.05, 0.05)
        outside_x = sample_rgb(pdf, 0, 0.15, 0.05)
        outside_y = sample_rgb(pdf, 0, 0.05, 0.15)
        is_marker = lambda c: c[0] > 200 and c[1] < 60 and c[2] > 200
        ok &= check(f"{tag}: marker fills page top-left 10% (stretched, not letterboxed)",
                    is_marker(inside) and not is_marker(outside_x) and not is_marker(outside_y),
                    f"in={inside} right={outside_x} below={outside_y}")

    RESULTS.append(("conversion geometry + stretch", ok))
    return m, x


def t_full_pipeline(src_png, tag, width_m, height_m, **kw):
    """Run the REAL run_slice() and report structural facts."""
    with open(src_png, "rb") as f:
        data = f.read()
    result = run_slice(data, width_m, height_m, colour_map=COLOUR_MAP, **kw)
    d = save_result(tag, result)
    print(f"  artifacts -> {os.path.relpath(d)}")
    return result


def t_structure():
    hdr("STEP 2a — strip count, page count, page dimensions (JPG, aspect-matched)")
    ok = True
    jpg = make_design_image(os.path.join(OUT, "src_design.jpg"), 900, 1800, fmt="jpg")
    res = t_full_pipeline(jpg, "jpg_default", 6.0, 12.0)

    ok &= check("strip count = ceil(6.0 / 1.5) = 4", len(res["strips"]) == 4,
                f"got {len(res['strips'])}")

    exp_page_h = 4.0 * PTS_PER_M          # output page WIDTH  (a 4 m band)
    exp_strip_w = 1.5 * PTS_PER_M         # output page HEIGHT (a 1.5 m strip)
    dims = page_dims(res["strips"][0]["bytes"])
    ok &= check("output page dims = 4 m x 1.5 m in pts",
                all(abs(w - exp_page_h) < 0.5 and abs(h - exp_strip_w) < 0.5 for w, h in dims),
                f"expected ({exp_page_h:.2f}, {exp_strip_w:.2f}), got {dims}")
    print(f"       strip 1 pages rendered: {len(dims)} of ceil(12/4)=3")
    RESULTS.append(("strip/page structure", ok))
    return res, jpg


def t_page_numbering(res):
    hdr("STEP 2b — page numbering on raster-sourced pages")
    ok = True
    for s in res["strips"][:2]:
        texts = page_texts(s["bytes"])
        nums = []
        for words in texts:
            nums.append([w for w in words if w.isdigit()])
        print(f"    {s['filename']}: digit tokens per page = {nums}")
        ok &= check(f"{s['filename']} every rendered page carries a page number",
                    all(n for n in nums), str(nums))
    RESULTS.append(("page numbering", ok))


def t_rotation(jpg):
    hdr("STEP 2c — rotation: default (bottom->top) vs ruta_nedre (top->bottom)")
    ok = True
    # The source has pink Skip across its TOP quarter and artwork below.
    # Default: page 1 = design BOTTOM (artwork), leftover pink at the LAST page.
    # ruta_nedre: page 1 = design TOP (pink), artwork after.
    dflt = t_full_pipeline(jpg, "jpg_default_rot", 6.0, 12.0)
    nedre = t_full_pipeline(jpg, "jpg_ruta_nedre", 6.0, 12.0, ruta_nedre=True)

    def pinkness(pdf_bytes, page_idx):
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        if page_idx >= len(doc):
            doc.close()
            return None
        pix = doc[page_idx].get_pixmap(matrix=fitz.Matrix(1, 1), colorspace=fitz.csRGB)
        a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3].astype(int)
        r, g, b = a[..., 0], a[..., 1], a[..., 2]
        m = (r > 200) & (g > 130) & (g < 200) & (b > 170) & (b < 230)
        doc.close()
        return round(float(m.mean()), 3)

    d0 = pinkness(dflt["strips"][1]["bytes"], 0)
    dl = pinkness(dflt["strips"][1]["bytes"], len(page_dims(dflt["strips"][1]["bytes"])) - 1)
    n0 = pinkness(nedre["strips"][1]["bytes"], 0)
    nl = pinkness(nedre["strips"][1]["bytes"], len(page_dims(nedre["strips"][1]["bytes"])) - 1)
    print(f"    default   strip2: pink fraction page1={d0}  lastpage={dl}")
    print(f"    ruta_nedre strip2: pink fraction page1={n0}  lastpage={nl}")

    ok &= check("default: page 1 is artwork (design bottom), pink lands late",
                d0 is not None and dl is not None and d0 < dl, f"{d0} < {dl}")
    ok &= check("ruta_nedre: page 1 is the design TOP (the pink leftover)",
                n0 is not None and nl is not None and n0 > nl, f"{n0} > {nl}")

    # Rotation faithfulness: a pure rotation must not mirror. The magenta marker
    # sits at the source's top-LEFT; under both modes it must appear exactly once
    # and as a solid block, not a mirrored ghost.
    def marker_frac(pdf_bytes):
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        tot = 0
        for p in doc:
            pix = p.get_pixmap(matrix=fitz.Matrix(1, 1), colorspace=fitz.csRGB)
            a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3].astype(int)
            tot += int(((a[..., 0] > 200) & (a[..., 1] < 60) & (a[..., 2] > 200)).sum())
        doc.close()
        return tot

    md = sum(marker_frac(s["bytes"]) for s in dflt["strips"])
    mn = sum(marker_frac(s["bytes"]) for s in nedre["strips"])
    ok &= check("marker pixel count preserved across both rotation modes",
                md > 0 and abs(md - mn) / max(md, 1) < 0.05, f"default={md} nedre={mn}")
    RESULTS.append(("rotation (default + ruta_nedre)", ok))
    return dflt, nedre


def t_klipp_with_background(dflt):
    hdr("STEP 2d — Klipp cut detection on a raster page WITH a declared Skip background")
    ok = True
    any_klipp = False
    for s in dflt["strips"]:
        texts = page_texts(s["bytes"])
        for i, words in enumerate(texts):
            if any(w == "Klipp" for w in words):
                any_klipp = True
                print(f"    {s['filename']} page {i+1}: 'Klipp' text + dashed line="
                      f"{has_dashed_line(s['bytes'], i)}")
    ok &= check("Klipp fires on raster art that declares a Skip background", any_klipp)
    RESULTS.append(("Klipp — raster WITH Skip background", ok))


def t_klipp_photo():
    hdr("STEP 2e — Klipp on a full-bleed photo (NO Skip background anywhere)")
    photo = make_photo_image(os.path.join(OUT, "src_photo.jpg"), 900, 1800, fmt="jpg")
    res = t_full_pipeline(photo, "photo_default", 6.0, 12.0)
    klipp = 0
    for s in res["strips"]:
        for words in page_texts(s["bytes"]):
            klipp += sum(1 for w in words if w == "Klipp")
    pages = sum(len(page_dims(s["bytes"])) for s in res["strips"])
    print(f"    rendered pages across all strips: {pages}   'Klipp' occurrences: {klipp}")
    ok = check("full-bleed photo draws NO Klipp marking (correct: no leftover fabric)",
               klipp == 0, f"found {klipp}")
    ok &= check("no page wrongly excluded as background", pages == 4 * 3, f"got {pages}")
    RESULTS.append(("Klipp — full-bleed photo (no Skip background)", ok))
    return res


def t_colour_labeling(dflt, jpg):
    hdr("STEP 3 — colour labeling and unknown_colors on a raster source")
    ok = True

    with open(jpg, "rb") as f:
        raster_pdf = image_to_pdf(f.read(), 6.0, 12.0)
    found = extract_pdf_colors(raster_pdf)
    ok &= check("extract_pdf_colors() returns an EMPTY set (no vector drawings)",
                found == set(), f"got {sorted(found)!r}")

    ok &= check("unknown_colors is empty, not 'every pixel unknown'",
                dflt["unknown_colors"] == [], f"got {dflt['unknown_colors']}")

    # ...but [] here means NOT CHECKED. Prove it: the same design as vector PDF
    # reports the unmapped magenta; as raster it reports nothing.
    ok &= check("colors_analyzed=False marks that [] means 'not checked'",
                dflt["colors_analyzed"] is False, f"got {dflt['colors_analyzed']}")

    vec = fitz.open()
    vp = vec.new_page(width=6.0 * PTS_PER_M, height=12.0 * PTS_PER_M)
    vs = vp.new_shape()
    vs.draw_rect(fitz.Rect(0, 0, vp.rect.width, vp.rect.height * 0.5))
    vs.finish(fill=_hexf("#1E7534"), color=None)
    vs.draw_rect(fitz.Rect(0, vp.rect.height * 0.5, vp.rect.width, vp.rect.height))
    vs.finish(fill=MARKER, color=None)
    vs.commit()
    vb = io.BytesIO()
    vec.save(vb)
    vec.close()
    vres = run_slice(vb.getvalue(), 6.0, 12.0, colour_map=COLOUR_MAP)
    print(f"    same design as VECTOR pdf: unknown_colors={vres['unknown_colors']} "
          f"colors_analyzed={vres['colors_analyzed']}")
    ok &= check("vector path still reports the unmapped colour (contrast case)",
                vres["unknown_colors"] == ["#FF00FF"] and vres["colors_analyzed"] is True,
                str(vres["unknown_colors"]))

    # Labels are pixel-based (_label_colors_on_page renders the OUTPUT page), so
    # they should appear on raster content despite extract_pdf_colors seeing none.
    codes = set(v for v in COLOUR_MAP.values() if v != "Skip")
    seen = set()
    for s in dflt["strips"]:
        for words in page_texts(s["bytes"]):
            seen |= (set(words) & codes)
    print(f"    NCS codes actually printed on raster strips: {sorted(seen)}")
    ok &= check("colour labels ARE drawn on raster pages (pixel-based labeler)",
                len(seen) > 0, f"codes seen: {sorted(seen)}")
    RESULTS.append(("colour labeling + unknown_colors", ok))


def t_png_mismatched_end_to_end(mismatched_png):
    hdr("STEP 4 — PNG with mismatched aspect through the real pipeline")
    res = t_full_pipeline(mismatched_png, "png_mismatched", 6.0, 12.0)
    ok = check("PNG accepted and sliced", len(res["strips"]) == 4, f"{len(res['strips'])} strips")
    dims = page_dims(res["strips"][0]["bytes"])
    ok &= check("page dims identical to the aspect-matched run (stretch, not letterbox)",
                all(abs(w - 4.0 * PTS_PER_M) < 0.5 for w, _ in dims), str(dims))
    RESULTS.append(("PNG mismatched-aspect end-to-end", ok))


def t_banderoll():
    hdr("STEP 5 — banderoll mode on a raster source")
    ok = True
    # Banderoll: artwork laid out LANDSCAPE, user enters PORTRAIT hung dimensions.
    # 3 m wide x 24 m tall hung  ->  landscape source image, wide and short.
    land = make_design_image(os.path.join(OUT, "src_banderoll.png"), 2400, 300)
    res = t_full_pipeline(land, "banderoll", 3.0, 24.0, banderoll=True)
    ok &= check("banderoll strip count = ceil(3.0/1.5) = 2", len(res["strips"]) == 2,
                f"got {len(res['strips'])}")
    dims = page_dims(res["strips"][0]["bytes"])
    ok &= check("banderoll output page dims = 4 m x 1.5 m in pts",
                all(abs(w - 4.0 * PTS_PER_M) < 0.5 and abs(h - 1.5 * PTS_PER_M) < 0.5
                    for w, h in dims),
                str(dims[:2]))
    print(f"       pages in strip 1: {len(dims)} of ceil(24/4)=6")
    RESULTS.append(("banderoll mode", ok))


def t_alpha_png():
    hdr("STEP 6 — PNG with an alpha channel")
    p = make_alpha_png(os.path.join(OUT, "src_alpha.png"), 600, 1200)
    res = t_full_pipeline(p, "png_alpha", 6.0, 12.0)
    c = sample_rgb(res["strips"][3]["bytes"], 0, 0.5, 0.5)
    print(f"    colour rendered where the PNG was transparent: {c}")
    ok = check("transparent region does not crash the pipeline", len(res["strips"]) == 4)
    RESULTS.append(("alpha PNG", ok))
    return c


def t_trailing_bg_exclusion():
    hdr("STEP 7 — TIF-68 trailing all-background page exclusion on a raster source")
    # 12 m tall, 3 pages of 4 m. Content occupies the bottom 7 m, so the top 5 m
    # is pure declared-Skip pink — page 3 (the last in printing order) is then
    # ~100% background and must be EXCLUDED, exactly as for a vector design.
    # marker=False: the stretch marker lives in that top region, and it is REAL
    # content, so leaving it in would (correctly) keep strip 1's page 3 alive and
    # test nothing. Confirmed in an earlier run: with the marker, strip 1 rendered
    # 3 pages and strips 2-4 rendered 2 — i.e. exclusion is content-sensitive, not
    # blindly geometric.
    src = make_design_image(os.path.join(OUT, "src_trailing_bg.png"), 600, 1200,
                            content_frac=7.0 / 12.0, marker=False)
    res = t_full_pipeline(src, "trailing_bg", 6.0, 12.0)
    counts = [len(page_dims(s["bytes"])) for s in res["strips"]]
    print(f"    pages rendered per strip: {counts} (3 would mean nothing excluded)")
    ok = check("the ~100%-background trailing page is excluded from every strip",
               all(c == 2 for c in counts), f"got {counts}")
    RESULTS.append(("TIF-68 trailing bg exclusion on raster", ok))


def t_jpeg_robustness():
    hdr("STEP 8 — JPEG compression vs COLOR_MATCH_TOLERANCE")
    # Worst case for JPEG ringing: thin alternating stripes of mapped colours.
    hexes = ["#1E7534", "#D4D1CD", "#392C1B", PINK_SKIP]
    doc = fitz.open()
    page = doc.new_page(width=600, height=1200)
    sh = page.new_shape()
    for i in range(60):
        sh.draw_rect(fitz.Rect(0, 1200 * i / 60, 600, 1200 * (i + 1) / 60))
        sh.finish(fill=_hexf(hexes[i % len(hexes)]), color=None)
    sh.commit()
    pix = page.get_pixmap(colorspace=fitz.csRGB)

    ok = True
    reps = [_hex_to_rgb255(h) for h in hexes]
    for q in (95, 75, 50, 30):
        pdf = image_to_pdf(pix.tobytes(output="jpg", jpg_quality=q), 6.0, 12.0)
        d = fitz.open(stream=pdf, filetype="pdf")
        p = d[0].get_pixmap(matrix=fitz.Matrix(slicer.LABEL_RENDER_SCALE,
                                               slicer.LABEL_RENDER_SCALE),
                            colorspace=fitz.csRGB)
        a = np.frombuffer(p.samples, dtype=np.uint8).reshape(p.height, p.width, p.n)[:, :, :3].astype(int)
        dist = np.stack([np.sqrt(((a - r) ** 2).sum(axis=2)) for r in reps]).min(axis=0)
        frac = float((dist <= slicer.COLOR_MATCH_TOLERANCE).mean())
        print(f"    jpg quality {q:3d}: {frac*100:5.1f}% of pixels within "
              f"tolerance {slicer.COLOR_MATCH_TOLERANCE} (mean drift {dist.mean():.2f})")
        ok &= frac > 0.85
        d.close()
    doc.close()
    RESULTS.append(("JPEG compression stays inside colour tolerance", check(
        "even quality-30 JPEG keeps >85% of pixels matchable", ok)))


def t_resolution_probe():
    hdr("RESOLUTION PROBE — effective print DPI of a raster upload")
    for px_w, label in ((900, "900 px"), (4000, "4000 px"), (12000, "12000 px")):
        # 6 m wide design; each output page covers a 1.5 m strip.
        px_per_m = px_w / 6.0
        # LABEL_RENDER_SCALE analysis grid, and the real-world dot pitch.
        mm_per_px = 1000.0 / px_per_m
        print(f"    source {label:>9} wide -> {px_per_m:7.1f} px/m  "
              f"= {mm_per_px:6.1f} mm per source pixel on the finished tifo")


def main():
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)
    print(f"Output folder cleared: {os.path.relpath(OUT)}")
    print(f"slicer VERSION={slicer.VERSION}  ENABLE_COLOR_LABELS={slicer.ENABLE_COLOR_LABELS}")
    print(f"PTS_PER_M={PTS_PER_M:.6f}")

    _matched, mismatched = t_conversion_geometry()
    res, jpg = t_structure()
    t_page_numbering(res)
    dflt, _nedre = t_rotation(jpg)
    t_klipp_with_background(dflt)
    t_klipp_photo()
    t_colour_labeling(dflt, jpg)
    t_png_mismatched_end_to_end(mismatched)
    t_banderoll()
    t_alpha_png()
    t_trailing_bg_exclusion()
    t_jpeg_robustness()
    t_resolution_probe()

    hdr("SUMMARY")
    for name, ok in RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    bad = [n for n, o in RESULTS if not o]
    print(f"\n  {len(RESULTS) - len(bad)}/{len(RESULTS)} groups passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
