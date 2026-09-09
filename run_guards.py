# -*- coding: utf-8 -*-
"""
run_guards.py — run the colour-labeling regression sweep across the real test
designs. Promoted from tif69_test/run_all_designs.py + run_remaining.py (TIF-71)
because those were untracked scratch and this repo has no other test runner.

Drives the REAL production labeling path (via _design_label_harness.run_design,
which calls slicer.slice_one_strip end to end) on each design and checks the
ZERO-SKIP CONTRACT: every patch that is both >= MIN_PATCH_PX and at/above
MIN_LABEL_PATCH_SIZE_PT's inscribed-circle radius must get exactly one label.
Patches below the size floor are expected to be skipped and are reported
separately, so a before/after skip count stays visible per run.

Exit code is 0 only if every design run satisfies the contract.

Usage
-----
  python run_guards.py              # DEFAULT: kenta + pest-mitten + pest-ovre
  python run_guards.py --enad       # default set plus ENAD (41 strips, slow)
  python run_guards.py --full       # same as --enad
  python run_guards.py --only kenta # a single design by name
  python run_guards.py --list       # show the designs and exit

ENAD IS OPT-IN ON PURPOSE (TIF-71). It is 41 strips x 5 pages and dominates the
runtime of the sweep. The default three designs still cover both rotation modes
(pest-mitten runs ruta_nedre=True), a 16-colour real production map (pest-ovre),
and a distinct shape vocabulary (kenta) — enough signal for routine use. Reach
for --enad before shipping a change to the labeler or the font-sizing ladder.

Renders land in validation/<design>/ (gitignored, regenerable).

This runner covers the label harness only. The standalone regression guards are
separate scripts, each run on its own:
    _validate_color_labels.py          label placement on strip-15
    _validate_unknown_colors.py        TIF-60 colour-map wipe
    _validate_klipp_partial.py         TIF-57 partial-page Klipp, both modes
    _validate_content_cut.py           TIF-57 content-driven Klipp
    _validate_label_skip_decoupling.py TIF-57 skip_colors gate
    _validate_klipp_margin.py          TIF-67 margin + text ladder
    _validate_bg_page_exclusion.py     TIF-68 trailing background page
    _validate_raster_source.py         TIF-73 JPG/PNG sources
"""
import os
import sys

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

DEFAULT_SET = ["kenta", "pest-mitten", "pest-ovre"]


def main(argv):
    args = argv[1:]

    if "--list" in args:
        print("designs:")
        for name, (pdf, w, h, _m, nedre, strips) in DESIGNS.items():
            tag = " (default)" if name in DEFAULT_SET else " (opt-in: --enad)"
            n = "all" if strips is None else f"{len(strips)} of 14"
            print(f"  {name:<12} {pdf:<28} {w}x{h} m  nedre={nedre}  strips={n}{tag}")
        return 0

    if "--only" in args:
        i = args.index("--only")
        if i + 1 >= len(args):
            print("--only needs a design name; see --list", file=sys.stderr)
            return 2
        name = args[i + 1]
        if name not in DESIGNS:
            print(f"unknown design {name!r}; see --list", file=sys.stderr)
            return 2
        selected = [name]
    elif "--enad" in args or "--full" in args:
        selected = DEFAULT_SET + ["enad"]
    else:
        selected = list(DEFAULT_SET)

    print(f"slicer VERSION {slicer.VERSION}  "
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
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
