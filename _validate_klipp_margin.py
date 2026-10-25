"""
_validate_klipp_margin.py — TIF-67 guard for the Klipp line margin and the right-
only / shrink / skip text ladder. REPLACED by synthetic cases (TIF-88); this file
is kept as a shim so `python _validate_klipp_margin.py` still runs exactly that
coverage and exits non-zero on failure.

Where each original assertion now lives (synth_cases.py, section A):
  1  MARGIN APPLIED           klipp/mid-page/*, klipp/margin-toggle/0pt
  1b MARGIN DOUBLED 2 -> 4    klipp/margin-toggle/2pt
  2  ~5pt TOTAL-PINK SUPPRESS klipp/gate/5pt-gate-off/{toggled,real}
  2b THRESHOLD RAISE 6 -> 12  klipp/gate/9pt-old-6pt-gate/{toggled,real}
  3  FONT-FLOOR SKIP          klipp/text/font-floor-{fits,misses}, .../floor-0.1
  4  NO-LEFT SWEEP            klipp/sweep/* (+ the global no-left invariant, which
                              now runs on EVERY case, not just this sweep)
Additions the original could not express: the gate as a just-over/just-under pair
(klipp/gate/pink-just-*) and the width gate on its own (klipp/text/width-gate-*).

What changed in the port: the original drew its fixtures at 100 pt/m; the cases
are at 1:100 (slicer.PTS_PER_M), the scale every absolute-pt constant here is
calibrated against, and their boundaries are aligned to the scan grid, so the
"toggle and measure the shift" proofs became exact absolute positions.

Run:  python _validate_klipp_margin.py
"""
import sys

import _validate_synthetic

if __name__ == "__main__":
    sys.exit(_validate_synthetic.main(["-k", "klipp/"]))
