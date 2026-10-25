"""
synth.py — synthetic fixture generation for the ruta pipeline's test suite (TIF-88).

THE single place a synthetic fixture is ever produced. Nothing here runs on
import; every fixture is a lazy recipe (a list of drawing ops) that is executed
into a fresh document on demand, so building a fixture twice really does run the
generator twice — which is what _validate_synthetic.py's determinism check needs.

WHY A CANONICAL WRITER
----------------------
PyMuPDF is NOT byte-deterministic out of the box. Measured (TIF-88 proposal, and
re-measured on PyMuPDF 1.27.2.3): content streams, object numbering and xref
offsets already reproduce exactly, and a freshly created document carries no
/Info dictionary at all — but every save writes a trailer /ID array of two random
16-byte identifiers. Two saves of identical drawing code differ in those 32 bytes
and nowhere else.

The obvious fix is a trap: doc.xref_set_key(-1, "ID", "[<..><..>]") pins only
the FIRST element; the second is regenerated at save time regardless. So
canonical_bytes() post-processes the saved bytes instead, replacing the whole
array, and fails loudly if the trailer is not shaped the way that replacement
assumes (exactly one /ID array of two strings) or if any timestamp/producer field
has appeared. A second trap, found by that loud failure: the random elements are
not always HEX strings (see _pin_trailer_id), so the replacement parses PDF
strings rather than pattern-matching hex.
A fixture that does not go through canonical_bytes() is caught by the suite's
generate-twice digest comparison.

HAZARDS THE PRIMITIVES HANDLE FOR YOU
-------------------------------------
H1  page.draw_rect() strokes at 1 pt by default, inflating a patch by 1 pt on
    every side — exactly the magnitude of MIN_LABEL_PATCH_SIZE_PT. Every fill
    primitive here passes color=None, width=0. Do not call draw_rect yourself.
H2  Small patches on a background page leave the page >= FULLY_BG_THRESHOLD
    background, so is_fully_background() drops it and the case tests nothing.
    Use .anchor() and let the case assert its pages survived.
H3  Scale. Fixtures are drawn at slicer.PTS_PER_M (1:100), the scale every real
    design and every absolute-pt constant in slicer.py is calibrated against.
    A different ppm is allowed only for a case that is specifically about scale;
    pts-per-metre is quadratic in memory (TIF-100), so keep fixtures tiny.
H4  Colours are explicit hex RGB literals in the cases. Never NCS codes from
    memory.
"""
import hashlib
import math
import re

import fitz

import slicer

PPM = slicer.PTS_PER_M

# Both trailer /ID elements, fixed. Any 16-byte value works; this one is legible
# in a hex dump and obviously not random.
_CANON_ID = b"/ID[<54494638385F53594E5448464958><54494638385F53594E5448464958>]"
# Fields that would make output depend on WHEN or WITH WHAT it was written.
_LEAKS    = (b"/CreationDate", b"/ModDate", b"/Producer", b"/Creator",
             b"<x:xmpmeta", b"/Metadata")


def _pdf_string_end(raw, i):
    """Index just past the PDF string object starting at raw[i] — hex <..> or
    literal (..) with backslash escapes and balanced nesting."""
    if raw[i:i + 1] == b"<":
        j = raw.index(b">", i)
        assert re.fullmatch(rb"[0-9A-Fa-f\s]*", raw[i + 1:j]), "malformed hex string"
        return j + 1
    assert raw[i:i + 1] == b"(", f"expected a string object at {i}, got {raw[i:i + 8]!r}"
    depth, j = 0, i
    while True:
        c = raw[j:j + 1]
        if c == b"\\":
            j += 2
            continue
        if c == b"(":
            depth += 1
        elif c == b")":
            depth -= 1
            if depth == 0:
                return j + 1
        elif not c:
            raise AssertionError("unterminated literal string in trailer /ID")
        j += 1


def _pin_trailer_id(raw):
    """Replace the trailer's /ID[<a><b>] with _CANON_ID; return (bytes, n_found).

    A scanner, not a hex-only regex, on purpose: MuPDF writes each random /ID
    element as a HEX string most of the time but as a LITERAL string, e.g.
    (\\355\\031\\)...), whenever it chooses to (measured: about once per hundred
    saves). A hex-only pattern finds no /ID on those saves — a suite failing at
    random. Both forms are parsed, escapes and nesting included."""
    hits = [m.start() for m in re.finditer(rb"/ID\s*\[", raw)]
    if len(hits) != 1:
        return raw, len(hits)
    start = hits[0]
    j = raw.index(b"[", start) + 1
    elems = 0
    while True:
        while raw[j:j + 1].isspace():
            j += 1
        if raw[j:j + 1] == b"]":
            break
        j = _pdf_string_end(raw, j)
        elems += 1
    assert elems == 2, f"trailer /ID holds {elems} strings, expected 2"
    return raw[:start] + _CANON_ID + raw[j + 1:], 1


