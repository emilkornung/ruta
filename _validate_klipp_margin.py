"""
_validate_klipp_margin.py — permanent guard for TIF-67: the 2 pt content-to-line
margin and the right-only / shrink / skip rules for the "Klipp" text.

WHY THIS EXISTS
---------------
TIF-67 refined the content-driven Klipp marking (on top of TIF-57):

  Part 1 — the dashed line no longer sits flush at the content boundary b*; it is
           nudged KLIPP_LINE_MARGIN_PT (2 pt) INTO the pink, away from the artwork.

  Part 2 — the "Klipp" text ALWAYS sits to the RIGHT of the line (TIF-57's
           left-of-line sliver fallback is gone for good).

The suppression / fit logic is now a single ladder with three named thresholds:
  1. total pink <= KLIPP_MIN_PINK_PT (12 pt) -> suppress EVERYTHING (line + text),
     decided ONCE upfront in _find_cut_boundary, before any margin/text logic;
  2. else the line always draws, and if room-to-right < KLIPP_TEXT_MIN_ROOM_PT
     (4 pt of WIDTH) -> skip the text;
  3. else shrink the text from KLIPP_FONT_DEFAULT toward KLIPP_MIN_FONT_SIZE_PT
     (4 pt of FONT SIZE) — if it cannot fit at >= that font, skip the text (it is
     never shrunk toward the colour labeler's 0.1 pt technical floor).

The three font-sizing designs divide evenly and the real designs cannot be dialled
to an exact leftover width, so none of them can exercise these edge cases. This
guard builds SYNTHETIC one-page designs whose content boundary sits at an exact,
chosen point, and drives the real slicer (_find_cut_boundary + slice_one_strip).

WHAT IT ASSERTS
  1. MARGIN APPLIED: on a comfortable mid-page boundary the drawn line sits exactly
     KLIPP_LINE_MARGIN_PT to the right of the content boundary (proven by toggling
     the constant to 0 and measuring the shift — no rasterisation guesswork).
  2. TOTAL-PINK SUPPRESSION (the ~5 pt case): with only ~5 pt of total pink — under
     any gate value tried here — the OLD (pre-gate) behaviour drew the line and merely
     skipped the text; the unified gate now suppresses BOTH. Shown by toggling
     KLIPP_MIN_PINK_PT to 0 (gate off => line draws, text skipped, the old behaviour)
     versus the real gate (gate on => nothing drawn).
  2b. THRESHOLD RAISE 6 -> 12 pt (the ~9 pt case): a boundary with ~9 pt of total pink
     sits ABOVE the old 6 pt gate but BELOW the new 12 pt gate. Shown by toggling
     KLIPP_MIN_PINK_PT to 6 (old threshold => line still drawn) versus the real 12
     (new threshold => suppressed). Proves the raise actually changed behaviour here.
  3. FONT-FLOOR SKIP: a boundary that passes the 4 pt WIDTH gate but whose text would
     need a sub-4 pt FONT. With the real KLIPP_MIN_FONT_SIZE_PT the text is skipped;
     with the floor toggled down to 0.1 it reappears at a sub-4 pt size — proving the
     new font floor, not the old technical floor, is what skips it. Line draws in both.
  4. NO LEFT PLACEMENT (hard rule): across a sweep of boundary positions, the text —
     whenever it is drawn — is ALWAYS to the right of the line. A single left-of-line
     occurrence fails the guard.

Not part of the production pipeline. Exits non-zero on any failure.

Run:  python _validate_klipp_margin.py
"""
import math
import sys

import fitz

import slicer

# Synthetic design geometry. One 1.5 m strip (so a single strip), 4 m tall (a single
# 4 m page), rendered at a round 100 pt/m so page_h_pts == full_h == 400 and the math
# is legible. ruta_nedre=True gives seq = design_y, so content painted from the top
# down to design_y = D is the artwork and D..full_h is the "Skip" pink to be cut off.
PTS_PER_M = 100.0
W_M, H_M  = 1.5, 4.0
FULL_W    = W_M * PTS_PER_M          # 150 pt strip width
FULL_H    = H_M * PTS_PER_M          # 400 pt = one 4 m page
PAGE_H    = slicer.PAGE_HEIGHT_M * PTS_PER_M   # 400 pt

