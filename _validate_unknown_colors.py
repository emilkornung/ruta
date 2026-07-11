"""
_validate_unknown_colors.py — TIF-60 permanent regression guard for the
unknown-color handling in run_slice(), and for independent "Skip" entries.

WHY THIS EXISTS
---------------
Two separate holes let a production bug through unseen.

1. The bug. run_slice() used to do this:

       unknown_colors = sorted(c for c in hex_colors if c not in color_map)
       if unknown_colors:
           color_map = {}          # <-- discards the ENTIRE map

   One unmapped color anywhere in a design threw away every other mapping, so
   the strips shipped with ZERO labels. Nothing logged it and nothing persisted
   it, so it looked like labeling had simply "not worked". This is what happened
   to ruta_jobs 5ac70b43 ("pest mitten 24x31,5m", 2026-07-11).

2. The gap that hid it. All six prior validation rounds drove
   _label_colors_on_page() / _code_masks() DIRECTLY, bypassing run_slice()
   entirely — so the wipe branch had no coverage at all and could not fail a
   test. This guard therefore exercises the real run_slice() entry point, the
   same one api.py calls.

While fixing (1) it also emerged that the membership test itself was wrong:
`c not in color_map` is EXACT hex matching, but the labeler (_code_masks) claims
pixels within COLOR_MATCH_TOLERANCE. Rendered vector fills routinely land 1-2
RGB units off the curated hex (#737272 in the PDF vs #737271 in the map), so the
exact test reported colors as unknown that the labeler matches perfectly — 11
reported unknowns on pest-mitten where only 1 was real. find_unknown_colors()
now uses the labeler's own nearest-within-tolerance semantics; DRIFT below is
the fixture that pins that down.

WHAT IS ASSERTED (synthetic 1.5 x 4 m design, one strip, one page)
------------------------------------------------------------------
A. unmapped-color regression — a design containing genuinely unmappable colors
   still labels every MAPPED color (pre-fix: zero labels). The unmapped colors
   are individually left unlabeled.
B. two independent "Skip" entries (pink + orange) coexist: both are treated as
   KNOWN (never reported unknown), both are left unlabeled, and neither
   suppresses nor steals from the other or from any mapped color.
C. each Skip is independently effective — dropping one from the map affects
   ONLY that color, leaving the other Skip and all mapped labels untouched.

Not part of the production pipeline. Exits non-zero if any scenario fails.

Run:  python _validate_unknown_colors.py
"""
import io
import sys

import fitz

import slicer

# ── Fixture colors ────────────────────────────────────────────────────────────
# Real values lifted from the pest-mitten design + its Supabase colour_map, so
# this guard fails on the exact data that broke production.
MAPPED_EXACT = "#1E7534"   # mapped verbatim              -> 3560
DRIFT        = "#737272"   # map holds #737271 (dist 1.0) -> 6000  (tolerance path)
MAPPED_EXACT2 = "#D4D1CD"  # mapped verbatim              -> 1500
UNKNOWN      = "#00B7EB"   # cyan: nothing within tolerance, unknown in EVERY scenario
SKIP_PINK    = "#EEA8CA"   # the real pest-mitten accent; map holds #EEA8CB (dist 1.0)
SKIP_ORANGE  = "#FF7E00"   # second, independent Skip entry

PATCHES = [MAPPED_EXACT, DRIFT, MAPPED_EXACT2, UNKNOWN, SKIP_PINK, SKIP_ORANGE]

BASE_MAP = {
    "#1E7534": "3560",
    "#737271": "6000",     # deliberately NOT the #737272 that the PDF contains
    "#D4D1CD": "1500",
}
PINK_SKIP   = {"#EEA8CB": "Skip"}   # deliberately NOT the #EEA8CA in the PDF
ORANGE_SKIP = {"#FF7E00": "Skip"}

LABEL_CODES = {"3560", "6000", "1500"}

PTS_PER_M = 100.0
WIDTH_M, HEIGHT_M = 1.5, 4.0   # exactly one strip, exactly one page (no partial)