def canonical_bytes(doc):
    """Serialise `doc` to byte-deterministic PDF bytes. Fails loudly, never quietly.

    Deterministic per PyMuPDF install, not across versions: MuPDF stamps its own
    version into a header comment ("% Written by MuPDF 1.27.2"), which is not a
    time or a random value and is left alone. The suite compares digests within a
    run, so that is the guarantee it needs.
    """
    doc.set_metadata({})
    doc.del_xml_metadata()
    # MuPDF 1.27.2 writes /Info<</Producer(MuPDF 1.27.2)>> INLINE in the Catalog,
    # which set_metadata({}) does not remove (measured). null == absent in PDF.
    doc.xref_set_key(doc.pdf_catalog(), "Info", "null")
    raw = doc.tobytes()
    out, n = _pin_trailer_id(raw)
    # One trailer, one /ID. Anything else (an incremental save, a second trailer,
    # an /ID PyMuPDF stopped writing in a new form) means the replacement above
    # did not pin what it claims to, so stop rather than emit drifting bytes.
    assert n == 1, f"canonical_bytes: expected exactly one trailer /ID array, found {n}"
    for key in _LEAKS:
        assert key not in out, f"canonical_bytes: {key.decode()} leaked into the fixture"
    return out


def digest(data):
    return hashlib.sha256(data).hexdigest()


def rgb01(hex_c):
    h = hex_c.lstrip("#")
    assert len(h) == 6, f"colour must be #RRGGBB, got {hex_c!r}"
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def grid_floor(x, scale=None):
    """Largest multiple of 1/LABEL_RENDER_SCALE <= x: a boundary the content scan
    can resolve exactly (its rows are 1/S pt tall), so expectations can be exact."""
    s = scale or slicer.LABEL_RENDER_SCALE
    return math.floor(x * s + 1e-9) / s


def grid_ceil(x, scale=None):
    s = scale or slicer.LABEL_RENDER_SCALE
    return math.ceil(x * s - 1e-9) / s


class Design:
    """
    A one-page vector design, width_m x height_m at `ppm` pts/m, built from ops.

    Coordinates are DESIGN points: x from the left, y from the top, as drawn.
    Sequence helpers (content_seq) take a distance along the PRINTING sequence
    instead, which is what every Klipp rule is phrased in:
        ruta_nedre=True : seq = y            (top -> bottom)
        default         : seq = full_h - y   (bottom -> top)
    """

    def __init__(self, width_m, height_m, ppm=PPM):
        self.width_m, self.height_m, self.ppm = width_m, height_m, ppm
        self.full_w = width_m * ppm
        self.full_h = height_m * ppm
        self.ops = []
        self._cache = None

    # ── geometry the cases phrase expectations in ────────────────────────────
    def page_h(self, page_height_m=None):
        return (page_height_m or slicer.PAGE_HEIGHT_M) * self.ppm

    @property
    def strip_w(self):
        return slicer.STRIP_WIDTH_M * self.ppm

    def strip_x(self, s):
        return s * self.strip_w, min((s + 1) * self.strip_w, self.full_w)

    # ── primitives ───────────────────────────────────────────────────────────
    def rect(self, x0, y0, x1, y1, hex_c):
        """Filled rectangle, NO stroke (hazard H1)."""
        self.ops.append((float(x0), float(y0), float(x1), float(y1), hex_c))
        self._cache = None
        return self

    def background(self, hex_c):
        return self.rect(0, 0, self.full_w, self.full_h, hex_c)

    def band(self, y0, y1, hex_c, x0=0.0, x1=None):
        return self.rect(x0, y0, self.full_w if x1 is None else x1, y1, hex_c)

    def content_seq(self, seq_end, hex_c, nedre, seq_start=0.0, x0=0.0, x1=None):
        """Paint content over printing-sequence [seq_start, seq_end]."""
        if nedre:
            return self.band(seq_start, seq_end, hex_c, x0, x1)
        return self.band(self.full_h - seq_end, self.full_h - seq_start, hex_c, x0, x1)

    def patch(self, x, y, w, h, hex_c):
        return self.rect(x, y, x + w, y + h, hex_c)

    def patch_grid(self, x0, y0, nx, ny, side, gap, hex_c):
        for j in range(ny):
            for i in range(nx):
                self.patch(x0 + i * (side + gap), y0 + j * (side + gap), side, side, hex_c)
        return self

    def anchor(self, hex_c="#000000", y0=None, height=None):
        """A solid content block that keeps a page off FULLY_BG_THRESHOLD (H2).
        Default: the full strip width, the first 30% of the design's height."""
        h = self.full_h * 0.3 if height is None else height
        y = 0.0 if y0 is None else y0
        return self.band(y, y + h, hex_c)

    # ── output ───────────────────────────────────────────────────────────────
    def build(self):
        """Execute the ops into a NEW document every call; canonical bytes."""
        doc = fitz.open()
        page = doc.new_page(width=self.full_w, height=self.full_h)
        for x0, y0, x1, y1, hex_c in self.ops:
            page.draw_rect(fitz.Rect(x0, y0, x1, y1), color=None,
                           fill=rgb01(hex_c), width=0)
        out = canonical_bytes(doc)
        doc.close()
        return out

    def pdf(self):
        """Cached build, for driving the slicer (the determinism check calls build())."""
        if self._cache is None:
            self._cache = self.build()
        return self._cache

    def fills(self):
        return {h.upper() for *_r, h in self.ops}


