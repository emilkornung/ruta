"""
slicer.py — Core PDF slicing logic for Hammaby Tifo ruta pipeline.

Public interface: run_slice()
All Gmail, Drive, OAuth, and email-parsing code lives in ruta.py (the
standalone backup). This module contains only the PDF geometry logic.
"""

import io
import math
from concurrent.futures import ThreadPoolExecutor, as_completed

import fitz  # pymupdf
import numpy as np
from scipy import ndimage

# ── Config ────────────────────────────────────────────────────────────────────

STRIP_WIDTH_M = 1.5
PAGE_HEIGHT_M = 4.0
SLICE_WORKERS = 6

VERSION = "1.2.0"

# Pink page detection
PINK_THRESHOLD         = 0.85
PINK_R_MIN             = 180
PINK_G_MIN, PINK_G_MAX = 100, 190
PINK_B_MIN, PINK_B_MAX = 140, 220

# Orange background detection (used for split "nedre" designs)
ORANGE_THRESHOLD           = 0.85
ORANGE_R_MIN               = 220
ORANGE_G_MIN, ORANGE_G_MAX = 90, 165
ORANGE_B_MAX               = 60

# Pink padding color for partial pages (0–1 RGB)
PINK_PAD_R, PINK_PAD_G, PINK_PAD_B = 0.957, 0.565, 0.710  # ≈ #F490B5

# ── Color labeling (map-driven, pixel-based) ───────────────────────────────────
# Rebuilt pipeline. Operates on each already-rendered, already-sliced strip page
# (post-rotation). All four knobs below are tunable.
ENABLE_COLOR_LABELS   = False  # Master switch — MUST stay False until color labeling
                               # is fully validated and explicitly approved (TIF-27).
                               # Validation harnesses call _label_colors_on_page
                               # directly and are unaffected by this switch.
COLOR_MATCH_TOLERANCE = 28     # RGB Euclidean distance for matching a mapped color
                               # (handles render drift, same idea as pink/orange bands)
MIN_PATCH_PX          = 10     # discard connected components smaller than this many
                               # pixels at LABEL_RENDER_SCALE. Strip-15 page 2 gave a
                               # clean dust/real gap of 8..22 (edge dust <=8px, thin real
                               # blade segments >=22px). ENAD strip 4 page 1 then showed a
                               # clearly visible ~19x7cm sliver whose thin ends anti-alias
                               # away, leaving only 10 in-tolerance px — so the threshold
                               # sits at 10: still above the 8px dust ceiling, low enough
                               # to label every patch a painter can actually see.
LABEL_RENDER_SCALE    = 2.0    # pixmap render scale used for color analysis

# Sizing philosophy (TIF-27 round 3): operators read labels from inches away,
# so there is no legibility floor — font size is purely a computational tool to
# make every label fit. Every patch >= MIN_PATCH_PX gets exactly one label at a
# modest consistent default, shrunk (never grown) until it fits fully inside
# its own patch without touching any other label.
LABEL_FONT_DEFAULT    = 3.0    # pt — modest default; labels never exceed this
LABEL_FONT_SHRINK     = 0.75   # per-step shrink ratio when a size doesn't fit
LABEL_FONT_TECH_MIN   = 0.1    # pt — technical safety floor only (guards against
                               # a literal 0/negative size). NOT a legibility
                               # judgment; hitting it is reported ("forced") and
                               # means a patch held text in near-zero space.

# Page-number exclusion zone (TIF-27 round 5, restores the legacy ruta.py
# labeler's dead zone in rect form). The orange page number is inserted AFTER
# labeling (see slice_one_strip: baseline (width-12, 10), fontsize 6, 1-2
# digits), so the labeler can't see it in the pixmap — its known footprint is
# reserved instead by seeding placed_rects. Fitted labels shrink/shift around
# it like around any other label; forced placements may still enter it (zero
# skips outranks the dead zone, the operator handles that corner manually).
PAGE_NUM_EXCL_W  = 13.5  # pt reserved leftward from the right page edge
PAGE_NUM_EXCL_Y0 = 4.0   # pt from the top edge (glyph top ≈ 5.7 minus pad)
PAGE_NUM_EXCL_Y1 = 11.5  # pt from the top edge (baseline 10 plus pad)

