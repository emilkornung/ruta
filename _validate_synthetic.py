# -*- coding: utf-8 -*-
"""
_validate_synthetic.py — the FAST DEFAULT guard sweep (TIF-88): every case in
synth_cases.py, on fixtures generated fresh by synth.py, through the real slicer.

WHY THIS EXISTS
---------------
The guard suite used to depend entirely on four untracked real designs. A fresh
clone could not run it, the default sweep took ~385 s (content_cut alone 241.8 s,
ENAD ~890 s), and it only ever tested the edge cases a real design happened to
contain. This suite is committed, needs no file on disk, and builds each edge case
on purpose. The real designs stay as the opt-in deeper tier (run_guards.py --real
/ --enad): they answer a different question — does the whole ladder still behave
on a real palette at real density — and only this one needs to be fast.

WHAT A RUN ASSERTS
  1. Every case's expectations (synth_cases.py docstring lists the keys).
  2. Global invariants on EVERY case, so no case can quietly forget them:
       - "Klipp" text is never left of its line, never off-page, never on the
         page number, never without a line (TIF-57/67);
       - at most one cut line per strip (TIF-57);
       - zero-skip: every labelable patch got exactly one label (TIF-69 contract);
       - fitted labels never collide and stay inside their own colour (TIF-27);
       - no text escapes a page horizontally (TIF-87);
       - page numbers strictly increase (pages are skipped, numbers preserved);
       - SURVIVAL: every strip produced real pages unless the case expects a
         placeholder — a fixture eaten by is_fully_background fails loudly
         instead of silently testing nothing (hazard H2).
  3. DETERMINISM (hard requirement): every fixture is built twice in-process and
     the sha256 digests compared; a second process with a different
     PYTHONHASHSEED rebuilds them all and its manifest must match.
  4. BUDGET: the whole sweep, determinism included, must finish within
     SYNTH_BUDGET_S. Overrunning FAILS the run and prints the slowest cases.

A case marked xfail documents a real slicer finding that is deliberately not
fixed in a test PR. It must fail; if it starts passing the run fails (stale xfail).

Review renders land in validation/_synth_out/ (gitignored), cleared every run:
failing cases always, every case with --render.

Usage
-----
  python _validate_synthetic.py              # the sweep
  python _validate_synthetic.py --render     # ...and write every case's PNGs
  python _validate_synthetic.py -k klipp     # only cases whose id contains "klipp"
  python _validate_synthetic.py --list       # list cases and exit
  python _validate_synthetic.py --manifest   # print fixture digests (used by 3.)
"""
import math
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import fitz

import probe
import slicer
import synth_cases

SYNTH_BUDGET_S = 10.0
OUT_DIR = os.path.join(ROOT, "validation", "_synth_out")
CUT_TOL = 0.01          # exact: synthetic boundaries are aligned to the scan grid
EPS = 0.5               # pt slack for glyph-box comparisons (as the old guards)


class Outcome:
    def __init__(self, case):
        self.case, self.design = case, case.design
        self.pdf = None
        self.strips = []          # [{"num", "bytes", "pages": [page_facts]}]
        self.stdout = ""
        self.rec = None
        self.result = None        # run_slice result, via="job"


# ── execution ────────────────────────────────────────────────────────────────

def execute(case):
    o = Outcome(case)
    job = dict(case.job)
    via = job.pop("via", "strip")
    if case.design is None:
        return o
    o.pdf = case.design.pdf()
    d = case.design
    with probe.patched(case.patch), probe.LabelRecorder() as rec:
        o.rec = rec
        if via == "strip":
            strips = job.pop("strips", [job.pop("strip", 0)])
            if strips == "all":
                strips = range(math.ceil(d.width_m / slicer.STRIP_WIDTH_M))
            for s in strips:
                b, out = probe.drive_strip(
                    o.pdf, job.get("width_m", d.width_m), job.get("height_m", d.height_m),
                    s, job.get("nedre", False), job.get("cmap", {}),
                    skip_labels=job.get("skip_labels", True),
                    page_height_m=job.get("page_height_m"))
                o.stdout += out
                o.strips.append({"num": s + 1, "bytes": b})
        elif via == "job":
            w = job.pop("width_m", d.width_m)
            h = job.pop("height_m", d.height_m)
            o.result, o.stdout = probe.drive_job(o.pdf, w, h, **job)
            o.strips = [{"num": int(s["filename"][6:8]), "bytes": s["bytes"]}
                        for s in o.result["strips"]]
        else:
            raise ValueError(f"{case.id}: unknown via={via!r}")
    for st in o.strips:
        st["pages"] = probe.strip_facts(st["bytes"])
    return o


# ── checks ───────────────────────────────────────────────────────────────────

