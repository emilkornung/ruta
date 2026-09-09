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

# ── Skissyta (the physical size of one printed ruta) ──────────────────────────
#
# STRIP_WIDTH_M is LOCKED (TIF-87). It is deliberately NOT configurable: it is
# set by the fabric, not by the job. Every output page's HEIGHT is
# STRIP_WIDTH_M * pts_per_m and therefore constant at 42.52 pt for every job.
#
# PAGE_HEIGHT_M is the DEFAULT for the per-job `page_height_m` parameter that is
# threaded through run_slice -> _slice_pdf -> slice_one_strip -> generate_grid_pdf.
# It is ALSO the value every historical job was produced at, so it must stay 4.0:
# passing it (or leaving it defaulted) must reproduce master's output exactly.
STRIP_WIDTH_M = 1.5
PAGE_HEIGHT_M = 4.0
SLICE_WORKERS = 6

VERSION = "1.5.0"

# Page scale for raster (JPG/PNG) sources — see image_to_pdf().
#
# Nothing in this module ever declares a pts-per-metre; slice_one_strip and
# generate_grid_pdf both DERIVE it as `full_w / width_m` from whatever page the
# source PDF happens to carry. Every real design we ship is drawn at 1:100 — one
# centimetre of page per metre of fabric — so that derivation lands on 72/2.54 =
# 28.3465 pts/m every time (measured: pest övre 28.3467/28.3463, pest mitten
# 28.3463/28.3492, ENAD_rutor 28.3465/28.3465).
#
# A raster upload carries no page size of its own, so the preprocessing step has
# to pick one, and it MUST pick this one. Every Klipp and label threshold in this
# module — KLIPP_MIN_PINK_PT, KLIPP_LINE_MARGIN_PT, MIN_LABEL_PATCH_SIZE_PT,
# LABEL_FONT_DEFAULT, PAGE_NUM_EXCL_* — is an absolute PDF-point value calibrated
# against that scale. Choose any other page size and all of them silently come to
# mean a different physical distance on the fabric.
#
# 72 pts/inch / 2.54 cm/inch is pts per CENTIMETRE, and one centimetre of page IS
# one metre of fabric — so the 1:100 reduction is already carried by that ratio
# and must NOT be applied a second time.
PTS_PER_M = 72.0 / 2.54           # 28.3465 pts/m (1 m fabric = 1 cm page)

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

# Render scale for is_fully_background's colour sample (TIF-68). Was 0.05, which
# rendered a full page to ~20-28 px and a partial to as few as ~15 — far too coarse
# to be faithful: solid fills averaged out of the PINK_*/ORANGE_* boxes and a 1-2 px
# anti-aliased edge was a large fraction of the sample, so genuinely ~100%-background
# pages read as 40-83% background and were NOT excluded (they rendered as blank pink/
# orange pages carrying only a page number). At 0.5 a full page is ~1200 px and the
# sampled fraction tracks the true fraction to within a few percent, with a slight
# pessimistic bias that errs toward rendering rather than dropping real content.
BG_SAMPLE_SCALE = 0.5

# Exclusion threshold for is_fully_background (TIF-68). The old code returned
# ORANGE_THRESHOLD (0.85), but that was safe only because BG_SAMPLE_SCALE=0.05
# under-counted so wildly that nothing was excluded. With the faithful, border-trimmed
# sample, 0.85 would EXCLUDE pages that are 85-95% background but still carry a real
# 5-15% content sliver (the tail of a design) — dropping printable artwork. TIF-68's
# reported bug is specifically the UNAMBIGUOUS ~100%-background page; the 85-95% band
# is a separate calibration concern (TIF-65). So this threshold targets only genuinely
# empty pages. With the border trimmed, genuinely empty pages sample ~100% (even the
# edge-bounded worst case) while pages carrying content sample <= ~95% — a wide gap.
# 0.98 sits in it, biased high so a page is only dropped when it is essentially all
# background (favouring "render" over "drop content", the safer error).
FULLY_BG_THRESHOLD = 0.98

# Pink padding color for partial pages (0–1 RGB)
PINK_PAD_R, PINK_PAD_G, PINK_PAD_B = 0.957, 0.565, 0.710  # ≈ #F490B5

# ── Color labeling (map-driven, pixel-based) ───────────────────────────────────
# Rebuilt pipeline. Operates on each already-rendered, already-sliced strip page
# (post-rotation). All four knobs below are tunable.
ENABLE_COLOR_LABELS   = True   # Master switch — color labeling is live in production
                               # (TIF-27, validated and approved). Validation harnesses
                               # call _label_colors_on_page directly and are unaffected
                               # by this switch.
# TIF-87: this does NOT scale with the Skissyta, and the reason is not merely
# "it isn't a length". (1) There is no mechanism: it is a Euclidean distance in
# RGB, and page geometry has no unit-bearing relationship to it. (2) The input it
# guards is provably unchanged — tolerance absorbs RENDER DRIFT, which is a
# function of render resolution per unit of artwork, and that is
# LABEL_RENDER_SCALE * pts_per_m = 56.7 px per fabric metre at EVERY page height
# (pts_per_m is derived from the DESIGN dimensions, never from the Skissyta —
# see the Skissyta block at the top of this file). Interior pixels of a smaller
# page are bit-identical to the same region on a larger one. (3) There is no
# headroom to spend anyway: see the TIF-55 note below. What a smaller page does
# change is the EDGE-pixel fraction, and that is the masking layer's job
# (MIN_PATCH_PX + the 1-px border trims), not the colour band's — widening the
# band to compensate would trade a mask problem for a colour-collision one.
COLOR_MATCH_TOLERANCE = 28     # RGB Euclidean distance for matching a mapped color
                               # (handles render drift, same idea as pink/orange bands)
                               # TIF-55: tightly calibrated, not an arbitrary default.
                               # Real production art needs matches up to ~26 RGB units
                               # of legitimate same-color shading/gradient variance
                               # (pest-mitten design, #231F20->8010 at 26.0) -- 28 clears
                               # that with only ~2.0 units of margin. Do not lower without
                               # re-verifying against real shipped designs; there is no
                               # headroom to spare. Watch condition: the tightest known-
                               # color pair today is 8010/8500 at 23.94 apart -- if a new
                               # manual colour_map entry ever lands near that neighborhood,
                               # re-verify this value.
MIN_PATCH_PX          = 10     # discard connected components smaller than this many
                               # pixels at LABEL_RENDER_SCALE. Strip-15 page 2 gave a
                               # clean dust/real gap of 8..22 (edge dust <=8px, thin real
                               # blade segments >=22px). ENAD strip 4 page 1 then showed a
                               # clearly visible ~19x7cm sliver whose thin ends anti-alias
                               # away, leaving only 10 in-tolerance px — so the threshold
                               # sits at 10: still above the 8px dust ceiling, low enough
                               # to label every patch a painter can actually see.
LABEL_RENDER_SCALE    = 2.0    # pixmap render scale used for color analysis

