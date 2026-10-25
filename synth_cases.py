"""
synth_cases.py — the synthetic test cases for the ruta pipeline (TIF-88), as data.

Adding an edge case is a few lines here: a Design (what goes in), a job (how the
real slicer is driven), an expect dict (what must come out) and a `why` naming the
bug or ticket it protects. _validate_synthetic.py runs them, applies the global
invariants to every one, checks fixture determinism and enforces the time budget.

RULES FOR CASES
  - Phrase positions and expectations against slicer's constants, never literals:
    `P - slicer.KLIPP_MIN_PINK_PT` keeps testing the gate if the gate is retuned;
    a hardcoded 101.4 silently stops.
  - Boundaries that a case asserts exactly are aligned to the content scan's raster
    (synth.grid_floor / align()) so the scan resolves them exactly.
  - Colours are explicit hex. Real values from shipped designs where it matters.
  - A case that exposes a REAL slicer bug gets xfail="<reason>" — it is not fixed
    in the test PR. An xfail that starts passing fails the run (stale xfail).

expect keys (all optional; see _validate_synthetic.evaluate):
  strips, pages, cut, cut_tol, text, text_size, pads, placeholder, labels,
  skipped_small, forced, unknown, colors_analyzed, page_w, page_h, pnums,
  stdout_has, stdout_lacks
"""
import math
from dataclasses import dataclass, field
from typing import Callable, Optional

import fitz
import numpy as np

import probe
import slicer
import synth
from synth import Design, Raster, Banderoll, grid_floor, grid_ceil


@dataclass
class Case:
    id: str
    why: str
    design: Optional[Design] = None
    job: dict = field(default_factory=dict)
    expect: dict = field(default_factory=dict)
    patch: dict = field(default_factory=dict)
    check: Optional[Callable] = None     # check(outcome) -> [failure strings]
    xfail: Optional[str] = None          # a real finding, deliberately not fixed here
    xfail_on: Optional[str] = None       # ...and the failure text that IS the finding

    def __post_init__(self):
        if not (isinstance(self.why, str) and self.why.strip()):
            raise ValueError(f"Case {self.id!r}: `why` is required — name the bug "
                             f"or ticket this case protects")
        if self.design is None and self.check is None:
            raise ValueError(f"Case {self.id!r}: needs a design or a check")
        if self.xfail and not self.xfail_on:
            raise ValueError(f"Case {self.id!r}: an xfail must say which failure is the "
                             f"finding (xfail_on), so it cannot pass for another reason")


# ── colours (explicit RGB; real values from shipped designs where noted) ─────
SKIP_PINK = "#EEA8CB"      # pest-mitten's real Skip background (inside PINK_*)
BLACK     = "#000000"      # unmapped content
WHITE     = "#FFFFFF"
ORANGE    = "#F7931D"      # ENAD's real orange background (inside ORANGE_*)
LILAC     = "#F9CDE7"      # the LILAC_* band's colour (TIF-92)
PAD_PINK  = "#F490B5"      # PINK_PAD as hex
BLUE_SKIP = "#9EC3E6"      # a Skip background OUTSIDE every legacy band: only the
                           # colour map can tell the content scan it is background
GREEN     = "#1E7534"      # 3560 in the real map
GREY      = "#D4D1CD"      # 1500 in the real map

SKIP_MAP  = {SKIP_PINK: "Skip"}
P         = synth.PPM * slicer.PAGE_HEIGHT_M        # one default 4 m page, in pt
SW        = synth.PPM * slicer.STRIP_WIDTH_M        # one strip, in pt


# ── the TIF-57/67 spec, as arithmetic ────────────────────────────────────────

def align(d, b, nedre):
    """Move a sequence boundary onto the content scan's pixel grid (<= b)."""
    return grid_floor(b) if nedre else d.full_h - grid_ceil(d.full_h - b)


