"""
_validate_klipp_partial.py — TIF-57 permanent regression guard for the
partial-page "Klipp" cut marking (pink padding + dashed cut line + text).

WHY THIS EXISTS
---------------
The three font-sizing validation designs (ENAD 61.5x20, kenta 21x20,
pest-mitten 31.5x24) all use dimensions that divide EVENLY into 1.5 m strips
and 4 m pages, so NONE of them ever produces a single partial page. The Klipp
marking therefore had zero automated coverage — which is exactly how a
near-full-partial placement bug survived unnoticed: on a partial page whose
content nearly fills the 4 m width, the pink pad is a thin sliver at the right
edge and the "Klipp" text (anchored just right of the cut line) ran off the
page and on top of the orange page number, so the whole marking looked absent.

This guard deliberately forces partial pages at several leftover widths —
including the NARROW / near-full case that actually broke — in BOTH the default
(rotate=270) and ruta_nedre (rotate=90) slicing modes.

TIF-67 UPDATE
-------------
TIF-57 originally required the "Klipp" text on every partial page, falling back
to a LEFT-of-line placement on narrow slivers. TIF-67 reversed that: the text now
ALWAYS sits to the RIGHT of the line, and if there is less than
KLIPP_TEXT_MIN_ROOM_PT of room to the right it is SKIPPED entirely (the line still
draws). So the contract this guard enforces is now:
  - pink pad + dashed line present on every partial page (unchanged);
  - the "Klipp" text is NEVER left of the line (hard rule — any left placement is
    a bug), and when present is fully on-page and clear of the page-number glyph;
  - on a WIDE partial the text must be present; on a NARROW/near-full partial it
    may legitimately be skipped, but only when the room to the right really is
    below the threshold (a skip with ample room would be a bug).

Not part of the production pipeline. Passes an empty colour map (labels are
irrelevant to the cut marking). Exits non-zero if any partial page fails.

Run:  python _validate_klipp_partial.py
"""
import math
import sys

import fitz

import slicer

PINK = (slicer.PINK_PAD_R, slicer.PINK_PAD_G, slicer.PINK_PAD_B)


def _spans(page):
    return [sp for blk in page.get_text("dict")["blocks"]
            for ln in blk.get("lines", []) for sp in ln["spans"]]


def _has_pink_fill(page):
    for dr in page.get_drawings():
        f = dr.get("fill")
        if f and all(abs(a - b) < 0.03 for a, b in zip(f, PINK)):
            return True
    return False


def _dashed_line_x(page):
    """x of the dashed cut line, or None if absent."""
    for dr in page.get_drawings():
        d = dr.get("dashes")
        if d and d not in ("[] 0", "[]0") and any(it[0] == "l" for it in dr["items"]):
            return dr["rect"].x0
    return None


def check_page(page, page_label, narrow):
    """Return list of failure strings for one partial page (empty == OK)."""
    fails = []
    w = page.rect.width
    sp = _spans(page)
    klipp = next((s for s in sp if s["text"].strip().startswith("Kli")), None)
    pnum  = next((s for s in sp if s["text"].strip().isdigit()), None)
    line_x = _dashed_line_x(page)

    if not _has_pink_fill(page):
        fails.append("pink padding rect missing")
    if line_x is None:
        fails.append("dashed cut line missing")

    if klipp is None:
        # TIF-67: a skip is allowed ONLY on a narrow partial whose room to the right
        # of the line is genuinely below the threshold. A skip anywhere else — or a
        # skip with ample room — is a bug (text vanished when it should have fit).
        if line_x is None:
            return fails  # no line at all; nothing further to say about the text
        right_limit = w - slicer.PAGE_NUM_EXCL_W - 2.0
        room = right_limit - line_x
        if not narrow:
            fails.append(f'"Klipp" text missing on a WIDE partial (room to right '
                         f'= {room:.1f}pt, threshold {slicer.KLIPP_TEXT_MIN_ROOM_PT})')
        elif room >= slicer.KLIPP_TEXT_MIN_ROOM_PT:
            fails.append(f'"Klipp" text skipped but room to right = {room:.1f}pt '
                         f'>= threshold {slicer.KLIPP_TEXT_MIN_ROOM_PT} — should fit')
        return fails

    kb = klipp["bbox"]
    # HARD RULE (TIF-67): the text must ALWAYS be to the RIGHT of the line.
    if line_x is not None and kb[0] < line_x - 0.5:
        fails.append(f'"Klipp" is LEFT of the cut line (x0={kb[0]:.1f} < line '
                     f'{line_x:.1f}) — TIF-67 forbids left placement entirely')
    if kb[0] < -0.5 or kb[2] > w + 0.5:
        fails.append(f'"Klipp" off-page: x[{kb[0]:.1f}..{kb[2]:.1f}] page width {w:.1f}')
    if pnum is not None:
        pb = pnum["bbox"]
        overlaps = not (kb[2] < pb[0] or pb[2] < kb[0])
        if overlaps:
            fails.append(f'"Klipp" collides with page number "{pnum["text"].strip()}" '
                         f'(Klipp x[{kb[0]:.1f}..{kb[2]:.1f}] vs #{pb[0]:.1f}..{pb[2]:.1f})')
    return fails