# TIF-67 (rev): ONE upfront space gate for the whole marking. If the total pink
# leftover — from where content stops (b*) to the page's outer edge — is at most
# KLIPP_MIN_PINK_PT, there is not enough fabric to mark: suppress the ENTIRE marking
# (line AND text together) before any margin or text-room logic runs. This is a
# single decision in _find_cut_boundary, replacing the two former independent checks
# (a per-line "does the 2pt margin fit" edge test + a separate per-text room test).
# It also subsumes the old seam/edge case: content running to the page edge leaves
# ~0 pink, well under the threshold. The 1/LABEL_RENDER_SCALE border-trim pull-in is
# ~0.5pt, far inside this margin, so the gate can never invent a cut with no fabric.
KLIPP_MIN_PINK_PT = 12.0     # pt — total pink below which nothing is marked at all.
                             # Must exceed KLIPP_LINE_MARGIN_PT (a marking needs the
                             # margin plus a little drawable pink beyond it). Raised
                             # 6.0 -> 12.0 (TIF-67); Emil is iterating this on real
                             # prints — keep adjustable.

KLIPP_LINE_MARGIN_PT = 4.0   # pt — TIF-67 Part 1. The dashed cut line sits this far
                             # INTO the pink, past where content stops (b*), so it
                             # never rides on the artwork edge. Folded into cut_x inside
                             # _find_cut_boundary AFTER the KLIPP_MIN_PINK_PT gate above
                             # has already guaranteed enough pink for it. Emit-time value;
                             # Emil expects to iterate it on real prints — keep adjustable.
                             # Doubled 2.0 -> 4.0 (TIF-67 reopened); still a fixed PDF-pt
                             # value, not a physical-distance conversion, and safely below
                             # KLIPP_MIN_PINK_PT.

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

MIN_LABEL_PATCH_SIZE_PT = 1.0  # pt — TIF-69. Reuses the SAME distance-transform /
                               # inscribed-circle metric that seeds the font-sizing
                               # ladder above (r_pts = dt.max() / S, computed once per
                               # patch) as an upfront gate: a patch whose inscribed
                               # radius does not clear this many points is skipped
                               # entirely, before the shrink-to-LABEL_FONT_TECH_MIN
                               # ladder runs at all. Below this size a patch is print-
                               # floor dust, not a labelable shape -- forcing a label
                               # onto it (down at the 0.1pt technical floor) produces
                               # illegible ink jammed into/over neighbouring paint, not
                               # a useful mark. This is a DIFFERENT gate from MIN_PATCH_PX
                               # (a pixel-COUNT dust filter on the raw component; this is
                               # a physical-SIZE filter on the largest inscribed shape a
                               # label could actually sit inside). 1.0pt is a starting
                               # value only -- Emil expects this to move as real prints
                               # show it skipping too much or too little. Keep it here,
                               # as the single source of truth; do not inline or
                               # duplicate this threshold elsewhere.

# "Klipp" cut-line text (TIF-67 Part 2). HARD RULE: the label ALWAYS sits to the
# RIGHT of the dashed line, never left, under any circumstance — this reverses
# TIF-57's thin-sliver left-of-line fallback, which is no longer acceptable. These
# knobs govern ONLY whether/how large the text is; they never touch the line, which
# always draws per KLIPP_LINE_MARGIN_PT regardless of whether the text fits.
KLIPP_FONT_DEFAULT     = 6.0   # pt — the label's default size (as in TIF-57)
KLIPP_TEXT_GAP_PT      = 3.0   # pt — breathing gap between the line and the text start
                               # (the original TIF-57 `cut_x + 3` offset, named)
# Two DISTINCT text floors — read the units, they are not the same knob:
KLIPP_TEXT_MIN_ROOM_PT = 4.0   # pt of WIDTH — minimum room to the RIGHT of the line
                               # (up to the page-number exclusion zone) before the text
                               # is even attempted. Below this width the text is skipped
                               # and the line stands alone. A horizontal-space gate.
KLIPP_MIN_FONT_SIZE_PT = 4.0   # pt of FONT SIZE — the smallest the label may shrink to.
                               # If "Klipp" cannot fit the available width at >= this
                               # font size, the text is skipped rather than shrunk into
                               # illegibility (this REPLACES the old LABEL_FONT_TECH_MIN
                               # 0.1pt floor for the Klipp label — the colour labeler
                               # still uses its own 0.1 floor). Distinct from
                               # KLIPP_TEXT_MIN_ROOM_PT: that is a width in pt, this is a
                               # glyph size in pt. Both STARTING values; Emil will iterate.

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

PAGE_NUM_FONT_PT   = 6.0   # pt — the orange page number's glyph size
PAGE_NUM_INSET_PT  = 12.0  # pt leftward from the right page edge to its origin
PAGE_NUM_BASELINE_PT = 10.0  # pt from the top edge to its baseline
# These three were hardcoded inline in slice_one_strip (`width - 12`, `10`,
# `fontsize=6`) even though PAGE_NUM_EXCL_* above exists precisely to reserve the
# box they occupy — the two were only kept consistent by hand. Named here so the
# reservation and the thing being reserved scale together (TIF-87).

KLIPP_LINE_WIDTH_PT = 1.0  # pt — dashed cut-line stroke weight (furniture: it is
                           # ink on the sheet, not a distance on the fabric)
KLIPP_DASH_PT       = 4.0  # pt — dash and gap length of that line's pattern

KLIPP_TEXT_PAD_PT = 2.0  # pt — clearance between the Klipp text's right limit and
                         # the page-number exclusion zone. Was an unnamed `- 2.0`
                         # inline in slice_one_strip (TIF-87).


# ── Page furniture scaling (TIF-87) ───────────────────────────────────────────
#
# Two classes of constant live in this file and they behave differently when the
# Skissyta's page height changes:
#
#   PHYSICAL (fabric-referenced) — KLIPP_MIN_PINK_PT, KLIPP_LINE_MARGIN_PT,
#     MIN_LABEL_PATCH_SIZE_PT, MIN_PATCH_PX, LABEL_FONT_DEFAULT, label MARGIN.
#     These are pt values that MEAN a distance on the fabric, and they are
#     ALREADY invariant: pts_per_m is derived as full_w / width_m from the
#     DESIGN, so 4.0 pt is 14.1 cm of fabric at every page height (measured
#     across 1.0-8.0 m). They must NOT be scaled — scaling them is what would
#     break their calibration, not what would preserve it.
#
#   FURNITURE (sheet-referenced) — the page number, the Klipp word, stroke
#     weights. These are annotations ON the printed sheet, not measurements of
#     fabric. Strip PDFs are viewed/printed at TRUE SCALE (fixed zoom), so a
#     6 pt page number has the same apparent size on every job and SHOULD stay
#     6 pt. Furniture is scaled for one reason only: FIT. Below a certain page
#     height the furniture no longer physically fits on the sheet.
#
# Hence the anchor is the FIT FLOOR, not the default page height. Measured
# floors, at STRIP_WIDTH_M = 1.5 (page_w = page_height_m * 28.3465):
#     page_w > 13.50 pt  (PAGE_NUM_EXCL_W fits at all)  -> 0.476 m
#     page_w > 18.67 pt  (2-digit page number fits)     -> 0.659 m
#     page_w > 27.39 pt  ("Klipp" fits at its floor fs) -> 0.966 m
# The binding one is ~0.97 m, so full-size furniture is guaranteed to fit at and
# above 1.0 m and that is where the scale is anchored.
#
# Anchoring here rather than at PAGE_HEIGHT_M (4.0) is deliberate: a 4.0 anchor
# would shrink the page number to 3 pt on a perfectly roomy 2 m ruta, making it
# needlessly less readable at true scale for no fit reason. Change this one
# constant to 4.0 if that judgement is ever reversed — nothing else moves.
FURNITURE_FIT_FLOOR_M = 1.0