# ── Helpers ───────────────────────────────────────────────────────────────────

def is_fully_background(src_doc, src_page_num, clip):
    """
    Render the clip region at low resolution and check if it's mostly
    the light pink background (~R244 G144 B181) OR the orange background
    (~R255 G128 B0). Returns True if >95% of pixels match either color,
    meaning the page has no real content worth printing.
    """
    mat = fitz.Matrix(0.05, 0.05)  # tiny render — fast, just for color sampling
    pix = src_doc[src_page_num].get_pixmap(matrix=mat, clip=clip, colorspace=fitz.csRGB)
    samples = pix.samples
    total   = pix.width * pix.height
    if total == 0:
        return False
    background_count = 0
    for i in range(0, len(samples), 3):
        r, g, b = samples[i], samples[i + 1], samples[i + 2]
        is_pink = (r > PINK_R_MIN
                   and PINK_G_MIN < g < PINK_G_MAX
                   and PINK_B_MIN < b < PINK_B_MAX
                   and r > g and r > b)
        is_orange = (r > ORANGE_R_MIN
                     and ORANGE_G_MIN < g < ORANGE_G_MAX
                     and b < ORANGE_B_MAX
                     and r > g and g > b)
        if is_pink or is_orange:
            background_count += 1
    return (background_count / total) > ORANGE_THRESHOLD


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
# The colour map is no longer loaded from a local color_map.json. It is sourced
# from Supabase (the colour_map table) by the web app and passed into run_slice()
# via the `colour_map` parameter as a {hex: ncs_code} dict — the exact shape the
# active _add_color_labels() labeling path consumes (see run_slice / api.py).

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


def _hex_to_rgb255(hex_c):
    """Parse '#RRGGBB' → np.int32 array [r, g, b] in 0–255. None if malformed."""
    h = hex_c.lstrip("#")
    if len(h) != 6:
        return None
    try:
        return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.int32)
    except ValueError:
        return None


def _code_masks(arr, color_map):
    """
    Assign every pixel to its NEAREST mapped color (within tolerance), then
    union the masks of hexes sharing a paint code.

    Nearest-only assignment is the round-4 fix for fragmented double labels:
    with independent per-hex tolerance bands, two mapped colors closer than
    2*COLOR_MATCH_TOLERANCE both claimed the same physical fill's pixels, so
    one shape formed a component in TWO masks and received two different
    code labels. Per-code unions additionally merge hexes that map to the
    same code (e.g. #FFFFFF and an off-white both -> "Vit") into a single
    connected component instead of adjacent duplicate labels.

    "Skip" entries participate in the assignment (they own their pixels, so
    near-black outline pixels can't leak into a neighbouring code's mask)
    but are excluded from the returned masks.

    Returns {code: bool mask}. Shared with the validation harnesses so the
    expected-patch recount uses identical semantics.
    """
    reps = []
    for hex_c, code in color_map.items():
        if not code:
            continue
        rgb = _hex_to_rgb255(hex_c)
        if rgb is not None:
            reps.append((code, rgb))
    if not reps:
        return {}

    dist = np.stack([((arr - rgb) ** 2).sum(axis=2) for _, rgb in reps])
    nearest = np.argmin(dist, axis=0)
    within  = np.min(dist, axis=0) <= COLOR_MATCH_TOLERANCE ** 2

    masks = {}
    for k, (code, _) in enumerate(reps):
        if code == "Skip":
            continue
        m = (nearest == k) & within
        if code in masks:
            masks[code] |= m
        else:
            masks[code] = m
    return {c: m for c, m in masks.items() if m.any()}


