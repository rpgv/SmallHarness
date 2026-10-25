"""Turn files into indexable text: plain text as is, PDFs and images through OCR.

OCR uses external programs so the harness stays stdlib-only: poppler's `pdftotext`
and `pdftoppm` for PDFs, and `tesseract` for images and rendered PDF pages. The
commands are set in [rag.ocr].
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


class ExtractError(Exception):
    """The file cannot be turned into text (missing OCR program, failed command, …)."""


def kind_of(path: Path, rag_cfg: dict) -> str | None:
    """'text', 'pdf', 'image', or None if the file type is not accepted."""
    suffix = path.suffix.lower()
    for kind in ("text", "pdf", "image"):
        if suffix in rag_cfg[f"{kind}_suffixes"]:
            return kind
    return None


def size_limit_mb(kind: str, rag_cfg: dict) -> float:
    return rag_cfg["max_text_mb"] if kind == "text" else rag_cfg["max_ocr_mb"]


def extract(path: Path, kind: str, rag_cfg: dict) -> str:
    if kind == "text":
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ExtractError("not UTF-8 text") from None
    ocr = rag_cfg["ocr"]
    if kind == "image":
        return _run(ocr["image"], ocr, input=path).strip()
    if kind == "pdf":
        return _pdf(path, ocr)
    raise ExtractError(f"unsupported file type {path.suffix}")


def _pdf(path: Path, ocr: dict) -> str:
    """Page by page, each page headed `[page N]`. In auto mode a page keeps the PDF's
    own text when it has enough of it; scanned pages are rendered and OCR'd."""
    text = _run(ocr["pdf_text"], ocr, input=path)
    pages = text.split("\f")
    if len(pages) > 1 and not pages[-1].strip():
        pages.pop()  # pdftotext ends every page with a form feed
    out = []
    for n, page in enumerate(pages, 1):
        if ocr["pdf_mode"] == "always" or len(page.strip()) < ocr["min_page_chars"]:
            page = _ocr_pdf_page(path, n, ocr)
        if page.strip():
            out.append(f"[page {n}]\n{page.strip()}")
    return "\n\n".join(out)


def _ocr_pdf_page(path: Path, page: int, ocr: dict) -> str:
    with tempfile.TemporaryDirectory(prefix="smallharness-ocr-") as d:
        _run(ocr["pdf_render"], ocr, input=path, output=Path(d) / "page", page=page)
        images = sorted(Path(d).glob("page*.png"))
        if not images:
            raise ExtractError(f"page {page} was not rendered ({ocr['pdf_render'][0]})")
        return _run(ocr["image"], ocr, input=images[0])


def _run(template: list[str], ocr: dict, **values) -> str:
    fill = {"lang": ocr["lang"], "dpi": ocr["dpi"], "output": "", "page": "", **values}
    argv = [part.format(**{k: str(v) for k, v in fill.items()}) for part in template]
    if not shutil.which(argv[0]):
        raise ExtractError(f"`{argv[0]}` is not installed; it is needed for OCR ({_INSTALL_HINT})")
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=ocr["timeout"])
    except subprocess.TimeoutExpired:
        raise ExtractError(f"`{argv[0]}` took longer than {ocr['timeout']}s") from None
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ExtractError(f"`{argv[0]}` failed: {err[-1] if err else f'exit {proc.returncode}'}")
    return proc.stdout.decode("utf-8", "replace")


_INSTALL_HINT = "macOS: brew install tesseract poppler; Debian/Ubuntu: apt install tesseract-ocr poppler-utils"