def furniture_scale(page_height_m):
    """
    Multiplier for the FURNITURE class above, so page furniture keeps fitting on
    a sheet shorter than FURNITURE_FIT_FLOOR_M. Never grows furniture.

    CAPPED AT 1.0 ON PURPOSE. At every page height at or above the fit floor —
    which includes the 4.0 m default and everything larger — this returns
    exactly 1.0, and `x * 1.0` is exact in IEEE-754. So default output is
    byte-identical to pre-TIF-87 master BY CONSTRUCTION, not merely by
    verification. The regression digest checks that; this guarantees it.

    Everything the scale touches is linear in it (widths, insets, font sizes,
    stroke weights), so below the floor the whole furniture set shrinks
    self-similarly and keeps fitting all the way down.
    """
    return min(1.0, page_height_m / FURNITURE_FIT_FLOOR_M)

# ── Helpers ───────────────────────────────────────────────────────────────────

def is_fully_background(src_doc, src_page_num, clip):
    """
    Render the clip region and check if it's mostly the light pink background
    (~R244 G144 B181) OR the orange background (~R255 G128 B0). Returns True if
    more than FULLY_BG_THRESHOLD of pixels match either colour, meaning the page has
    no real content worth printing.

    TIF-68: three coupled fixes for why genuinely-empty pages were not excluded.
    (1) BG_SAMPLE_SCALE was 0.05, far too coarse to be faithful, so a ~100%-background
    page read as 40-83% background. (2) The 1-px anti-aliased BORDER of the render
    (the clip edge blended toward whatever lies outside it) reads as false content and,
    on a small sample, is a large fraction — so even at a faithful scale an all-pink
    page bounded by content/white read ~94%. That border is trimmed here, the same way
    _find_cut_boundary trims it, so the sample reflects the page interior. (3) With a
    faithful, trimmed sample the old 0.85 threshold would over-exclude pages holding a
    real content sliver, so the exclusion threshold is FULLY_BG_THRESHOLD, high enough
    to drop only genuinely empty pages. The colour boxes are unchanged.
    """
    mat = fitz.Matrix(BG_SAMPLE_SCALE, BG_SAMPLE_SCALE)
    pix = src_doc[src_page_num].get_pixmap(matrix=mat, clip=clip, colorspace=fitz.csRGB)
    if pix.width == 0 or pix.height == 0:
        return False
    arr = np.frombuffer(pix.samples, dtype=np.uint8) \
            .reshape(pix.height, pix.width, pix.n)[:, :, :3].astype(np.int32)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    is_pink = ((r > PINK_R_MIN)
               & (g > PINK_G_MIN) & (g < PINK_G_MAX)
               & (b > PINK_B_MIN) & (b < PINK_B_MAX)
               & (r > g) & (r > b))
    is_orange = ((r > ORANGE_R_MIN)
                 & (g > ORANGE_G_MIN) & (g < ORANGE_G_MAX)
                 & (b < ORANGE_B_MAX)
                 & (r > g) & (g > b))
    bg = is_pink | is_orange
    # Discard the 1-px border ring — partial-coverage raster artifact, not artwork.
    if bg.shape[0] >= 3 and bg.shape[1] >= 3:
        bg = bg[1:-1, 1:-1]
    return bool(bg.mean() > FULLY_BG_THRESHOLD)


# ── Raster source preprocessing ───────────────────────────────────────────────

# Magic bytes, not the filename extension: run_slice() receives raw bytes with no
# name attached, and sniffing the content is the only check that is true at the
# point the decision is actually made. api.py still screens extensions so a
# mislabelled upload fails with a clear 400 rather than deep inside pymupdf.
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC  = b"\x89PNG\r\n\x1a\n"


def is_raster_source(data):
    """True if `data` looks like a JPG or PNG rather than a PDF."""
    return data.startswith(_JPEG_MAGIC) or data.startswith(_PNG_MAGIC)


def _image_has_alpha(img_bytes):
    """
    True if the image carries an alpha CHANNEL and therefore may need flattening.

    Deliberately conservative: this asks whether transparency is representable,
    not whether any pixel is actually transparent, so a fully-opaque RGBA PNG also
    returns True and gets an underlay it does not need. That costs nothing visible
    (an opaque image covers it) — the only trace is the underlay's own fill
    appearing in extract_pdf_colors, which is never consulted for a raster job
    since colour analysis is skipped for them outright.

    JPEG has no alpha channel in any variant, so it short-circuits without
    decoding — which matters, since the photographic uploads are the large ones.
    PNG is asked properly via pymupdf rather than by reading the IHDR colour type,
    because transparency also arrives via a tRNS chunk on palette and greyscale
    images, which a header check would miss (verified against a hand-built
    palette+tRNS PNG).

    On an unreadable image this returns True: the caller then lays down the
    underlay, which is harmless if unnecessary. Erring the other way would leave a
    genuinely transparent design unflattened.
    """
    if img_bytes.startswith(_JPEG_MAGIC):
        return False
    try:
        return bool(fitz.Pixmap(img_bytes).alpha)
    except Exception:
        return True


def image_to_pdf(img_bytes, width_m, height_m):
    """
    Wrap a JPG/PNG into a single-page PDF sized width_m x height_m at PTS_PER_M,
    with the image STRETCHED to fill that page exactly.

    This is the whole of raster support: once the image is a normally-scaled
    single-page PDF, every stage after it — banderoll rotation, strip slicing,
    the Klipp content scan, colour labeling, page numbering, the grid — runs
    unchanged, because none of them ever ask whether the page's marks came from
    vectors or from an embedded image.

    Aspect ratio is deliberately NOT preserved (keep_proportion=False). The
    entered dimensions are the truth about the physical tifo; an upload whose
    pixel aspect disagrees is stretched to match rather than letterboxed or
    rejected. Letterboxing would paint bands of blank page that the content scan
    and the labeler would then both have to reason about, and rejecting would put
    a pixel-exact demand on the upload form that nobody can meet.

    ALPHA FLATTENING: when (and only when) the image carries transparency, the
    page is first filled with PINK_PAD — the very constant the slicer already uses
    to pad partial pages — and the image is composited on top, so transparent
    regions end up that exact pink rather than white.

    This matters mechanically, not cosmetically: PINK_PAD (244,144,181) falls
    inside the PINK_* band that _background_mask and is_fully_background classify
    as background, so transparency is read as "not real content" by the Klipp
    content-boundary scan and by trailing-page exclusion — the same treatment a
    design gets when it paints its own leftover fabric. Flattened onto white it
    would instead read as content, and worse, #FFFFFF is the real paint code
    "Vit", so transparent areas would be labeled as white paint.

    The flattening is an underlay composited by the PDF renderer rather than done
    by hand on the source pixels, because MuPDF pixmap samples are PREMULTIPLIED:
    correct hand-compositing is `rgb + bg*(1-a)`, and the intuitive
    `rgb*a + bg*(1-a)` silently double-applies alpha and darkens every
    partially-transparent edge. The renderer cannot get that wrong, and it
    composites at full render resolution rather than at source resolution.

    The underlay is skipped entirely for opaque images. It would be invisible
    under them anyway, but it is a vector fill, and emitting one would make
    extract_pdf_colors() report #F490B5 as a design colour on every raster job —
    a phantom entry in a place that is supposed to find nothing.
    """
    out  = fitz.open()
    page = out.new_page(width=width_m * PTS_PER_M, height=height_m * PTS_PER_M)
    if _image_has_alpha(img_bytes):
        page.draw_rect(page.rect, fill=(PINK_PAD_R, PINK_PAD_G, PINK_PAD_B),
                       color=None, fill_opacity=1.0)
    page.insert_image(page.rect, stream=img_bytes, keep_proportion=False)

    buf = io.BytesIO()
    out.save(buf)
    out.close()
    return buf.getvalue()


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