# (pdf, width_m, height_m, strip_index, ruta_nedre, label, is_narrow)
# Heights are chosen to force a partial LAST page at a known leftover fraction.
# The narrow / near-full cases (leftover ~97 % of a page) are the ones that the
# even-dimension font-sizing designs can never produce and that actually broke.
CASES = [
    # Emil's confirmed real reproduction: pest-mitten strip 21, page 6 is a
    # near-full partial (31.5x23.9 -> page 6 holds 3.9 m of a 4 m page).
    ("pest mitten 24x31,5m.pdf", 31.5, 23.9, 20, True,  "pest s21 p6 near-full (reported)", True),
    # Single near-full partial page (leftover ~97 %) in both modes.
    ("kenta.pdf",                21.0,  3.9,  6, False, "kenta near-full partial",          True),
    ("ENAD_rutor.pdf",           61.5,  3.9, 19, False, "ENAD near-full partial",           True),
    ("pest mitten 24x31,5m.pdf", 31.5,  3.9, 10, True,  "pest near-full partial (nedre)",   True),
    # Wider partials — the marking must keep working across the whole range.
    ("kenta.pdf",                21.0,  2.0,  6, False, "kenta half partial",               False),
    ("ENAD_rutor.pdf",           61.5,  1.0, 19, False, "ENAD wide partial",                False),
]


def run():
    total_pages = 0
    total_fail  = 0
    saw_narrow  = False

    for pdf_path, W, H, strip, nedre, label, narrow in CASES:
        try:
            with open(pdf_path, "rb") as f:
                pdf_bytes = f.read()
        except FileNotFoundError:
            print(f"  SKIP {label}: {pdf_path} not found in repo root")
            continue

        ns  = math.ceil(W / slicer.STRIP_WIDTH_M)
        npg = math.ceil(H / slicer.PAGE_HEIGHT_M)
        _, strip_bytes = slicer.slice_one_strip(
            (strip, pdf_bytes, W, H, ns, npg, {}, nedre, True))
        doc = fitz.open(stream=strip_bytes, filetype="pdf")

        # A page is "partial" iff the slicer drew the pink pad on it.
        partial_pages = [(i, pg) for i, pg in enumerate(doc) if _has_pink_fill(pg)]
        if not partial_pages:
            print(f"  FAIL {label}: expected a partial page but none was produced "
                  f"(content strip may be fully background — pick another strip)")
            total_fail += 1
            continue

        for i, pg in partial_pages:
            total_pages += 1
            saw_narrow |= narrow
            fails = check_page(pg, f"{label} page {i + 1}", narrow)
            status = "OK  " if not fails else "FAIL"
            marker = " [narrow]" if narrow else ""
            print(f"  {status} {label} page {i + 1}{marker}")
            for msg in fails:
                print(f"        - {msg}")
                total_fail += 1

    print(f"\nPartial pages checked: {total_pages}   failures: {total_fail}")
    if not saw_narrow:
        print("ERROR: no narrow/near-full partial was exercised — this guard is "
              "pointless without it (that is the case that regressed).")
        return False
    ok = total_fail == 0
    print("KLIPP PARTIAL-PAGE CHECK:", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
