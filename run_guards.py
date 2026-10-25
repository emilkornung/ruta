# -*- coding: utf-8 -*-
"""
run_guards.py — the regression sweep. Two tiers (TIF-88):

  FAST (default) — _validate_synthetic.py: every edge case built on purpose by
    synth.py, through the real slicer, in well under 10 s (enforced), with NO
    design file on disk. Run it on every change.

  REAL (opt-in) — the colour-labeling sweep across the real test designs,
    promoted from tif69_test/run_all_designs.py + run_remaining.py (TIF-71). It
    drives the REAL production labeling path (_design_label_harness.run_design ->
    slicer.slice_one_strip) and checks the ZERO-SKIP CONTRACT: every patch both
    >= MIN_PATCH_PX and at/above MIN_LABEL_PATCH_SIZE_PT's inscribed-circle radius
    gets exactly one label; sub-floor patches are reported, not failed. It asks a
    different question from the fast tier — does the whole ladder still behave on
    a real palette at real density — so it stays, but behind a flag.

Exit code is 0 only if everything that ran passed.

Usage
-----
  python run_guards.py              # DEFAULT: the synthetic sweep only (~3 s)
  python run_guards.py --real       # + kenta, pest-mitten, pest-ovre (the old default)
  python run_guards.py --enad       # + those three plus ENAD (41 strips, slow)
  python run_guards.py --full       # same as --enad
  python run_guards.py --standalone # + every standalone real-file _validate_*.py
                                    #   (combinable with --real / --enad)
  python run_guards.py --only kenta # a single real design by name (no synthetic)
  python run_guards.py --only synth # the synthetic sweep only
  python run_guards.py --list       # show the designs and exit

ENAD IS OPT-IN ON PURPOSE (TIF-71). It is 41 strips x 5 pages and dominates the
runtime of the sweep. The --real three still cover both rotation modes
(pest-mitten runs ruta_nedre=True), a 16-colour real production map (pest-ovre),
and a distinct shape vocabulary (kenta). Reach for --real before merging a slicer
change and --enad before shipping a change to the labeler or the font-sizing
ladder. Missing design PDFs are reported as skipped, never as a pass.

Renders land in validation/<design>/ and validation/_synth_out/ (gitignored).

The standalone guards (--standalone runs them as subprocesses). Synthetic ports
of their assertions run in the fast tier; the scripts themselves need the real
designs and remain the deeper check:
    _validate_color_labels.py          label placement on strip-15 (committed)
    _validate_klipp_partial.py         TIF-57 partial-page Klipp, both modes
    _validate_content_cut.py           TIF-57 content-driven Klipp
    _validate_label_skip_decoupling.py TIF-57 skip_colors gate
    _validate_bg_page_exclusion.py     TIF-68 trailing background page (+ lilac)
    _validate_raster_source.py         TIF-73 JPG/PNG sources (full image set)
    _validate_skissyta.py              TIF-87 configurable page height
Replaced by synthetic cases, kept as shims that run them:
    _validate_unknown_colors.py        TIF-60 colour-map wipe
    _validate_klipp_margin.py          TIF-67 margin + text ladder
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.chdir(ROOT)  # the DUMMY_MAP configs and the harness resolve paths from repo root

import slicer
from _design_label_harness import run_design
from _validate_enad_font_sizing import DUMMY_MAP as ENAD_MAP
from _validate_kenta_font_sizing import DUMMY_MAP as KENTA_MAP
from _validate_pest_mitten_font_sizing import DUMMY_MAP as MITTEN_MAP

# pest-ovre's real production colour map (TIF-68 round): all 16 of its real fill
# colours matched the canonical known-colours table within tolerance. Kept inline
# because pest-ovre has no _validate_*_font_sizing.py config of its own.
PEST_OVRE_MAP = {
    "#030405": "S",    "#162715": "7020", "#1C5329": "5540", "#1E753A": "3560",
    "#313131": "8500", "#392C1B": "8010", "#515150": "7500", "#694E31": "6020",
    "#737272": "6000", "#908F8E": "4500", "#946F42": "5030", "#B2B0AE": "3000",
    "#BC9164": "3030", "#D4D1CD": "1500", "#E5C598": "1515", "#EEA8CA": "Skip",
}

# kenta strip 14 (0-based index 13) is now INCLUDED again. It used to crash
# slice_one_strip with "cannot save with zero pages" — every page of that strip is
# fully background, TIF-68 correctly excludes all of them, and slice_one_strip had
# no handling for a strip left with zero pages. It was excluded here from TIF-69
# onward so the sweep would not die on a known unrelated fault.
#
# TIF-87 fixed it: such a strip now emits one blank pink page (keeping strip
# numbering dense) instead of raising. Measured to be invariant to the Skissyta
# page height — it fired identically at 1.0-8.0 m — so it was never a TIF-87
# regression, just a crash worth fixing while the surrounding code was open.
# Included again because it is now real coverage: a strip with nothing to label
# must still produce a file.
KENTA_STRIPS = list(range(14))

# name -> (pdf, width_m, height_m, colour map, ruta_nedre, strips or None)
DESIGNS = {
    "kenta":       ("kenta.pdf",                  21,   20, KENTA_MAP,     False, KENTA_STRIPS),
    "pest-mitten": ("pest mitten 24x31,5m.pdf",   31.5,  24, MITTEN_MAP,    True,  None),
    "pest-ovre":   ("pest övre 8x24m.pdf",         24,    8, PEST_OVRE_MAP, False, None),
    "enad":        ("ENAD_rutor.pdf",             61.5,  20, ENAD_MAP,      False, None),
}

REAL_SET = ["kenta", "pest-mitten", "pest-ovre"]   # was DEFAULT_SET before TIF-88
DEFAULT_SET = REAL_SET                            # kept for importers of the old name

STANDALONE = [
    "_validate_color_labels.py", "_validate_klipp_partial.py",
    "_validate_content_cut.py", "_validate_label_skip_decoupling.py",
    "_validate_bg_page_exclusion.py", "_validate_raster_source.py",
    "_validate_skissyta.py",
]


def run_synthetic():
    import _validate_synthetic
    print(f"{'=' * 70}\nsynthetic sweep (fast tier)\n{'=' * 70}")
    return _validate_synthetic.main([])


def run_standalone():
    """Each standalone guard in its own process, as it is run by hand."""
    results = {}
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    for script in STANDALONE:
        print(f"\n{'=' * 70}\n{script}\n{'=' * 70}", flush=True)
        t0 = time.time()
        rc = subprocess.call([sys.executable, script], cwd=ROOT, env=env)
        results[script] = rc
        print(f"-> exit {rc} in {time.time() - t0:.0f}s")
    print(f"\n{'=' * 70}\nSTANDALONE GUARDS\n{'=' * 70}")
    for script, rc in results.items():
        print(f"  {script:<38} {'PASS' if rc == 0 else f'FAIL (exit {rc})'}")
    return 0 if all(rc == 0 for rc in results.values()) else 1


def main(argv):
    args = argv[1:]

    if "--list" in args:
        print("synth        (default) the synthetic sweep, no design file needed")
        print("designs:")
        for name, (pdf, w, h, _m, nedre, strips) in DESIGNS.items():
            tag = " (--real)" if name in REAL_SET else " (--enad)"
            n = "all" if strips is None else f"{len(strips)} of 14"
            print(f"  {name:<12} {pdf:<28} {w}x{h} m  nedre={nedre}  strips={n}{tag}")
        return 0

    synth_rc = None
    if "--only" in args:
        i = args.index("--only")
        if i + 1 >= len(args):
            print("--only needs a design name; see --list", file=sys.stderr)
            return 2
        name = args[i + 1]
        if name == "synth":
            return run_synthetic()
        if name not in DESIGNS:
            print(f"unknown design {name!r}; see --list", file=sys.stderr)
            return 2
        selected = [name]
    else:
        synth_rc = run_synthetic()
        if "--enad" in args or "--full" in args:
            selected = REAL_SET + ["enad"]
        elif "--real" in args:
            selected = list(REAL_SET)
        else:
            selected = []

    standalone_rc = run_standalone() if "--standalone" in args else None
    if not selected:
        if synth_rc == 0 and standalone_rc in (None, 0):
            print("\n(real designs not run — pass --real, --enad or --standalone "
                  "for the deeper tier)")
        return 1 if synth_rc or standalone_rc else 0
    synth_failed = bool(synth_rc) or bool(standalone_rc)

    print(f"\nslicer VERSION {slicer.VERSION}  "
          f"MIN_LABEL_PATCH_SIZE_PT={slicer.MIN_LABEL_PATCH_SIZE_PT}")
    print(f"running: {', '.join(selected)}")
    if "enad" not in selected:
        print("(ENAD not included — pass --enad for the full sweep)")

    results, missing = {}, []
    for name in selected:
        pdf_path, w, h, dmap, nedre, strips = DESIGNS[name]
        if not os.path.exists(pdf_path):
            print(f"\nSKIP {name}: {pdf_path} not found in repo root")
            missing.append(name)
            continue
        print(f"\n{'=' * 70}\n{name}\n{'=' * 70}")
        results[name] = run_design(pdf_path, w, h, dmap, name,
                                   strips=strips, ruta_nedre=nedre,
                                   out_dir=os.path.join("validation", name))

    print(f"\n\n{'=' * 70}\nSUMMARY (MIN_LABEL_PATCH_SIZE_PT="
          f"{slicer.MIN_LABEL_PATCH_SIZE_PT}pt)\n{'=' * 70}")
    print(f"{'design':<14} {'before':>8} {'after':>8} {'skipped':>8} {'zero-skip':>11}")
    tb = ta = ts = 0
    for name, res in results.items():
        tb += res["before"]; ta += res["placed"]; ts += res["skipped_small"]
        print(f"{name:<14} {res['before']:>8} {res['placed']:>8} "
              f"{res['skipped_small']:>8} {'PASS' if res['zero_skips'] else 'FAIL':>11}")
    print(f"{'TOTAL':<14} {tb:>8} {ta:>8} {ts:>8}")

    failed = [n for n, r in results.items() if not r["zero_skips"]]
    if missing:
        print(f"\nnot run (design pdf absent): {', '.join(missing)}")
    if failed:
        print(f"\nZERO-SKIP CONTRACT VIOLATED: {', '.join(failed)}")
        return 1
    if not results:
        print("\nnothing ran — no design pdfs found in repo root")
        return 2
    print("\nall runs satisfied the zero-skip contract")
    if synth_failed:
        print("...but the synthetic sweep or a standalone guard FAILED (see above)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