def _label_colors_on_page(page, color_map):
    """
    Map-driven, pixel-based color labeling for a single rendered strip page.

    For every color in color_map (skipping "Skip"):
      1. Render the page to a pixmap at LABEL_RENDER_SCALE.
      2. Build a boolean mask of pixels within COLOR_MATCH_TOLERANCE (RGB
         Euclidean distance) of the mapped color — a tolerance band that absorbs
         render drift, same principle as the pink/orange background bands.
      3. Find separate visible patches with scipy.ndimage.label.
      4. Discard any patch smaller than MIN_PATCH_PX (filters anti-alias noise).
      5. Every surviving patch receives EXACTLY ONE label — no skips.

    Placement/sizing per patch (fit-verified, collision-aware, no legibility
    floor — operators read labels from inches away):
      - Sizes start at LABEL_FONT_DEFAULT (a modest, consistently small size —
        labels are never sized up to fill available space) and shrink by
        LABEL_FONT_SHRINK steps until a placement exists.
      - A placement means: the label's FULL rendered bbox lies inside the
        patch's own mask (minimum_filter erosion) AND overlaps no label already
        placed on the page (any color). The position is the deepest interior
        point among valid centers. Patches place largest-first so sliver
        clusters yield to big neighbours.
      - The shrink ladder bottoms out at LABEL_FONT_TECH_MIN (0.1 pt), where
        the text is smaller than one analysis pixel and therefore always fits
        inside the patch. If even that size has no collision-free position,
        the label is placed anyway at the deepest interior point ("forced" —
        still inside its own patch, may touch another label) and counted, so
        a zero-space cluster is visible in the summary rather than silent.

    Returns {code: {"count", "font_sizes", "rects", "placement", "forced"}}
    for inspection/validation ("rects" are glyph bboxes in page points,
    aligned with "font_sizes"; "placement" entries are "fit"/"forced").
    Keyed by paint CODE (not hex) since round 4. The slicing path ignores
    the return value.
    """
    summary = {}

    pix = page.get_pixmap(
        matrix=fitz.Matrix(LABEL_RENDER_SCALE, LABEL_RENDER_SCALE),
        colorspace=fitz.csRGB,
    )
    pw, ph = pix.width, pix.height
    if not pw or not ph or pix.n < 3:
        return summary

    # int32 avoids overflow when squaring channel differences (255² > int16 max).
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(ph, pw, pix.n)
    arr = arr[:, :, :3].astype(np.int32)

    S = LABEL_RENDER_SCALE

    # ── Pass 1: collect every labelable patch, one mask per paint code ─────
    # Nearest-color assignment + per-code unions (see _code_masks) so one
    # physical patch can only ever be one component in one mask.
    code_text_color = {}
    for hex_c, code in color_map.items():
        rgb = _hex_to_rgb255(hex_c)
        if code and code != "Skip" and rgb is not None and code not in code_text_color:
            code_text_color[code] = _text_color_for(
                (rgb[0] / 255, rgb[1] / 255, rgb[2] / 255))

    patches = []
    for code, mask in _code_masks(arr, color_map).items():
        lbl, num = ndimage.label(mask)
        if num == 0:
            continue
        counts  = np.bincount(lbl.ravel())
        objects = ndimage.find_objects(lbl)

        for comp in range(1, num + 1):
            if counts[comp] < MIN_PATCH_PX:
                continue
            sl       = objects[comp - 1]
            sub_mask = lbl[sl] == comp
            # Pad by 1px so the pixmap/page edge counts as a patch boundary:
            # without it, EDT overestimates the inscribed radius of patches
            # clipped by the page edge (and of patches exactly filling their
            # bbox, which have no background pixels in the sub-mask at all).
            dt = ndimage.distance_transform_edt(np.pad(sub_mask, 1))[1:-1, 1:-1]
            patches.append((int(counts[comp]), code, sl, sub_mask, dt,
                            code_text_color[code]))

    # Largest patches place first: in dense clusters the slivers yield to
    # (shrink/shift/skip around) their big neighbours, not the reverse.
    patches.sort(key=lambda p: -p[0])

    # Seeded with the page number's reserved rect so fitted labels avoid the
    # spot where slice_one_strip will draw it after labeling.
    placed_rects = [fitz.Rect(page.rect.width - PAGE_NUM_EXCL_W,
                              PAGE_NUM_EXCL_Y0,
                              page.rect.width, PAGE_NUM_EXCL_Y1)]
    MARGIN       = 0.5  # pt clearance required between labels

    for npx, code, sl, sub_mask, dt, text_color in patches:
        entry = summary.setdefault(
            code, {"count": 0, "font_sizes": [], "rects": [], "placement": [],
                   "forced": 0})

        r_pts = float(dt.max()) / S
        wpp   = fitz.get_text_length(code, fontname="helv", fontsize=1.0) or 1.0
        # Start at the modest default, pre-capped by a cheap geometric estimate
        # of what the inscribed circle can hold (the fit check verifies anyway;
        # this just skips pointless filter passes on small patches).
        fs0 = min(LABEL_FONT_DEFAULT, 2 * r_pts / wpp, 2 * r_pts / 0.72)
        fs0 = max(fs0, LABEL_FONT_TECH_MIN)
        sizes = [round(fs0, 3)]
        while sizes[-1] * LABEL_FONT_SHRINK > LABEL_FONT_TECH_MIN:
            sizes.append(round(sizes[-1] * LABEL_FONT_SHRINK, 3))
        if sizes[-1] > LABEL_FONT_TECH_MIN:
            sizes.append(LABEL_FONT_TECH_MIN)

        def _fit_map(fs, exclude_collisions):
            tw = wpp * fs
            hh = 0.72 * fs   # glyph height: digits/short words, no descenders
            # Exact ceiling, no extra margin: a +1 pad would force the window
            # to >=2px at ANY size, slamming 1px-wide slivers straight to the
            # technical floor even though e.g. 0.3pt text (~0.4px tall) fits.
            w_px = max(1, int(np.ceil(tw * S)))
            h_px = max(1, int(np.ceil(hh * S)))
            if w_px > sub_mask.shape[1] or h_px > sub_mask.shape[0]:
                return None
            # Centers where the label's full bbox lies inside the patch mask.
            # mode="constant" makes anything outside the bbox count as non-fit,
            # which also keeps fitted labels fully on the page.
            fit = ndimage.minimum_filter(
                sub_mask.astype(np.uint8), size=(h_px, w_px),
                mode="constant", cval=0).astype(bool)
            if exclude_collisions and fit.any():
                # Exclude centers whose bbox would overlap an existing label.
                for R in placed_rects:
                    ex0 = int(np.floor((R.x0 - tw / 2) * S)) - sl[1].start
                    ex1 = int(np.ceil((R.x1 + tw / 2) * S)) - sl[1].start
                    ey0 = int(np.floor((R.y0 - hh / 2) * S)) - sl[0].start
                    ey1 = int(np.ceil((R.y1 + hh / 2) * S)) - sl[0].start
                    if ex1 <= 0 or ey1 <= 0 or ex0 >= fit.shape[1] or ey0 >= fit.shape[0]:
                        continue
                    fit[max(0, ey0):ey1, max(0, ex0):ex1] = False
            return fit if fit.any() else None

        chosen = None
        for fs in sizes:
            fit = _fit_map(fs, exclude_collisions=True)
            if fit is None:
                continue
            # Best-fitting position: deepest interior point among valid centers.
            yloc, xloc = np.unravel_index(int(np.argmax(np.where(fit, dt, -1.0))),
                                          fit.shape)
            chosen = (fs, yloc, xloc, False)
            break

        if chosen is None:
            # Even sub-pixel text has no collision-free pixel: every pixel of
            # this patch is already covered by other labels' rects. Zero skips
            # is the contract, so place anyway at the deepest interior point —
            # still inside the patch, may touch another label — and count it.
            fit = _fit_map(LABEL_FONT_TECH_MIN, exclude_collisions=False)
            yloc, xloc = np.unravel_index(int(np.argmax(np.where(fit, dt, -1.0))),
                                          fit.shape)
            chosen = (LABEL_FONT_TECH_MIN, yloc, xloc, True)

        fs, yloc, xloc, forced = chosen
        tw = wpp * fs
        hh = 0.72 * fs
        px = (sl[1].start + xloc + 0.5) / S
        py = (sl[0].start + yloc + 0.5) / S
        # Fitted bboxes are inside the page by construction; the clamp only
        # bites for sub-pixel/forced placements right at a page edge.
        ox = min(max(px - tw / 2, 0.0), page.rect.width - tw)
        oy = min(max(py + fs * 0.35, hh), page.rect.height)
        rect = fitz.Rect(ox - MARGIN, oy - hh - MARGIN, ox + tw + MARGIN, oy + MARGIN)

        page.insert_text(fitz.Point(ox, oy), code, fontsize=fs, color=text_color)
        placed_rects.append(rect)
        entry["count"]      += 1
        entry["font_sizes"].append(round(fs, 2))
        entry["rects"].append((round(ox, 2), round(oy - hh, 2),
                               round(ox + tw, 2), round(oy, 2)))
        entry["placement"].append("forced" if forced else "fit")
        entry["forced"]     += 1 if forced else 0

    return {h: e for h, e in summary.items() if e["count"]}


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
    s, pdf_bytes, width_m, height_m, num_strips, num_pages, color_map, ruta_nedre = args

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
        # Default (ruta_nedre=False): bottom-to-top — page 1 = bottom of design,
        # partial/pink leftover at the top. rotate=270. This path is PROTECTED and
        # MUST stay pixel-identical.
        # ruta_nedre=True: top-to-bottom — page 1 = top of design, partial/pink
        # leftover lands at the design's bottom (the end of the sewing sequence).
        # The ONLY differences from the default path are (1) the bands are taken
        # top-to-bottom and (2) rotate=90 instead of 270. rotate=90 reverses the
        # within-page design-y direction (so consecutive pages flow continuously
        # under top-to-bottom page numbering — verified R0,R1,…) while keeping the
        # artwork faithful (a pure rotation, NOT a mirror — no backwards text).
        # Everything else — left-aligned content, pink/cut on the right free edge,
        # page numbering — is shared with the default path so the Rad (strip)
        # left-to-right order and grid stay identical to default (see note below).
        for page_num in range(num_pages):
            if ruta_nedre:
                y0 = page_num * page_h_pts
                y1 = min(full_h, (page_num + 1) * page_h_pts)
                rotate = 90
            else:
                y1 = full_h - page_num * page_h_pts
                y0 = max(0.0, full_h - (page_num + 1) * page_h_pts)
                rotate = 270

            clip = fitz.Rect(x0, y0, x1, y1)

            if is_fully_background(src_doc, src_page.number, clip):
                continue

            content_w  = y1 - y0
            is_partial = content_w < page_h_pts - 1
            new_page   = out_doc.new_page(width=page_h_pts, height=x1 - x0)
            # Identical placement in both modes: content left-aligned, pink fills
            # the right free edge. For ruta_nedre the rotate=90 above puts that
            # free edge on the design's BOTTOM (the leftover-at-end edge); for the
            # default rotate=270 it is the design's TOP. content_w is the true
            # captured source height, used for both clip extent and dest width.
            pad_x     = content_w
            dest_rect = fitz.Rect(0, 0, content_w, x1 - x0)
            new_page.show_pdf_page(dest_rect, src_doc, src_page.number, clip=clip, rotate=rotate)
        # ── END PROTECTED BLOCK ───────────────────────────────────────────────

            # Color labels — per page, after rendering, using map-driven pixel
            # tolerance-band detection + deepest-interior-point placement.
            if color_map:
                _label_colors_on_page(new_page, color_map)

            # Pink padding + dotted cut line on partial pages.
            # pad_x (= content_w) is the content↔pink boundary (= cut line). In
            # BOTH modes the content is left-aligned and the pink dead-space fills
            # the right — the design's free outer edge. rotate makes that edge the
            # design TOP (default) or BOTTOM (ruta_nedre); the placement code is
            # the same.
            if is_partial:
                pink_rect = fitz.Rect(pad_x, 0, page_h_pts, x1 - x0)
                klipp_pt  = fitz.Point(pad_x + 3, 10)

                shape = new_page.new_shape()
                shape.draw_rect(pink_rect)
                shape.finish(fill=(PINK_PAD_R, PINK_PAD_G, PINK_PAD_B), fill_opacity=1.0, color=None)
                shape.commit()

                shape = new_page.new_shape()
                shape.draw_line(fitz.Point(pad_x, 0), fitz.Point(pad_x, x1 - x0))
                shape.finish(color=(0.15, 0.15, 0.15), width=1.0, dashes="[4 4] 0")
                shape.commit()

                new_page.insert_text(
                    klipp_pt,
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


def _slice_pdf(pdf_bytes, width_m, height_m, color_map=None, ruta_nedre=False):
    """Slice pdf_bytes into vertical strips in parallel. Returns sorted list of (strip_num, bytes)."""
    num_strips = math.ceil(width_m  / STRIP_WIDTH_M)
    num_pages  = math.ceil(height_m / PAGE_HEIGHT_M)

    args = [
        (s, pdf_bytes, width_m, height_m, num_strips, num_pages, color_map or {}, ruta_nedre)
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


def generate_grid_pdf(pdf_bytes, width_m, height_m, ruta_nedre=False):
    """
    Generate a rotated grid overview that matches the sliced strips.

    Default (ruta_nedre=False): rotate=270, bottom→top page numbering. UNCHANGED
    and byte-identical to before.

    ruta_nedre=True: rotate=90 to match the strips (top→bottom page flow). This
    keeps the design-y→grid-x direction increasing so "Ruta 1" still labels the
    design's TOP band at grid-left (same label positions as default). rotate=90
    reverses the design-x→grid-y direction, so each strip's content lands on the
    vertically-flipped band; the Rad bands are flipped to match so every "Rad s"
    label sits on the strip it names. The Rad NUMBERING is unchanged — Rad 1 is
    still strip 0 = the leftmost design column — so the Rad left-to-right order is
    identical to default.
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

        # Grid-y band [ny0, ny1] occupied by strip s. rotate=90 flips design-x→y
        # so for ruta_nedre the band is mirrored about full_w (keeps the label on
        # the strip's actual content); Rad numbering itself is unchanged.
        def strip_band(s):
            x0s = s * strip_w_pts
            x1s = min((s + 1) * strip_w_pts, full_w)
            if ruta_nedre:
                return (full_w - x1s, full_w - x0s)
            return (x0s, x1s)

        new_page = out_doc.new_page(width=full_h, height=full_w)
        new_page.show_pdf_page(new_page.rect, src_doc, src_page.number,
                               rotate=90 if ruta_nedre else 270)

        shape = new_page.new_shape()
        for k in range(1, num_pages):
            nx = k * page_h_pts
            shape.draw_line(fitz.Point(nx, 0), fitz.Point(nx, full_w))
        shape.finish(color=(0.9, 0.1, 0.1), width=1.5, stroke_opacity=0.6)
        shape.commit()

        shape = new_page.new_shape()
        for s in range(1, num_strips):
            ny = (full_w - s * strip_w_pts) if ruta_nedre else (s * strip_w_pts)
            shape.draw_line(fitz.Point(0, ny), fitz.Point(full_h, ny))
        shape.finish(color=(0.9, 0.1, 0.1), width=1.5, stroke_opacity=0.6)
        shape.commit()

        for s in range(num_strips):
            ny0, ny1 = strip_band(s)
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
    ruta_nedre: bool = False,
    colour_map: dict = None,
) -> dict:
    """
    Slices a PDF into 1.5m-wide vertical strips.

    colour_map is a {hex: ncs_code} dict sourced from Supabase and passed in by
    the caller (api.py). It replaces the retired local color_map.json. When
    labeling is disabled (ENABLE_COLOR_LABELS=False) it is ignored, exactly as
    the file-loaded map was.

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
        color_map  = colour_map or {}
        hex_colors = extract_pdf_colors(pdf_bytes)
        unknown_colors = sorted(c for c in hex_colors if c not in color_map)
        if unknown_colors:
            # Unknown colors found — slice without labels so strips are still returned
            color_map = {}
    else:
        color_map = {}

    strips_raw = _slice_pdf(pdf_bytes, width_m, height_m, color_map, ruta_nedre=ruta_nedre)

    strips = [
        {"filename": f"strip-{strip_num:02d}.pdf", "bytes": strip_bytes}
        for strip_num, strip_bytes in strips_raw
    ]

    grid_bytes = generate_grid_pdf(pdf_bytes, width_m, height_m, ruta_nedre=ruta_nedre)

    return {"strips": strips, "unknown_colors": unknown_colors, "grid_pdf": grid_bytes}