def _hex_to_01(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def build_design():
    """
    A 1.5 x 4 m design: six generous patches on the default white page.

    No background rectangle is drawn — the page is white by default, so
    get_drawings() reports exactly the six patch fills and nothing else. White
    is >80 RGB units from every mapped color, so background pixels are claimed
    by no mask. Patches are kept small relative to the page so neither the pink
    nor the orange one can trip is_fully_background()'s whole-page drop.
    """
    doc  = fitz.open()
    page = doc.new_page(width=WIDTH_M * PTS_PER_M, height=HEIGHT_M * PTS_PER_M)
    for i, hx in enumerate(PATCHES):
        row, col = divmod(i, 2)
        x0 = 10 + col * 70
        y0 = 10 + row * 130
        page.draw_rect(fitz.Rect(x0, y0, x0 + 60, y0 + 110),
                       color=None, fill=_hex_to_01(hx), width=0)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def labels_on(strip_bytes):
    """Every label code drawn on the strip, as a sorted list (page number dropped)."""
    doc = fitz.open(stream=strip_bytes, filetype="pdf")
    out = []
    for page in doc:
        for blk in page.get_text("dict")["blocks"]:
            for ln in blk.get("lines", []):
                for sp in ln["spans"]:
                    t = sp["text"].strip()
                    # The orange page number ("1") is inserted after labeling and
                    # is not a color label.
                    if t and t != "1":
                        out.append(t)
    doc.close()
    return sorted(out)


def scenario(name, colour_map, want_unknown, want_labels, pdf_bytes):
    result = slicer.run_slice(
        pdf_bytes, WIDTH_M, HEIGHT_M,
        banderoll=False, skip_colors=False, ruta_nedre=False,
        colour_map=colour_map,
    )
    got_unknown = sorted(result["unknown_colors"])
    got_labels  = sorted(l for s in result["strips"] for l in labels_on(s["bytes"]))

    ok_u = got_unknown == sorted(want_unknown)
    ok_l = got_labels  == sorted(want_labels)

    print(f"\n--- {name}")
    print(f"  map entries    : {len(colour_map)}")
    print(f"  unknown_colors : {got_unknown}")
    print(f"           expect: {sorted(want_unknown)}   {'OK' if ok_u else 'FAIL'}")
    print(f"  labels drawn   : {got_labels}")
    print(f"           expect: {sorted(want_labels)}   {'OK' if ok_l else 'FAIL'}")
    if "Skip" in got_labels:
        print("  FAIL: a 'Skip' entry was drawn as a label")
        return False
    return ok_u and ok_l


def main():
    pdf_bytes = build_design()

    found = sorted(slicer.extract_pdf_colors(pdf_bytes))
    if found != sorted(PATCHES):
        print(f"FIXTURE BROKEN: design fills {found} != {sorted(PATCHES)}")
        return 1
    print(f"Fixture: {WIDTH_M}x{HEIGHT_M} m, fills {found}")
    print(f"Constants: TOL={slicer.COLOR_MATCH_TOLERANCE} "
          f"MIN_PATCH_PX={slicer.MIN_PATCH_PX} "
          f"ENABLE_COLOR_LABELS={slicer.ENABLE_COLOR_LABELS}")

    results = []

    # A. The regression. Three unmappable colors present. Pre-fix this wiped the
    #    map and drew NOTHING; the three mapped colors must still be labeled.
    results.append(scenario(
        "A  unmapped colors must not suppress mapped labels (TIF-60 regression)",
        dict(BASE_MAP),
        want_unknown=[UNKNOWN, SKIP_PINK, SKIP_ORANGE],
        want_labels=LABEL_CODES,
        pdf_bytes=pdf_bytes,
    ))

    # B. Two independent Skip entries. Both known, both unlabeled, and the mapped
    #    labels are byte-for-byte the same set as in A -> no interference.
    results.append(scenario(
        "B  two independent Skip entries (pink + orange) coexist",
        {**BASE_MAP, **PINK_SKIP, **ORANGE_SKIP},
        want_unknown=[UNKNOWN],
        want_labels=LABEL_CODES,
        pdf_bytes=pdf_bytes,
    ))

    # C. Each Skip is independently effective: drop one, and ONLY its color
    #    returns to the unknown list. The other Skip keeps working.
    results.append(scenario(
        "C1 pink Skip only -> orange falls back to unknown, pink still skipped",
        {**BASE_MAP, **PINK_SKIP},
        want_unknown=[UNKNOWN, SKIP_ORANGE],
        want_labels=LABEL_CODES,
        pdf_bytes=pdf_bytes,
    ))
    results.append(scenario(
        "C2 orange Skip only -> pink falls back to unknown, orange still skipped",
        {**BASE_MAP, **ORANGE_SKIP},
        want_unknown=[UNKNOWN, SKIP_PINK],
        want_labels=LABEL_CODES,
        pdf_bytes=pdf_bytes,
    ))

    ok = all(results)
    print(f"\n{'ALL SCENARIOS PASS' if ok else 'FAILURES PRESENT'} "
          f"({sum(results)}/{len(results)})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