SKIP_HEX  = "#EEA8CB"                 # pest-mitten's real Skip colour
SKIP_RGB  = tuple(int(SKIP_HEX[i:i + 2], 16) / 255 for i in (1, 3, 5))
COLOUR_MAP = {SKIP_HEX: "Skip"}       # black content is > tolerance from it => content

# Right boundary for the text and the derived transition points (mirrors the slicer).
RIGHT_LIMIT = PAGE_H - slicer.PAGE_NUM_EXCL_W - 2.0
MARGIN      = slicer.KLIPP_LINE_MARGIN_PT
MIN_ROOM    = slicer.KLIPP_TEXT_MIN_ROOM_PT
MIN_PINK    = slicer.KLIPP_MIN_PINK_PT       # total-pink gate: <= this -> suppress all
MIN_FONT    = slicer.KLIPP_MIN_FONT_SIZE_PT  # font floor: never shrink below this


def make_design(boundary_pt):
    """A one-page PDF: black content over design_y [0, boundary_pt], Skip-pink below.

    Returns pdf bytes. Under ruta_nedre the content boundary in printing sequence is
    exactly `boundary_pt`, so the slicer must find b* there (± sub-pixel).
    """
    doc  = fitz.open()
    page = doc.new_page(width=FULL_W, height=FULL_H)
    page.draw_rect(fitz.Rect(0, 0, FULL_W, boundary_pt),
                   color=(0, 0, 0), fill=(0, 0, 0))
    page.draw_rect(fitz.Rect(0, boundary_pt, FULL_W, FULL_H),
                   color=SKIP_RGB, fill=SKIP_RGB)
    b = doc.tobytes()
    doc.close()
    return b


def find_cut(pdf_bytes):
    """cut result from the real _find_cut_boundary for a synthetic design, or None."""
    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    cut = slicer._find_cut_boundary(src, 0, 0.0, FULL_W, FULL_H, PAGE_H,
                                    1, True, COLOUR_MAP)
    src.close()
    return cut


def render(pdf_bytes):
    """Drive the real slice_one_strip; return the single output page."""
    _, out = slicer.slice_one_strip(
        (0, pdf_bytes, W_M, H_M, 1, 1, COLOUR_MAP, True, True))
    return fitz.open(stream=out, filetype="pdf")


def _dashed_line_x(page):
    for dr in page.get_drawings():
        d = dr.get("dashes")
        if d and d not in ("[] 0", "[]0") and any(it[0] == "l" for it in dr["items"]):
            return dr["rect"].x0
    return None


def _klipp_xs(page):
    """(x0, font_size) of the "Klipp" span, or None. NOTE: font size is sp['size'],
    NOT sp['bbox'][1] (that is a y-coordinate) — kb[0]=x0, kb[1]=font size."""
    for blk in page.get_text("dict")["blocks"]:
        for ln in blk.get("lines", []):
            for sp in ln["spans"]:
                if sp["text"].strip().startswith("Kli"):
                    return (sp["bbox"][0], sp["size"])
    return None


