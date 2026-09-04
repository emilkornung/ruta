"""
Pest mitten validation (TIF-27, new design + ruta_nedre). Design:
'pest mitten 24x31,5m.pdf' (expected untracked in repo root), 892.9 x 680.4 pts
-> 31.5 x 24 m at 28.35 pts/m. Purpose: confirm zero-skip label sizing
generalizes to a new shape vocabulary AND to the ruta_nedre (top-to-bottom,
split-OH lower-half) slicing mode, neither of which has been exercised
together with color labeling before.

Colors: 18 raw vector fills, EACH matched independently against the
canonical known-colors table (color_map.json) at COLOR_MATCH_TOLERANCE.
Round 3 fix: rounds 1-2 pre-clustered raw fills at threshold 25 before
matching against the known table, which silently merged #313131 (22px,
really 8500) into the #392C1B (8010) bucket -- they're only 23.9 RGB units
apart, under the clustering threshold, even though they're two distinct
known codes 28 apart in tolerance terms. Same bug folded 2px of #231F20
into the #172816/7020 cluster when it's actually closer to #392C1B/8010.
Matching every raw fill independently (no pre-clustering) avoids this:
17 matched a known code; 1 (a faint pink accent, #EEA8CB) had no match
within tolerance and got placeholder code C1.

Run:  python _validate_pest_mitten_font_sizing.py           # all 21 strips
      python _validate_pest_mitten_font_sizing.py 2 7       # only these strip numbers

Shared runner: _design_label_harness.py (renders to validation/pest-mitten/).
"""
import sys

from _design_label_harness import run_design

DUMMY_MAP = {
    "#392C1B": "8010",   # nearest known #392C1B, dist 0.0
    "#231F20": "8010",   # nearest known #392C1B, dist 26.0 (round 3: was wrongly
                         # folded into the #172816/7020 cluster)
    "#6A4F32": "6020",   # nearest known #694E31, dist 1.7
    "#E5C699": "1515",   # nearest known #E6C699, dist 1.0
    "#172816": "7020",   # nearest known #0D2606, dist 19.0
    "#030505": "S",      # nearest known #000000, dist 7.7
    "#947042": "5030",   # nearest known #946F42, dist 1.0
    "#BD9164": "3030",   # nearest known #BC9063, dist 1.7
    "#1C5429": "5540",   # nearest known #1B5325, dist 4.2
    "#90908F": "4500",   # nearest known #8F8E8D, dist 3.0
    "#D4D2CE": "1500",   # nearest known #D4D1CD, dist 1.4
    "#313131": "8500",   # nearest known #313131, dist 0.0 (round 3: was wrongly
                         # folded into the #392C1B/8010 cluster, see docstring)
    "#1F763B": "3560",   # nearest known #1E7534, dist 7.1
    "#B2B0AF": "3000",   # nearest known #B2B0AE, dist 1.0
    "#737372": "6000",   # nearest known #737271, dist 1.4
    "#515150": "7500",   # nearest known #51504F, dist 1.4
    "#FFFFFF": "v",      # nearest known #FFFFFF, dist 0.0
    "#EEA8CB": "C1",     # faint pink accent — no known match (nearest dist 48.6)
}

OUT_DIR = "validation/pest-mitten"

if __name__ == "__main__":
    strips = [int(a) - 1 for a in sys.argv[1:]] or None
    num_strips = 21  # ceil(31.5 / 1.5)
    # Every strip exported as a labeled vector PDF (not just samples) per
    # this round's requirement.
    run_design("pest mitten 24x31,5m.pdf", 31.5, 24, DUMMY_MAP, "pest-mitten",
                strips, pdf_strips=set(range(1, num_strips + 1)),
                ruta_nedre=True, out_dir=OUT_DIR)
