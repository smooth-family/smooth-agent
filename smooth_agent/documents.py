"""Read the files people actually send: PDF, Word, Excel, PowerPoint, OpenDocument,
images and archives — as plain text the model can work with.

    extract_text(path)            -> Extracted(text, kind, note)
    list_archive(path)            -> [(name, size)]
    extract_archive(path, dest)   -> [paths]   (safe: no escaping paths, no links, size limits)

Office Open XML (.docx .xlsx .pptx) and OpenDocument (.odt .ods .odp) are zip files
of XML, read here with the standard library. PDFs use pypdf, or the `pdftotext`
program when pypdf is missing; a PDF without a text layer (a scan) is rendered to
images for the vision model. RAR and 7z need an extractor program on the machine
(unrar, unar or 7z); zip and tar need nothing.
"""
from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import tarfile
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

MAX_ROWS = 400          # rows shown per spreadsheet sheet
MAX_PAGES = 200         # PDF pages read
MAX_RENDER = 6          # pages of a scanned PDF rendered to images
MAX_FILES = 5_000       # files taken out of one archive
MAX_TOTAL = 1 << 30     # bytes taken out of one archive (1 GiB)
MAX_RATIO = 200         # compressed -> uncompressed ratio above this looks like a zip bomb

DOC_EXT = {".pdf", ".docx", ".docm", ".dotx", ".xlsx", ".xlsm", ".xltx", ".pptx", ".pptm", ".odt", ".ods", ".odp",
           ".doc", ".xls", ".ppt", ".rtf"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic"}
ARCHIVE_EXT = {".zip", ".rar", ".7z", ".tar", ".tgz", ".tbz2", ".txz", ".gz", ".bz2", ".xz", ".jar", ".apk"}


class DocumentError(Exception):
    pass


@dataclass
class Extracted:
    text: str
    kind: str          # "PDF", "Word", "Excel", …
    note: str = ""     # e.g. "12 pages", "3 sheets", "scanned: pages rendered to …"


def _head(path: Path, n: int) -> bytes:
    with open(path, "rb") as f:  # closed right away (an open handle locks the file on Windows)
        return f.read(n)


def kind_of(path: Path) -> str:
    """'document', 'image', 'archive' or 'text' — by extension, then by magic bytes."""
    ext = "".join(path.suffixes[-2:]).lower() if path.name.lower().endswith((".tar.gz", ".tar.bz2", ".tar.xz")) else path.suffix.lower()
    if ext in (".tar.gz", ".tar.bz2", ".tar.xz") or ext in ARCHIVE_EXT:
        return "archive"
    if ext in DOC_EXT:
        return "document"
    if ext in IMAGE_EXT:
        return "image"
    try:
        head = _head(path, 8)
    except OSError:
        return "text"
    if head.startswith(b"%PDF"):
        return "document"
    if head.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF8")) or head[:4] == b"RIFF":
        return "image"
    if head.startswith((b"PK\x03\x04", b"Rar!", b"7z\xbc\xaf", b"\x1f\x8b")):
        return "archive"
    return "text"


# ---------------------------------------------------------------- dispatch

def extract_text(path: Path, *, render_dir: Path | None = None) -> Extracted:
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".pdf" or _head(path, 4) == b"%PDF":
        return _pdf(path, render_dir)
    if ext in (".docx", ".docm", ".dotx"):
        return _docx(path)
    if ext in (".xlsx", ".xlsm", ".xltx"):
        return _xlsx(path)
    if ext in (".pptx", ".pptm"):
        return _pptx(path)
    if ext in (".odt", ".ods", ".odp"):
        return _odf(path)
    if ext in (".doc", ".xls", ".ppt", ".rtf"):
        return _legacy(path)
    k = kind_of(path)
    if k == "image":
        return _image(path)
    if k == "archive":
        rows = list_archive(path)
        total = sum(s for _, s in rows)
        lines = [f"{s:>12,}  {n}" if total else n for n, s in rows[:500]]
        more = [f"… {len(rows) - 500} more entries"] if len(rows) > 500 else []
        size = f", {total:,} bytes unpacked" if total else ""
        return Extracted("\n".join(lines + more), "Archive", f"{len(rows)} entries{size} — extract_archive unpacks it")
    raise DocumentError("not a document type I convert; read it as text")


def _zip_xml(z: zipfile.ZipFile, name: str) -> ET.Element | None:
    try:
        return ET.fromstring(z.read(name))
    except KeyError:
        return None


def _open_zip(path: Path, what: str) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise DocumentError(f"this {what} file is damaged or not really a {what} file") from None


# ---------------------------------------------------------------- PDF

def _pdf(path: Path, render_dir: Path | None) -> Extracted:
    pages: list[str] = []
    n_pages = 0
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                raise DocumentError("the PDF is password protected") from None
        n_pages = len(reader.pages)
        for page in reader.pages[:MAX_PAGES]:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                pages.append("")
    except ImportError:
        if not shutil.which("pdftotext"):
            raise DocumentError("cannot read PDFs here: pip install pypdf (or install poppler-utils)") from None
        r = subprocess.run(["pdftotext", "-layout", "-l", str(MAX_PAGES), str(path), "-"], capture_output=True, timeout=120)
        if r.returncode:
            raise DocumentError(f"pdftotext failed: {r.stderr.decode(errors='replace')[:200]}")
        pages = r.stdout.decode("utf-8", "replace").split("\f")
        n_pages = len([p for p in pages if p.strip()]) or len(pages)
    except DocumentError:
        raise
    except Exception as e:
        raise DocumentError(f"could not read the PDF: {e}") from None
    text_chars = sum(len(p.strip()) for p in pages)
    if text_chars < 10 * max(1, min(n_pages, len(pages))):  # (almost) no text layer: a scan or photos of pages
        note = f"{n_pages} pages, no text layer (scanned)"
        imgs = render_pdf(path, render_dir) if render_dir else []
        if imgs:
            note += "; pages rendered to " + ", ".join(str(i) for i in imgs) + " — look at them with vision_analyze"
        return Extracted("\n\n".join(f"--- page {i} ---\n{p.strip()}" for i, p in enumerate(pages, 1) if p.strip()),
                         "PDF", note)
    body = "\n\n".join(f"--- page {i} ---\n{p.strip()}" for i, p in enumerate(pages, 1))
    note = f"{n_pages} pages" + (f", first {MAX_PAGES} read" if n_pages > MAX_PAGES else "")
    return Extracted(body, "PDF", note)


def render_pdf(path: Path, out_dir: Path, pages: int = MAX_RENDER) -> list[Path]:
    """Render the first pages of a PDF to PNG (needs pdftoppm). Returns the image paths."""
    if not shutil.which("pdftoppm"):
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = out_dir / f"{path.stem}-page"
    r = subprocess.run(["pdftoppm", "-png", "-r", "110", "-f", "1", "-l", str(pages), str(path), str(prefix)],
                       capture_output=True, timeout=180)
    if r.returncode:
        return []
    return sorted(out_dir.glob(f"{path.stem}-page*.png"))


# ---------------------------------------------------------------- Word

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _w_text(el: ET.Element) -> str:
    out = []
    for node in el.iter():
        if node.tag == W + "t" and node.text:
            out.append(node.text)
        elif node.tag == W + "tab":
            out.append("\t")
        elif node.tag in (W + "br", W + "cr"):
            out.append("\n")
    return "".join(out)


def _docx(path: Path) -> Extracted:
    with _open_zip(path, "Word") as z:
        root = _zip_xml(z, "word/document.xml")
        if root is None:
            raise DocumentError("no word/document.xml inside: not a Word file")
        body = root.find(W + "body")
        lines, tables = [], 0
        for el in (body if body is not None else []):
            if el.tag == W + "p":
                text = _w_text(el)
                style = el.find(f"{W}pPr/{W}pStyle")
                level = re.match(r"(?i)heading\s*(\d)", style.get(W + "val", "")) if style is not None else None
                if level and text.strip():
                    text = "#" * int(level.group(1)) + " " + text
                lines.append(text)
            elif el.tag == W + "tbl":
                tables += 1
                for tr in el.iter(W + "tr"):
                    lines.append(" | ".join(_w_text(tc).replace("\n", " ").strip() for tc in tr.findall(W + "tc")))
                lines.append("")
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return Extracted(text, "Word", f"{len(text.split())} words" + (f", {tables} tables" if tables else ""))


# ---------------------------------------------------------------- Excel

S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _col_index(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _xlsx(path: Path) -> Extracted:
    with _open_zip(path, "Excel") as z:
        shared = []
        sst = _zip_xml(z, "xl/sharedStrings.xml")
        if sst is not None:
            shared = ["".join(t.text or "" for t in si.iter(S + "t")) for si in sst.findall(S + "si")]
        wb = _zip_xml(z, "xl/workbook.xml")
        rels = _zip_xml(z, "xl/_rels/workbook.xml.rels")
        if wb is None:
            raise DocumentError("no xl/workbook.xml inside: not an Excel file")
        targets = {r.get("Id"): r.get("Target", "") for r in (rels if rels is not None else [])}
        out, sheets = [], 0
        for sh in wb.iter(S + "sheet"):
            target = targets.get(sh.get(R_ID), "")
            target = target.lstrip("/")
            target = target if target.startswith("xl/") else "xl/" + target
            ws = _zip_xml(z, target)
            if ws is None:
                continue
            sheets += 1
            rows: list[list[str]] = []
            n_rows = 0
            for row in ws.iter(S + "row"):
                n_rows += 1
                if len(rows) >= MAX_ROWS:
                    continue
                cells: dict[int, str] = {}
                for c in row.findall(S + "c"):
                    t, v = c.get("t"), c.find(S + "v")
                    if t == "s" and v is not None:
                        val = shared[int(v.text)] if v.text and int(v.text) < len(shared) else ""
                    elif t == "inlineStr":
                        val = "".join(x.text or "" for x in c.iter(S + "t"))
                    elif t == "b" and v is not None:
                        val = "TRUE" if v.text == "1" else "FALSE"
                    else:
                        val = v.text if v is not None and v.text is not None else ""
                    if val != "" and c.get("r"):
                        cells[_col_index(c.get("r"))] = val.replace("\t", " ").replace("\n", " ")
                if cells:
                    rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
            cols = max((len(r) for r in rows), default=0)
            out.append(f"## Sheet: {sh.get('name')} ({n_rows} rows × {cols} columns)")
            out += ["\t".join(r) for r in rows]
            if n_rows > MAX_ROWS:
                out.append(f"… {n_rows - MAX_ROWS} more rows (convert to CSV with a script to work on all of them)")
            out.append("")
    return Extracted("\n".join(out).strip(), "Excel", f"{sheets} sheet{'s' if sheets != 1 else ''}, tab-separated")


# ---------------------------------------------------------------- PowerPoint

A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _pptx(path: Path) -> Extracted:
    with _open_zip(path, "PowerPoint") as z:
        names = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                       key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)))
        out = []
        for i, name in enumerate(names, 1):
            root = _zip_xml(z, name)
            paras = ["".join(t.text or "" for t in p.iter(A + "t")) for p in root.iter(A + "p")] if root is not None else []
            out.append(f"--- slide {i} ---\n" + "\n".join(p for p in paras if p.strip()))
    return Extracted("\n\n".join(out), "PowerPoint", f"{len(names)} slides")