def spec_cut(b, page_h, npages, margin=None, gate=None):
    """Where the Klipp line belongs for content ending (grid-aligned) at sequence
    position b: (page_num, cut_x) or None. Half a scan pixel back from the
    boundary decides the page (a boundary on a seam belongs to the page it sits
    on); total pink <= gate suppresses everything; the line sits `margin` in."""
    margin = slicer.KLIPP_LINE_MARGIN_PT if margin is None else margin
    gate = slicer.KLIPP_MIN_PINK_PT if gate is None else gate
    half_px = 0.5 / slicer.LABEL_RENDER_SCALE
    pn = min(max(int((b - half_px) // page_h), 0), npages - 1)
    bstar = b - pn * page_h
    if page_h - bstar <= gate:
        return None
    return pn, bstar + margin


def klipp_w(fs):
    return fitz.get_text_length("Klipp", fontsize=fs)


def right_limit(page_w, fscale=1.0):
    return page_w - (slicer.PAGE_NUM_EXCL_W + slicer.KLIPP_TEXT_PAD_PT) * fscale


def spec_text(cut_x, page_w):
    """The TIF-67 text ladder: True iff 'Klipp' prints right of a line at cut_x —
    room >= KLIPP_TEXT_MIN_ROOM_PT, and it fits at >= KLIPP_MIN_FONT_SIZE_PT."""
    rl = right_limit(page_w)
    if rl - cut_x < slicer.KLIPP_TEXT_MIN_ROOM_PT:
        return False
    return klipp_w(slicer.KLIPP_MIN_FONT_SIZE_PT) <= rl - cut_x - slicer.KLIPP_TEXT_GAP_PT


def klipp_design(height_m, b, nedre, bg=SKIP_PINK, fg=BLACK, width_m=1.5):
    """bg everywhere, fg content over printing sequence [0, b]."""
    return Design(width_m, height_m).background(bg).content_seq(b, fg, nedre)


def mode(nedre):
    return "nedre" if nedre else "default"


CASES = []


def add(*cases):
    CASES.extend(cases)


# ════════════════════════════════════════════════════════════════════════════
# A. Klipp: content boundary, margin, the unified gate (TIF-57, TIF-67)
# ════════════════════════════════════════════════════════════════════════════

for nedre in (True, False):
    d = klipp_design(4.0, 0, nedre)
    b = align(d, P / 2, nedre)
    add(Case(
        f"klipp/mid-page/{mode(nedre)}",
        "TIF-57/67: content ending mid-page draws ONE line KLIPP_LINE_MARGIN_PT into "
        "the pink, text to its right at the default size",
        klipp_design(4.0, b, nedre),
        job=dict(nedre=nedre, cmap=SKIP_MAP),
        expect=dict(pages=1, cut=spec_cut(b, P, 1), text=True,
                    text_size=(slicer.KLIPP_FONT_DEFAULT, slicer.KLIPP_FONT_DEFAULT),
                    pads=0)))

# Margin toggles: absolute positions, no raster guesswork (b is exact).
b = grid_floor(P / 2)
for m in (0.0, 2.0):
    add(Case(
        f"klipp/margin-toggle/{m:g}pt",
        "TIF-67 (reopened 2 -> 4 pt): the drawn line moves with KLIPP_LINE_MARGIN_PT "
        "and nothing else — toggled, the line lands exactly at b* + margin",
        klipp_design(4.0, b, True),
        job=dict(nedre=True, cmap=SKIP_MAP),
        patch={"KLIPP_LINE_MARGIN_PT": m},
        expect=dict(cut=spec_cut(b, P, 1, margin=m))))

# The unified gate: total pink just over / just under KLIPP_MIN_PINK_PT.
over = grid_floor(P - slicer.KLIPP_MIN_PINK_PT - 1e-6)      # pink in (12, 12.5]
under = grid_ceil(P - slicer.KLIPP_MIN_PINK_PT + 1e-6)      # pink in [11.5, 12)
add(Case("klipp/gate/pink-just-over",
         "TIF-67: total pink just over KLIPP_MIN_PINK_PT draws the line (text skipped: "
         "no room right of it)",
         klipp_design(4.0, over, True), job=dict(nedre=True, cmap=SKIP_MAP),
         expect=dict(cut=spec_cut(over, P, 1), text=False)),
    Case("klipp/gate/pink-just-under",
         "TIF-67: total pink just under KLIPP_MIN_PINK_PT suppresses line AND text",
         klipp_design(4.0, under, True), job=dict(nedre=True, cmap=SKIP_MAP),
         expect=dict(cut=None, text=False)))

# Gate-toggle proofs ported from _validate_klipp_margin (2 and 2b): the GATE is what
# suppresses, not something else — off/old value draws, real value suppresses.
for pink, toggled, label in ((5.0, 0.0, "5pt-gate-off"), (9.0, 6.0, "9pt-old-6pt-gate")):
    b = grid_floor(P - pink)

    # The fixture must sit between the two gates or it proves nothing. The real gate
    # is bound HERE: checks run under the case's patch, where it reads as `toggled`.
    def in_band(o, b=b, toggled=toggled, gate=slicer.KLIPP_MIN_PINK_PT):
        got = P - b
        return [] if toggled < got <= gate else [
            f"fixture pink {got:.2f}pt not in ({toggled}, {gate}]"]

    add(Case(f"klipp/gate/{label}/toggled",
             f"TIF-67: ~{pink:g}pt of pink with the gate at {toggled:g}pt still draws "
             "the line (the pre-change behaviour this gate replaced)",
             klipp_design(4.0, b, True), job=dict(nedre=True, cmap=SKIP_MAP),
             patch={"KLIPP_MIN_PINK_PT": toggled},
             expect=dict(cut=spec_cut(b, P, 1, gate=toggled), text=False),
             check=in_band),
        Case(f"klipp/gate/{label}/real",
             f"TIF-67: the same ~{pink:g}pt of pink under the real gate draws nothing",
             klipp_design(4.0, b, True), job=dict(nedre=True, cmap=SKIP_MAP),
             expect=dict(cut=None, text=False)))


# Text ladder: width gate (KLIPP_TEXT_MIN_ROOM_PT) and font floor
# (KLIPP_MIN_FONT_SIZE_PT), each just over / just under. b is solved from the
# constants; a precondition re-measures where the fixture really landed.
def _b_for_avail(avail):
    """b such that avail (= right_limit - cut_x - gap) is just >= the target."""
    cut_x = right_limit(P) - slicer.KLIPP_TEXT_GAP_PT - avail
    return grid_floor(cut_x - slicer.KLIPP_LINE_MARGIN_PT)


def _b_for_room(room):
    return grid_floor(right_limit(P) - room - slicer.KLIPP_LINE_MARGIN_PT)


def _band(lo, hi, what, b):
    def chk(o):
        cx = b + slicer.KLIPP_LINE_MARGIN_PT
        v = right_limit(P) - cx - (slicer.KLIPP_TEXT_GAP_PT if what == "avail" else 0)
        return [] if lo <= v < hi else [f"fixture landed {what}={v:.3f}, outside the "
                                        f"intended [{lo:.3f}, {hi:.3f}) — it no longer "
                                        f"tests the threshold"]
    return chk


w_floor = klipp_w(slicer.KLIPP_MIN_FONT_SIZE_PT)
w_next = klipp_w(round(slicer.KLIPP_FONT_DEFAULT * slicer.LABEL_FONT_SHRINK, 3))
b_fits = _b_for_avail(w_floor)            # avail in [w_floor, w_floor + 0.5)
b_miss = _b_for_avail(w_floor - 0.5)      # avail in [w_floor - 0.5, w_floor)
add(Case("klipp/text/font-floor-fits",
         "TIF-67 rung 3: room for 'Klipp' at exactly the font floor -> drawn AT the floor",
         klipp_design(4.0, b_fits, True), job=dict(nedre=True, cmap=SKIP_MAP),
         expect=dict(cut=spec_cut(b_fits, P, 1), text=True,
                     text_size=(slicer.KLIPP_MIN_FONT_SIZE_PT, slicer.KLIPP_MIN_FONT_SIZE_PT)),
         check=_band(w_floor, min(w_next, w_floor + 0.5), "avail", b_fits)),
    Case("klipp/text/font-floor-misses",
         "TIF-67 rung 3: a hair less room -> text SKIPPED (never shrunk below the "
         "floor, never moved left); the line still draws",
         klipp_design(4.0, b_miss, True), job=dict(nedre=True, cmap=SKIP_MAP),
         expect=dict(cut=spec_cut(b_miss, P, 1), text=False),
         check=_band(w_floor - 0.5, w_floor, "avail", b_miss)),
    Case("klipp/text/font-floor-misses/floor-0.1",
         "TIF-67 rung 3 proof: same fixture with the floor at the old 0.1pt -> the text "
         "reappears below 4pt, so it is the NEW floor that skips it",
         klipp_design(4.0, b_miss, True), job=dict(nedre=True, cmap=SKIP_MAP),
         patch={"KLIPP_MIN_FONT_SIZE_PT": 0.1},
         expect=dict(text=True, text_size=(0.1, slicer.KLIPP_MIN_FONT_SIZE_PT - 1e-6))))

b_room_ok = _b_for_room(slicer.KLIPP_TEXT_MIN_ROOM_PT)          # room in [4, 4.5)
b_room_no = _b_for_room(slicer.KLIPP_TEXT_MIN_ROOM_PT - 0.5)    # room in [3.5, 4)
for b, ok, tag in ((b_room_ok, True, "just-over"), (b_room_no, False, "just-under")):
    add(Case(f"klipp/text/width-gate-{tag}",
             "TIF-67 rung 2: with the font floor out of the way (0.1pt), text appears "
             "iff the room right of the line reaches KLIPP_TEXT_MIN_ROOM_PT",
             klipp_design(4.0, b, True), job=dict(nedre=True, cmap=SKIP_MAP),
             patch={"KLIPP_MIN_FONT_SIZE_PT": 0.1},
             expect=dict(cut=spec_cut(b, P, 1), text=ok),
             check=_band(slicer.KLIPP_TEXT_MIN_ROOM_PT - (0 if ok else 0.5),
                         slicer.KLIPP_TEXT_MIN_ROOM_PT + (0.5 if ok else 0), "room", b)))

# No-left sweep (ported from _validate_klipp_margin 4): every position from mid-page
# to near-full; the global invariants check right-of-line on each, the spec the rest.
for frac in (0.3, 0.6, 0.8, 0.88, 0.92, 0.95, 0.97, 0.99):
    b = grid_floor(P * frac)
    cut = spec_cut(b, P, 1)
    add(Case(f"klipp/sweep/{frac:.2f}",
             "TIF-67 hard rule: across the whole range the text is NEVER left of the "
             "line, and line/text follow the three-rung ladder",
             klipp_design(4.0, b, True), job=dict(nedre=True, cmap=SKIP_MAP),
             expect=dict(cut=cut, text=bool(cut) and spec_text(cut[1], P))))


# ════════════════════════════════════════════════════════════════════════════
# B. Content ending exactly at / just before / just after a page boundary
#    (TIF-57/67), incl. the boundary landing on an EXCLUDED page (added in review)
# ════════════════════════════════════════════════════════════════════════════

for nedre in (True, False):
    d = klipp_design(8.0, 0, nedre)
    seam = P
    add(Case(f"seam/exact/{mode(nedre)}",
             "TIF-57/67: content ending exactly on a page seam leaves ~0 pink on its page "
             "-> no marking; the empty page after it is excluded (TIF-68)",
             klipp_design(8.0, seam, nedre), job=dict(nedre=nedre, cmap=SKIP_MAP),
             expect=dict(pages=1, cut=None, pnums=[["1"]])),
        Case(f"seam/just-before/{mode(nedre)}",
             "TIF-57/67: half a pixel short of the seam is still under the gate",
             klipp_design(8.0, align(d, seam - 0.5, nedre), nedre),
             job=dict(nedre=nedre, cmap=SKIP_MAP),
             expect=dict(pages=1, cut=None)))
    b = align(d, seam + 8.0, nedre)
    add(Case(f"seam/after-kept/{mode(nedre)}",
             "TIF-57: content running ~8pt past the seam is marked on page 2, not page 1",
             klipp_design(8.0, b, nedre), job=dict(nedre=nedre, cmap=SKIP_MAP),
             expect=dict(pages=2, cut=spec_cut(b, P, 2), text=True)))


def _excluded_boundary(nedre):
    d = klipp_design(8.0, 0, nedre)
    b = align(d, P + 1.0, nedre)

    def chk(o):
        raw = probe.find_cut(o.pdf, o.design, 0, nedre, SKIP_MAP)
        want = spec_cut(b, P, 2)
        if raw is None or raw[0] != 1 or abs(raw[1] - want[1]) > 0.01:
            return [f"content scan itself is wrong: _find_cut_boundary={raw}, spec {want}"]
        return []

    return Case(f"seam/after-on-excluded-page/{mode(nedre)}",
                "TIF-68 x TIF-57 (review): content ends ~1pt into page 2, which "
                "is_fully_background drops. The scan must still find page 2 (checked "
                "directly), no line may be derived from the unprinted page, and the "
                "slicer must SAY so",
                klipp_design(8.0, b, nedre), job=dict(nedre=nedre, cmap=SKIP_MAP),
                expect=dict(pages=1, cut=None,
                            stdout_has=["is_fully_background() excluded"]),
                check=chk)


add(_excluded_boundary(True), _excluded_boundary(False))


# ════════════════════════════════════════════════════════════════════════════
# C. Geometric partial pages (TIF-57), ported from _validate_klipp_partial at
#    TRUE scale. Empty map: the legacy fallback, content runs to the clip edge.
# ════════════════════════════════════════════════════════════════════════════

def partial_design(pad_pt, pages=1):
    """Black content to the design edge; the last page short by pad_pt."""
    h_m = (pages * P - pad_pt) / synth.PPM
    return Design(1.5, h_m).background(BLACK)


for nedre in (True, False):
    for pad, line, tag in ((slicer.KLIPP_MIN_PINK_PT + 1.5, True, "pad-over-gate"),
                           (slicer.KLIPP_MIN_PINK_PT - 1.5, False, "pad-under-gate"),
                           (P * 0.5, True, "half"),
                           (P * 0.75, True, "wide")):
        d = partial_design(pad)
        # The scan trims the render's 1px border, so with content to the edge b*
        # sits one scan pixel inside the clip edge (_find_cut_boundary docstring).
        bstar = d.full_h - 1.0 / slicer.LABEL_RENDER_SCALE
        cut = (0, bstar + slicer.KLIPP_LINE_MARGIN_PT) if line else None
        add(Case(f"partial/{tag}/{mode(nedre)}",
                 "TIF-57: a geometric partial page always gets the PINK_PAD; the line "
                 "follows the TIF-67 gate on the TOTAL pink (pad + the 1px trim). "
                 "Real-scale port of _validate_klipp_partial, whose doctored dims made "
                 "every 'near-full' pad ~14.5pt",
                 d, job=dict(nedre=nedre, cmap={}),
                 expect=dict(pages=1, pads=1, cut=cut, cut_tol=0.6)))

# Partial page whose content does NOT reach the clip edge: the content-driven
# trigger and the geometric pad on the same page, independently.
d = Design(1.5, 6.0).background(SKIP_PINK)
b = grid_floor(P + 30.0)
d.content_seq(b, BLACK, True)
add(Case("partial/content-ends-before-pad",
         "TIF-57: pad (geometric) and cut (content) are independent — both on page 2",
         d, job=dict(nedre=True, cmap=SKIP_MAP),
         expect=dict(pages=2, pads=1, cut=spec_cut(b, P, 2), text=True)))


# ════════════════════════════════════════════════════════════════════════════
# D. Background page exclusion (TIF-68, lilac TIF-92), ported from
#    _validate_bg_page_exclusion's synthetic half, plus orange and default mode
# ════════════════════════════════════════════════════════════════════════════

for bg in (SKIP_PINK, ORANGE, LILAC):
    for nedre in (True, False):
        d = Design(1.5, 12.0).background(bg).content_seq(2 * P, BLACK, nedre)
        add(Case(f"bg/trailing-full/{bg}/{mode(nedre)}",
                 f"TIF-68/92: a trailing 100% {bg} page is never rendered — the strip "
                 "stops at the last content page",
                 d, job=dict(nedre=nedre, cmap={}),
                 expect=dict(pages=2, pnums=[["1", "2"]])))
    # ~10% content sliver on an otherwise background page MUST be kept.
    d = Design(1.5, 8.0).background(bg).content_seq(P * 1.1, BLACK, True)
    add(Case(f"bg/sliver-kept/{bg}",
             f"TIF-68 over-exclusion guard: a {bg} page with a ~10% content sliver renders",
             d, job=dict(nedre=True, cmap={}), expect=dict(pages=2)))

# A LEADING background page (default mode: page 1 is the design bottom) is dropped
# too, and numbering is preserved: the first output page is "2".
d = Design(1.5, 8.0).background(SKIP_PINK).band(0, P, BLACK)
add(Case("bg/leading-full/default",
         "TIF-68: exclusion is not only trailing — page numbers are PRESERVED, so the "
         "first shipped page of this strip is numbered 2",
         d, job=dict(nedre=False, cmap={}), expect=dict(pages=1, pnums=[["2"]])))


def _band_disjointness(o):
    """1e, exact over the whole RGB cube but cheap: each band is an axis-aligned box
    (plus r>g / r>b guards that only shrink it). Two bands can share a colour only
    inside the intersection of their boxes, so only that is enumerated — and an
    empty intersection proves disjointness outright."""
    fails = []
    boxes = {
        "pink":   [(slicer.PINK_R_MIN + 1, 255), (slicer.PINK_G_MIN + 1, slicer.PINK_G_MAX - 1),
                   (slicer.PINK_B_MIN + 1, slicer.PINK_B_MAX - 1)],
        "orange": [(slicer.ORANGE_R_MIN + 1, 255),
                   (slicer.ORANGE_G_MIN + 1, slicer.ORANGE_G_MAX - 1),
                   (0, slicer.ORANGE_B_MAX - 1)],
        "lilac":  [(slicer.LILAC_R_MIN + 1, 255), (slicer.LILAC_G_MIN + 1, slicer.LILAC_G_MAX - 1),
                   (slicer.LILAC_B_MIN + 1, slicer.LILAC_B_MAX - 1)],
    }
    for a, b in (("pink", "lilac"), ("orange", "lilac"), ("pink", "orange")):
        lo = [max(boxes[a][i][0], boxes[b][i][0]) for i in range(3)]
        hi = [min(boxes[a][i][1], boxes[b][i][1]) for i in range(3)]
        if any(l > h for l, h in zip(lo, hi)):
            continue                                   # boxes disjoint: proven
        axes = [np.arange(lo[i], hi[i] + 1) for i in range(3)]
        cube = np.stack(np.meshgrid(*axes, indexing="ij"), axis=-1).astype(np.int32)
        m = probe.band_masks(cube)
        n = int((m[a] & m[b]).sum())
        if n:
            fails.append(f"bands {a} and {b} overlap on {n} RGB triples — re-derive "
                         f"the separation")
    for hx in (PAD_PINK, SKIP_PINK, "#EEA8CA"):
        px = np.array([[[int(hx[i:i + 2], 16) for i in (1, 3, 5)]]], dtype=np.int32)
        m = probe.band_masks(px)
        if not m["pink"][0, 0] or m["lilac"][0, 0] or m["orange"][0, 0]:
            fails.append(f"{hx}: expected pink-only, got "
                         f"{ {k: bool(x[0, 0]) for k, x in m.items()} }")
    return fails


add(Case("bg/bands-disjoint",
         "TIF-92: the LILAC_* band is provably disjoint from PINK_* and ORANGE_*; the "
         "real pinks stay pink-only (ported 1e, now by box intersection)",
         check=_band_disjointness))


# ════════════════════════════════════════════════════════════════════════════
# E. Strip where every page is background — the zero-page crash (TIF-72 / TIF-87)
# ════════════════════════════════════════════════════════════════════════════

for ph in (1.0, 2.0, 4.0, 8.0):
    d = Design(3.0, 8.0).background(SKIP_PINK).band(0, 8.0 * synth.PPM, BLACK, 0, SW / 2)
    add(Case(f"zero-page/all-bg-strip/ph{ph:g}",
             "TIF-72/87: a strip whose every page is background yields ONE numbered "
             "PINK_PAD placeholder page (not a crash), invariant to page height",
             d, job=dict(via="job", colour_map=SKIP_MAP, skip_colors=True,
                         page_height_m=ph),
             expect=dict(strips=2, placeholder={1}, pnums={1: ["1"]},
                         page_w=ph * synth.PPM)))

d = Design(1.5, 8.0).background(ORANGE)
add(Case("zero-page/all-orange",
         "TIF-87: the placeholder is PINK_PAD even on an orange-background design "
         "(documented: it is 'nothing here', not artwork)",
         d, job=dict(nedre=False, cmap={}), expect=dict(placeholder={0})))


# ════════════════════════════════════════════════════════════════════════════
# F. Colour labelling: strip seams, size gate, tolerance, density (TIF-27/55/69)
# ════════════════════════════════════════════════════════════════════════════

LABEL_MAP = {SKIP_PINK: "Skip", GREEN: "3560", GREY: "1500"}


def label_design(width_m=1.5, height_m=4.0):
    """Skip-pink ground plus a black content anchor (H2) over the first third."""
    return Design(width_m, height_m).background(SKIP_PINK).anchor(BLACK, height=P / 3)


d = label_design(3.0)
d.patch(SW - 10, P * 0.6, 20, 20, GREEN)     # centred on the strip seam
add(Case("labels/straddles-strip-seam",
         "TIF-27: labelling is per strip page, never across a slice boundary — a patch "
         "split by the seam is labelled once in EACH strip",
         d, job=dict(via="job", colour_map=LABEL_MAP, ruta_nedre=True),
         expect=dict(strips=2, labels={"3560": 2})))

# MIN_LABEL_PATCH_SIZE_PT: a long bar whose thickness is just under / at the size
# that gives an inscribed radius of MIN_LABEL_PATCH_SIZE_PT. Thin along design y so
# it is thin along output x on page 1 under ruta_nedre (y = seq there), where the
# scan grid and the design grid coincide.
r_px = slicer.MIN_LABEL_PATCH_SIZE_PT * slicer.LABEL_RENDER_SCALE   # radius in px
for px, labelled in ((int(2 * r_px) - 1, True), (int(2 * r_px) - 2, False)):
    t = px / slicer.LABEL_RENDER_SCALE                # bar thickness in pt
    d = label_design()
    d.patch(5.0, grid_floor(P * 0.6), 30.0, t, GREEN)
    add(Case(f"labels/size-gate/{t:g}pt-bar",
             f"TIF-69: a {px}px-thick bar has inscribed radius "
             f"{'>=' if labelled else '<'} MIN_LABEL_PATCH_SIZE_PT -> "
             f"{'labelled' if labelled else 'skipped as dust'} (stroke width=0, H1)",
             d, job=dict(nedre=True, cmap=LABEL_MAP, skip_labels=False),
             expect=dict(pages=1, labels={"3560": 1 if labelled else 0},
                         skipped_small=0 if labelled else 1)))
d = label_design()
d.patch(5.0, grid_floor(P * 0.6), 30.0, (int(2 * r_px) - 2) / slicer.LABEL_RENDER_SCALE, GREEN)
add(Case("labels/size-gate/toggled",
         "TIF-69 proof: the sub-gate bar IS labelled with the gate halved — the gate, "
         "not the dust filter, is what skipped it",
         d, job=dict(nedre=True, cmap=LABEL_MAP, skip_labels=False),
         patch={"MIN_LABEL_PATCH_SIZE_PT": slicer.MIN_LABEL_PATCH_SIZE_PT / 2},
         expect=dict(labels={"3560": 1}, skipped_small=0)))

# COLOR_MATCH_TOLERANCE: a fill exactly at / one unit past the tolerance from GREEN.
g = np.array([int(GREEN[i:i + 2], 16) for i in (1, 3, 5)])
for dist, inside in ((slicer.COLOR_MATCH_TOLERANCE, True),
                     (slicer.COLOR_MATCH_TOLERANCE + 1, False)):
    hx = "#{:02X}{:02X}{:02X}".format(g[0] + dist, g[1], g[2])
    d = label_design()
    d.patch(8.0, P * 0.5, 25.0, 25.0, hx)
    add(Case(f"labels/tolerance/{dist}",
             f"TIF-55: a fill {dist} RGB units from a mapped colour is "
             f"{'matched (labelled, not unknown)' if inside else 'unmatched (unlabelled, reported unknown)'}",
             d, job=dict(via="job", colour_map=LABEL_MAP, ruta_nedre=True),
             expect=dict(labels={"3560": 1 if inside else 0},
                         unknown=[BLACK] if inside else [hx, BLACK],   # BLACK: the anchor
                         colors_analyzed=True)))

# Two mapped colours closer than 2x tolerance: a fill between them goes to the
# NEAREST, once — never both (the round-4 double-label fix).
A, Bc = "#1E7534", "#2A7F3E"          # 16.5 apart: bands overlap heavily
mid = "#257A39"                       # nearer B (≈7.0) than A (≈9.6)
d = label_design()
d.patch(8.0, P * 0.5, 25.0, 25.0, mid)
add(Case("labels/tolerance/nearest-of-two",
         "TIF-27 round 4 / TIF-55: overlapping tolerance bands resolve to the NEAREST "
         "mapped colour, one label, never a double",
         d, job=dict(nedre=True, cmap={SKIP_PINK: "Skip", A: "AAAA", Bc: "BBBB"},
                     skip_labels=False),
         expect=dict(labels={"BBBB": 1})))

# Dense clusters: collision-aware placement must keep the zero-skip contract and
# never overlap fitted labels (global invariants), at rising density.
# Smallest side 2.0pt: 4x4 scan px = 16 >= MIN_PATCH_PX with an inscribed radius of
# exactly MIN_LABEL_PATCH_SIZE_PT; anything smaller is dust by design, not density.
for k, side, gap in ((2, 4.0, 1.0), (4, 2.5, 0.5), (6, 2.0, 0.5)):
    n = k * k
    d = label_design()
    d.patch_grid(4.0, grid_floor(P * 0.45), k, k, side, gap, GREEN)
    add(Case(f"labels/dense/{n}x{side:g}pt",
             "TIF-27 rounds 3-5: dense small-patch clusters — every labelable patch gets "
             "exactly one label, fitted labels never collide",
             d, job=dict(nedre=True, cmap=LABEL_MAP, skip_labels=False),
             expect=dict(pages=1, labels={"3560": n})))


# ════════════════════════════════════════════════════════════════════════════
# G. Unknown colours + independent Skip entries (TIF-60), ported from
#    _validate_unknown_colors at 1:100
# ════════════════════════════════════════════════════════════════════════════

MAPPED_EXACT, DRIFT, MAPPED_EXACT2 = "#1E7534", "#737272", "#D4D1CD"
UNKNOWN, SKIP_PINK_A, SKIP_ORANGE = "#00B7EB", "#EEA8CA", "#FF7E00"
BASE_MAP = {"#1E7534": "3560", "#737271": "6000", "#D4D1CD": "1500"}
PINK_SKIP_E, ORANGE_SKIP_E = {"#EEA8CB": "Skip"}, {"#FF7E00": "Skip"}

d60 = Design(1.5, 4.0)   # default white page: get_drawings sees exactly the 6 fills
for i, hx in enumerate([MAPPED_EXACT, DRIFT, MAPPED_EXACT2, UNKNOWN, SKIP_PINK_A, SKIP_ORANGE]):
    row, col = divmod(i, 2)
    d60.patch(2 + col * 20, 3 + row * 37, 18, 33, hx)


def _fills_are_patches(o):
    found = slicer.extract_pdf_colors(o.pdf)
    return [] if found == d60.fills() else [f"FIXTURE BROKEN: fills {sorted(found)}"]


for tag, cmap, unknown in (
        ("A-unmapped-must-not-suppress", BASE_MAP, [UNKNOWN, SKIP_PINK_A, SKIP_ORANGE]),
        ("B-two-skips-coexist", {**BASE_MAP, **PINK_SKIP_E, **ORANGE_SKIP_E}, [UNKNOWN]),
        ("C1-pink-skip-only", {**BASE_MAP, **PINK_SKIP_E}, [UNKNOWN, SKIP_ORANGE]),
        ("C2-orange-skip-only", {**BASE_MAP, **ORANGE_SKIP_E}, [UNKNOWN, SKIP_PINK_A])):
    add(Case(f"unknown/{tag}",
             "TIF-60: unmapped colours are REPORTED and left unlabelled individually; "
             "they never wipe the map. Skip entries are known, never labelled, and "
             "independent of each other. Tolerance-drifted fills are not 'unknown'",
             d60, job=dict(via="job", colour_map=dict(cmap)),
             expect=dict(unknown=sorted(unknown), labels={"3560": 1, "6000": 1, "1500": 1},
                         colors_analyzed=True),
             check=_fills_are_patches))


# ════════════════════════════════════════════════════════════════════════════
# H. skip_colors suppresses labels ONLY, never the cut scan (TIF-57 follow-up),
#    ported from _validate_label_skip_decoupling — on a Skip colour no legacy band
#    can see, so the map is provably load-bearing
# ════════════════════════════════════════════════════════════════════════════

BLUE_MAP = {BLUE_SKIP: "Skip", GREEN: "3560"}
d = Design(1.5, 8.0).background(BLUE_SKIP)
b = grid_floor(P + 40.0)
d.content_seq(b, GREY, True).patch(8, 20, 20, 20, GREEN)
decouple_cut = spec_cut(b, P, 2)
add(Case("decouple/A-skip-labels-with-map",
         "TIF-57 follow-up: skip_labels=True with the real map — no NCS codes, but the "
         "cut IS found through the map's Skip entry",
         d, job=dict(nedre=True, cmap=BLUE_MAP, skip_labels=True),
         expect=dict(labels={}, cut=decouple_cut)),
    Case("decouple/B-labels-on-same-cut",
         "TIF-57 follow-up: labels on — codes drawn and the cut does not move",
         d, job=dict(nedre=True, cmap=BLUE_MAP, skip_labels=False),
         expect=dict(labels={"3560": 1}, cut=decouple_cut)),
    Case("decouple/C-run-slice-skip-colors",
         "TIF-57 follow-up: through run_slice(skip_colors=True) — the production path "
         "that used to wipe the map — the cut survives",
         d, job=dict(via="job", colour_map=BLUE_MAP, skip_colors=True, ruta_nedre=True),
         expect=dict(labels={}, cut=decouple_cut, colors_analyzed=False)),
    Case("decouple/D-empty-map-is-blind",
         "TIF-57 follow-up: the degradation it guards against, pinned — with the map "
         "wiped, a non-legacy Skip background is content to the edge: no cut",
         d, job=dict(nedre=True, cmap={}, skip_labels=True),
         expect=dict(labels={}, cut=None)))


# ════════════════════════════════════════════════════════════════════════════
# I. Content-driven Klipp on EVEN dimensions through run_slice (TIF-57),
#    ported from _validate_content_cut: no page is ever partial
# ════════════════════════════════════════════════════════════════════════════

d = Design(4.5, 12.0).background(SKIP_PINK)
ends = [grid_floor(2 * P + 50.0), grid_floor(P + 20.0), grid_floor(3 * P)]
for s, e in enumerate(ends):
    d.band(0, e, BLACK, s * SW, (s + 1) * SW)
d.patch(10, 40, 20, 20, GREEN)
add(Case("content-cut/even-dims",
         "TIF-57: even dimensions, artwork ending mid-page inside painted background — "
         "no pad anywhere, at most one marking per strip, each at its content boundary; "
         "content to the end leaves none",
         d, job=dict(via="job", colour_map=LABEL_MAP, ruta_nedre=True),
         expect=dict(strips=3, pads=0, pages=[3, 2, 3],
                     cut={0: spec_cut(ends[0], P, 3), 1: spec_cut(ends[1], P, 3), 2: None},
                     labels={"3560": 1})))


# ════════════════════════════════════════════════════════════════════════════
# J. Non-divisible dimensions: partial strips AND partial pages, independently
# ════════════════════════════════════════════════════════════════════════════

for w_m, h_m in ((3.0, 12.0), (1.5, 10.0), (4.0, 12.0), (4.0, 10.0)):
    ns, npg = math.ceil(w_m / 1.5), math.ceil(h_m / 4.0)
    last_w = (w_m - 1.5 * (ns - 1)) * synth.PPM
    partial_pages = 0 if h_m % 4.0 == 0 else 1
    add(Case(f"dims/{w_m:g}x{h_m:g}",
             "non-divisible dimensions: a partial last STRIP is a narrower page; a "
             "partial last PAGE gets one pad per strip; the two never interact",
             Design(w_m, h_m).background(BLACK),
             job=dict(via="job", colour_map={}, skip_colors=True),
             expect=dict(strips=ns, pages=[npg] * ns, pads=partial_pages * ns,
                         page_w=P,
                         page_h={**{s: SW for s in range(ns - 1)}, ns - 1: last_w},
                         pnums=[[str(i + 1) for i in range(npg)]] * ns)))


# ════════════════════════════════════════════════════════════════════════════
# K. Banderoll x ruta_nedre, 2x2 (added in review)
# ════════════════════════════════════════════════════════════════════════════

HUNG_W, HUNG_H = 3.0, 8.0
bd = Banderoll(HUNG_W, HUNG_H).background(SKIP_PINK)
b_hung = grid_floor(P + 40.0)                       # content: hung top -> 1.35 pages
bd.band(0, b_hung, BLACK)
bd.patch(4, 4, 16, 16, GREEN)                       # hung top-left: strip 1, design top
for banderoll in (False, True):
    for nedre in (False, True):
        src = bd if banderoll else bd.portrait()
        # ruta_nedre: seq = y, content ends at b_hung -> page 2. Default: seq rises
        # from the hung BOTTOM, so the last content in sequence is the design's top
        # edge — content to the end, nothing to cut.
        cut = spec_cut(b_hung, P, 2) if nedre else None
        # The top-left marker is in strip 1 on the page holding the design top.
        top_page = "1" if nedre else "2"

        def chk(o, top_page=top_page):
            st = o.strips[0]
            hit = [pg["pnum"]["text"] for pg in st["pages"]
                   if any(t == "3560" for t, _b, _s in pg["labels"])]
            return [] if hit == [top_page] else [
                f"hung top-left marker labelled on strip 1 page(s) {hit}, expected "
                f"[{top_page}] — orientation/mirroring is wrong"]

        add(Case(f"banderoll/{'B' if banderoll else 'portrait'}x{mode(nedre)}",
                 "TIF-87 axes: banderoll rotation x ruta_nedre. A landscape banderoll "
                 "upload must slice exactly like the same design uploaded portrait",
                 src, job=dict(via="job", colour_map=LABEL_MAP, banderoll=banderoll,
                               ruta_nedre=nedre, width_m=HUNG_W, height_m=HUNG_H),
                 expect=dict(strips=2, pages=[2, 2], cut={0: cut, 1: cut},
                             labels={"3560": 1}, page_w=P, page_h=SW),
                 check=chk))


def _banderoll_matches_portrait(o):
    fails = []
    for nedre in (False, True):
        dig = []
        for src, ban in ((bd, True), (bd.portrait(), False)):
            res, _ = probe.drive_job(src.pdf(), HUNG_W, HUNG_H, banderoll=ban,
                                     ruta_nedre=nedre, colour_map=LABEL_MAP)
            dig.append([probe.render_digest(s["bytes"], 1.0) for s in res["strips"]])
        if dig[0] != dig[1]:
            fails.append(f"nedre={nedre}: banderoll strips render differently from the "
                         f"portrait upload of the same design")
    return fails


add(Case("banderoll/render-identical-to-portrait",
         "TIF-87 axes: rotate_pdf_90 is a pure rotation — banderoll and portrait "
         "uploads of one design produce pixel-identical strips, in both modes",
         check=_banderoll_matches_portrait))


# ════════════════════════════════════════════════════════════════════════════
# L. Skissyta page height (TIF-87), ported from _validate_skissyta
# ════════════════════════════════════════════════════════════════════════════

def _furniture_identity(o):
    fails = []
    for ph in (1.0, 2.0, 3.0, 4.0, 4.5, 5.0, 8.0, 12.0, 100.0):
        if slicer.furniture_scale(ph) != 1.0:
            fails.append(f"furniture_scale({ph}) = {slicer.furniture_scale(ph)!r}, not 1.0")
    for ph in (0.25, 0.5, 0.75):
        if slicer.furniture_scale(ph) != ph:
            fails.append(f"furniture_scale({ph}) not linear below the floor")
    fs = slicer.furniture_scale(slicer.PAGE_HEIGHT_M)
    literals = {"PAGE_NUM_EXCL_W": 13.5, "PAGE_NUM_EXCL_Y0": 4.0, "PAGE_NUM_EXCL_Y1": 11.5,
                "PAGE_NUM_FONT_PT": 6.0, "PAGE_NUM_INSET_PT": 12.0,
                "PAGE_NUM_BASELINE_PT": 10.0, "KLIPP_FONT_DEFAULT": 6.0,
                "KLIPP_MIN_FONT_SIZE_PT": 4.0, "KLIPP_TEXT_GAP_PT": 3.0,
                "KLIPP_TEXT_MIN_ROOM_PT": 4.0, "KLIPP_TEXT_PAD_PT": 2.0,
                "KLIPP_LINE_WIDTH_PT": 1.0, "KLIPP_DASH_PT": 4.0}
    for name, v in literals.items():
        a = getattr(slicer, name)
        if not (a == v and a * fs == v):
            fails.append(f"{name}: {a} * {fs} != literal {v} at the default")
    dash = f"{round(slicer.KLIPP_DASH_PT * fs, 3):g}"
    if f"[{dash} {dash}] 0" != "[4 4] 0":
        fails.append(f"dash pattern re-emits [{dash} {dash}] 0, not [4 4] 0")
    for name, v in (("KLIPP_MIN_PINK_PT", 12.0), ("KLIPP_LINE_MARGIN_PT", 4.0),
                    ("MIN_LABEL_PATCH_SIZE_PT", 1.0), ("LABEL_FONT_DEFAULT", 3.0),
                    ("MIN_PATCH_PX", 10), ("COLOR_MATCH_TOLERANCE", 28)):
        if getattr(slicer, name) != v:
            fails.append(f"physical constant {name} changed: {getattr(slicer, name)!r}")
    return fails


def _fit_floors(o):
    """The measured furniture fit floors the FURNITURE_FIT_FLOOR_M anchor (and the
    web UI's 1.0 m warning) rest on — the page-number zone fits, and "Klipp" fits
    at its floor font — as a page height in metres (slicer.py's furniture block)."""
    ppm = synth.PPM
    floors = {
        "page-number zone": slicer.PAGE_NUM_EXCL_W / ppm,
        "Klipp at floor font":(slicer.PAGE_NUM_EXCL_W + slicer.KLIPP_TEXT_PAD_PT
                                + slicer.KLIPP_TEXT_GAP_PT
                                + klipp_w(slicer.KLIPP_MIN_FONT_SIZE_PT)) / ppm,
    }
    fails = []
    if not floors["page-number zone"] < floors["Klipp at floor font"]:
        fails.append(f"fit floors out of order: {floors}")
    if not 0.90 <= floors["Klipp at floor font"] <= 1.05:
        fails.append(f"Klipp fit floor moved: {floors['Klipp at floor font']:.3f} m "
                     f"(the 1.0 m warn threshold no longer matches)")
    if not floors["Klipp at floor font"] <= slicer.FURNITURE_FIT_FLOOR_M:
        fails.append("FURNITURE_FIT_FLOOR_M sits below the measured Klipp fit floor")
    return fails


add(Case("skissyta/furniture-identity",
         "TIF-87: furniture_scale is EXACTLY 1.0 at/above the floor and every furniture "
         "constant reproduces its literal at the default — byte-identity by construction",
         check=_furniture_identity),
    Case("skissyta/fit-floors",
         "TIF-87: the measured fit floors behind FURNITURE_FIT_FLOOR_M and the 1.0 m "
         "Skissyta warning still hold",
         check=_fit_floors))

# End-to-end geometry at non-default page heights, incl. 1.0, below the floor and
# taller than the design. Content ends mid-design so every height has a cut to find.
for ph in (0.5, 1.0, 2.0, 2.5, 4.0, 5.0, 10.0):
    d = Design(3.0, 8.0).background(SKIP_PINK)
    b = grid_floor(5.3 * synth.PPM)
    # The patch sits inside the first 0.5 m (14.2pt) so no page seam ever splits it.
    d.content_seq(b, BLACK, True).patch(6, 2, 10, 8, GREEN)
    page_w = ph * synth.PPM
    npg = math.ceil(8.0 / ph)
    survivors = sum(1 for k in range(npg) if k * page_w < b - 4.0)  # pages holding content
    cut = spec_cut(b, page_w, npg)
    add(Case(f"skissyta/page-height-{ph:g}m",
             "TIF-87: a non-default Skissyta — pages are ph x 1.5 m, counts follow ceil(), "
             "no text escapes a page, and the Klipp gate stays PHYSICAL (12pt of fabric "
             "whatever the page height)",
             d, job=dict(via="job", colour_map=LABEL_MAP, ruta_nedre=True,
                         page_height_m=ph),
             expect=dict(strips=2, page_w=page_w, page_h=SW,
                         pages=[survivors, survivors], cut={0: cut, 1: cut},
                         labels={"3560": 1})))

# Klipp TEXT at the 1.0 m Skissyta. The line can sit no further left than
# KLIPP_LINE_MARGIN_PT past the content, and the page holding that content must
# survive is_fully_background (a few % content), so b* here is the smallest that
# keeps the page. Is there then ANY room for the text?
d = Design(1.5, 4.0).background(SKIP_PINK)
b = grid_floor(2 * synth.PPM + 3.0)      # content 3pt (~10%) into page 3: kept
d.content_seq(b, BLACK, True)
add(Case("skissyta/1.0m-klipp-text-reachable",
         "TIF-87: the 1.0 m warn threshold is the Klipp-fit floor; at 1.0 m, with the "
         "line as far left as the rules allow, 'Klipp' should still be printable",
         d, job=dict(nedre=True, cmap=SKIP_MAP, page_height_m=1.0),
         expect=dict(pages=3, cut=spec_cut(b, synth.PPM, 4), text=True),
         xfail="FINDING: at a 1.0 m Skissyta the 'Klipp' text can never print. The "
               "0.966 m fit floor (slicer furniture block) assumes the line at x=0, but "
               "it sits >= KLIPP_LINE_MARGIN_PT past real content, and room right of it "
               "must reach gap + width('Klipp' @ KLIPP_MIN_FONT_SIZE_PT)",
         xfail_on="Klipp text absent"))

# No content-bearing strip is emptied by page height (a tall page dilutes a sliver).
for ph in (1.0, 2.0, 4.0, 8.0):
    d = Design(1.5, 8.0).background(SKIP_PINK).content_seq(0.5 * synth.PPM, BLACK, True)
    add(Case(f"skissyta/sliver-not-emptied/ph{ph:g}",
             "TIF-87: a tall page dilutes a content sliver toward FULLY_BG_THRESHOLD — "
             "a 0.5 m sliver must survive at every page height",
             d, job=dict(nedre=True, cmap=SKIP_MAP, page_height_m=ph),
             expect=dict(pages=1)))


# ════════════════════════════════════════════════════════════════════════════
# M. Raster (JPG/PNG) sources (TIF-73), a small-image subset of
#    _validate_raster_source
# ════════════════════════════════════════════════════════════════════════════

RASTER_MAP = {SKIP_PINK: "Skip", GREEN: "3560", GREY: "1500"}


def raster(fmt="png", alpha=False, content_m=6.0, **kw):
    """1.5 x 8 m: mapped artwork over the design's BOTTOM content_m, the top left
    Skip pink (or transparent, alpha=True) — the leftover a real design leaves."""
    r = Raster(1.5, 8.0, fmt=fmt, alpha=alpha, **kw)
    top = (8.0 - content_m) * synth.PPM
    if not alpha:
        r.band(0, top, SKIP_PINK)
    r.band(top, r.full_h, GREY).patch(8, top + 20, 20, 20, GREEN)
    return r


# Default mode: seq rises from the bottom, so content ends at seq = content_m.
raster_cut = (1, 6.0 * synth.PPM - P + slicer.KLIPP_LINE_MARGIN_PT)
for fmt in ("png", "jpg"):
    add(Case(f"raster/{fmt}/klipp-and-no-labels",
             "TIF-73: a raster upload is an ordinary page after image_to_pdf — Klipp "
             "fires off its declared Skip background — but it is NEVER colour-labelled "
             "and unknown_colors is 'not checked', not 'all matched'",
             raster(fmt), job=dict(via="job", colour_map=RASTER_MAP),
             expect=dict(strips=1, pages=2, cut=raster_cut, cut_tol=1.0, labels={},
                         unknown=[], colors_analyzed=False, page_w=P, page_h=SW,
                         pnums=[["1", "2"]])))

add(Case("raster/png-alpha/flattened-to-pink-pad",
         "TIF-73 fix 2: transparency flattens onto PINK_PAD, which the content scan reads "
         "as background — same Klipp as a design that painted that pink itself",
         raster("png", alpha=True), job=dict(via="job", colour_map=RASTER_MAP),
         expect=dict(pages=2, cut=raster_cut, cut_tol=1.0, labels={})),
    Case("raster/trailing-bg-excluded",
         "TIF-73 x TIF-68: a raster page that is all declared-Skip pink is excluded",
         raster("png", content_m=3.0), job=dict(via="job", colour_map=RASTER_MAP),
         expect=dict(pages=1)),
    Case("raster/png/never-labels-under-every-flag",
         "TIF-73 fix 1: no labels under ruta_nedre either (the gate is in run_slice)",
         raster("png"), job=dict(via="job", colour_map=RASTER_MAP, ruta_nedre=True),
         expect=dict(labels={}, colors_analyzed=False)))


def _stretch_geometry(o):
    """image_to_pdf stretches to fill at PTS_PER_M, whatever the image aspect."""
    fails = []
    for px in ((60, 320), (320, 60)):                    # matched-ish and 4:1 wrong
        r = Raster(1.5, 8.0, px=px).band(0, 8.0 * synth.PPM * 0.1, "#FF00FF")
        doc = fitz.open(stream=slicer.image_to_pdf(r.pdf(), 1.5, 8.0), filetype="pdf")
        pg = doc[0]
        if abs(pg.rect.width - 1.5 * synth.PPM) > 0.01 or abs(pg.rect.height - 8 * synth.PPM) > 0.01:
            fails.append(f"{px}: page {pg.rect} not 1.5 x 8 m at PTS_PER_M")
        a = probe.render(pg, 1.0)
        mag = (a[:, :, 0] > 200) & (a[:, :, 1] < 60)
        rows = np.flatnonzero(mag.any(axis=1))
        frac = (rows.max() + 1) / a.shape[0] if rows.size else 0
        if abs(frac - 0.1) > 0.02:
            fails.append(f"{px}: top band covers {frac:.3f} of the page, not 0.10 "
                         f"(letterboxed instead of stretched?)")
        doc.close()
    return fails


add(Case("raster/stretch-to-fill",
         "TIF-73: image_to_pdf lays a raster out at PTS_PER_M and STRETCHES it — "
         "aspect mismatch is absorbed, never letterboxed",
         check=_stretch_geometry))


# ════════════════════════════════════════════════════════════════════════════
# N. Scale (the one place a non-1:100 fixture is allowed, H3)
# ════════════════════════════════════════════════════════════════════════════

d = Design(1.5, 4.0, ppm=100.0).background(SKIP_PINK)
b = grid_floor(4.0 * 100.0 / 2)
d.content_seq(b, BLACK, True)
add(Case("scale/100ppm-constants-are-absolute-pt",
         "H3 / TIF-87: pts-per-metre is DERIVED from the design, and every Klipp "
         "constant is absolute pt — at 100 pt/m the line is still exactly 4pt past b*",
         d, job=dict(nedre=True, cmap=SKIP_MAP),
         expect=dict(cut=spec_cut(b, 400.0, 1), page_w=400.0, page_h=150.0)))