class Banderoll(Design):
    """
    A banderoll upload: drawn here in HUNG coordinates (width_m x height_m as the
    user enters them), written out LANDSCAPE the way the artwork actually arrives,
    so run_slice(banderoll=True)'s rotate_pdf_90 turns it back into exactly the
    portrait Design with the same ops.

    Mapping measured on rotate_pdf_90 (clockwise): landscape (lx, ly) lands at
    portrait (ly, Lw - lx), Lw = the landscape width = the hung height. Inverted:
    hung (X, Y) comes from landscape (Lw - Y, X).
    """

    def portrait(self):
        """The same design as an ordinary (non-banderoll) upload."""
        d = Design(self.width_m, self.height_m, self.ppm)
        d.ops = list(self.ops)
        return d

    def build(self):
        doc = fitz.open()
        lw = self.full_h
        page = doc.new_page(width=lw, height=self.full_w)
        for x0, y0, x1, y1, hex_c in self.ops:
            page.draw_rect(fitz.Rect(lw - y1, x0, lw - y0, x1), color=None,
                           fill=rgb01(hex_c), width=0)
        out = canonical_bytes(doc)
        doc.close()
        return out


class Raster(Design):
    """
    A JPG/PNG upload (TIF-73): the same ops, rasterised at `px_scale` px per
    design pt and encoded. With alpha=True the unpainted area is transparent
    instead of white.

    px_scale is the IMAGE's resolution only. It does not touch hazard H3: a raster
    carries no page size, and image_to_pdf always lays it out at PTS_PER_M from
    width_m/height_m, whatever its pixel count.

    JPEG and PNG encoding through MuPDF carry no timestamps; the suite still
    digests the bytes twice rather than trusting that.
    """

    def __init__(self, width_m, height_m, fmt="png", alpha=False, px_scale=2.0,
                 px=None):
        super().__init__(width_m, height_m)
        assert fmt in ("png", "jpg")
        assert not (alpha and fmt == "jpg"), "JPEG has no alpha channel"
        self.fmt, self.alpha, self.px_scale = fmt, alpha, px_scale
        self.px = px     # (w, h) pixel size, overriding px_scale — aspect mismatch

    def build(self):
        doc = fitz.open()
        page = doc.new_page(width=self.full_w, height=self.full_h)
        for x0, y0, x1, y1, hex_c in self.ops:
            page.draw_rect(fitz.Rect(x0, y0, x1, y1), color=None,
                           fill=rgb01(hex_c), width=0)
        if self.px:
            mat = fitz.Matrix(self.px[0] / self.full_w, self.px[1] / self.full_h)
        else:
            mat = fitz.Matrix(self.px_scale, self.px_scale)
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB, alpha=self.alpha)
        out = pix.tobytes(output=self.fmt)
        doc.close()
        return out
