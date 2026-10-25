"""
probe.py — measuring / inspection helpers for the ruta pipeline's tests (TIF-88).

Deduplicated from the copies that had grown in the _validate_* guards (_spans x3,
dashed-line extraction x3, background/pink detection x3, a slice-driver wrapper
x4). Import-safe: nothing runs on import, nothing is patched until a driver is
called, and every driver restores what it patched.

Everything here MEASURES output; nothing here decides what output should be.
Expectations live with the cases (synth_cases.py), phrased against slicer's own
constants.
"""
import contextlib
import hashlib
import io
import math
import os
import threading

import fitz
import numpy as np
from scipy import ndimage

import slicer

PINK_PAD = (slicer.PINK_PAD_R, slicer.PINK_PAD_G, slicer.PINK_PAD_B)
PINK_PAD_255 = tuple(round(v * 255) for v in PINK_PAD)

# Span colours slice_one_strip writes with, as get_text("dict") reports them.
PAGE_NUM_COLOR = 0xFF8000      # (1, 0.5, 0)
KLIPP_COLOR    = 0x262626      # (0.15, 0.15, 0.15)


# ── raw page inspection ──────────────────────────────────────────────────────

def spans(page):
    return [sp for blk in page.get_text("dict")["blocks"]
            for ln in blk.get("lines", []) for sp in ln["spans"]]


def dashed_lines(page):
    """x of every dashed cut line on the page (Klipp's line is the only dashed
    drawing slice_one_strip emits)."""
    out = []
    for dr in page.get_drawings():
        d = dr.get("dashes")
        if d and d.replace(" ", "") not in ("[]0", "") \
                and any(it[0] == "l" for it in dr["items"]):
            out.append(dr["rect"].x0)
    return out


def pink_pads(page):
    """PINK_PAD rectangles drawn ON the output page (partial-page padding).
    Excludes the full-page fill of an all-background placeholder page."""
    out = []
    for dr in page.get_drawings():
        f = dr.get("fill")
        if f and all(abs(a - b) < 0.03 for a, b in zip(f, PINK_PAD)):
            r = dr["rect"]
            if not (r.x0 <= 0.01 and r.x1 >= page.rect.width - 0.01):
                out.append(r)
    return out


def render(page, scale=slicer.LABEL_RENDER_SCALE):
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB)
    return np.frombuffer(pix.samples, dtype=np.uint8) \
             .reshape(pix.height, pix.width, pix.n)[:, :, :3].astype(np.int32)


def band_masks(arr):
    """is_fully_background's three colour bands, separately, re-read from slicer's
    constants (so a retuned band is tested as retuned)."""
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    return {
        "pink": ((r > slicer.PINK_R_MIN) & (g > slicer.PINK_G_MIN) & (g < slicer.PINK_G_MAX)
                 & (b > slicer.PINK_B_MIN) & (b < slicer.PINK_B_MAX) & (r > g) & (r > b)),
        "orange": ((r > slicer.ORANGE_R_MIN) & (g > slicer.ORANGE_G_MIN)
                   & (g < slicer.ORANGE_G_MAX) & (b < slicer.ORANGE_B_MAX)
                   & (r > g) & (g > b)),
        "lilac": ((r > slicer.LILAC_R_MIN) & (g > slicer.LILAC_G_MIN)
                  & (g < slicer.LILAC_G_MAX) & (b > slicer.LILAC_B_MIN)
                  & (b < slicer.LILAC_B_MAX) & (r > g) & (r > b)),
    }


def bg_fraction(page):
    """Fraction of a RENDERED output page in the pink/orange/lilac bands."""
    m = band_masks(render(page, 2.0))
    return float((m["pink"] | m["orange"] | m["lilac"]).mean())


def is_placeholder(page):
    """The all-background placeholder slice_one_strip emits for a zero-page strip:
    essentially all PINK_PAD. Rendered at 2.0 with the border ring trimmed, as in
    _validate_skissyta (at 0.5 the ring alone is 12% of a 113x42 pt page)."""
    arr = render(page, 2.0)
    close = np.abs(arr - np.array(PINK_PAD_255)).max(axis=2) <= 2
    if close.shape[0] >= 3 and close.shape[1] >= 3:
        close = close[1:-1, 1:-1]
    return bool(close.mean() > 0.95)