# ---------------------------------------------------------------- OpenDocument

TXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
TBL = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"


def _odf_text(el: ET.Element) -> str:
    return "".join(el.itertext())


def _odf(path: Path) -> Extracted:
    with _open_zip(path, "OpenDocument") as z:
        root = _zip_xml(z, "content.xml")
    if root is None:
        raise DocumentError("no content.xml inside: not an OpenDocument file")
    kind = {".odt": "Writer", ".ods": "Calc", ".odp": "Impress"}[path.suffix.lower()]
    out = []
    if kind == "Calc":
        for table in root.iter(TBL + "table"):
            out.append(f"## Sheet: {table.get(TBL + 'name')}")
            for n, row in enumerate(table.iter(TBL + "table-row")):
                if n >= MAX_ROWS:
                    out.append("… more rows")
                    break
                cells = []
                for cell in row.findall(TBL + "table-cell"):
                    rep = min(int(cell.get(TBL + "number-columns-repeated", "1")), 50)
                    cells += [_odf_text(cell)] * rep
                while cells and not cells[-1]:
                    cells.pop()
                if cells:
                    out.append("\t".join(cells))
            out.append("")
    else:
        for el in root.iter():
            if el.tag in (TXT + "h", TXT + "p"):
                text = _odf_text(el)
                if el.tag == TXT + "h" and text.strip():
                    text = "#" * int(el.get(TXT + "outline-level", "1")) + " " + text
                out.append(text)
    return Extracted(re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip(), f"OpenDocument {kind}")


