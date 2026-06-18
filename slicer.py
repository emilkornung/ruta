"""
slicer.py — Core PDF slicing logic for Hammaby Tifo ruta pipeline.

Public interface: run_slice()
All Gmail, Drive, OAuth, and email-parsing code lives in ruta.py (the
standalone backup). This module contains only the PDF geometry logic.
"""

import io
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import fitz  # pymupdf

# ── Config ────────────────────────────────────────────────────────────────────

SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
COLOR_MAP_FILE = os.path.join(SCRIPT_DIR, "color_map.json")

STRIP_WIDTH_M = 1.5
PAGE_HEIGHT_M = 4.0
SLICE_WORKERS = 6

# Pink page detection
PINK_THRESHOLD         = 0.85
PINK_R_MIN             = 180
PINK_G_MIN, PINK_G_MAX = 100, 190
PINK_B_MIN, PINK_B_MAX = 140, 220

# Pink padding color for partial pages (0–1 RGB)
PINK_PAD_R, PINK_PAD_G, PINK_PAD_B = 0.957, 0.565, 0.710  # ≈ #F490B5

ENABLE_COLOR_LABELS = False    # Master switch — set False to disable color labeling
                               # globally without needing "inga färger" in every subject

# ── Helpers ───────────────────────────────────────────────────────────────────

def is_fully_pink(src_doc, src_page_num, clip):
    """
    Render the clip region at low resolution and check if it's mostly the
    light pink background color (~R244 G144 B181). Returns True if >85% of
    pixels are pink-ish, meaning the page has no real content.
    """
    mat = fitz.Matrix(0.05, 0.05)  # tiny render — fast, just for color sampling
    pix = src_doc[src_page_num].get_pixmap(matrix=mat, clip=clip, colorspace=fitz.csRGB)
    samples = pix.samples
    total   = pix.width * pix.height
    if total == 0:
        return False
    pink_count = 0
    for i in range(0, len(samples), 3):
        r, g, b = samples[i], samples[i + 1], samples[i + 2]
        if (r > PINK_R_MIN
                and PINK_G_MIN < g < PINK_G_MAX
                and PINK_B_MIN < b < PINK_B_MAX
                and r > g and r > b):
            pink_count += 1
    return (pink_count / total) > PINK_THRESHOLD


def rotate_pdf_90(pdf_bytes, clockwise=True):
    """
    Rotate every page in the PDF 90° (clockwise by default).

    Used for banderoll mode: the source PDF is laid out landscape (wide and
    short) but the user describes the design in portrait dimensions (e.g.
    "RUTA 3x63" meaning 3m wide × 63m tall when hung). Rotating once here
    aligns the PDF's axes with the user's dimensions so the existing
    slicing logic works unchanged.
    """
    src   = fitz.open(stream=pdf_bytes, filetype="pdf")
    out   = fitz.open()
    angle = 90 if clockwise else 270

    for src_page in src:
        r = src_page.rect
        new_page = out.new_page(width=r.height, height=r.width)
        new_page.show_pdf_page(new_page.rect, src, src_page.number, rotate=angle)

    buf = io.BytesIO()
    out.save(buf)
    out.close()
    src.close()
    return buf.getvalue()


# ── Color mapping ─────────────────────────────────────────────────────────────

