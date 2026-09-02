from fastapi import FastAPI, UploadFile, File, Form, HTTPException
import base64
import json
import logging
from slicer import run_slice, VERSION

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

ACCEPTED_EXTENSIONS = ('.pdf', '.jpg', '.jpeg', '.png')


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
    # Raster uploads are converted to a single-page PDF inside run_slice (see
    # slicer.image_to_pdf); the actual format decision there sniffs magic bytes.
    # This extension check stays as the friendly front door — a wrong file type
    # fails here with a clear 400 instead of deep inside pymupdf.
    if not file.filename.lower().endswith(ACCEPTED_EXTENSIONS):
        raise HTTPException(
            400, f"Only {', '.join(ACCEPTED_EXTENSIONS)} files are accepted")

    banderoll_bool = parse_bool(banderoll)
    skip_colors_bool = parse_bool(skip_colors)
    ruta_nedre_bool = parse_bool(ruta_nedre)

    # Colour map arrives as a JSON array of {hex, ncs_code, tolerance} sourced
    # from Supabase (the web app's getColourMapForRuta). Collapse it into the
    # {hex: ncs_code} dict the labeling path consumes. Hex is upper-cased to
    # match the keys that labeling builds from rendered pixels.
    # Per-entry `tolerance` is not consumed: labeling matches against the single
    # global slicer.COLOR_MATCH_TOLERANCE band. Per-entry tolerance remains
    # unimplemented.
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

    # TIF-60: unknown colors used to silently wipe the whole colour_map and ship
    # unlabeled strips, with nothing in the logs to show for it. They no longer
    # suppress labeling, but they DO mean some patches went unlabeled — so say so
    # loudly. The caller also persists this list onto the job record.
    unknown = result["unknown_colors"]
    if unknown:
        logger.warning(
            f"{len(unknown)} unknown color(s) — no colour_map entry within "
            f"tolerance; these are left UNLABELED (all mapped colors still "
            f"labeled): {', '.join(unknown)}"
        )
    elif result["colors_analyzed"]:
        logger.info("All design colors matched a colour_map entry.")
    else:
        # Do NOT claim a clean bill of health here. unknown_colors is [] because
        # the check never ran — a raster upload has no vector fills for
        # extract_pdf_colors to read (labels are still drawn; they are placed from
        # rendered pixels). Unmapped colours in a raster design ship UNLABELED and
        # silently, so say that instead of "all colors matched".
        logger.info(
            "Unknown-color detection did not run (raster source or skip_colors); "
            "unknown_colors=[] means NOT CHECKED, not 'all colors matched'."
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
        "unknown_colors": result["unknown_colors"],
        "colors_analyzed": result["colors_analyzed"]
    }


@app.get("/health")
def health():
    return {"status": "ok", "version": VERSION}
