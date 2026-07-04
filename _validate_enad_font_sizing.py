"""
ENAD regression validation (TIF-27). Design: ENAD_rutor.pdf (expected
untracked in repo root), 61.5 x 20 m ("FRAMÅT MED ENAD KRAFT!"), 28.35 pts/m.
17 raw vector fills cluster (threshold 25, legacy test_colors.py) to the ~10
pixel-level colors mapped below.

Run:  python _validate_enad_font_sizing.py            # all 41 strips
      python _validate_enad_font_sizing.py 4 16 26    # only these strip numbers

Shared runner: _design_label_harness.py (renders to validation/enad_*.png).
"""
import sys

from _design_label_harness import run_design

# Cluster representatives from ENAD_rutor.pdf pixel analysis. Same format as
# color_map.json ("Skip" = acknowledged, never labeled).
DUMMY_MAP = {
    "#F7931D": "0505",   # orange background — the giant-patch stress case
    "#FECB03": "0580",   # yellow (crest, glow outlines)
    "#0D5129": "3580",   # darkest green
    "#0F6B37": "3560",   # dark green
    "#268E58": "3050",   # mid green
    "#72A982": "3020",   # sage
    "#B3C8BB": "1010",   # pale sage
    "#FFFFFF": "Vit",    # white letters / shirts
    "#EBECE8": "Skip",   # off-white highlight tint
    "#2E251E": "Skip",   # near-black outlines
}

if __name__ == "__main__":
    strips = [int(a) - 1 for a in sys.argv[1:]] or None
    run_design("ENAD_rutor.pdf", 61.5, 20, DUMMY_MAP, "enad", strips)