# ---------------------------------------------------------------- old binary Office, RTF

def _legacy(path: Path) -> Extracted:
    ext = path.suffix.lower()
    tools = {".doc": [["antiword", str(path)], ["catdoc", str(path)]], ".xls": [["xls2csv", str(path)]],
             ".rtf": [["unrtf", "--text", str(path)]], ".ppt": [["catppt", str(path)]]}.get(ext, [])
    for cmd in tools:
        if shutil.which(cmd[0]):
            r = subprocess.run(cmd, capture_output=True, timeout=120)
            if r.returncode == 0 and r.stdout.strip():
                return Extracted(r.stdout.decode("utf-8", "replace"), f"old {ext[1:].upper()}")
    office = shutil.which("libreoffice") or shutil.which("soffice")
    if office:
        target = {".doc": "docx", ".xls": "xlsx", ".ppt": "pptx", ".rtf": "docx"}[ext]
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([office, "--headless", "--convert-to", target, "--outdir", tmp, str(path)],
                           capture_output=True, timeout=240)
            converted = Path(tmp) / f"{path.stem}.{target}"
            if converted.exists():
                return extract_text(converted)
    if ext == ".rtf":  # crude but useful: strip control words
        raw = path.read_text("latin-1", errors="replace")
        text = re.sub(r"\\[a-z]+-?\d* ?|[{}]", "", raw)
        return Extracted(text.strip(), "RTF", "control words stripped")
    raise DocumentError(f"old {ext} format: install LibreOffice (or antiword/catdoc) to read it, "
                        f"or ask the owner to send it as .{ {'.doc': 'docx', '.xls': 'xlsx', '.ppt': 'pptx'}[ext] }")


