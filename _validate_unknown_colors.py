"""
_validate_unknown_colors.py — TIF-60 guard for unknown-colour handling in
run_slice() and for independent "Skip" entries. REPLACED by synthetic cases
(TIF-88); kept as a shim so `python _validate_unknown_colors.py` still runs exactly
that coverage and exits non-zero on failure.

WHY IT EXISTS (unchanged): run_slice() once wiped the WHOLE colour map when a
design held a single unmapped colour, shipping strips with zero labels (ruta_jobs
5ac70b43, pest mitten, 2026-07-11); and exact-hex membership reported tolerance-
drifted fills (#737272 vs mapped #737271) as unknown. Both are pinned through the
real run_slice() entry point, the one api.py calls.

Where each original scenario now lives (synth_cases.py, section G):
  fixture fills == the six patches    _fills_are_patches (every scenario)
  A  unmapped must not suppress       unknown/A-unmapped-must-not-suppress
  B  two independent Skips coexist    unknown/B-two-skips-coexist
  C1 pink Skip only                   unknown/C1-pink-skip-only
  C2 orange Skip only                 unknown/C2-orange-skip-only
"Skip never drawn as a label" is implied: each scenario expects exactly the three
mapped codes. Same colours as the original (real pest-mitten values); the fixture
is at 1:100 instead of 100 pt/m.

Run:  python _validate_unknown_colors.py
"""
import sys

import _validate_synthetic

if __name__ == "__main__":
    sys.exit(_validate_synthetic.main(["-k", "unknown/"]))
