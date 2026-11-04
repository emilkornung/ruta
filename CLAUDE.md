# CLAUDE.md

## Working from a Linear ticket

- When Emil says "Read Linear TIF-NNN and follow the 'Claude Code prompt' section", fetch the ticket **and its comments** with the Linear tools and follow that section. The rest of the ticket is context. If the ticket has no "Claude Code prompt" section, ask instead of guessing.
- Write your report back as a **comment on that ticket** in plain markdown: headings and bullet lists, no box-drawing tables (they get garbled). Use normal markdown tables only if small.
- Review artifacts you create locally (draft PDFs, screenshots, renders) go in one dedicated folder for the ticket. List their **full local paths** in the comment.
- After posting, give a **short summary in chat** (a few lines) and the link to the comment.
- Do not post secrets, tokens or credentials in comments.
- Stop at any approval gate in the prompt (e.g. "wait for approval") and say so in the comment.

## What this repo is

- The Railway slicer API: FastAPI in `api.py`, PDF/raster slicing in `slicer.py`. Deployed via `Procfile` (`uvicorn api:app`).
- `ruta.py` and the Gmail pipeline are **not in this repo**. The README says the script is kept separately in `Ruta_New/`; `slicer.py` is only its geometry extraction. `color_map.json` is a legacy file the code no longer loads. Do not add Gmail, Drive or OAuth code here.
- Windows environment: use PowerShell, and set `PYTHONIOENCODING=utf-8` when running scripts that print non-ASCII.

## Stateless, no secrets

- ruta never gets Supabase credentials and reads no secrets. The colour map arrives in the `/slice` request (`colour_map`). Do not add database access, environment secrets or credentials.
- Known gap: the API has no authentication (parked in TIF-101). Do not fix it here.

## Colour map

- An `ncs_code` of `"Skip"` means the colour is known but never labeled.
- Never generate NCS codes from model recall. Codes come only from the colour map. Test maps (`DUMMY_MAP` etc.) are test data, not a source of truth.

## Workflow

- Feature branch, then a PR. Emil reviews and merges. Never merge, never push to `master`.
- Do not touch Railway, Supabase or Vercel unless the ticket says so.

## Guards

- Run `python run_guards.py` on every change. The default is the synthetic sweep (`_validate_synthetic.py`), which needs no design file. It has an enforced 10 s budget (`SYNTH_BUDGET_S`); overrunning fails the run.
- Options: `--real` (kenta, pest-mitten, pest-ovre), `--enad` or `--full` (those plus ENAD, slow), `--standalone` (every standalone `_validate_*.py`, combinable), `--only <design|synth>`, `--list`. Real designs are the untracked PDFs in the repo root; missing ones are reported as skipped, not passed.
- New behaviour needs a synthetic `Case` in `synth_cases.py`. `why` is required and must name the bug or ticket the case protects.

## Test output

- Review PNGs and PDFs for a ticket go in one dedicated folder for that ticket, cleared each round. List their full paths in the report.
- `validation/`, `raster_test/` and `tif87_out/` are gitignored scratch output.

## Versioning

- `VERSION` lives in `slicer.py` (served by `GET /health`).
- A behaviour change needs a `VERSION` bump, a `CHANGELOG.md` entry and a README update. Changes to CLAUDE.md only need none.