# ---------------------------------------------------------------- images

def image_size(path: Path) -> tuple[str, int, int] | None:
    """(format, width, height) from the file header, stdlib only."""
    with open(path, "rb") as f:
        head = f.read(64 * 1024)
    if head.startswith(b"\x89PNG") and len(head) >= 24:
        w, h = struct.unpack(">II", head[16:24])
        return "PNG", w, h
    if head[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", head[6:10])
        return "GIF", w, h
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        if head[12:16] == b"VP8X":
            return "WEBP", 1 + int.from_bytes(head[24:27], "little"), 1 + int.from_bytes(head[27:30], "little")
        if head[12:16] == b"VP8 ":
            w, h = struct.unpack("<HH", head[26:30])
            return "WEBP", w & 0x3FFF, h & 0x3FFF
        return "WEBP", 0, 0
    if head.startswith(b"\xff\xd8"):
        i = 2
        while i + 9 < len(head):
            if head[i] != 0xFF:
                i += 1
                continue
            marker = head[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", head[i + 5:i + 9])
                return "JPEG", w, h
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            i += 2 + struct.unpack(">H", head[i + 2:i + 4])[0]
        return "JPEG", 0, 0
    return None


def _image(path: Path) -> Extracted:
    info = image_size(path)
    desc = f"{info[0]} {info[1]}×{info[2]}" if info and info[1] else (info[0] if info else path.suffix.upper()[1:])
    text = ""
    if shutil.which("tesseract"):
        r = subprocess.run(["tesseract", str(path), "-", "-l", "eng+ind"], capture_output=True, timeout=120)
        if r.returncode:
            r = subprocess.run(["tesseract", str(path), "-"], capture_output=True, timeout=120)
        text = r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else ""
    note = f"{desc} image" + ("; text found by OCR below" if text else "") + " — use vision_analyze to see it"
    return Extracted(text, "Image", note)


# ---------------------------------------------------------------- archives

def _archive_type(path: Path) -> str:
    head = _head(path, 8)
    name = path.name.lower()
    if head.startswith(b"Rar!"):
        return "rar"
    if head.startswith(b"7z\xbc\xaf"):
        return "7z"
    if zipfile.is_zipfile(path):
        return "zip"
    if name.endswith((".tar", ".tgz", ".tbz2", ".txz", ".tar.gz", ".tar.bz2", ".tar.xz")) or tarfile.is_tarfile(path):
        return "tar"
    if head.startswith(b"\x1f\x8b") or name.endswith((".gz", ".bz2", ".xz")):
        return "single"  # one compressed file
    raise DocumentError("not an archive format I know (zip, tar, rar, 7z, gz)")


def _extractors(kind: str) -> list[str]:
    """Extractor programs for rar/7z on this machine, best first. Debian's '7zip' (dfsg) build cannot read RAR,
    so for RAR the free unar comes before 7z."""
    order = ["unrar", "unar", "bsdtar", "7z", "7zz", "7za"] if kind == "rar" else ["7z", "7zz", "7za", "unar", "bsdtar"]
    return [p for p in order if shutil.which(p) and (p != "unar" or shutil.which("lsar"))]


def _external(kind: str) -> list[str] | None:
    found = _extractors(kind)
    return found[:1] or None


def list_archive(path: Path) -> list[tuple[str, int]]:
    kind = _archive_type(path)
    if kind == "zip":
        with zipfile.ZipFile(path) as z:
            return [(i.filename, i.file_size) for i in z.infolist() if not i.is_dir()]
    if kind == "tar":
        with tarfile.open(path) as t:
            return [(m.name, m.size) for m in t.getmembers() if m.isfile()]
    if kind == "single":
        return [(re.sub(r"\.(gz|bz2|xz)$", "", path.name, flags=re.I), 0)]
    progs = _extractors(kind)
    if not progs:
        raise DocumentError(f"{kind.upper()} needs an extractor program: install unrar, unar or 7-Zip (7z)")
    errors = []
    for prog in progs:
        cmd = {"unrar": ["unrar", "lb", "-p-", str(path)], "unar": ["lsar", str(path)],
               "bsdtar": ["bsdtar", "-tf", str(path)]}.get(prog, [prog, "l", "-ba", "-p", str(path)])
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        if r.returncode:
            errors.append((r.stderr + r.stdout).decode(errors="replace"))
            continue
        lines = r.stdout.decode("utf-8", "replace").splitlines()
        if prog in ("7z", "7zz", "7za"):  # "date time attr size compressed name"
            out = []
            for ln in lines:
                parts = ln.split(None, 5)
                if len(parts) == 6 and "D" not in parts[2]:
                    out.append((parts[5], int(parts[3]) if parts[3].isdigit() else 0))
            return out
        if prog == "unar":
            lines = lines[1:]  # first line is the archive name
        return [(ln.strip(), 0) for ln in lines if ln.strip() and not ln.endswith("/")]
    if any(re.search(r"(?i)password|encrypt", e) for e in errors):
        raise DocumentError("the archive is password protected (even its file list); "
                            "ask the owner for the password and pass it to extract_archive")
    raise DocumentError(f"could not list the archive: {errors[-1].strip()[-200:]}")


def _safe_target(dest: Path, name: str) -> Path | None:
    name = name.replace("\\", "/").lstrip("/")
    if re.match(r"^[A-Za-z]:", name):
        name = name[2:].lstrip("/")
    target = (dest / name).resolve()
    return target if target == dest or dest in target.parents else None


class _Budget:
    def __init__(self):
        self.files = 0
        self.bytes = 0

    def take(self, size: int) -> None:
        self.files += 1
        self.bytes += max(0, size)
        if self.files > MAX_FILES:
            raise DocumentError(f"more than {MAX_FILES} files in the archive; stopped")
        if self.bytes > MAX_TOTAL:
            raise DocumentError(f"more than {MAX_TOTAL >> 20} MB unpacked; stopped (zip bomb?)")


def extract_archive(path: Path, dest: Path, password: str | None = None) -> list[Path]:
    """Unpack into dest. Paths that would land outside dest, links and devices are skipped;
    file count, total size and compression ratio are capped."""
    path, dest = Path(path), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    dest = dest.resolve()
    kind = _archive_type(path)
    budget, out = _Budget(), []
    if kind == "zip":
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if info.is_dir() or (info.external_attr >> 16) & 0o170000 == 0o120000:  # dirs, symlinks
                    continue
                target = _safe_target(dest, info.filename)
                if target is None:
                    continue
                if info.compress_size and info.file_size / info.compress_size > MAX_RATIO and info.file_size > 50 << 20:
                    raise DocumentError(f"{info.filename} expands {info.file_size // info.compress_size}× — looks like a zip bomb")
                budget.take(info.file_size)
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with z.open(info, pwd=password.encode() if password else None) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                except RuntimeError as e:  # encrypted member: wrong / missing password
                    target.unlink(missing_ok=True)
                    raise DocumentError(f"cannot extract {info.filename}: password protected"
                                        + (" and the password is wrong" if password else "; pass the password")) from None
                except NotImplementedError:  # AES zip: the standard library cannot decrypt it
                    target.unlink(missing_ok=True)
                    if _external("7z"):
                        return _extract_external(path, dest, "7z", password, budget)
                    raise DocumentError("this zip uses AES encryption: install 7-Zip (7z) to open it") from None
                out.append(target)
        return out
    if kind == "tar":
        with tarfile.open(path) as t:
            for m in t.getmembers():
                if not m.isfile():
                    continue  # dirs are made as needed; links and devices are never created
                target = _safe_target(dest, m.name)
                if target is None:
                    continue
                budget.take(m.size)
                target.parent.mkdir(parents=True, exist_ok=True)
                src = t.extractfile(m)
                if src is None:
                    continue
                with src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                out.append(target)
        return out
    if kind == "single":
        import bz2
        import gzip
        import lzma
        opener = {".gz": gzip.open, ".bz2": bz2.open, ".xz": lzma.open}.get(path.suffix.lower(), gzip.open)
        target = dest / re.sub(r"\.(gz|bz2|xz)$", "", path.name, flags=re.I)
        with opener(path, "rb") as src, open(target, "wb") as dst:
            while True:
                block = src.read(1 << 20)
                if not block:
                    break
                budget.bytes += len(block)
                if budget.bytes > MAX_TOTAL:
                    raise DocumentError("more than 1 GB unpacked; stopped")
                dst.write(block)
        return [target]
    return _extract_external(path, dest, kind, password, budget)


def _extract_external(path: Path, dest: Path, kind: str, password: str | None, budget: "_Budget") -> list[Path]:
    progs = _extractors(kind)
    if not progs:
        raise DocumentError(f"{kind.upper()} needs an extractor program: install unrar, unar or 7-Zip (7z)")
    pw = password or ""
    errors = []
    for prog in progs:
        with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
            cmd = {"unrar": ["unrar", "x", "-o+", f"-p{pw}" if pw else "-p-", "-y", str(path), tmp + os.sep],
                   "unar": ["unar", "-q", "-f", "-p", pw, "-o", tmp, str(path)],
                   "bsdtar": ["bsdtar", "-xf", str(path), "-C", tmp]}.get(prog, [prog, "x", "-y", f"-p{pw}", f"-o{tmp}", str(path)])
            if prog == "unar":
                # unar creates the links an archive holds and then writes through them: take only regular files
                indices = _unar_file_indices(path, pw)
                if indices is None:
                    errors.append("lsar could not list the archive")
                    continue
                if not indices:
                    return []
                cmd = ["unar", "-q", "-f", "-p", pw, "-o", tmp, "-i", str(path), *map(str, indices)]
            r = subprocess.run(cmd, capture_output=True, timeout=600)
            msg = (r.stderr + r.stdout).decode(errors="replace")
            root = Path(tmp).resolve()
            got = [f for f in root.rglob("*") if f.is_file() and not f.is_symlink()]
            # a non-zero exit is only a partial success when the refused entries were (unsafe) links;
            # a wrong password or an unsupported method leaves broken or empty files behind
            if r.returncode and not (got and re.search(r"(?i)unsafe|link", msg)
                                     and not re.search(r"(?i)password|unsupported|crc failed|checksum", msg)):
                errors.append(msg)
                continue
            out: list[Path] = []
            for f in sorted(got):
                target = _safe_target(dest, str(f.relative_to(root)))
                if target is None:
                    continue
                budget.take(f.stat().st_size)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(target))
                out.append(target)
            return out
    if any(re.search(r"(?i)password|encrypt", e) for e in errors):
        raise DocumentError("the archive is password protected" + (" and the password is wrong" if pw else
                            "; ask the owner for the password and pass it to extract_archive"))
    raise DocumentError(f"could not extract it ({', '.join(progs)} tried): {errors[-1].strip()[-200:]}")


def _unar_file_indices(path: Path, pw: str) -> list[int] | None:
    """Indexes of the regular files in an archive, from `lsar -j` (links and folders left out)."""
    import json
    cmd = ["lsar", "-j"] + (["-p", pw] if pw else []) + [str(path)]
    r = subprocess.run(cmd, capture_output=True, timeout=120)
    try:
        entries = json.loads(r.stdout.decode("utf-8", "replace"))["lsarContents"]
    except (ValueError, KeyError, TypeError):
        return None
    return [int(e["XADIndex"]) for e in entries
            if not e.get("XADIsLink") and not e.get("XADIsDirectory") and "XADIndex" in e]


__all__ = ["Extracted", "DocumentError", "extract_text", "list_archive", "extract_archive", "kind_of",
           "image_size", "render_pdf", "DOC_EXT", "IMAGE_EXT", "ARCHIVE_EXT"]


# ---------------------------------------------------------------- writing Word and Excel (stdlib)

def _x(text: str) -> str:
    from xml.sax.saxutils import escape
    import re as _re
    return escape(_re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(text)))


