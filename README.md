# hammaby-ruta-api

FastAPI microservice that slices tifo PDF designs into 1.5 m-wide vertical strips for print production.

## What it does

Accepts a PDF upload together with the physical dimensions (width × height in metres) and returns the PDF split into numbered strip files, each page rotated 90° to landscape. Handles:

- Arbitrary strip count calculated from `ceil(width / 1.5)`
- Bottom-to-top page ordering within each strip
- Pink partial-page padding with a dotted cut line and "Klipp" label
- Banderoll mode: rotates a landscape source PDF 90° before slicing
- Per-page colour code labels, enabled in production via `ENABLE_COLOR_LABELS`
- Parallel strip generation

## Run locally

```bash
pip install -r requirements.txt
uvicorn api:app --reload
```

API is then available at `http://localhost:8000`.

## Endpoints

### `GET /health`

Returns `{"status": "ok"}`. Use this as a liveness check.

### `POST /slice`

Multipart form upload. Parameters:

| Field | Type | Required | Description |
|---|---|---|---|
| `file` | PDF file | yes | The source PDF to slice |
| `width_m` | float | yes | Total design width in metres (e.g. `63.0`) |
| `height_m` | float | yes | Total design height in metres (e.g. `20.0`) |
| `banderoll` | bool | no | `true` if the PDF is landscape and should be rotated 90° first (default `false`) |
| `skip_colors` | bool | no | `true` to skip colour-label checking entirely (default `false`) |
| `ruta_nedre` | bool | no | `true` for the lower half of a split (tudelat) motif (default `false`) |
| `colour_map` | JSON string | no | Array of `{hex, ncs_code, tolerance}` sourced from the Supabase `colour_map` table. Collapsed to a `{hex: ncs_code}` dict. An `ncs_code` of `"Skip"` marks a colour as known-but-not-labeled. Per-entry `tolerance` is **not** consumed — matching uses the global `COLOR_MATCH_TOLERANCE`. |

Response (JSON):

```json
{
  "strips": [
    {
      "filename": "strip-01.pdf",
      "data": "<base64-encoded PDF bytes>"
    }
  ],
  "unknown_colors": ["#RRGGBB"]
}
```

`unknown_colors` lists the design colours that **no** `colour_map` entry claims — i.e. whose nearest mapped colour is further away than `COLOR_MATCH_TOLERANCE`. It is empty when colour labeling is skipped or every colour matches an entry.

Unknown colours are left unlabeled **individually**; they do not suppress labeling of the colours that *are* mapped (TIF-60 — before 1.3.0 a single unknown colour wiped the whole map and the strips shipped with no labels at all). A `"Skip"` entry counts as claiming its colour: known, deliberately unlabeled, never reported as unknown.

**Example with curl:**

```bash
curl -X POST http://localhost:8000/slice \
  -F "file=@design.pdf" \
  -F "width_m=6.0" \
  -F "height_m=4.0"
```

## Deploy to Railway

1. Push this repository to GitHub.
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub repo.
3. Railway detects Python via `requirements.txt` and builds automatically.
4. The `Procfile` sets the start command:
   ```
   web: uvicorn api:app --host 0.0.0.0 --port $PORT
   ```
5. Railway injects `$PORT` automatically — no manual config needed.
6. No colour-map file is needed in production: the caller sends the map in the `/slice` payload (sourced from the Supabase `colour_map` table). The local `color_map.json` is retired and used only by the standalone `ruta.py` Gmail script.

## File layout

```
hammaby-ruta-api/
├── api.py           # FastAPI app — HTTP layer only
├── slicer.py        # Core PDF slicing logic
├── color_map.json   # Legacy hex → colour-code map; used only by ruta.py, not the API
├── requirements.txt
├── Procfile         # Railway start command
└── .gitignore
```

## Relation to ruta.py

`ruta.py` (kept separately in `Ruta_New/`) is the original standalone script that polls Gmail, downloads PDF attachments, slices them, uploads strips to Google Drive, and labels processed threads. It is the authoritative backup and is **not modified** by this service. `slicer.py` is an extraction of the PDF geometry logic only — no Google API code.
