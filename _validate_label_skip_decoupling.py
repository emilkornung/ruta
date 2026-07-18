"""
_validate_label_skip_decoupling.py — guard for the TIF-57 follow-up: skip_colors
suppresses PRINTED LABELS ONLY, it does not blind the cut-boundary scan.

WHY THIS EXISTS
---------------
run_slice() used to do `if effective_skip: color_map = {}`, wiping the map before
it ever reached the slicer. That conflated two unrelated consumers of the map:

  - pass B (_label_colors_on_page) draws the visible NCS codes — this is what
    skip_colors is actually about;
  - pass A (_find_cut_boundary) reads the map's "Skip" entries to find where the
    artwork stops and the "Klipp" cut line belongs — this has nothing to do with
    whether codes get printed.

Wiping the map silently downgraded every skip_colors run to the legacy pink/orange
fallback, i.e. geometric-only cut detection, blind to any design that ends mid-page
inside its own painted background. Nothing failed; the cut line just quietly went
missing. The map is now always passed through and ONLY the label-drawing call is
gated on the flag.

Failure here is invisible in the output (a missing cut line looks like a design
that simply has nothing to cut), which is exactly why it is pinned by a test.

Run:  python _validate_label_skip_decoupling.py
"""
import math
import sys

import fitz

import slicer

PEST       = "pest mitten 24x31,5m.pdf"
PEST_W     = 31.5
PEST_H     = 24.0
PEST_STRIP = 20      # 0-based: strip 21, whose artwork ends mid-page 6
PEST_PAGE  = 5       # 0-based: page 6

# Geometric-partial case, for the genuinely-empty-map path. Same shape the TIF-57
# guard uses: a height that does NOT divide evenly, forcing a partial last page.
KENTA       = "kenta.pdf"
KENTA_W     = 21.0
KENTA_H     = 3.0        # wide leftover (~1 m of pink) so the TIF-67 margin fits with
                        # room to spare — this guard is about cut DETECTION, not the
                        # near-full sliver edge cases (those live in the TIF-67 guard)
KENTA_STRIP = 6

# The real production map (Supabase source='manual'): #EEA8CB is the Skip colour.
COLOUR_MAP = {
    "#EEA8CB": "Skip", "#392C1B": "8010", "#231F20": "8010", "#6A4F32": "6020",
    "#E5C699": "1515", "#172816": "7020", "#030505": "S",    "#947042": "5030",
    "#BD9164": "3030", "#1C5429": "5540", "#90908F": "4500", "#D4D2CE": "1500",
    "#313131": "8500", "#1F763B": "3560", "#B2B0AF": "3000", "#737372": "6000",
    "#515150": "7500", "#FFFFFF": "V",
}

# NCS code labels are drawn at <= LABEL_FONT_DEFAULT (3.0 pt) and shrink from
# there; "Klipp" and the page number are both 6 pt. Font size separates them
# cleanly, with room to spare.
LABEL_FS_MAX = 4.0


def _spans(page):
    return [sp for blk in page.get_text("dict")["blocks"]
            for ln in blk.get("lines", []) for sp in ln["spans"]]


def _n_code_labels(doc):
    return sum(1 for pg in doc for sp in _spans(pg) if sp["size"] < LABEL_FS_MAX)


def _klipp(doc):
    """(page_index, line_x) of the one Klipp cut LINE, or None.

    Detects the dashed line itself, NOT the "Klipp" text. TIF-67 decoupled the two:
    the text is skipped on a narrow sliver (room < KLIPP_TEXT_MIN_ROOM_PT) while the
    line still draws, so keying cut-detection off the text — as this guard used to —
    would now wrongly report "no cut" whenever the text was merely skipped. The line
    is the true signal that the map reached the cut scan, which is what this guard
    exists to pin.
    """
    for i, pg in enumerate(doc):
        lines = [d for d in pg.get_drawings()
                 if d.get("dashes") and d["dashes"] not in ("[] 0", "[]0")
                 and any(it[0] == "l" for it in d["items"])]
        if lines:
            return i, lines[0]["rect"].x0
    return None


