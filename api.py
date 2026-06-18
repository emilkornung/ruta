from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse
import base64
from slicer import run_slice

app = FastAPI()


@app.post("/slice")
async def slice_pdf(
    file: UploadFile = File(...),
    width_m: float = Form(...),
    height_m: float = Form(...),
    banderoll: bool = Form(False),
    skip_colors: bool = Form(False),
):
    if not file.filename.endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted")

    pdf_bytes = await file.read()
    result = run_slice(pdf_bytes, width_m, height_m, banderoll, skip_colors)

    # Base64 encode strip bytes for JSON transport
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
