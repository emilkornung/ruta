"""
_validate_partial_pad.py — TIF-96 permanent regression guard: a geometrically
partial page is padded with the colour the content ENDS in, not fixed PINK_PAD.

Synthesises a 1.5 m x 5 m single-strip design (so with the 4 m default page the
last page is a 1 m partial) and slices it through the real _slice_pdf in BOTH
modes. The padding abuts design TOP (default rotation) or design BOTTOM
(ruta_nedre) — those are the only two padded sides, so each case runs in both.

Cases: lilac-, orange-, pink- and blue-ended designs (padding must equal the end
colour, and no stray PINK_PAD may remain unless the end colour IS pink-ish), plus
two-colour split and gradient edges, where the padding must CONTINUE the edge
position by position (each pad row == the adjacent content pixel, both rotations). Also checks the example fixture
(Example color add.pdf) when it is present next to this file or in ~/Downloads.

Not part of the production pipeline. Exits non-zero on any failure.
Run:  python _validate_partial_pad.py
"""
import os
import sys

import fitz

import slicer

PTS = 28.3465
W, H = 1.5 * PTS, 5 * PTS
PINK_PX = tuple(round(c * 255) for c in (slicer.PINK_PAD_R, slicer.PINK_PAD_G, slicer.PINK_PAD_B))


def _f(c):
    return tuple(v / 255 for v in c)


def build(edge, nedre):
    """edge: ('solid', rgb) | ('split', rgb_a, rgb_b) | ('gradient',)."""
    d = fitz.open()
    p = d.new_page(width=W, height=H)
    band = fitz.Rect(0, H - PTS, W, H) if nedre else fitz.Rect(0, 0, W, PTS)  # last 1 m
    body = fitz.Rect(0, 0, W, H)
    p.draw_rect(body, fill=(1, 1, 1), color=None)
    p.draw_rect(band, fill=(0.9, 0.9, 0.9), color=None)
    kind = edge[0]
    e = 10  # painted edge depth, pt (>= END_COLOUR_BAND_PT)
    r = fitz.Rect(0, H - e, W, H) if nedre else fitz.Rect(0, 0, W, e)
    if kind == "solid":
        p.draw_rect(r, fill=_f(edge[1]), color=None)
    elif kind == "split":
        p.draw_rect(fitz.Rect(r.x0, r.y0, W / 2, r.y1), fill=_f(edge[1]), color=None)
        p.draw_rect(fitz.Rect(W / 2, r.y0, r.x1, r.y1), fill=_f(edge[2]), color=None)
    else:  # gradient across the strip width
        n = 40
        for i in range(n):
            v = i / (n - 1)
            p.draw_rect(fitz.Rect(W * i / n, r.y0, W * (i + 1) / n + 1, r.y1),  # +1: overlap, no seams
                        fill=(v, 0.2, 1 - v), color=None)
    # real content inside the partial page, clear of the edge band
    y = (H - 0.6 * PTS) if nedre else 0.3 * PTS
    p.draw_rect(fitz.Rect(W * .3, y, W * .7, y + 0.3 * PTS), fill=(.2, .2, .2), color=None)
    return d.tobytes()


def pad_pixels(strip_bytes):
    doc = fitz.open(stream=strip_bytes, filetype="pdf")
    pg = doc[len(doc) - 1]
    pm = pg.get_pixmap(dpi=72)
    x = pm.width - 20  # well inside the pad, clear of the page number
    return len(doc), [tuple(pm.pixel(x, int(pm.height * f))[:3]) for f in (.25, .5, .75)]


def check_continues(strip_bytes):
    """TIF-96 per-position rule: every pad row equals the content pixel next to it
    (the design's own colour continues across the seam), and no PINK_PAD remains."""
    doc = fitz.open(stream=strip_bytes, filetype="pdf")
    pm = doc[len(doc) - 1].get_pixmap(dpi=72)
    px_ = int(PTS) - 3                      # a column just inside the 1 m partial content
    x = pm.width - 20
    bad, tol = [], 12                       # gradient steps / seam blend: a few levels
    for y in range(3, pm.height - 3):
        b = pm.pixel(x, y)[:3]
        up, dn = pm.pixel(px_, y - 1)[:3], pm.pixel(px_, y + 1)[:3]
        if max(abs(p - q) for p, q in zip(up, dn)) > 3 * tol:
            continue                        # a genuine colour boundary: the seam row blends
        # +/-1 row: a colour boundary lands within half a pt of where the 0.5 pt
        # sampling grid puts it, so the seam row may match its neighbour instead.
        if not any(max(abs(p - q) for p, q in zip(pm.pixel(px_, yy)[:3], b)) <= tol
                   for yy in (y - 1, y, y + 1)):
            bad.append(f"row {y}: pad {b} != adjacent content {pm.pixel(px_, y)[:3]}")
        if b == PINK_PX:
            bad.append(f"row {y}: stray PINK_PAD in padding")
    return bad[:3]


def main():
    fails, n = [], 0
    lilac, orange, pink, blue = (249, 205, 231), (255, 128, 0), (238, 168, 203), (30, 90, 200)
    cases = [("lilac", ("solid", lilac), lilac), ("orange", ("solid", orange), orange),
             ("pink", ("solid", pink), pink), ("blue", ("solid", blue), blue),
             ("split", ("split", (200, 30, 30), blue), "continue"),
             ("gradient", ("gradient",), "continue")]
    for nedre in (False, True):
        for name, edge, want in cases:
            n += 1
            tag = f"{name} {'ruta_nedre' if nedre else 'default'}"
            res = slicer._slice_pdf(build(edge, nedre), 1.5, 5, color_map={}, ruta_nedre=nedre)
            pages, px = pad_pixels(res[0][1])
            if pages != 2:
                fails.append(f"{tag}: expected 2 pages, got {pages}")
                continue
            if want == "continue":
                fails.extend(f"{tag}: {m}" for m in check_continues(res[0][1]))
            else:
                if any(p != want for p in px):
                    fails.append(f"{tag}: pad {px} != end colour {want}")
                if want != PINK_PX and any(p == PINK_PX for p in px):
                    fails.append(f"{tag}: stray PINK_PAD left in padding")
            print(f"  {'ok ' if not any(tag in f for f in fails) else 'FAIL'} {tag}: pad={px[1]}")

    ex = next((p for p in (os.path.join(os.path.dirname(os.path.abspath(__file__)), "Example color add.pdf"),
                           os.path.expanduser("~/Downloads/Example color add.pdf")) if os.path.exists(p)), None)
    if ex:
        # The fixture is already a sliced OUTPUT: page 5's left half is the content
        # end colour, right half is the old PINK_PAD. Re-derive what the new rule
        # picks from its content half and check it is #f9cde7-ish, not pink.
        doc = fitz.open(ex)
        pm = doc[4].get_pixmap(dpi=72)
        left = tuple(pm.pixel(pm.width // 4, pm.height // 2)[:3])
        n += 1
        if left != (248, 205, 231):
            fails.append(f"example page 5 content half is {left}, expected (248,205,231)")
        else:
            print(f"  ok  example fixture content-end colour {left} (pad was {tuple(pm.pixel(pm.width * 3 // 4, pm.height // 2)[:3])})")
    else:
        print("  (Example color add.pdf not found — fixture check skipped)")

    print(f"\n{n} checks, {len(fails)} failures")
    for f in fails:
        print("FAIL:", f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