def invariants(o):
    fails = []
    for st in o.strips:
        tag = f"strip {st['num']}"
        lines = [(pg, x) for pg in st["pages"] for x in pg["lines"]]
        if len(lines) > 1:
            fails.append(f"{tag}: {len(lines)} cut lines — at most one per strip (TIF-57)")
        last_pn = 0
        for pg in st["pages"]:
            ptag = f"{tag} p{pg['index'] + 1}"
            k = pg["klipp"]
            if k is not None:
                kb = k["bbox"]
                if not pg["lines"]:
                    fails.append(f"{ptag}: 'Klipp' text without a cut line")
                elif kb[0] < pg["lines"][0] - EPS:
                    fails.append(f"{ptag}: 'Klipp' LEFT of the line (x0={kb[0]:.2f} < "
                                 f"{pg['lines'][0]:.2f}) — TIF-67 forbids it")
                if kb[0] < -EPS or kb[2] > pg["w"] + EPS:
                    fails.append(f"{ptag}: 'Klipp' off-page x[{kb[0]:.1f}..{kb[2]:.1f}]")
                if pg["pnum"] is not None:
                    pb = pg["pnum"]["bbox"]
                    if not (kb[2] < pb[0] or pb[2] < kb[0]):
                        fails.append(f"{ptag}: 'Klipp' collides with the page number")
            spans = [(t, bb) for t, bb, _s in pg["labels"]]
            if pg["klipp"]:
                spans.append(("Klipp", pg["klipp"]["bbox"]))
            if pg["pnum"]:
                spans.append((pg["pnum"]["text"], pg["pnum"]["bbox"]))
            for t, bb in spans:
                if bb[0] < -0.01 or bb[2] > pg["w"] + 0.01:
                    fails.append(f"{ptag}: text {t!r} escapes the page horizontally {bb}")
            if pg["pnum"] is None:
                fails.append(f"{ptag}: no page number")
            else:
                n = int(pg["pnum"]["text"])
                if n <= last_pn:
                    fails.append(f"{ptag}: page number {n} after {last_pn}")
                last_pn = n
    rec = o.rec
    if rec is not None and rec.calls:
        if rec.placed != rec.expected:
            fails.append(f"zero-skip violated: {rec.expected} labelable patches, "
                         f"{rec.placed} labels placed (TIF-69 contract)")
        if rec.escapes:
            fails.append(f"{len(rec.escapes)} fitted label(s) escape their own colour: "
                         f"{rec.escapes[:3]}")
        by_page = {}
        for key, code, r in rec.fit_rects:
            by_page.setdefault(key, []).append(fitz.Rect(*r))
        for rects in by_page.values():
            for i in range(len(rects)):
                for j in range(i + 1, len(rects)):
                    if rects[i].intersects(rects[j]):
                        fails.append(f"fitted labels collide: {rects[i]} vs {rects[j]}")
    # SURVIVAL (H2): the case is only meaningful if its pages exist.
    want_ph = o.case.expect.get("placeholder", set())
    for i, st in enumerate(o.strips):
        is_ph = len(st["pages"]) == 1 and bool(st["pages"][0]["placeholder"])
        if not st["pages"]:
            fails.append(f"strip {st['num']}: no pages at all")
        elif is_ph and i not in want_ph:
            fails.append(f"strip {st['num']}: only the all-background placeholder came "
                         f"out — the fixture was eaten by is_fully_background (H2), so "
                         f"this case tests nothing")
    return fails


def _per_strip(v, n):
    """Expectation value -> one entry per strip (scalar applies to all)."""
    if isinstance(v, dict):
        return [v.get(i, "_any") for i in range(n)]
    if isinstance(v, list):
        return v
    return [v] * n


def _cut_of(st):
    for pg in st["pages"]:
        if pg["lines"]:
            return int(pg["pnum"]["text"]) - 1 if pg["pnum"] else None, pg["lines"][0], pg
    return None