def _map_reps(color_map):
    """
    Flatten a {hex: code} map into [(code, rgb255), ...] — the representatives
    every colour decision is made against. One entry per hex, so two hexes
    sharing a code (or two distinct "Skip" hexes) stay INDEPENDENT claimants
    during nearest-color assignment.

    Shared by _code_masks() (what gets labeled) and find_unknown_colors() (what
    gets reported as unmappable) so the two can never drift apart.
    """
    reps = []
    for hex_c, code in color_map.items():
        if not code:
            continue
        rgb = _hex_to_rgb255(hex_c)
        if rgb is not None:
            reps.append((code, rgb))
    return reps


def find_unknown_colors(hex_colors, color_map):
    """
    Return the design colors that NO colour_map entry can claim — i.e. whose
    nearest mapped color is further than COLOR_MATCH_TOLERANCE away.

    Uses the same nearest-within-tolerance test as _code_masks(), which is what
    actually decides whether a pixel gets a label. The pre-TIF-60 gate used
    exact hex membership (`c not in color_map`) instead, which is the wrong
    question: rendered vector fills routinely land 1-2 RGB units off the
    curated hex (e.g. #737272 vs the mapped #737271), so exact matching
    reported as "unknown" a pile of colors the labeler matches perfectly. On
    the pest-mitten design that meant 11 reported unknowns where only 1 was
    real.

    A "Skip" entry counts as claiming its color: the color is known, it is
    deliberately not labeled. It must never show up as unknown.
    """
    reps = _map_reps(color_map)
    tol_sq = COLOR_MATCH_TOLERANCE ** 2

    unknown = []
    for hex_c in hex_colors:
        rgb = _hex_to_rgb255(hex_c)
        if rgb is None:
            continue
        if not reps or min(int(((rgb - r) ** 2).sum()) for _, r in reps) > tol_sq:
            unknown.append(hex_c)
    return sorted(unknown)


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
    reps = _map_reps(color_map)
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


def _background_mask(arr, color_map):
    """
    True where a pixel is design BACKGROUND — i.e. NOT real content.

    Two signals, unioned, because they fail in different places:

      1. colour_map "Skip" entries — a pixel whose NEAREST map rep (within
         COLOR_MATCH_TOLERANCE) carries the code "Skip". This is the precise,
         general answer: it follows whatever hex a design declares as its
         not-to-be-cut background, not just pest-mitten's pink #EEA8CB. It
         reuses _map_reps(), the very reps the labeler matches with, so the
         classifier and the labeler cannot drift apart (the TIF-60 lesson).

      2. The legacy pink/orange RGB ranges (is_fully_background's classifier), a
         best-effort net for when colour_map is empty — run_slice() passes {}
         whenever skip_colors=True or ENABLE_COLOR_LABELS=False.

    Signal 2 alone is NOT sufficient to find a content boundary, and is not
    relied on to. Its range is a hard box, and rendered background anti-aliases
    out of it: pest-mitten's bottom edge row comes out #F2BFD8, whose green 191
    is one unit past PINK_G_MAX=190, so the row reads as content and drags the
    boundary to the design's edge. Signal 1 gets it right (#F2BFD8 is 26.7 from
    the mapped #EEA8CB, inside COLOR_MATCH_TOLERANCE=28) because nearest-rep
    matching absorbs exactly that drift.

    So with no colour_map the scan degrades to finding the boundary at the clip
    edge — which is precisely cut_x == the old pad_x, i.e. the pre-existing
    geometric behaviour, no better and no worse. That degradation is safe by
    construction and is what _validate_klipp_partial.py (which passes {}) pins
    down. Widening the pink box to cover the anti-aliased fringe is NOT done
    here: PINK_G_MAX is is_fully_background's, and a second, divergent
    definition of "pink" is the drift TIF-60 warned about.

    Everything else is content, deliberately including:
      - unknown colours (no rep within tolerance) — real artwork that still
        prints, merely unlabeled (TIF-60);
      - white — #FFFFFF is the real paint code "V" (Vit), not blank paper.
    """
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]

    is_pink = ((r > PINK_R_MIN)
               & (g > PINK_G_MIN) & (g < PINK_G_MAX)
               & (b > PINK_B_MIN) & (b < PINK_B_MAX)
               & (r > g) & (r > b))
    is_orange = ((r > ORANGE_R_MIN)
                 & (g > ORANGE_G_MIN) & (g < ORANGE_G_MAX)
                 & (b < ORANGE_B_MAX) & (r > g) & (g > b))
    bg = is_pink | is_orange

    reps = _map_reps(color_map)
    skip_ks = [k for k, (code, _) in enumerate(reps) if code == "Skip"]
    if skip_ks:
        dist    = np.stack([((arr - rgb) ** 2).sum(axis=2) for _, rgb in reps])
        nearest = np.argmin(dist, axis=0)
        within  = np.min(dist, axis=0) <= COLOR_MATCH_TOLERANCE ** 2
        for k in skip_ks:
            bg |= (nearest == k) & within
    return bg


