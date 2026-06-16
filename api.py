import logging
import base64

from fastapi import FastAPI, UploadFile, File, Form, HTTPException

from slicer import run_slice

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()


def parse_bool(value) -> bool:
    """Parse boolean from FormData string values ('true'/'false')."""
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
):
    logger.info(f"Received: width={width_m}, height={height_m}, banderoll={banderoll!r}, skip_colors={skip_colors!r}")

    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(400, "Only PDF files are accepted")

    banderoll_bool = parse_bool(banderoll)
    skip_colors_bool = parse_bool(skip_colors)

    logger.info(f"Parsed: banderoll={banderoll_bool}, skip_colors={skip_colors_bool}")

    pdf_bytes = await file.read()
    result = run_slice(pdf_bytes, width_m, height_m, banderoll_bool, skip_colors_bool)

    return {
        "strips": [
            {
                "filename": s["filename"],
                "data": base64.b64encode(s["bytes"]).decode(),
            }
            for s in result["strips"]
        ],
        "unknown_colors": result["unknown_colors"],
    }


@app.get("/health")
def health():
    return {"status": "ok"}