def load_color_map():
    """Load color_map.json. Creates an empty file if it doesn't exist yet."""
    if not os.path.exists(COLOR_MAP_FILE):
        with open(COLOR_MAP_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, indent=2)
        return {}
    with open(COLOR_MAP_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _fill_to_hex(fill):
    """Convert pymupdf fill tuple (r, g, b) in 0–1 range to '#RRGGBB'."""
    return "#{:02X}{:02X}{:02X}".format(
        int(fill[0] * 255),
        int(fill[1] * 255),
        int(fill[2] * 255),
    )


def _text_color_for(fill):
    """Return black or white depending on background brightness."""
    lum = 0.299 * fill[0] + 0.587 * fill[1] + 0.114 * fill[2]
    return (0, 0, 0) if lum > 0.45 else (1, 1, 1)


def _add_color_labels(page, color_map, strip_h_pts):
    """
    Render the page at low resolution, divide into a 4×2 grid, and place
    EXACTLY ONE label per mapped color per page.

    The grid is used to find the best position for each color (its dominant
    winning cell), but only one label is ever placed per color.

    Pass 1 — for each color, find the cell where it has the most pixels AND
             is the dominant color. Place one label at that best cell.
    Pass 2 — guarantee coverage: any color not labeled in pass 1 (never
             dominant) gets placed at its best cell, verified by pixel check.

    Page number exclusion zone: labels too close to the top-right page number
    are skipped.
    """
    TARGET_PX = 80
    GRID_COLS = 4
    GRID_ROWS = 2
    MIN_CELL  = 0.005   # ≥0.5% of total pixels to be a valid placement

    r     = page.rect
    scale = min(TARGET_PX / r.width, TARGET_PX / r.height)

    pix    = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB)
    pw, ph = pix.width, pix.height
    if not pw or not ph:
        return

    samples  = pix.samples
    total_px = pw * ph

    def _sample(xi, yi):
        xi  = max(0, min(pw - 1, xi))
        yi  = max(0, min(ph - 1, yi))
        idx = (yi * pw + xi) * 3
        return "#{:02X}{:02X}{:02X}".format(samples[idx], samples[idx+1], samples[idx+2])

    # Build cells: (col, row) → {hex_c: [sx, sy, n, r_f, g_f, b_f, code]}
    cells = {}
    for yi in range(ph):
        for xi in range(pw):
            idx        = (yi * pw + xi) * 3
            rv, gv, bv = samples[idx], samples[idx + 1], samples[idx + 2]
            hex_c      = "#{:02X}{:02X}{:02X}".format(rv, gv, bv)
            code       = color_map.get(hex_c)
            if not code or code == "Skip":
                continue
            col = min(GRID_COLS - 1, int(xi * GRID_COLS / pw))
            row = min(GRID_ROWS - 1, int(yi * GRID_ROWS / ph))
            key = (col, row)
            if key not in cells:
                cells[key] = {}
            if hex_c not in cells[key]:
                cells[key][hex_c] = [0, 0, 0, rv / 255, gv / 255, bv / 255, code]
            cells[key][hex_c][0] += xi
            cells[key][hex_c][1] += yi
            cells[key][hex_c][2] += 1

    min_cell_px = max(1, total_px * MIN_CELL)
    fs          = max(4, strip_h_pts * 0.04)

    # Page number exclusion zone — small radius around top-right corner
    pn_x, pn_y = r.width - 12, 10
    excl_r_sq  = max(fs, 10) ** 2

    def _ok(cx, cy):
        return ((cx - pn_x) ** 2 + (cy - pn_y) ** 2) > excl_r_sq

    def _place(data, verify_hex=None):
        sx, sy, n, r_f, g_f, b_f, code = data
        cx_px = round(sx / n)
        cy_px = round(sy / n)
        if verify_hex and _sample(cx_px, cy_px) != verify_hex:
            return False
        cx, cy = cx_px / scale, cy_px / scale
        if not _ok(cx, cy):
            return False
        page.insert_text(
            fitz.Point(cx, cy), code,
            fontsize = fs,
            color    = _text_color_for((r_f, g_f, b_f)),
        )
        return True

    # ── Pass 1: one label per color at its best winning cell ─────────────────
    # For each color find the cell where it wins AND has the most pixels
    best_wins = {}   # hex_c → best data from a cell where it's dominant
    for cell_colors in cells.values():
        winner_hex = max(cell_colors, key=lambda h: cell_colors[h][2])
        data       = cell_colors[winner_hex]
        if data[2] < min_cell_px:
            continue
        if winner_hex not in best_wins or data[2] > best_wins[winner_hex][2]:
            best_wins[winner_hex] = data

    labeled = set()
    for hex_c, data in best_wins.items():
        if _place(data):
            labeled.add(hex_c)

    # ── Pass 2: guarantee every visible color has exactly one label ───────────
    # Collect best cell per color across ALL cells (not just winning ones)
    all_best = {}
    for cell_colors in cells.values():
        for hex_c, data in cell_colors.items():
            if hex_c not in all_best or data[2] > all_best[hex_c][2]:
                all_best[hex_c] = data

    for hex_c, data in all_best.items():
        if hex_c in labeled:
            continue
        if data[2] < 1:
            continue
        if _place(data, verify_hex=hex_c):
            labeled.add(hex_c)


def extract_pdf_colors(pdf_bytes):
    """Return set of unique hex fill colors found in PDF vector drawings."""
    src_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    colors  = set()
    for page in src_doc:
        for d in page.get_drawings():
            fill = d.get("fill")
            if fill and len(fill) >= 3:
                colors.add(_fill_to_hex(fill))
    src_doc.close()
    return colors


# ── Core slicing ──────────────────────────────────────────────────────────────

