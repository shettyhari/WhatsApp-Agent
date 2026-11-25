"""Turn an uploaded file into knowledge-base text.

  - Text files (txt, csv, md, json, html, xml...) are read as they are.
  - Word (.docx), Excel (.xlsx) and PowerPoint (.pptx) are read with python-docx,
    openpyxl and python-pptx.
  - PDFs and images (including scans and photos of menus or price lists) are read by
    Claude, which writes out every fact a customer-service agent would need.

Only the extracted text is kept; the original file is not stored.
"""
import base64
import html
import io
import json
import re
from html.parser import HTMLParser

import _claude
from _core import ApiError

MAX_UPLOAD_BYTES = 3 * 1024 * 1024   # Vercel accepts request bodies up to 4.5 MB (base64 adds a third)
MAX_FILE_TEXT = 50000                # characters kept per file

TEXT_EXTS = {"txt", "md", "markdown", "csv", "tsv", "json", "xml", "html", "htm", "rtf", "log", "yaml", "yml", "ini", "vcf", "srt"}
IMAGE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
OLD_OFFICE = {"doc": "Word", "xls": "Excel", "ppt": "PowerPoint"}
UNREADABLE = {
    "mp3", "wav", "m4a", "ogg", "opus", "aac", "flac", "mp4", "mov", "avi", "mkv", "webm", "3gp",
    "zip", "rar", "7z", "tar", "gz", "exe", "msi", "dmg", "apk", "heic", "heif", "tif", "tiff", "bmp", "svg", "psd", "ai",
}
SUPPORTED_HINT = "PDF, Word (.docx), Excel (.xlsx), PowerPoint (.pptx), images (JPG, PNG, WEBP, GIF) and text files (TXT, CSV, MD, JSON, HTML)"

EXTRACT_PROMPT = """This file was uploaded to teach a WhatsApp customer-service assistant about a brand.
File name: {name}

Write out everything in it that could help answer customers: products and services, prices, sizes and variants, offers, delivery and return policies, opening hours, locations, contact details, FAQs, terms and any other facts.

- Copy facts, numbers, names and prices exactly. Never add anything that isn't in the file.
- Turn tables into readable lines (for example "Classic Tee, size M: Rs 799").
- Use short plain-text sections with simple headings. No commentary about the file itself.
- If the file contains nothing useful for customers, reply with exactly: NO_USEFUL_CONTENT"""