def run():
    fails = []

    # ── 1. MARGIN APPLIED ────────────────────────────────────────────────────
    # A comfortable mid-page boundary. Prove the drawn line is exactly MARGIN to the
    # right of b* by recomputing with the constant toggled to 0 and measuring the
    # shift — this isolates the margin from any rasterisation rounding in b* itself.
    D = 200.0
    pdf = make_design(D)
    cut_with = find_cut(pdf)
    saved = slicer.KLIPP_LINE_MARGIN_PT
    try:
        slicer.KLIPP_LINE_MARGIN_PT = 0.0
        cut_without = find_cut(pdf)
    finally:
        slicer.KLIPP_LINE_MARGIN_PT = saved

    if cut_with is None or cut_without is None:
        fails.append(f"1 MARGIN: expected a cut at D={D}, got "
                     f"with={cut_with} without={cut_without}")
    else:
        shift = cut_with[1] - cut_without[1]
        # b* itself should land within a sub-pixel of D (1/LABEL_RENDER_SCALE).
        b_star = cut_without[1]
        doc = render(pdf)
        line_x = _dashed_line_x(doc[0])
        kb = _klipp_xs(doc[0])
        doc.close()
        print(f"1 MARGIN APPLIED  D={D:.1f}: b*={b_star:.2f}  line={line_x}  "
              f"shift={shift:.3f} (expect {MARGIN})  klipp_x0="
              f"{None if kb is None else round(kb[0], 1)}")
        if abs(shift - MARGIN) > 0.01:
            fails.append(f"1 MARGIN: line shifted {shift:.3f} pt, expected {MARGIN}")
        if abs(b_star - D) > 1.0:
            fails.append(f"1 MARGIN: b* landed at {b_star:.2f}, expected ~{D}")
        if line_x is None:
            fails.append("1 MARGIN: no dashed line rendered")
        elif abs(line_x - (D + MARGIN)) > 1.0:
            fails.append(f"1 MARGIN: rendered line at {line_x:.2f}, "
                         f"expected ~{D + MARGIN:.2f}")
        if kb is None:
            fails.append("1 MARGIN: text missing though room is ample")
        elif line_x is not None and kb[0] < line_x - 0.5:
            fails.append(f"1 MARGIN: text LEFT of line ({kb[0]:.1f} < {line_x:.1f})")

    # ── 2. TOTAL-PINK SUPPRESSION — the ~5 pt case ───────────────────────────
    # ~5 pt of total pink: between the 2 pt the margin needs and the 4 pt the text
    # needs. BEFORE this rev the line drew and only the text was skipped; the unified
    # KLIPP_MIN_PINK_PT gate must now suppress BOTH. Prove the gate is the cause by
    # toggling it to 0 (gate off -> the old behaviour: line draws, text skipped) and
    # comparing to the real 6 pt gate (nothing drawn).
    D = FULL_H - 5.0        # 395 -> ~5 pt of total pink
    pdf = make_design(D)
    cut_on  = find_cut(pdf)
    doc = render(pdf)
    line_on = _dashed_line_x(doc[0]);  kb_on = _klipp_xs(doc[0]);  doc.close()

    saved = slicer.KLIPP_MIN_PINK_PT
    try:
        slicer.KLIPP_MIN_PINK_PT = 0.0        # disable the gate == pre-rev behaviour
        cut_off  = find_cut(pdf)
        doc = render(pdf)
        line_off = _dashed_line_x(doc[0]);  kb_off = _klipp_xs(doc[0]);  doc.close()
    finally:
        slicer.KLIPP_MIN_PINK_PT = saved

    print(f"2 TOTAL-PINK SUPPRESSION  D={D:.1f} (~{FULL_H - D:.0f}pt pink):")
    print(f"    gate OFF (old behaviour): line={line_off}  "
          f"text={'present' if kb_off else 'skipped'}")
    print(f"    gate ON  (this rev)     : cut={cut_on}  line={line_on}  "
          f"text={'present' if kb_on else 'skipped'}")
    # Gate OFF must reproduce the exact bug being fixed: line drawn, text skipped.
    if line_off is None:
        fails.append("2 SUPPRESS: with the gate OFF the line should still draw "
                     "(that is the pre-rev behaviour this case is contrasted against)")
    if kb_off is not None:
        fails.append("2 SUPPRESS: with the gate OFF the text should be skipped "
                     "(room < 4pt) — fixture is not the intended ~5pt case")
    # Gate ON must suppress the ENTIRE marking.
    if cut_on is not None:
        fails.append(f"2 SUPPRESS: marking not suppressed at ~5pt pink (cut={cut_on})")
    if line_on is not None:
        fails.append(f"2 SUPPRESS: line still drawn at ~5pt pink (x={line_on:.2f})")
    if kb_on is not None:
        fails.append("2 SUPPRESS: text still drawn at ~5pt pink")

    # ── 2b. THRESHOLD RAISE 6 -> 12 pt — the ~9 pt case ──────────────────────
    # ~9 pt of total pink sits ABOVE the old 6 pt gate but BELOW the new 12 pt gate.
    # Under the OLD threshold the marking still drew; under the NEW threshold it must
    # be suppressed. Prove the raise (not something else) is the cause by toggling
    # KLIPP_MIN_PINK_PT to the old 6.0 and comparing to the real 12.0.
    assert slicer.KLIPP_MIN_PINK_PT >= 9.0, "this case assumes the gate was raised past 9pt"
    D = FULL_H - 9.0        # 391 -> ~9 pt of total pink (b* ~0.5 under D, so ~9.5pt)
    pdf = make_design(D)
    cut_new = find_cut(pdf)
    doc = render(pdf)
    line_new = _dashed_line_x(doc[0]);  kb_new = _klipp_xs(doc[0]);  doc.close()

    saved = slicer.KLIPP_MIN_PINK_PT
    try:
        slicer.KLIPP_MIN_PINK_PT = 6.0        # the OLD threshold
        cut_old = find_cut(pdf)
        doc = render(pdf)
        line_old = _dashed_line_x(doc[0]);  kb_old = _klipp_xs(doc[0]);  doc.close()
    finally:
        slicer.KLIPP_MIN_PINK_PT = saved

    b_star_meas = None if cut_old is None else cut_old[1] - MARGIN
    pink_meas   = None if b_star_meas is None else PAGE_H - b_star_meas
    print(f"2b THRESHOLD RAISE 6->12  D={D:.1f} "
          f"(~{'?' if pink_meas is None else round(pink_meas,1)}pt pink):")
    print(f"    gate 6pt  (old threshold): cut={cut_old}  line={line_old}  "
          f"text={'present' if kb_old else 'skipped'}")
    print(f"    gate {MIN_PINK:.0f}pt (this rev)   : cut={cut_new}  line={line_new}  "
          f"text={'present' if kb_new else 'skipped'}")
    # Sanity: the fixture must actually land in the 6..12 band, else it proves nothing.
    if pink_meas is None or not (6.0 < pink_meas <= MIN_PINK):
        fails.append(f"2b RAISE: fixture pink={pink_meas} not in the (6, {MIN_PINK:.0f}] "
                     f"band — it cannot distinguish the old and new thresholds")
    # OLD threshold (6pt): the marking must still DRAW (this is the whole point — it
    # was NOT suppressed before the raise).
    if cut_old is None or line_old is None:
        fails.append("2b RAISE: under the old 6pt gate the line should still draw "
                     "(if it did not, the raise changed nothing here)")
    # NEW threshold (12pt): the marking must now be SUPPRESSED entirely.
    if cut_new is not None:
        fails.append(f"2b RAISE: marking not suppressed under the new gate (cut={cut_new})")
    if line_new is not None:
        fails.append(f"2b RAISE: line still drawn under the new gate (x={line_new:.2f})")
    if kb_new is not None:
        fails.append("2b RAISE: text still drawn under the new gate")

    # ── 3. FONT-FLOOR SKIP ───────────────────────────────────────────────────
    # A boundary that PASSES the 4 pt width gate but whose "Klipp" would need a
    # sub-4 pt FONT to fit. With the real floor the text is skipped; with the floor
    # dropped to 0.1 it reappears at a sub-4 pt size — so the NEW font floor, not the
    # old technical floor, is what skips it. The line draws in both.
    D = 375.0
    pdf = make_design(D)
    doc = render(pdf)
    line_hi = _dashed_line_x(doc[0]);  kb_hi = _klipp_xs(doc[0]);  doc.close()
    room = None if line_hi is None else RIGHT_LIMIT - line_hi

    saved = slicer.KLIPP_MIN_FONT_SIZE_PT
    try:
        slicer.KLIPP_MIN_FONT_SIZE_PT = 0.1   # old technical-floor behaviour
        doc = render(pdf)
        line_lo = _dashed_line_x(doc[0]);  kb_lo = _klipp_xs(doc[0]);  doc.close()
    finally:
        slicer.KLIPP_MIN_FONT_SIZE_PT = saved

    print(f"3 FONT-FLOOR SKIP  D={D:.1f}: line={line_hi}  "
          f"room_right={None if room is None else round(room, 1)} "
          f"(>= {MIN_ROOM} width gate PASSES)")
    print(f"    floor {MIN_FONT}pt (this rev): text="
          f"{'skipped' if kb_hi is None else 'present @'+str(round(kb_hi[1],2))+'pt'}")
    print(f"    floor 0.1pt (old)          : text="
          f"{'skipped' if kb_lo is None else 'present @'+str(round(kb_lo[1],2))+'pt'}")
    if line_hi is None:
        fails.append("3 FONT-FLOOR: line must draw (this is a text-only skip case)")
    elif room < MIN_ROOM:
        fails.append(f"3 FONT-FLOOR: room {room:.1f} < {MIN_ROOM}; this case must PASS "
                     f"the width gate so the skip is attributable to the font floor")
    if kb_hi is not None:
        fails.append(f"3 FONT-FLOOR: text drawn @{kb_hi[1]:.2f}pt with the {MIN_FONT}pt "
                     f"floor — it should be skipped, not shrunk below the floor")
    if kb_lo is None:
        fails.append("3 FONT-FLOOR: with the floor at 0.1pt the text should reappear "
                     "(shrunk) — the fixture must be one that only the font floor skips")
    elif kb_lo[1] >= MIN_FONT:
        fails.append(f"3 FONT-FLOOR: 0.1pt-floor text is @{kb_lo[1]:.2f}pt (>= {MIN_FONT}) "
                     f"— it would have fit under the real floor too; pick a tighter case")

    # ── 4. NO LEFT PLACEMENT — sweep ─────────────────────────────────────────
    # Sweep from mid-page to near-full. At every step assert the hard rule (text, when
    # present, is RIGHT of the line) and cross-check line/text against the three-rung
    # ladder, computed from the MEASURED line position (no rasterisation guesswork).
    def klipp_w(sz):
        return fitz.get_text_length("Klipp", fontsize=sz)

    print("4 NO-LEFT SWEEP:")
    sweep = [150, 250, 340, 360, 368, 375, 382, 388, 391, 395, 398]
    for D in sweep:
        D = float(D)
        pdf = make_design(D)
        doc = render(pdf)
        line_x = _dashed_line_x(doc[0]);  kb = _klipp_xs(doc[0]);  doc.close()
        has_line = line_x is not None
        has_text = kb is not None

        # Expected outcome from the measured geometry.
        if has_line:
            pink   = PAGE_H - (line_x - MARGIN)          # b* = line_x - margin
            avail  = RIGHT_LIMIT - (line_x + slicer.KLIPP_TEXT_GAP_PT)
            room   = RIGHT_LIMIT - line_x
            exp_line = True                              # a drawn line must clear the gate
            exp_text = (room >= MIN_ROOM) and (klipp_w(MIN_FONT) <= avail)
            gate_ok  = pink > MIN_PINK
        else:
            exp_line = False
            exp_text = False
            gate_ok  = (PAGE_H - D) <= MIN_PINK + 1.0    # suppressed => pink is small

        tag = []
        if has_text and has_line and kb[0] < line_x - 0.5:
            fails.append(f"4 NO-LEFT: D={D}: text x0={kb[0]:.1f} LEFT of line "
                         f"{line_x:.1f} — forbidden")
            tag.append("LEFT!")
        if not gate_ok:
            fails.append(f"4 NO-LEFT: D={D}: line/suppress inconsistent with the "
                         f"{MIN_PINK}pt pink gate")
            tag.append("gate?")
        if has_line != exp_line:
            fails.append(f"4 NO-LEFT: D={D}: line present={has_line}, expected {exp_line}")
            tag.append("line?")
        if has_text != exp_text:
            fails.append(f"4 NO-LEFT: D={D}: text present={has_text}, expected {exp_text}")
            tag.append("text?")
        side = "-"
        if has_text and has_line:
            side = "right" if kb[0] >= line_x - 0.5 else "LEFT"
        fs = "" if not has_text else f" @{kb[1]:.2f}pt"
        print(f"    D={D:5.1f}  line={'Y' if has_line else 'n'} "
              f"text={'Y' if has_text else 'n'}{fs:11s} side={side:5s} "
              f"{'OK' if not tag else 'FAIL ' + ','.join(tag)}")

    print()
    for m in fails:
        print(f"  FAIL {m}")
    ok = not fails
    print("KLIPP MARGIN/TEXT CHECK (TIF-67):", "PASS" if ok else f"FAIL ({len(fails)})")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