def _find_cut_boundary(src_doc, src_page_num, x0, x1, full_h, page_h_pts,
                       num_pages, ruta_nedre, color_map):
    """
    Find the ONE point across a whole strip where real design content stops for
    good — where "Klipp" belongs. Returns (page_num, cut_x) with cut_x in OUTPUT
    page coordinates, or None when there is nothing to cut.

    Why this replaces is_partial as the trigger: is_partial is a purely GEOMETRIC
    test (does the source clip cover the full output page?). It is blind to a
    design whose artwork ends mid-page inside its own painted background.
    pest-mitten is 24 m tall with 4 m pages, so it divides into six FULL pages
    and no page is ever partial — yet its artwork stops partway into page 6 and
    the rest is painted pink. No cut line was drawn anywhere on that design.

    The strip's column is scanned ONCE, at LABEL_RENDER_SCALE — deliberately NOT
    the 21-pixel sampler is_fully_background() uses, which is far too coarse to
    locate a boundary row (see TIF-65). The last content pixel in PRINTING
    SEQUENCE order wins.

    One coordinate unifies both rotation modes, so nothing below branches on
    ruta_nedre except the definition itself:

        seq(design_y) = full_h - design_y   (default,    rotate=270, bottom→top)
        seq(design_y) = design_y            (ruta_nedre, rotate=90,  top→bottom)

    seq is "distance travelled along the printing sequence", and for any point
    in the strip it gives both the page and the position on it:

        page_num = floor(seq / page_h_pts)
        output_x = seq mod page_h_pts

    which is exactly where the old code drew the line — at output_x = content_w,
    the seq-position where the source CLIP runs out. This function swaps that for
    the seq-position where the CONTENT runs out. The geometric case falls out as a
    special case: content reaching the clip edge yields the content boundary at
    content_w (then offset right by KLIPP_LINE_MARGIN_PT, see below).

    TIF-67 (rev): the returned cut_x is the content boundary shifted
    KLIPP_LINE_MARGIN_PT into the pink, so the line never rides the artwork edge.
    One upfront gate decides the whole marking first: if the total pink leftover to
    the page edge is <= KLIPP_MIN_PINK_PT, this returns None and NOTHING is marked
    (line and text both). That single threshold covers content running off the page,
    content ending on a page seam, and too-little-pink-for-the-margin alike.
    """
    S    = LABEL_RENDER_SCALE
    pix  = src_doc[src_page_num].get_pixmap(
        matrix=fitz.Matrix(S, S), clip=fitz.Rect(x0, 0.0, x1, full_h),
        colorspace=fitz.csRGB)
    if pix.width == 0 or pix.height == 0:
        return None

    arr = np.frombuffer(pix.samples, dtype=np.uint8) \
            .reshape(pix.height, pix.width, pix.n)[:, :, :3].astype(np.int32)

    content = ~_background_mask(arr, color_map)

    # Discard the 1-pixel BORDER of the render. Those pixels have PARTIAL coverage
    # — the rasterizer blends the clip's edge with whatever lies outside it — so
    # they are an artifact, not artwork, and they are full-width/full-height, which
    # means they sail straight past the MIN_PATCH_PX dust filter below.
    #
    # Both axes bite, and both were caught on pest-mitten:
    #   rows — the design's bottom row renders #F2BFD8 at LABEL_RENDER_SCALE (26.7
    #     from the mapped #EEA8CB, i.e. inside COLOR_MATCH_TOLERANCE by a mere 1.3)
    #     and #F7D6E6 at scale 4.0 (54.1, well outside), while the row behind it is
    #     a clean #EEA8CA at 1.0. Read as content, it pins the boundary to the
    #     design's edge and suppresses the cut line — silently.
    #   cols — the first and last strip sit on the design's left/right edge, so
    #     their outer pixel column washes out the same way, full height, and reads
    #     as a content column running past the true boundary.
    # Untrimmed, this detector works only by luck at one particular render scale.
    #
    # Trimming all four sides covers both slicing modes (the sequence's far end is
    # row 0 under rotate=270, the last row under rotate=90). Where real content
    # does reach an edge, this pulls the boundary in by just 1/S pt — far inside the
    # KLIPP_MIN_PINK_PT gate below — so it can never invent a cut line where there is
    # nothing to cut.
    if content.shape[0] >= 3 and content.shape[1] >= 3:
        content[0, :]  = content[-1, :] = False
        content[:, 0]  = content[:, -1] = False

    # Dust filter — the same connected-component + MIN_PATCH_PX semantics the
    # labeler uses, so anti-alias speckle inside a background region cannot read
    # as "content" and drag the boundary to the far end of the strip.
    lbl, n = ndimage.label(content)
    if n == 0:
        return None
    counts  = np.bincount(lbl.ravel())
    keep    = counts >= MIN_PATCH_PX
    keep[0] = False                      # component 0 is the background itself
    content = keep[lbl]

    rows = np.flatnonzero(content.any(axis=1))
    if rows.size == 0:
        return None

    # The last content pixel in sequence order: seq rises with design_y under
    # ruta_nedre and falls with it under the default, so the winning row is the
    # last or the first respectively. The boundary is that pixel's FAR edge (the
    # side the leftover fabric lies on); its MIDPOINT decides which page owns it,
    # which keeps content that ends exactly on a page seam attributed to the page
    # it actually sits on rather than to the empty page after it.
    if ruta_nedre:
        r_last       = int(rows[-1])
        boundary_seq = (r_last + 1) / S
        seq_mid      = (r_last + 0.5) / S
    else:
        r_last       = int(rows[0])
        boundary_seq = full_h - r_last / S
        seq_mid      = full_h - (r_last + 0.5) / S

    pn     = min(max(int(seq_mid // page_h_pts), 0), num_pages - 1)
    b_star = boundary_seq - pn * page_h_pts       # raw content boundary, no margin

    # TIF-67 (rev): ONE upfront gate for the whole marking, before any margin logic.
    # The total pink leftover from the content boundary to this page's outer edge is
    # page_h_pts - b_star. If that is at most KLIPP_MIN_PINK_PT there is not enough
    # fabric to mark — suppress the ENTIRE marking (returning None drops both the line
    # AND the text downstream). This subsumes the old cases in one decision: content
    # running to the edge (pink ~0), content ending on a page seam (pink ~0), and "not
    # enough pink for the 2pt margin" (pink < margin) are all just small-pink cases.
    if page_h_pts - b_star <= KLIPP_MIN_PINK_PT:
        return None

    # Nudge the line KLIPP_LINE_MARGIN_PT into the pink, past where content stops, so
    # it never rides the artwork edge. Added AFTER pn is fixed and AFTER the gate has
    # guaranteed enough pink, so it can only push toward THIS page's own outer edge —
    # never onto the next page, never negative, and never flush with the edge.
    cut_x = b_star + KLIPP_LINE_MARGIN_PT
    return pn, cut_x


def _label_colors_on_page(page, color_map, pn_excl=None):
    """
    Map-driven, pixel-based color labeling for a single rendered strip page.

    For every color in color_map (skipping "Skip"):
      1. Render the page to a pixmap at LABEL_RENDER_SCALE.
      2. Build a boolean mask of pixels within COLOR_MATCH_TOLERANCE (RGB
         Euclidean distance) of the mapped color — a tolerance band that absorbs
         render drift, same principle as the pink/orange background bands.
      3. Find separate visible patches with scipy.ndimage.label.
      4. Discard any patch smaller than MIN_PATCH_PX (filters anti-alias noise).
      5. Compute r_pts (the inscribed-circle radius, via distance transform) for
         each surviving patch. If r_pts < MIN_LABEL_PATCH_SIZE_PT (TIF-69), skip
         the label entirely — before any sizing/placement logic runs. This is
         true print-floor dust, not a space a label can usefully occupy.
      6. Every OTHER surviving patch receives EXACTLY ONE label — no skips.

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

    Returns {code: {"count", "font_sizes", "rects", "placement", "forced",
    "skipped_small"}} for inspection/validation ("rects" are glyph bboxes in
    page points, aligned with "font_sizes"; "placement" entries are
    "fit"/"forced"; "skipped_small" counts patches skipped under
    MIN_LABEL_PATCH_SIZE_PT, TIF-69). Keyed by paint CODE (not hex) since
    round 4. The slicing path ignores the return value.
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
    #
    # pn_excl is the ALREADY-SCALED (w, y0, y1) triple from the caller (TIF-87):
    # the page number shrinks below FURNITURE_FIT_FLOOR_M, so the box reserved
    # for it has to shrink by the same factor or it would reserve dead space that
    # the number no longer occupies. Defaults to the unscaled module constants so
    # the validation guards that call this function directly keep working.
    excl_w, excl_y0, excl_y1 = pn_excl or (PAGE_NUM_EXCL_W, PAGE_NUM_EXCL_Y0,
                                           PAGE_NUM_EXCL_Y1)
    placed_rects = [fitz.Rect(page.rect.width - excl_w,
                              excl_y0,
                              page.rect.width, excl_y1)]
    MARGIN       = 0.5  # pt clearance required between labels

    for npx, code, sl, sub_mask, dt, text_color in patches:
        entry = summary.setdefault(
            code, {"count": 0, "font_sizes": [], "rects": [], "placement": [],
                   "forced": 0, "skipped_small": 0})

        r_pts = float(dt.max()) / S

        # TIF-69: top-of-ladder size gate, using the SAME r_pts metric the sizing
        # ladder below is about to consume. A patch this small is print-floor
        # dust, not a labelable shape — skip entirely rather than proceeding
        # into the shrink-to-LABEL_FONT_TECH_MIN logic (which would force an
        # illegible/overlapping label onto it). No sizing, no placement, no
        # collision bookkeeping for this patch at all.
        if r_pts < MIN_LABEL_PATCH_SIZE_PT:
            entry["skipped_small"] += 1
            continue

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

    # Keep entries with only skipped_small patches (TIF-69): a code whose every
    # patch was below MIN_LABEL_PATCH_SIZE_PT would otherwise vanish from the
    # summary entirely, hiding those skips from validation/reporting.
    return {h: e for h, e in summary.items() if e["count"] or e["skipped_small"]}


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

    color_map and skip_labels are INDEPENDENT (TIF-57 follow-up). The map has two
    consumers and they are not the same concern:
      - pass A (_find_cut_boundary) needs it to recognise the design's "Skip"
        background, i.e. to know where the artwork stops and the Klipp line goes;
      - pass B (_label_colors_on_page) needs it to draw the visible NCS codes.
    skip_labels suppresses ONLY the latter. Callers that want no printed labels
    must therefore pass skip_labels=True and still hand over the real map, rather
    than wiping it to {} — a wiped map silently downgrades cut detection to the
    legacy pink/orange fallback, which cannot see a design whose background is
    some other declared Skip colour.
    """
    # page_height_m is APPENDED at the end of the tuple (TIF-87) rather than
    # inserted next to width_m/height_m, so the five external callers that build
    # this tuple by hand (_design_label_harness and the _validate_* guards) need
    # only append, and a review of this change can see at a glance that no
    # existing positional meaning moved.
    (s, pdf_bytes, width_m, height_m, num_strips, num_pages, color_map,
     ruta_nedre, skip_labels, page_height_m) = args

    fscale = furniture_scale(page_height_m)
    pn_excl_w  = PAGE_NUM_EXCL_W  * fscale
    pn_excl_y0 = PAGE_NUM_EXCL_Y0 * fscale
    pn_excl_y1 = PAGE_NUM_EXCL_Y1 * fscale
    pn_font    = PAGE_NUM_FONT_PT * fscale
    pn_inset   = PAGE_NUM_INSET_PT * fscale
    pn_base    = PAGE_NUM_BASELINE_PT * fscale

    src_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    out_doc = fitz.open()

    for src_page in src_doc:
        r       = src_page.rect
        full_w  = r.width
        full_h  = r.height

        pts_per_m_x = full_w / width_m
        pts_per_m_y = full_h / height_m
        strip_w_pts = STRIP_WIDTH_M * pts_per_m_x
        page_h_pts  = page_height_m * pts_per_m_y

        x0 = s * strip_w_pts
        x1 = min((s + 1) * strip_w_pts, full_w)

        # ── Pass A: where does this strip's content stop for good? ────────────
        # Scanned across the WHOLE strip before anything is drawn, because "the
        # single last-content boundary" is a property of the strip, not of any
        # one page — deciding it per page independently is what let the old
        # is_partial trigger miss it entirely. Yields the one page that gets the
        # cut marking (and where on it), or None when there is nothing to cut.
        cut = _find_cut_boundary(src_doc, src_page.number, x0, x1, full_h,
                                 page_h_pts, num_pages, ruta_nedre, color_map)
        cut_page, cut_x = cut if cut else (None, None)
        rendered_pages  = set()

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

            rendered_pages.add(page_num)

            # Color labels — per page, after rendering, using map-driven pixel
            # tolerance-band detection + deepest-interior-point placement.
            # Gated on skip_labels, NOT on the map being empty: pass A above still
            # needs the real map even when no labels are to be printed.
            if color_map and not skip_labels:
                _label_colors_on_page(new_page, color_map,
                                      pn_excl=(pn_excl_w, pn_excl_y0, pn_excl_y1))

            # Pink padding on a GEOMETRICALLY partial page — the source clip did
            # not cover the full output page, so pad_x (= content_w) onwards is
            # dead space. Purely geometric, unchanged: this fills the void, it is
            # NOT the cut marking (a full page can need a cut and no pad; a padded
            # page can need no cut). In BOTH modes the content is left-aligned and
            # the pad fills the right — the design's free outer edge. rotate makes
            # that edge the design TOP (default) or BOTTOM (ruta_nedre).
            if is_partial:
                shape = new_page.new_shape()
                shape.draw_rect(fitz.Rect(pad_x, 0, page_h_pts, x1 - x0))
                shape.finish(fill=(PINK_PAD_R, PINK_PAD_G, PINK_PAD_B), fill_opacity=1.0, color=None)
                shape.commit()

            # Dashed cut line + "Klipp" — drawn on the ONE page per strip that
            # holds the content boundary found by pass A, at cut_x (which already
            # carries KLIPP_LINE_MARGIN_PT, TIF-67 Part 1). Formerly gated on
            # is_partial and drawn at pad_x, which only ever fired on a geometric
            # leftover; it could not see a design ending mid-page inside its own
            # painted background.
            if cut_page is not None and page_num == cut_page:
                # The dashed line ALWAYS draws here — it is never affected by whether
                # the text below fits (TIF-67 Part 2, rule 3). If cut_x had left too
                # little pink for the margin, _find_cut_boundary would already have
                # suppressed the whole marking (cut_page/cut_x would be None).
                shape = new_page.new_shape()
                shape.draw_line(fitz.Point(cut_x, 0), fitz.Point(cut_x, x1 - x0))
                # cut_x is PHYSICAL (KLIPP_LINE_MARGIN_PT into the pink, unscaled);
                # the stroke weight and dash period that DRAW it are furniture.
                # %g so the default scale re-emits the original literal "[4 4] 0"
                # rather than "[4.0 4.0] 0" — same rendering either way, but it
                # keeps the CONTENT STREAM identical too, not just the pixels.
                dash = f"{round(KLIPP_DASH_PT * fscale, 3):g}"
                shape.finish(color=(0.15, 0.15, 0.15),
                             width=KLIPP_LINE_WIDTH_PT * fscale,
                             dashes=f"[{dash} {dash}] 0")
                shape.commit()

                # "Klipp" text — HARD RULE (TIF-67 Part 2): ALWAYS to the RIGHT of
                # the line, never left, under any circumstance (this removes TIF-57's
                # thin-sliver left-of-line fallback for good). The right boundary is
                # the page-number exclusion zone: the text baseline (y=10) sits inside
                # that zone's y-band, so the page number is the binding limit, same as
                # TIF-57 used. "room" is the width available to the right of the line,
                # before that zone.
                right_limit = page_h_pts - pn_excl_w - KLIPP_TEXT_PAD_PT * fscale
                room        = right_limit - cut_x
                if room >= KLIPP_TEXT_MIN_ROOM_PT * fscale:
                    # Width gate passed. Place the text a small gap right of the line
                    # and shrink to fit — but only down to KLIPP_MIN_FONT_SIZE_PT, a
                    # legibility floor (NOT the colour labeler's 0.1pt technical floor).
                    # Step by LABEL_FONT_SHRINK; the ladder bottoms out at the font
                    # floor. Pick the largest size whose "Klipp" fits the available
                    # width; if even the floor size is too wide, SKIP the text rather
                    # than shrink it into illegibility. The line still stands, and the
                    # text is never placed left — skipping is the only fallback.
                    text_x0   = cut_x + KLIPP_TEXT_GAP_PT * fscale
                    avail     = right_limit - text_x0
                    klipp_min = KLIPP_MIN_FONT_SIZE_PT * fscale
                    sizes     = [KLIPP_FONT_DEFAULT * fscale]
                    while sizes[-1] * LABEL_FONT_SHRINK > klipp_min:
                        sizes.append(round(sizes[-1] * LABEL_FONT_SHRINK, 3))
                    if sizes[-1] > klipp_min:
                        sizes.append(klipp_min)
                    chosen = next(
                        (sz for sz in sizes
                         if fitz.get_text_length("Klipp", fontsize=sz) <= avail),
                        None)
                    if chosen is not None:
                        new_page.insert_text(
                            fitz.Point(text_x0, pn_base),
                            "Klipp",
                            fontsize = chosen,
                            color    = (0.15, 0.15, 0.15),
                        )

            # Small orange page number, tight to top right corner
            new_page.insert_text(
                fitz.Point(new_page.rect.width - pn_inset, pn_base),
                str(page_num + 1),
                fontsize = pn_font,
                color    = (1, 0.5, 0),
            )

        # The content scan says the strip's content ends on a page that
        # is_fully_background() then excluded from rendering. Those two should
        # never disagree — a page holding content is not ~85%+ background — but the
        # sampler is a coarse low-res colour count (TIF-65/TIF-68), so near its
        # threshold it can still be wrong. Drawing a cut line derived from a page
        # nobody prints would be worse than drawing none: suppress, and say so.
        if cut_page is not None and cut_page not in rendered_pages:
            print(f"    WARNING: strip {s + 1} — content boundary falls on page "
                  f"{cut_page + 1}, which is_fully_background() excluded from "
                  f"rendering. No Klipp marking drawn for this strip.")

    # A strip every page of which is_fully_background() excluded ends up with no
    # pages at all, and out_doc.save() then raises "cannot save with zero pages",
    # failing the WHOLE job (the exception propagates out of the thread pool in
    # _slice_pdf). kenta strip 14 does exactly this — it is solid background top
    # to bottom.
    #
    # NOT a TIF-87 regression, and measured not to be: it fires identically at
    # every page_height_m from 1.0 to 8.0 m, the default 4.0 included, i.e. it is
    # invariant to the parameter this ticket added (a strip's content is a
    # property of its column, and the column is fixed once the strip width is).
    # Fixed here because the crash is real either way and a half-configured
    # Skissyta is a bad place to leave a job-killing exception.
    #
    # Emit ONE blank page rather than dropping the strip: strip files are named
    # strip-NN and an operator counts them against the grid PDF, so a missing
    # number reads as a bug they have to chase. A blank page says "this strip is
    # all background" in the one place they will actually look.
    #
    # PINK_PAD even on an orange-background design: this page carries no artwork,
    # so the fill is a placeholder for "nothing here", and PINK_PAD is already the
    # colour this module paints dead space with.
    # `src_doc.page_count` guards the page_h_pts/strip_w_pts read below: both are
    # bound inside the `for src_page` loop, so a (pathological) zero-page source
    # would make this a NameError — strictly worse than the ValueError it fixes.
    if out_doc.page_count == 0 and src_doc.page_count:
        blank = out_doc.new_page(width=page_h_pts, height=strip_w_pts)
        shape = blank.new_shape()
        shape.draw_rect(blank.rect)
        shape.finish(fill=(PINK_PAD_R, PINK_PAD_G, PINK_PAD_B),
                     fill_opacity=1.0, color=None)
        shape.commit()
        blank.insert_text(
            fitz.Point(blank.rect.width - pn_inset, pn_base),
            "1", fontsize=pn_font, color=(1, 0.5, 0),
        )
        print(f"    NOTE: strip {s + 1} — every page is background; emitting one "
              f"blank page so strip numbering stays dense.")

    buf = io.BytesIO()
    out_doc.save(buf)
    out_doc.close()
    src_doc.close()
    return (s + 1, buf.getvalue())


def _slice_pdf(pdf_bytes, width_m, height_m, color_map=None, ruta_nedre=False,
               skip_labels=False, page_height_m=None):
    """Slice pdf_bytes into vertical strips in parallel. Returns sorted list of (strip_num, bytes)."""
    page_height_m = PAGE_HEIGHT_M if page_height_m is None else page_height_m
    num_strips = math.ceil(width_m  / STRIP_WIDTH_M)
    num_pages  = math.ceil(height_m / page_height_m)

    args = [
        (s, pdf_bytes, width_m, height_m, num_strips, num_pages, color_map or {},
         ruta_nedre, skip_labels, page_height_m)
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


def generate_grid_pdf(pdf_bytes, width_m, height_m, ruta_nedre=False,
                      page_height_m=None):
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
    page_height_m = PAGE_HEIGHT_M if page_height_m is None else page_height_m
    src_doc    = fitz.open(stream=pdf_bytes, filetype="pdf")
    out_doc    = fitz.open()
    num_strips = math.ceil(width_m  / STRIP_WIDTH_M)
    num_pages  = math.ceil(height_m / page_height_m)

    for src_page in src_doc:
        r       = src_page.rect
        full_w  = r.width
        full_h  = r.height
        strip_w_pts = STRIP_WIDTH_M * (full_w / width_m)
        page_h_pts  = page_height_m * (full_h / height_m)

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
    page_height_m: float = None,
) -> dict:
    """
    Slices a PDF into 1.5m-wide vertical strips.

    page_height_m is the Skissyta's page height — how tall one printed ruta is,
    in metres (TIF-87). None means PAGE_HEIGHT_M (4.0), the value every job
    before TIF-87 was produced at; passing 4.0 explicitly is identical. The
    strip WIDTH is deliberately not a parameter: it is fixed by the fabric, not
    by the job (see the Skissyta block at the top of this module).

    Only page-FURNITURE constants react to it, and only below
    FURNITURE_FIT_FLOOR_M — see furniture_scale(). Fabric-referenced constants
    are already invariant, because pts-per-metre is derived from the DESIGN
    dimensions rather than from the Skissyta.

    Accepts a JPG or PNG in place of a PDF (sniffed from the bytes, see
    is_raster_source). A raster upload is converted to a single-page PDF first —
    stretched to fill a page sized from width_m/height_m at PTS_PER_M, over a
    PINK_PAD underlay when (and only when) the image carries transparency — after
    which every stage below is the ordinary vector path, with ONE deliberate
    difference: colour labeling is forced off for raster sources regardless of
    skip_colors.

    colour_map is a {hex: ncs_code} dict sourced from Supabase and passed in by
    the caller (api.py). It replaces the retired local color_map.json.

    skip_colors (and ENABLE_COLOR_LABELS=False) suppress the printed NCS code
    labels ONLY. The map is still handed to the slicer, because it has a second,
    unrelated consumer: the cut-boundary scan reads its "Skip" entries to find
    where the artwork stops and the Klipp line belongs. Wiping the map for
    skip_colors runs — as this used to — left them on geometric-only cut
    detection, blind to a design that ends mid-page inside painted background.

    Unknown colors (no map entry within COLOR_MATCH_TOLERANCE) are reported in
    "unknown_colors" and left unlabeled INDIVIDUALLY. They do not suppress
    labeling of the colors that ARE mapped — see TIF-60.

    Returns:
    {
        "strips": [
            {"filename": "strip-01.pdf", "bytes": b"..."},
            ...
        ],
        "unknown_colors": ["#RRGGBB", ...],  # empty if skip_colors=True or no unknowns
        "colors_analyzed": bool,             # False when unknown_colors was never
                                             # computed (skip_colors, ENABLE_COLOR_
                                             # LABELS off, or a raster source, which
                                             # is never labeled) — so [] is not
                                             # misread as "every colour matched"
    }
    """
    # Raster preprocessing runs FIRST, so every stage below — banderoll rotation
    # included — sees an ordinary single-page PDF and needs no raster awareness.
    #
    # The banderoll swap: in banderoll mode the caller's width/height describe the
    # design as HUNG (portrait), while the artwork is laid out landscape and
    # rotate_pdf_90 below reconciles the two. A raster upload is laid out the same
    # landscape way, so the page is built in that same pre-rotation orientation —
    # dimensions swapped — and the existing rotation then fixes it up exactly as it
    # does for a vector source. Building it portrait instead would hand
    # rotate_pdf_90 an already-correct page and leave it transposed.
    raster_source = is_raster_source(pdf_bytes)
    if raster_source:
        pdf_bytes = (image_to_pdf(pdf_bytes, height_m, width_m) if banderoll
                     else image_to_pdf(pdf_bytes, width_m, height_m))

    if banderoll:
        pdf_bytes = rotate_pdf_90(pdf_bytes)

    # Mirror ruta.py logic: ENABLE_COLOR_LABELS=False means always skip.
    #
    # raster_source is an UNCONDITIONAL third reason to skip, overriding whatever
    # the request asked for. Colour labeling is a vector-design feature: the map
    # describes the discrete paints a design was drawn with, and a raster upload
    # has no such guarantee — it is pixels, which may be photographic, resampled,
    # or JPEG-ringed, and any of those can drift a region into a mapped colour's
    # tolerance band by accident. Note this is NOT self-enforcing: the labeler
    # (_label_colors_on_page) reads the RENDERED page, so unlike the vector-only
    # extract_pdf_colors it happily labels raster artwork unless stopped here.
    # It was doing exactly that before this gate existed.
    #
    # Forcing it at this level covers every strip and page of the job in one
    # decision. There is no mixed vector/raster job to worry about: is_raster_source
    # tests the whole upload, image_to_pdf replaces pdf_bytes wholesale with a
    # SINGLE-page PDF, so a job is entirely one or entirely the other.
    effective_skip = skip_colors or raster_source or (not ENABLE_COLOR_LABELS)
    unknown_colors = []

    # The map is ALWAYS handed to the slicer, even when labels are suppressed.
    # effective_skip means "print no NCS codes", not "forget what the colours
    # mean": the cut-boundary scan (pass A) still needs the map to recognise the
    # design's "Skip" background and place Klipp where the artwork actually ends.
    # This used to be `color_map = {}`, which silently downgraded every
    # skip_colors run to the legacy pink/orange fallback — geometric-only cut
    # detection, blind to any design whose background is a different Skip colour.
    color_map = colour_map or {}

    # Unknown-colour detection is meaningful only where labels are actually drawn,
    # so it follows effective_skip exactly — which now folds in raster_source.
    #
    # For a raster job that means unknown_colors stays [] for two independent and
    # mutually reinforcing reasons: nothing is labeled at all (the gate above), and
    # there would be nothing to inspect anyway, since extract_pdf_colors reads
    # VECTOR fills via get_drawings and a raster page has none. Verified: the same
    # two-colour design with one unmapped colour reports ['#FF00FF'] as a PDF and
    # [] as a PNG.
    #
    # [] is the right value to return (the alternative, reporting every distinct
    # pixel value, would be thousands of useless entries on a photo), but callers
    # must be able to tell "nothing unknown" from "not checked" — otherwise they
    # report a clean bill of health for a design that may well ship unlabeled
    # patches, which is exactly the silent failure TIF-60 was raised for. Hence
    # this flag, decided here rather than re-derived by each caller.
    colors_analyzed = not effective_skip

    if colors_analyzed:
        hex_colors = extract_pdf_colors(pdf_bytes)
        # TIF-60: unknown colors are REPORTED, never a reason to discard the map.
        # The old code set color_map = {} whenever any unknown was found, so a
        # single unmapped accent shipped the whole design with zero labels —
        # silently, since nothing logged or persisted unknown_colors. Unmapped
        # colors are simply left unlabeled by _code_masks (they fall outside
        # every tolerance band); every mapped color still gets its label.
        unknown_colors = find_unknown_colors(hex_colors, color_map)

    strips_raw = _slice_pdf(pdf_bytes, width_m, height_m, color_map,
                            ruta_nedre=ruta_nedre, skip_labels=effective_skip,
                            page_height_m=page_height_m)

    strips = [
        {"filename": f"strip-{strip_num:02d}.pdf", "bytes": strip_bytes}
        for strip_num, strip_bytes in strips_raw
    ]

    grid_bytes = generate_grid_pdf(pdf_bytes, width_m, height_m,
                                   ruta_nedre=ruta_nedre,
                                   page_height_m=page_height_m)

    return {"strips": strips, "unknown_colors": unknown_colors,
            "grid_pdf": grid_bytes, "colors_analyzed": colors_analyzed}