def evaluate(o):
    e, fails = o.case.expect, []
    n = len(o.strips)
    if "strips" in e and n != e["strips"]:
        fails.append(f"{n} strips, expected {e['strips']}")
    if "pages" in e:
        got = [len(st["pages"]) for st in o.strips]
        want = _per_strip(e["pages"], n)
        if any(w != "_any" and g != w for g, w in zip(got, want)) or len(want) != n:
            fails.append(f"pages per strip {got}, expected {want}")
    if "placeholder" in e:
        got = {i for i, st in enumerate(o.strips)
               if len(st["pages"]) == 1 and st["pages"][0]["placeholder"]}
        if got != set(e["placeholder"]):
            fails.append(f"placeholder strips {sorted(got)}, expected {sorted(e['placeholder'])}")
    tol = e.get("cut_tol", CUT_TOL)
    if "cut" in e:
        for i, (st, want) in enumerate(zip(o.strips, _per_strip(e["cut"], n))):
            if want == "_any":
                continue
            got = _cut_of(st)
            if want is None and got is not None:
                fails.append(f"strip {i + 1}: cut line at page {got[0] + 1} x={got[1]:.3f}, "
                             f"expected NONE")
            elif want is not None and got is None:
                fails.append(f"strip {i + 1}: no cut line, expected page {want[0] + 1} "
                             f"x={want[1]:.3f}")
            elif want is not None and (got[0] != want[0] or abs(got[1] - want[1]) > tol):
                fails.append(f"strip {i + 1}: cut at page {got[0] + 1} x={got[1]:.3f}, "
                             f"expected page {want[0] + 1} x={want[1]:.3f} (tol {tol})")
    if "text" in e or "text_size" in e:
        for i, st in enumerate(o.strips):
            ks = [pg["klipp"] for pg in st["pages"] if pg["klipp"]]
            want = _per_strip(e.get("text", "_any"), n)[i]
            if want != "_any" and bool(ks) != want:
                fails.append(f"strip {i + 1}: Klipp text {'present' if ks else 'absent'}, "
                             f"expected {'present' if want else 'absent'}")
            if "text_size" in e and ks:
                lo, hi = e["text_size"]
                if not lo - 1e-6 <= ks[0]["size"] <= hi + 1e-6:
                    fails.append(f"strip {i + 1}: Klipp at {ks[0]['size']:.3f}pt, "
                                 f"expected [{lo}, {hi}]")
    if "pads" in e:
        got = sum(len(pg["pads"]) for st in o.strips for pg in st["pages"])
        if got != e["pads"]:
            fails.append(f"{got} PINK_PAD pads, expected {e['pads']}")
    for key, axis in (("page_w", "w"), ("page_h", "h")):
        if key in e:
            for i, (st, want) in enumerate(zip(o.strips, _per_strip(e[key], n))):
                if want == "_any":
                    continue
                bad = [round(pg[axis], 2) for pg in st["pages"] if abs(pg[axis] - want) > 0.01]
                if bad:
                    fails.append(f"strip {i + 1}: page {axis} {bad}, expected {want:.2f}")
    if "pnums" in e:
        for i, (st, want) in enumerate(zip(o.strips, _per_strip(e["pnums"], n))):
            got = [pg["pnum"]["text"] if pg["pnum"] else None for pg in st["pages"]]
            if want != "_any" and got != want:
                fails.append(f"strip {i + 1}: page numbers {got}, expected {want}")
    if "labels" in e:
        got = {c: k for c, k in o.rec.by_code.items() if k}
        want = {c: k for c, k in e["labels"].items() if k}
        if got != want:
            fails.append(f"labels placed {got}, expected {want}")
    if "skipped_small" in e and o.rec.skipped_small != e["skipped_small"]:
        fails.append(f"{o.rec.skipped_small} patches skipped as sub-"
                     f"{slicer.MIN_LABEL_PATCH_SIZE_PT}pt, expected {e['skipped_small']}")
    if "forced" in e and o.rec.forced != e["forced"]:
        fails.append(f"{o.rec.forced} forced placements, expected {e['forced']}")
    if "unknown" in e and sorted(o.result["unknown_colors"]) != sorted(e["unknown"]):
        fails.append(f"unknown_colors {o.result['unknown_colors']}, expected {e['unknown']}")
    if "colors_analyzed" in e and o.result["colors_analyzed"] is not e["colors_analyzed"]:
        fails.append(f"colors_analyzed={o.result['colors_analyzed']}, "
                     f"expected {e['colors_analyzed']}")
    for s in e.get("stdout_has", []):
        if s not in o.stdout:
            fails.append(f"slicer output lacks {s!r}")
    for s in e.get("stdout_lacks", []):
        if s in o.stdout:
            fails.append(f"slicer output unexpectedly has {s!r}")
    return fails


def run_case(case):
    t0 = time.perf_counter()
    try:
        o = execute(case)
        fails = invariants(o) + evaluate(o)
        if case.check:
            with probe.patched(case.patch):
                fails += case.check(o)
    except Exception as ex:          # a crash is a failure of THIS case, not the run
        o, fails = Outcome(case), [f"raised {type(ex).__name__}: {ex}"]
    return o, fails, time.perf_counter() - t0


# ── determinism ──────────────────────────────────────────────────────────────

def _designs():
    """Every distinct fixture the suite drives, keyed by the first case using it
    (several cases share one design; check-only cases have none)."""
    seen, out = set(), []
    for c in synth_cases.CASES:
        if c.design is not None and id(c.design) not in seen:
            seen.add(id(c.design))
            out.append((c.id, c.design))
    return out