def slice_one_strip(args):
    """
    Slice a single strip from the source PDF. Runs in a thread.
    Pages rotated 270° to landscape with correct left→right edge continuity.
    Fully pink pages are skipped but page numbers are preserved.
    Color labels are added PER STRIP PAGE after rendering (never cross slice boundaries).
    Returns (strip_number, pdf_bytes).
    """
    s, pdf_bytes, width_m, height_m, num_strips, num_pages, color_map = args

    src_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    out_doc = fitz.open()

    for src_page in src_doc:
        r       = src_page.rect
        full_w  = r.width
        full_h  = r.height

        pts_per_m_x = full_w / width_m
        pts_per_m_y = full_h / height_m
        strip_w_pts = STRIP_WIDTH_M * pts_per_m_x
        page_h_pts  = PAGE_HEIGHT_M * pts_per_m_y

        x0 = s * strip_w_pts
        x1 = min((s + 1) * strip_w_pts, full_w)

        # ── PROTECTED ROTATION BLOCK — DO NOT CHANGE WITHOUT EXPLICIT CONSENT ─
        for page_num in range(num_pages):
            y1 = full_h - page_num * page_h_pts
            y0 = max(0.0, full_h - (page_num + 1) * page_h_pts)

            clip = fitz.Rect(x0, y0, x1, y1)

            if is_fully_pink(src_doc, src_page.number, clip):
                continue

            content_w  = y1 - y0
            is_partial = content_w < page_h_pts - 1
            new_page   = out_doc.new_page(width=page_h_pts, height=x1 - x0)
            dest_rect  = fitz.Rect(0, 0, content_w, x1 - x0)
            new_page.show_pdf_page(dest_rect, src_doc, src_page.number, clip=clip, rotate=270)
        # ── END PROTECTED BLOCK ───────────────────────────────────────────────

            # Color labels — per page, after rendering, using pixel centroid detection
            if color_map:
                _add_color_labels(new_page, color_map, strip_w_pts)

            # Pink padding + dotted cut line on partial pages
            if is_partial:
                shape = new_page.new_shape()
                shape.draw_rect(fitz.Rect(content_w, 0, page_h_pts, x1 - x0))
                shape.finish(fill=(PINK_PAD_R, PINK_PAD_G, PINK_PAD_B), fill_opacity=1.0, color=None)
                shape.commit()

                shape = new_page.new_shape()
                shape.draw_line(fitz.Point(content_w, 0), fitz.Point(content_w, x1 - x0))
                shape.finish(color=(0.15, 0.15, 0.15), width=1.0, dashes="[4 4] 0")
                shape.commit()

                new_page.insert_text(
                    fitz.Point(content_w + 3, 10),
                    "Klipp",
                    fontsize = 6,
                    color    = (0.15, 0.15, 0.15),
                )

            # Small orange page number, tight to top right corner
            new_page.insert_text(
                fitz.Point(new_page.rect.width - 12, 10),
                str(page_num + 1),
                fontsize = 6,
                color    = (1, 0.5, 0),
            )

    buf = io.BytesIO()
    out_doc.save(buf)
    out_doc.close()
    src_doc.close()
    return (s + 1, buf.getvalue())


def _slice_pdf(pdf_bytes, width_m, height_m, color_map=None):
    """Slice pdf_bytes into vertical strips in parallel. Returns sorted list of (strip_num, bytes)."""
    num_strips = math.ceil(width_m  / STRIP_WIDTH_M)
    num_pages  = math.ceil(height_m / PAGE_HEIGHT_M)

    args = [
        (s, pdf_bytes, width_m, height_m, num_strips, num_pages, color_map or {})
        for s in range(num_strips)
    ]

    results = {}
    with ThreadPoolExecutor(max_workers=SLICE_WORKERS) as pool:
        futures = {pool.submit(slice_one_strip, a): a[0] for a in args}
        for future in as_completed(futures):
            strip_num, strip_bytes = future.result()
            results[strip_num] = strip_bytes
            print(f"    Strip {strip_num}/{num_strips} sliced.")

    return [(n, results[n]) for n in sorted(results)]