def page_facts(page, index):
    """Everything the cases assert on, for one output page, as plain data."""
    sp = spans(page)
    klipp = next((s for s in sp if s["text"].strip().startswith("Kli")), None)
    # Colour AND digits: get_text can emit an empty orange span between "Klipp"
    # and the number when the two sit close on the same baseline.
    pnum = next((s for s in sp if s["color"] == PAGE_NUM_COLOR
                 and s["text"].strip().isdigit()), None)
    labels = [s for s in sp if s is not klipp and s is not pnum]
    return {
        "index": index,
        "w": page.rect.width, "h": page.rect.height,
        "lines": dashed_lines(page),
        "pads": pink_pads(page),
        "klipp": None if klipp is None else {"bbox": tuple(klipp["bbox"]),
                                             "size": klipp["size"]},
        "pnum": None if pnum is None else {"text": pnum["text"].strip(),
                                           "bbox": tuple(pnum["bbox"])},
        "labels": [(s["text"].strip(), tuple(s["bbox"]), s["size"]) for s in labels],
        "placeholder": None,       # filled lazily, rendering is the costly part
    }


def strip_facts(strip_bytes):
    doc = fitz.open(stream=strip_bytes, filetype="pdf")
    pages = [page_facts(p, i) for i, p in enumerate(doc)]
    if len(pages) == 1 and not pages[0]["labels"] and not pages[0]["lines"]:
        pages[0]["placeholder"] = is_placeholder(doc[0])
    doc.close()
    return pages


# ── labeled grid (TIF-95) ────────────────────────────────────────────────────
# get_texttrace, NOT get_text("dict"): it reports one entry per text-showing
# operation, so two adjacent labels with the same style can never merge into one
# span and silently lose a count, and nothing is dropped for being tiny.

GRID_RED = (0.9, 0.1, 0.1)
_FURNITURE_GREY = (0.15, 0.15, 0.15)


def trace(page):
    """Every text operation on the page: dicts of text, origin x/y, size, colour
    (rounded to 3 dp), glyph bbox, opacity."""
    out = []
    for t in page.get_texttrace():
        if not t["chars"]:
            continue
        x, y = t["chars"][0][2]
        out.append({"text": "".join(chr(c[0]) for c in t["chars"]), "x": x, "y": y,
                    "size": t["size"], "color": tuple(round(c, 3) for c in t["color"]),
                    "bbox": fitz.Rect(t["bbox"]), "opacity": t["opacity"]})
    return out


def strip_labels(strip_bytes):
    """[(page_num, span)] for every paint-code label on a strip PDF. page_num is
    read from the page's own orange number, because a page excluded by
    is_fully_background leaves a gap: the PDF page index is not page_num.
    Furniture is removed EXACTLY, never by a text pattern: the page number is
    orange AND all digits, Klipp is dark grey AND the literal "Klipp". A short
    numeric code ("S", "1500") is black/white and so can never match either."""
    out = []
    doc = fitz.open(stream=strip_bytes, filetype="pdf")
    for i, pg in enumerate(doc):
        sp = trace(pg)
        pn = [t for t in sp if t["color"] == (1.0, 0.5, 0.0) and t["text"].strip().isdigit()]
        if len(pn) != 1:
            raise AssertionError(f"strip page {i + 1}: {len(pn)} page numbers")
        page_num = int(pn[0]["text"]) - 1
        for t in sp:
            if t is pn[0] or (t["color"] == _FURNITURE_GREY and t["text"] == "Klipp"):
                continue
            out.append((page_num, t))
    doc.close()
    return out


def grid_texts(grid_bytes):
    """(labels, refs) on page 1 of a grid PDF: refs are the red Rad/Ruta texts."""
    doc = fitz.open(stream=grid_bytes, filetype="pdf")
    sp = trace(doc[0])
    doc.close()
    return ([t for t in sp if t["color"] != GRID_RED],
            [t for t in sp if t["color"] == GRID_RED])