def manifest():
    """id -> digest of a FRESH build (build() reruns the generator every call)."""
    return {cid: probe.hashlib.sha256(d.build()).hexdigest() for cid, d in _designs()}


def start_cross_process():
    env = dict(os.environ, PYTHONHASHSEED="12345", PYTHONIOENCODING="utf-8")
    return subprocess.Popen([sys.executable, os.path.abspath(__file__), "--manifest"],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def determinism(proc):
    fails = []
    first = manifest()
    second = manifest()
    for cid in first:
        if first[cid] != second[cid]:
            fails.append(f"{cid}: two in-process builds differ "
                         f"({first[cid][:12]} vs {second[cid][:12]})")
    out, err = proc.communicate(timeout=60)
    if proc.returncode != 0:
        fails.append(f"cross-process manifest failed: {err.strip()[-300:]}")
    else:
        other = dict(line.split("  ", 1)[::-1] for line in out.strip().splitlines())
        for cid, dg in first.items():
            if other.get(cid) != dg:
                fails.append(f"{cid}: differs across processes "
                             f"({dg[:12]} vs {str(other.get(cid))[:12]})")
    return fails, len(first)


# ── main ─────────────────────────────────────────────────────────────────────

def main(argv):
    if "--manifest" in argv:
        for cid, dg in manifest().items():
            print(f"{dg}  {cid}")
        return 0
    cases = synth_cases.CASES
    if "-k" in argv:
        sub = argv[argv.index("-k") + 1]
        cases = [c for c in cases if sub in c.id]
    if "--list" in argv:
        for c in cases:
            print(f"{c.id:<52} {'[xfail] ' if c.xfail else ''}{c.why[:70]}")
        return 0
    render_all = "--render" in argv

    t0 = time.perf_counter()
    if os.path.isdir(OUT_DIR):
        shutil.rmtree(OUT_DIR)
    os.makedirs(OUT_DIR)
    proc = start_cross_process()     # overlaps with the cases below

    print(f"synthetic sweep: {len(cases)} cases  slicer VERSION {slicer.VERSION}  "
          f"budget {SYNTH_BUDGET_S:.0f}s")
    timings, failed, xfailed, xpassed = [], [], [], []
    for c in cases:
        o, fails, dt = run_case(c)
        timings.append((dt, c.id))
        if c.xfail:
            if fails and not any(c.xfail_on in f for f in fails):
                failed.append(c)
                print(f"  FAIL {c.id}  ({dt:.2f}s) — xfail, but NOT for its stated reason "
                      f"({c.xfail_on!r})")
                for f in fails:
                    print(f"        - {f}")
            elif fails:
                xfailed.append(c)
                print(f"  XFAIL {c.id}  ({dt:.2f}s)\n        {c.xfail}")
                for f in fails:
                    print(f"        observed: {f}")
            else:
                xpassed.append(c)
                print(f"  XPASS {c.id} — marked xfail but passed; remove the xfail "
                      f"(was: {c.xfail})")
        elif fails:
            failed.append(c)
            print(f"  FAIL {c.id}  ({dt:.2f}s)\n        why: {c.why}")
            for f in fails:
                print(f"        - {f}")
        else:
            print(f"  ok   {c.id}  ({dt:.2f}s)")
        if fails or render_all:
            stem = c.id.replace("/", "__").replace("#", "")
            for st in o.strips:
                probe.save_pngs(st["bytes"], OUT_DIR, f"{stem}__s{st['num']}")

    dfails, nfix = determinism(proc) if "-k" not in argv else ([], 0)
    elapsed = time.perf_counter() - t0
    if "-k" not in argv:
        print(f"\ndeterminism: {nfix} fixtures built twice in-process + once in a second "
              f"process (PYTHONHASHSEED=12345): "
              f"{'IDENTICAL' if not dfails else 'DRIFT'}")
        for f in dfails:
            print(f"  - {f}")

    over = elapsed > SYNTH_BUDGET_S
    print(f"\nelapsed {elapsed:.2f}s of {SYNTH_BUDGET_S:.0f}s budget"
          f"{'  — OVER BUDGET' if over else ''}")
    if over or "--slowest" in argv:
        print("slowest cases:")
        for dt, cid in sorted(timings, reverse=True)[:8]:
            print(f"  {dt:6.2f}s  {cid}")

    passed = len(cases) - len(failed) - len(xfailed) - len(xpassed)
    print(f"\n{passed} passed, {len(failed)} failed, {len(xfailed)} xfail, "
          f"{len(xpassed)} xpass   (renders: {os.path.relpath(OUT_DIR, ROOT)})")
    ok = not failed and not xpassed and not dfails and not over
    print("SYNTHETIC SWEEP:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
