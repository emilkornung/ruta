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
(rotate=270) and ruta_nedre (rotate=90) slicing modes, and asserts on every
partial page that the marking is: present (pink + dashed line + "Klipp" text),
fully on-page, and clear of the page-number glyph.

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


def _has_dashed_line(page):
    for dr in page.get_drawings():
        d = dr.get("dashes")
        if d and d not in ("[] 0", "[]0") and any(it[0] == "l" for it in dr["items"]):
            return True
    return False


def check_page(page, page_label):
    """Return list of failure strings for one partial page (empty == OK)."""
    fails = []
    w = page.rect.width
    sp = _spans(page)
    klipp = next((s for s in sp if s["text"].strip().startswith("Kli")), None)
    pnum  = next((s for s in sp if s["text"].strip().isdigit()), None)

    if not _has_pink_fill(page):
        fails.append("pink padding rect missing")
    if not _has_dashed_line(page):
        fails.append("dashed cut line missing")
    if klipp is None:
        fails.append('"Klipp" text missing')
        return fails  # nothing more to check without the text

    kb = klipp["bbox"]
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
            (strip, pdf_bytes, W, H, ns, npg, {}, nedre))
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
            fails = check_page(pg, f"{label} page {i + 1}")
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