def generate_grid_pdf(pdf_bytes, width_m, height_m):
    """
    Generate a rotated grid overview (rotate=270). Bottom→top numbering.
    Rad 1 on LEFT (bottom of design), last Rad on RIGHT (top/small).
    """
    src_doc    = fitz.open(stream=pdf_bytes, filetype="pdf")
    out_doc    = fitz.open()
    num_strips = math.ceil(width_m  / STRIP_WIDTH_M)
    num_pages  = math.ceil(height_m / PAGE_HEIGHT_M)

    for src_page in src_doc:
        r       = src_page.rect
        full_w  = r.width
        full_h  = r.height
        strip_w_pts = STRIP_WIDTH_M * (full_w / width_m)
        page_h_pts  = PAGE_HEIGHT_M * (full_h / height_m)

        new_page = out_doc.new_page(width=full_h, height=full_w)
        new_page.show_pdf_page(new_page.rect, src_doc, src_page.number, rotate=270)

        shape = new_page.new_shape()
        for k in range(1, num_pages):
            nx = k * page_h_pts
            shape.draw_line(fitz.Point(nx, 0), fitz.Point(nx, full_w))
        shape.finish(color=(0.9, 0.1, 0.1), width=1.5, stroke_opacity=0.6)
        shape.commit()

        shape = new_page.new_shape()
        for s in range(1, num_strips):
            ny = s * strip_w_pts
            shape.draw_line(fitz.Point(0, ny), fitz.Point(full_h, ny))
        shape.finish(color=(0.9, 0.1, 0.1), width=1.5, stroke_opacity=0.6)
        shape.commit()

        for s in range(num_strips):
            ny0 = s * strip_w_pts
            ny1 = min((s + 1) * strip_w_pts, full_w)
            cell_w = ny1 - ny0
            top_fs = max(6, min(14, cell_w / 8))

            # Guard against degenerate rect on very narrow partial strips
            label_rect = fitz.Rect(2, ny0 + 2, 2 + top_fs + 4, ny1 - 2)
            if label_rect.is_valid and label_rect.width > 2 and label_rect.height > 2:
                new_page.insert_textbox(
                    label_rect,
                    f"Rad {s + 1}", fontsize=top_fs, color=(0.9, 0.1, 0.1), align=1)

            for page_num in range(num_pages):
                nx0 = page_num * page_h_pts
                nx1 = min((page_num + 1) * page_h_pts, full_h)
                cell_h = nx1 - nx0
                fs = max(6, min(24, min(cell_w, cell_h) / 10))

                cell_rect = fitz.Rect(nx0 + 4, ny0 + 4, nx1 - 4, ny1 - 4)
                if cell_rect.is_valid and cell_rect.width > 2 and cell_rect.height > 2:
                    rc = new_page.insert_textbox(
                        cell_rect,
                        f"Rad {s + 1} / Ruta {page_num + 1}",
                        fontsize=fs, color=(0.9, 0.1, 0.1), align=1)
                    if rc < 0:
                        new_page.insert_text(
                            fitz.Point(nx0 + 2, ny0 + fs + 2),
                            f"R{s + 1}/R{page_num + 1}",
                            fontsize=max(5, fs * 0.7), color=(0.9, 0.1, 0.1))

    buf = io.BytesIO()
    out_doc.save(buf)
    out_doc.close()
    src_doc.close()
    return buf.getvalue()


# ── Public API ────────────────────────────────────────────────────────────────

def run_slice(
    pdf_bytes: bytes,
    width_m: float,
    height_m: float,
    banderoll: bool = False,
    skip_colors: bool = False,
) -> dict:
    """
    Slices a PDF into 1.5m-wide vertical strips.
    Returns:
    {
        "strips": [
            {"filename": "strip-01.pdf", "bytes": b"..."},
            ...
        ],
        "unknown_colors": ["#RRGGBB", ...]  # empty if skip_colors=True or no unknowns
    }
    """
    if banderoll:
        pdf_bytes = rotate_pdf_90(pdf_bytes)

    # Mirror ruta.py logic: ENABLE_COLOR_LABELS=False means always skip
    effective_skip = skip_colors or (not ENABLE_COLOR_LABELS)
    unknown_colors = []

    if not effective_skip:
        color_map  = load_color_map()
        hex_colors = extract_pdf_colors(pdf_bytes)
        unknown_colors = sorted(c for c in hex_colors if c not in color_map)
        if unknown_colors:
            # Unknown colors found — slice without labels so strips are still returned
            color_map = {}
    else:
        color_map = {}

    strips_raw = _slice_pdf(pdf_bytes, width_m, height_m, color_map)

    strips = [
        {"filename": f"strip-{strip_num:02d}.pdf", "bytes": strip_bytes}
        for strip_num, strip_bytes in strips_raw
    ]

    grid_bytes = generate_grid_pdf(pdf_bytes, width_m, height_m)

    return {"strips": strips, "unknown_colors": unknown_colors, "grid_pdf": grid_bytes}