_CT_DOCX = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""
_RELS_DOCX = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
_DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""
_STYLES_DOCX = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/><w:sz w:val="22"/></w:rPr></w:rPrDefault>
<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="240"/></w:pPr><w:rPr><w:b/><w:sz w:val="40"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="240" w:after="120"/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="200" w:after="80"/><w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="160" w:after="60"/><w:outlineLvl w:val="2"/></w:pPr><w:rPr><w:b/><w:sz w:val="24"/></w:rPr></w:style>
<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:tblPr><w:tblBorders>
<w:top w:val="single" w:sz="4" w:space="0" w:color="999999"/><w:left w:val="single" w:sz="4" w:space="0" w:color="999999"/>
<w:bottom w:val="single" w:sz="4" w:space="0" w:color="999999"/><w:right w:val="single" w:sz="4" w:space="0" w:color="999999"/>
<w:insideH w:val="single" w:sz="4" w:space="0" w:color="999999"/><w:insideV w:val="single" w:sz="4" w:space="0" w:color="999999"/>
</w:tblBorders><w:tblCellMar><w:left w:w="80" w:type="dxa"/><w:right w:w="80" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>
</w:styles>"""


def _runs(text: str, *, bold: bool = False, mono: bool = False) -> str:
    """Inline Markdown (**bold**, `code`, [label](url)) -> Word runs."""
    import re as _re
    text = _re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1 (\2)", text)
    out = []
    for i, part in enumerate(_re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", text)):
        if not part:
            continue
        b, m = bold, mono
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            part, b = part[2:-2], True
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            part, m = part[1:-1], True
        props = ("<w:b/>" if b else "") + ('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>' if m else "")
        out.append(f'<w:r>{"<w:rPr>" + props + "</w:rPr>" if props else ""}<w:t xml:space="preserve">{_x(part)}</w:t></w:r>')
    return "".join(out)


def _para(text: str, style: str = "", indent: int = 0, mono: bool = False) -> str:
    ppr = (f'<w:pStyle w:val="{style}"/>' if style else "") + (f'<w:ind w:left="{indent}" w:hanging="240"/>' if indent else "")
    if mono:
        ppr += '<w:spacing w:after="0"/>'
    return f'<w:p>{"<w:pPr>" + ppr + "</w:pPr>" if ppr else ""}{_runs(text, mono=mono)}</w:p>'


def _table(rows: list[list[str]]) -> str:
    cols = max(len(r) for r in rows)
    grid = "".join('<w:gridCol w:w="%d"/>' % (9000 // cols) for _ in range(cols))
    body = []
    for i, row in enumerate(rows):
        cells = "".join(f'<w:tc><w:tcPr><w:tcW w:w="{9000 // cols}" w:type="dxa"/></w:tcPr>'
                        f'<w:p><w:pPr><w:spacing w:after="0"/></w:pPr>{_runs(c, bold=i == 0)}</w:p></w:tc>'
                        for c in row + [""] * (cols - len(row)))
        body.append(f"<w:tr>{cells}</w:tr>")
    return (f'<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="0" w:type="auto"/></w:tblPr>'
            f"<w:tblGrid>{grid}</w:tblGrid>{''.join(body)}</w:tbl><w:p/>")


def write_docx(path: Path, markdown: str) -> Path:
    """Markdown -> a real .docx: # headings, paragraphs, **bold**, `code`, - bullets, 1. lists, | tables |, ``` code."""
    import re as _re
    lines = str(markdown).replace("\r\n", "\n").split("\n")
    body, i = [], 0
    while i < len(lines):
        ln = lines[i]
        s = ln.strip()
        if s.startswith("```"):
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(_para(lines[i] or " ", mono=True))
                i += 1
            body.append("<w:p/>")
        elif s.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(_re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            if rows:
                body.append(_table(rows))
            continue
        elif _re.match(r"#{1,6}\s", s):
            level = len(s) - len(s.lstrip("#"))
            body.append(_para(s.lstrip("#").strip(), "Title" if level == 1 and not body else f"Heading{min(level, 3)}"))
        elif _re.match(r"[-*+]\s", s):
            body.append(_para("•\t" + s[2:].strip(), indent=360 * (1 + (len(ln) - len(ln.lstrip())) // 2)))
        elif _re.match(r"\d+[.)]\s", s):
            body.append(_para(s, indent=360))
        elif s:
            body.append(_para(s))
        i += 1
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           + "".join(body) +
           '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" w:bottom="1134" '
           'w:left="1134" w:header="567" w:footer="567" w:gutter="0"/></w:sectPr></w:body></w:document>')
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT_DOCX)
        z.writestr("_rels/.rels", _RELS_DOCX)
        z.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        z.writestr("word/styles.xml", _STYLES_DOCX)
        z.writestr("word/document.xml", doc)
    return path


_NUM = re.compile(r"-?(0|[1-9]\d{0,14})(\.\d+)?")


def _cell_ref(col: int, row: int) -> str:
    letters = ""
    col += 1
    while col:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row}"


def write_xlsx(path: Path, sheets: dict[str, list[list]]) -> Path:
    """{sheet name: rows} -> a real .xlsx. The first row of each sheet is a bold, frozen header; numbers
    become numbers (but '0812…', '00123' and long digit strings stay text so nothing is lost)."""
    names, used = [], set()
    for name in sheets or {"Sheet1": []}:
        clean = re.sub(r"[\[\]:*?/\\]", "_", str(name)).strip("'")[:31] or "Sheet"
        base, n = clean, 2
        while clean.lower() in used:
            clean = f"{base[:28]}~{n}"
            n += 1
        used.add(clean.lower())
        names.append(clean)
    rows_list = list((sheets or {"Sheet1": []}).values())
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                             for i in range(1, len(names) + 1)) + "</Types>")
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   "</Relationships>")
        z.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   + "".join(f'<sheet name="{_x(n)}" sheetId="{i}" r:id="rId{i}"/>' for i, n in enumerate(names, 1))
                   + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                             f'Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(names) + 1))
                   + f'<Relationship Id="rId{len(names) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                   "</Relationships>")
        z.writestr("xl/styles.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
                   '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
                   '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
                   '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                   '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
                   '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
                   '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
                   "</styleSheet>")
        for i, rows in enumerate(rows_list, 1):
            rows = [list(r) if isinstance(r, (list, tuple)) else [r] for r in (rows or [])]
            widths: dict[int, int] = {}
            xml_rows = []
            for r, row in enumerate(rows, 1):
                cells = []
                for c, val in enumerate(row):
                    if val is None or val == "":
                        continue
                    ref, style = _cell_ref(c, r), ' s="1"' if r == 1 and len(rows) > 1 else ""
                    widths[c] = max(widths.get(c, 0), min(60, len(str(val))))
                    if isinstance(val, bool):
                        cells.append(f'<c r="{ref}" t="b"{style}><v>{int(val)}</v></c>')
                    elif isinstance(val, (int, float)) or (isinstance(val, str) and _NUM.fullmatch(val.strip())
                                                           and not (val.strip().startswith(("0", "-0")) and len(val.strip().lstrip("-")) > 1
                                                                    and not val.strip().lstrip("-").startswith("0."))):
                        cells.append(f'<c r="{ref}"{style}><v>{str(val).strip()}</v></c>')
                    else:
                        cells.append(f'<c r="{ref}" t="inlineStr"{style}><is><t xml:space="preserve">{_x(val)}</t></is></c>')
                xml_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
            cols = "".join(f'<col min="{c + 1}" max="{c + 1}" width="{max(8, w + 2)}" customWidth="1"/>'
                           for c, w in sorted(widths.items()))
            freeze = ('<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" '
                      'state="frozen"/></sheetView></sheetViews>') if len(rows) > 1 else ""
            z.writestr(f"xl/worksheets/sheet{i}.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                       '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' + freeze
                       + (f"<cols>{cols}</cols>" if cols else "") + f"<sheetData>{''.join(xml_rows)}</sheetData></worksheet>")
    return path


__all__ += ["write_docx", "write_xlsx"]