def _slice(pdf_path, W, H, strip, nedre, cmap, skip_labels):
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()
    ns  = math.ceil(W / slicer.STRIP_WIDTH_M)
    npg = math.ceil(H / slicer.PAGE_HEIGHT_M)
    _, b = slicer.slice_one_strip(
        (strip, pdf_bytes, W, H, ns, npg, cmap, nedre, skip_labels))
    return fitz.open(stream=b, filetype="pdf")


def run():
    fails = []

    # ── A. skip_colors=True WITH the real map ────────────────────────────────
    # No printed codes (unchanged), but the cut line IS found via the Skip colour.
    a = _slice(PEST, PEST_W, PEST_H, PEST_STRIP, True, COLOUR_MAP, True)
    a_labels, a_cut = _n_code_labels(a), _klipp(a)
    print(f"A  skip_labels=True  + real map : labels={a_labels}  klipp={a_cut}")
    if a_labels:
        fails.append(f"A: {a_labels} NCS labels drawn — skip must suppress all")
    if a_cut is None:
        fails.append("A: no Klipp — the map was not reaching the cut scan "
                     "(this is the exact regression this guard exists for)")
    elif a_cut[0] != PEST_PAGE:
        fails.append(f"A: Klipp on page {a_cut[0]+1}, expected {PEST_PAGE+1}")

    # ── B. skip_colors=False, same map ───────────────────────────────────────
    # Labels present; cut line identical to A — the flag must not move it.
    b = _slice(PEST, PEST_W, PEST_H, PEST_STRIP, True, COLOUR_MAP, False)
    b_labels, b_cut = _n_code_labels(b), _klipp(b)
    print(f"B  skip_labels=False + real map : labels={b_labels}  klipp={b_cut}")
    if b_labels == 0:
        fails.append("B: no NCS labels drawn — labeling is broken")
    if b_cut is None:
        fails.append("B: no Klipp")
    if a_cut and b_cut and (a_cut[0] != b_cut[0]
                            or abs((a_cut[1] or 0) - (b_cut[1] or 0)) > 0.01):
        fails.append(f"B: cut moved with the label flag — {a_cut} vs {b_cut}; "
                     f"the boundary must not depend on whether codes are printed")

    # ── C. genuinely empty map ───────────────────────────────────────────────
    # No map at all => legacy pink/orange fallback. On a GEOMETRIC partial that
    # still yields the old behaviour: cut at the content boundary (= pad_x), now
    # offset KLIPP_LINE_MARGIN_PT into the pink (TIF-67 Part 1). This is the
    # degradation path, and it must not regress. KENTA_H is chosen wide enough that
    # the margin comfortably fits (not a near-full sliver, which would be legitimately
    # suppressed) so the cut line is genuinely present.
    c = _slice(KENTA, KENTA_W, KENTA_H, KENTA_STRIP, False, {}, True)
    c_labels, c_cut = _n_code_labels(c), _klipp(c)
    doc0 = fitz.open(KENTA)
    full_h  = doc0[0].rect.height
    page_h  = slicer.PAGE_HEIGHT_M * (full_h / KENTA_H)
    content_w = full_h - (math.ceil(KENTA_H / slicer.PAGE_HEIGHT_M) - 1) * page_h
    doc0.close()
    expected_x = content_w + slicer.KLIPP_LINE_MARGIN_PT
    print(f"C  empty map, geometric partial : labels={c_labels}  klipp={c_cut}  "
          f"(expected cut at pad_x+margin={expected_x:.2f})")
    if c_labels:
        fails.append(f"C: {c_labels} NCS labels drawn from an empty map")
    if c_cut is None:
        fails.append("C: no Klipp line on a geometric partial — the legacy fallback "
                     "regressed")
    elif c_cut[1] is None or abs(c_cut[1] - expected_x) > 1.0:
        fails.append(f"C: cut at x={c_cut[1]}, expected pad_x+margin={expected_x:.2f} "
                     f"— the empty-map path must reproduce the geometric placement "
                     f"plus the TIF-67 margin")

    for d in (a, b, c):
        d.close()

    print()
    for m in fails:
        print(f"  FAIL {m}")
    ok = not fails
    print("LABEL/CUT DECOUPLING CHECK:", "PASS" if ok else f"FAIL ({len(fails)})")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