def match_labels(a, b, tol=0.01):
    """Multiset match of (text, x, y, size, colour) tuples, positions within tol.
    Returns (only_in_a, only_in_b)."""
    pool = {}
    for t in b:
        pool.setdefault((t[0], round(t[3], 3), t[4]), []).append([t[1], t[2], False])
    only_a = []
    for t in a:
        cands = pool.get((t[0], round(t[3], 3), t[4]), [])
        hit = next((c for c in cands if not c[2] and abs(c[0] - t[1]) <= tol
                    and abs(c[1] - t[2]) <= tol), None)
        if hit is None:
            only_a.append(t)
        else:
            hit[2] = True
    only_b = [(k[0], c[0], c[1], k[1], k[2]) for k, cs in pool.items() for c in cs if not c[2]]
    return only_a, only_b


def render_wide(pdf_bytes, extra_w, scale=2.0):
    """Page 1 rendered WITH alpha on a mediabox widened by extra_w to the right,
    so ink drawn past the page edge (and white text on nothing) becomes visible."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pg = doc[0]
    r = pg.mediabox
    pg.set_mediabox(fitz.Rect(r.x0, r.y0, r.x1 + extra_w, r.y1))
    pix = pg.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=True)
    a = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n).astype(int)
    doc.close()
    return a


# ── the zero-skip contract (moved from _design_label_harness, TIF-88) ────────

def count_labelable_patches(arr, color_map):
    """
    Independently recount the connected patches on a rendered page, split by the
    TIF-69 size gate. This is the SINGLE definition of the "zero skips" contract
    (it was _design_label_harness.count_labelable_patches, which now re-exports
    this; before TIF-71 a second guard kept its own copy and drifted).

    `arr` must be (H, W, 3) int, rendered at slicer.LABEL_RENDER_SCALE. Returns two
    {code: n} dicts:
      expected       -- patches >= MIN_PATCH_PX whose inscribed-circle radius is
                        >= MIN_LABEL_PATCH_SIZE_PT. Every one MUST get one label.
      skipped_small  -- patches >= MIN_PATCH_PX but below that radius floor.
                        Expected to be skipped (TIF-69) — reported, not asserted.
    Uses the labeler's own nearest-colour masks (slicer._code_masks) and the same
    distance-transform r_pts its sizing ladder uses.
    """
    S = slicer.LABEL_RENDER_SCALE
    expected, skipped_small = {}, {}
    for code, mask in slicer._code_masks(arr, color_map).items():
        lbl, num = ndimage.label(mask)
        if num == 0:
            continue
        counts  = np.bincount(lbl.ravel())
        objects = ndimage.find_objects(lbl)
        for comp in range(1, num + 1):
            if counts[comp] < slicer.MIN_PATCH_PX:
                continue
            sub_mask = lbl[objects[comp - 1]] == comp
            dt = ndimage.distance_transform_edt(np.pad(sub_mask, 1))[1:-1, 1:-1]
            r_pts = float(dt.max()) / S
            bucket = skipped_small if r_pts < slicer.MIN_LABEL_PATCH_SIZE_PT else expected
            bucket[code] = bucket.get(code, 0) + 1
    return expected, skipped_small


def _fit_escapes(summary, masks, shape):
    """Fitted labels whose glyph bbox is not inside their OWN colour's mask (ported
    from _validate_color_labels 3b). Sub-pixel labels are checked at their centre."""
    S = slicer.LABEL_RENDER_SCALE
    out = []
    for code, e in summary.items():
        mask = masks.get(code, np.zeros(shape, dtype=bool))
        for rect, kind in zip(e["rects"], e["placement"]):
            if kind != "fit":
                continue
            x0, y0 = int(np.ceil(rect[0] * S)), int(np.ceil(rect[1] * S))
            x1, y1 = int(np.floor(rect[2] * S)), int(np.floor(rect[3] * S))
            if x1 > x0 and y1 > y0:
                inside = bool(mask[y0:y1, x0:x1].all())
            else:
                cy = min(int((rect[1] + rect[3]) / 2 * S), shape[0] - 1)
                cx = min(int((rect[0] + rect[2]) / 2 * S), shape[1] - 1)
                inside = bool(mask[cy, cx])
            if not inside:
                out.append((code, rect))
    return out


class LabelRecorder:
    """
    Wraps slicer._label_colors_on_page for the duration of a `with` block, the way
    _design_label_harness does: recounts labelable patches on each clean page
    before the real labeler runs, then records what it placed. Thread-safe, since
    run_slice labels strips from a thread pool.
    """

    def __init__(self):
        self.expected = self.placed = self.skipped_small = self.forced = 0
        self.by_code = {}           # code -> placed
        self.expected_by_code = {}
        self.fit_rects = []         # (page_key, code, rect) of fitted labels
        self.escapes = []           # fitted labels whose glyph box leaves its colour
        self.calls = 0
        self._lock = threading.Lock()
        self._real = None

    def __enter__(self):
        self._real = slicer._label_colors_on_page

        def recording(page, color_map, **kw):
            arr = render(page)                      # the CLEAN page, before any text
            exp, small = count_labelable_patches(arr, color_map)
            masks = slicer._code_masks(arr, color_map)
            summary = self._real(page, color_map, **kw)
            escapes = _fit_escapes(summary, masks, arr.shape[:2])
            with self._lock:
                self.calls += 1
                key = self.calls
                self.expected += sum(exp.values())
                self.skipped_small += sum(small.values())
                self.escapes.extend(escapes)
                for c, n in exp.items():
                    self.expected_by_code[c] = self.expected_by_code.get(c, 0) + n
                for code, e in summary.items():
                    self.placed += e["count"]
                    self.forced += e["forced"]
                    self.by_code[code] = self.by_code.get(code, 0) + e["count"]
                    for rect, kind in zip(e["rects"], e["placement"]):
                        if kind == "fit":
                            self.fit_rects.append((key, code, rect))
            return summary

        slicer._label_colors_on_page = recording
        return self

    def __exit__(self, *exc):
        slicer._label_colors_on_page = self._real
        return False


@contextlib.contextmanager
def patched(constants):
    """Temporarily override slicer module constants (the gate-toggle proofs)."""
    saved = {k: getattr(slicer, k) for k in constants}
    for k in constants:
        assert hasattr(slicer, k), f"slicer has no constant {k}"
    try:
        for k, v in constants.items():
            setattr(slicer, k, v)
        yield
    finally:
        for k, v in saved.items():
            setattr(slicer, k, v)


# ── drivers ──────────────────────────────────────────────────────────────────

def drive_strip(pdf_bytes, width_m, height_m, strip, nedre, cmap,
                skip_labels=True, page_height_m=None):
    """The real slice_one_strip on one strip. Returns (strip_bytes, stdout)."""
    ph = slicer.PAGE_HEIGHT_M if page_height_m is None else page_height_m
    ns = math.ceil(width_m / slicer.STRIP_WIDTH_M)
    npg = math.ceil(height_m / ph)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _, b = slicer.slice_one_strip(
            (strip, pdf_bytes, width_m, height_m, ns, npg, cmap, nedre,
             skip_labels, ph))
    return b, buf.getvalue()


def drive_job(data, width_m, height_m, **kw):
    """The real run_slice (the api.py entry point). Returns (result, stdout)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        res = slicer.run_slice(data, width_m, height_m, **kw)
    return res, buf.getvalue()


def find_cut(pdf_bytes, design, strip, nedre, cmap, page_height_m=None):
    """slicer._find_cut_boundary called directly on one strip of a fixture."""
    ph = design.page_h(page_height_m)
    npg = math.ceil(design.height_m / (page_height_m or slicer.PAGE_HEIGHT_M))
    x0, x1 = design.strip_x(strip)
    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return slicer._find_cut_boundary(src, 0, x0, x1, design.full_h, ph, npg,
                                         nedre, cmap)
    finally:
        src.close()


# ── digests / review output ──────────────────────────────────────────────────

def render_digest(pdf_bytes, scale=2.0):
    """Rendered-pixel digest (TIF-87's method): what prints, not what is stored."""
    h = hashlib.sha256()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    for p in doc:
        h.update(p.get_pixmap(matrix=fitz.Matrix(scale, scale),
                              colorspace=fitz.csRGB).samples)
    doc.close()
    return h.hexdigest()


def save_pngs(pdf_bytes, out_dir, stem, scale=8.0):
    os.makedirs(out_dir, exist_ok=True)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    for i, p in enumerate(doc):
        p.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB) \
         .save(os.path.join(out_dir, f"{stem}_p{i + 1}.png"))
    doc.close()
