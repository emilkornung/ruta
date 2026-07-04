"""
Kenta primary validation (TIF-27 round 3). Design: kenta.pdf (expected
untracked in repo root), 595 x 567.5 pts -> 21 x 20 m at the same 1:100
drawing scale as ENAD (28.35 pts/m). Purpose: confirm the fit-verified,
collision-aware, no-floor sizing generalizes to a different design's shapes,
not just ones tuned against ENAD.

Run:  python _validate_kenta_font_sizing.py           # all 14 strips
      python _validate_kenta_font_sizing.py 2 7       # only these strip numbers

Shared runner: _design_label_harness.py (renders to validation/kenta_*.png).
"""
import sys

from _design_label_harness import run_design

# Pixel-cluster representatives from kenta.pdf (57 raw vector fills, clustered
# at threshold 25 like ENAD). Placeholder C-codes except where a cluster
# coincides with the ENAD scheme: #0E6938 ~ ENAD #0F6B37 -> 3560,
# #FDC014 ~ ENAD #FECB03 -> 0580, white -> Vit.
DUMMY_MAP = {
    "#E4A323": "C1",     # orange-gold background (39% of pixels)
    "#918F8E": "C2",     # light grey
    "#737272": "C3",     # mid grey
    "#51504E": "C4",     # dark grey
    "#B1B1AE": "C5",     # pale grey
    "#D8BBA8": "C6",     # light skin tone
    "#BE9F8D": "C7",     # mid skin tone
    "#9B7D72": "C8",     # dark skin tone
    "#896D67": "C9",     # deepest skin/shadow
    "#0B9447": "C10",    # bright green
    "#D4D1CC": "C11",    # pale warm grey
    "#EEDBCD": "C12",    # pale rose
    "#0E6938": "3560",   # dark green — coincides with ENAD 3560
    "#FDC014": "0580",   # yellow — coincides with ENAD 0580
    "#FFFFFF": "Vit",    # white
    "#221F1F": "Skip",   # near-black outlines
}

if __name__ == "__main__":
    strips = [int(a) - 1 for a in sys.argv[1:]] or None
    run_design("kenta.pdf", 21, 20, DUMMY_MAP, "kenta", strips)