def extension(name):
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def decode_upload(data_b64):
    try:
        raw = base64.b64decode(str(data_b64 or ""), validate=True)
    except (ValueError, TypeError):
        raise ApiError(400, "The file couldn't be uploaded. Try again.")
    if not raw:
        raise ApiError(400, "That file is empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ApiError(413, f"That file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB. Split it, or save a smaller copy (for example, export just the pages you need).")
    return raw


def extract(name, mime, raw):
    """Returns (text, how, cut): how describes the method; cut is True if the AI stopped early."""
    ext = extension(name)
    mime = (mime or "").lower()
    if ext in OLD_OFFICE:
        kind = OLD_OFFICE[ext]
        raise ApiError(400, f"Old {kind} files (.{ext}) can't be read. Open it in {kind} and save it as .{ext}x or as a PDF, then attach that.")
    if ext == "pdf" or mime == "application/pdf":
        text, cut = read_with_claude(name, raw, "pdf")
        return text, "Read by AI", cut
    if ext in IMAGE_TYPES or mime in IMAGE_TYPES.values():
        text, cut = read_with_claude(name, raw, IMAGE_TYPES.get(ext) or mime)
        return text, "Read by AI", cut
    if ext == "docx":
        return read_docx(raw), "Word document", False
    if ext in ("xlsx", "xlsm"):
        return read_xlsx(raw), "Excel sheet", False
    if ext == "pptx":
        return read_pptx(raw), "PowerPoint", False
    if ext in UNREADABLE or mime.startswith(("audio/", "video/")):
        raise ApiError(400, f"Files like {name} can't be added to the knowledge base. Supported: {SUPPORTED_HINT}.")
    text = decode_text(raw)
    if text is None:
        raise ApiError(400, f"{name} doesn't contain readable text. Supported: {SUPPORTED_HINT}.")
    if ext in ("html", "htm") or "html" in mime:
        text = html_to_text(text)
    elif ext == "json":
        try:
            text = json.dumps(json.loads(text), indent=1, ensure_ascii=False)
        except ValueError:
            pass
    elif ext == "rtf":
        text = rtf_to_text(text)
    return text, "Text file", False


def clean(text):
    text = re.sub(r"[ \t\u00a0]+", " ", text or "")
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def limit(text):
    """Keep the first MAX_FILE_TEXT characters. Returns (text, truncated)."""
    text = clean(text)
    if len(text) <= MAX_FILE_TEXT:
        return text, False
    cut = text[:MAX_FILE_TEXT]
    return cut[:cut.rfind("\n")] if "\n" in cut[-2000:] else cut, True


# ---------------------------------------------------------------------------
# Plain text
# ---------------------------------------------------------------------------
def decode_text(raw):
    for enc in ("utf-8-sig", "utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        sample = text[:5000]
        control = sum(1 for c in sample if ord(c) < 32 and c not in "\r\n\t\f")
        return text if not sample or control / len(sample) < 0.02 else None
    return None


class _TextOnly(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table"}

    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(text):
    p = _TextOnly()
    p.feed(text)
    return html.unescape("".join(p.parts))


def rtf_to_text(text):
    text = re.sub(r"\\par[d]?", "\n", text)
    text = re.sub(r"\\'([0-9a-f]{2})", lambda m: bytes.fromhex(m.group(1)).decode("cp1252", "ignore"), text)
    text = re.sub(r"\\[a-z]+-?\d* ?|[{}]", "", text)
    return text


# ---------------------------------------------------------------------------
# Office files
# ---------------------------------------------------------------------------
def _need(module, package):
    try:
        return __import__(module)
    except ImportError:
        raise ApiError(503, f"The server is missing {package}. Run: pip install -r requirements.txt")


def _office_error(kind):
    return ApiError(400, f"That {kind} file couldn't be opened. Check it isn't password-protected or damaged.")


def read_docx(raw):
    docx = _need("docx", "python-docx")
    try:
        doc = docx.Document(io.BytesIO(raw))
    except Exception:
        raise _office_error("Word")
    lines = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        lines.append("")
        for row in table.rows:
            cells = []
            for c in row.cells:
                t = c.text.strip()
                if t and (not cells or cells[-1] != t):  # merged cells repeat their text
                    cells.append(t)
            if cells:
                lines.append(" | ".join(cells))
    return "\n".join(lines)


def read_xlsx(raw):
    openpyxl = _need("openpyxl", "openpyxl")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception:
        raise _office_error("Excel")
    lines, size = [], 0
    for ws in wb.worksheets:
        lines.append(f"\n## Sheet: {ws.title}")
        header = None
        for row in ws.iter_rows(values_only=True):
            cells = ["" if v is None else str(v).strip() for v in row]
            if not any(cells):
                continue
            while cells and not cells[-1]:
                cells.pop()
            if header is None:
                header = cells
                line = " | ".join(cells)
            elif len(header) == len(cells) and all(header):
                # "Column: value" keeps each row understandable on its own
                line = "; ".join(f"{h}: {v}" for h, v in zip(header, cells) if v)
            else:
                line = " | ".join(cells)
            lines.append(line)
            size += len(line)
            if size > MAX_FILE_TEXT * 2:
                break
    wb.close()
    return "\n".join(lines)


def read_pptx(raw):
    pptx = _need("pptx", "python-pptx")
    try:
        deck = pptx.Presentation(io.BytesIO(raw))
    except Exception:
        raise _office_error("PowerPoint")
    lines = []
    for i, slide in enumerate(deck.slides, 1):
        lines.append(f"\n## Slide {i}")
        for shape in slide.shapes:
            if shape.has_text_frame:
                lines.append(shape.text_frame.text)
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    lines.append(" | ".join(c.text.strip() for c in row.cells))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
            lines.append("Notes: " + slide.notes_slide.notes_text_frame.text)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# PDFs and images: Claude reads them (works for scans and photos too)
# ---------------------------------------------------------------------------
def read_with_claude(name, raw, media_type):
    """Returns (text, cut): cut is True if the file had more than the AI could write out in one go."""
    data = base64.standard_b64encode(raw).decode("ascii")
    if media_type == "pdf":
        block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}
    else:
        block = {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
    client = _claude.client(timeout=280.0)

    def call():
        # Streamed so long documents don't hit HTTP timeouts
        with client.beta.messages.stream(
            model=_claude.MODEL,
            max_tokens=16000,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": [block, {"type": "text", "text": EXTRACT_PROMPT.format(name=name)}]}],
            **_claude.FALLBACK_ARGS,
        ) as stream:
            return stream.get_final_message()

    message = _claude.run(call, what=name)
    if message.stop_reason == "refusal":
        raise ApiError(400, f"The AI declined to read {name}.")
    text = "\n".join(b.text for b in message.content if b.type == "text").strip()
    if not text or text == "NO_USEFUL_CONTENT":
        raise ApiError(400, f"No customer-relevant information was found in {name}.")
    return text, message.stop_reason == "max_tokens"
