from fastapi import FastAPI, UploadFile, File, Form, HTTPException
import base64
import json
import logging
from slicer import run_slice, VERSION

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()


def parse_bool(value) -> bool:
    """Safely parse boolean from FormData string values."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ('true', '1', 'yes')
    return bool(value)


@app.post("/slice")
async def slice_pdf(
    file: UploadFile = File(...),
    width_m: float = Form(...),
    height_m: float = Form(...),
    banderoll: str = Form('false'),
    skip_colors: str = Form('false'),
    ruta_nedre: str = Form('false'),
    colour_map: str = Form('')
):
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(400, "Only PDF files are accepted")

    banderoll_bool = parse_bool(banderoll)
    skip_colors_bool = parse_bool(skip_colors)
    ruta_nedre_bool = parse_bool(ruta_nedre)

    # Colour map arrives as a JSON array of {hex, ncs_code, tolerance} sourced
    # from Supabase (the web app's getColourMapForRuta). Collapse it into the
    # {hex: ncs_code} dict the active _add_color_labels() path consumes. Hex is
    # upper-cased to match the keys that labeling builds from rendered pixels.
    # tolerance is not consumed by the current exact-match labeling path; it is
    # reserved for the TIF-27 tolerance-band labeling once that is enabled.
    colour_map_list = json.loads(colour_map) if colour_map else []
    colour_map_dict = {
        entry["hex"].upper(): entry["ncs_code"]
        for entry in colour_map_list
        if entry.get("hex") and entry.get("ncs_code")
    }

    logger.info(
        f"Request: width={width_m}, height={height_m}, "
        f"banderoll={banderoll!r} → {banderoll_bool}, "
        f"skip_colors={skip_colors!r} → {skip_colors_bool}, "
        f"ruta_nedre={ruta_nedre!r} → {ruta_nedre_bool}, "
        f"colour_map={len(colour_map_dict)} entries"
    )

    pdf_bytes = await file.read()
    result = run_slice(
        pdf_bytes,
        width_m,
        height_m,
        banderoll=banderoll_bool,
        skip_colors=skip_colors_bool,
        ruta_nedre=ruta_nedre_bool,
        colour_map=colour_map_dict
    )

    return {
        "strips": [
            {
                "filename": s["filename"],
                "data": base64.b64encode(s["bytes"]).decode()
            }
            for s in result["strips"]
        ],
        "grid_pdf": base64.b64encode(result["grid_pdf"]).decode(),
        "unknown_colors": result["unknown_colors"]
    }


@app.get("/health")
def health():
    return {"status": "ok", "version": VERSION}
