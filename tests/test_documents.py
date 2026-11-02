"""Reading documents and archives: PDF, Word, Excel, PowerPoint, OpenDocument, images, zip/tar/gz."""
import io
import shutil
import struct
import tarfile
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest import mock

from smooth_agent import documents
from smooth_agent.documents import DocumentError, extract_archive, extract_text, image_size, kind_of, list_archive
from smooth_agent.tools import Workspace, default_registry


def tmp() -> Path:
    return Path(tempfile.mkdtemp())


def make_pdf(path: Path, pages_text: list[str]) -> Path:
    """A real (tiny) PDF: one Helvetica text line per page; '' gives a page with no text layer."""
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>", 5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"}
    kids = []
    n = 6
    for text in pages_text:
        page, content = n, n + 1
        n += 2
        kids.append(f"{page} 0 R")
        stream = (f"BT /F1 18 Tf 20 100 Td ({text}) Tj ET".encode() if text else b"0 0 1 rg 20 20 100 100 re f")
        objs[content] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        objs[page] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents {content} 0 R "
                      "/Resources << /Font << /F1 5 0 R >> >> >>").encode()
    objs[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>".encode()
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = {}
    for i in sorted(objs):
        offsets[i] = out.tell()
        out.write(b"%d 0 obj\n" % i + objs[i] + b"\nendobj\n")
    xref = out.tell()
    size = max(objs) + 1
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % size)
    for i in range(1, size):
        out.write(b"%010d 00000 n \n" % offsets[i] if i in offsets else b"0000000000 65535 f \n")
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref))
    path.write_bytes(out.getvalue())
    return path


def make_zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in files.items():
            z.writestr(name, body)
    return path


W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
S_NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
R_NS = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'


def make_docx(path: Path) -> Path:
    return make_zip(path, {"word/document.xml": f"""<w:document {W_NS}><w:body>
<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Laporan Bulanan</w:t></w:r></w:p>
<w:p><w:r><w:t xml:space="preserve">Penjualan naik </w:t></w:r><w:r><w:t>12%.</w:t></w:r></w:p>
<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Produk</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Unit</w:t></w:r></w:p></w:tc></w:tr>
<w:tr><w:tc><w:p><w:r><w:t>Kopi</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>340</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
</w:body></w:document>"""})


def make_xlsx(path: Path) -> Path:
    return make_zip(path, {
        "xl/workbook.xml": f'<workbook {S_NS} {R_NS}><sheets><sheet name="Penjualan" sheetId="1" r:id="rId1"/>'
                           f'<sheet name="Kosong" sheetId="2" r:id="rId2"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                      '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                                      '<Relationship Id="rId2" Target="/xl/worksheets/sheet2.xml"/></Relationships>',
        "xl/sharedStrings.xml": f'<sst {S_NS}><si><t>Produk</t></si><si><t>Harga</t></si><si><r><t>Ko</t></r><r><t>pi</t></r></si></sst>',
        "xl/worksheets/sheet1.xml": f"""<worksheet {S_NS}><sheetData>
<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="D1" t="inlineStr"><is><t>Stok?</t></is></c></row>
<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>25000.5</v></c><c r="D2" t="b"><v>1</v></c></row>
</sheetData></worksheet>""",
        "xl/worksheets/sheet2.xml": f"<worksheet {S_NS}><sheetData/></worksheet>",
    })


def make_pptx(path: Path) -> Path:
    def slide(text):
        return ('<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody>'
                f'<a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>')
    return make_zip(path, {"ppt/slides/slide2.xml": slide("Dua"), "ppt/slides/slide10.xml": slide("Sepuluh"),
                           "ppt/slides/slide1.xml": slide("Satu")})


def png_bytes(w=3, h=2):
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    ch = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)  # noqa: E731
    return b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + ch(b"IDAT", zlib.compress(raw)) + ch(b"IEND", b"")


class FormatTests(unittest.TestCase):
    def test_pdf_text(self):
        p = make_pdf(tmp() / "invoice.pdf", ["Invoice total 1500", "Paid in full"])
        ex = extract_text(p)
        self.assertEqual((ex.kind, ex.note), ("PDF", "2 pages"))
        self.assertIn("--- page 1 ---\nInvoice total 1500", ex.text)
        self.assertIn("Paid in full", ex.text)

    def test_pdf_falls_back_to_pdftotext(self):
        if not shutil.which("pdftotext"):
            self.skipTest("pdftotext not installed")
        p = make_pdf(tmp() / "a.pdf", ["Hello fallback"])
        with mock.patch.dict("sys.modules", {"pypdf": None}):  # import pypdf -> ImportError
            self.assertIn("Hello fallback", extract_text(p).text)

    def test_scanned_pdf_is_rendered_for_vision(self):
        d = tmp()
        p = make_pdf(d / "scan.pdf", ["", ""])
        ex = extract_text(p, render_dir=d / "scan-pages")
        self.assertIn("no text layer", ex.note)
        if shutil.which("pdftoppm"):
            self.assertIn("vision_analyze", ex.note)
            self.assertEqual(len(list((d / "scan-pages").glob("*.png"))), 2)

    def test_docx(self):
        ex = extract_text(make_docx(tmp() / "laporan.docx"))
        self.assertEqual(ex.kind, "Word")
        self.assertIn("# Laporan Bulanan", ex.text)
        self.assertIn("Penjualan naik 12%.", ex.text)
        self.assertIn("Produk | Unit\nKopi | 340", ex.text)
        self.assertIn("1 tables", ex.note)

    def test_xlsx(self):
        ex = extract_text(make_xlsx(tmp() / "data.xlsx"))
        self.assertEqual(ex.kind, "Excel")
        self.assertIn("## Sheet: Penjualan (2 rows × 4 columns)", ex.text)
        self.assertIn("Produk\tHarga\t\tStok?", ex.text)          # empty column C kept in place
        self.assertIn("Kopi\t25000.5\t\tTRUE", ex.text)           # rich-text shared string, number, boolean
        self.assertIn("## Sheet: Kosong", ex.text)
        self.assertEqual(ex.note, "2 sheets, tab-separated")

    def test_xlsx_row_cap(self):
        rows = "".join(f'<row r="{i}"><c r="A{i}"><v>{i}</v></c></row>' for i in range(1, 11))
        p = make_zip(tmp() / "big.xlsx", {
            "xl/workbook.xml": f'<workbook {S_NS} {R_NS}><sheets><sheet name="S" r:id="r1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                          '<Relationship Id="r1" Target="worksheets/s.xml"/></Relationships>',
            "xl/worksheets/s.xml": f"<worksheet {S_NS}><sheetData>{rows}</sheetData></worksheet>"})
        with mock.patch.object(documents, "MAX_ROWS", 3):
            text = extract_text(p).text
        self.assertIn("(10 rows", text)
        self.assertIn("… 7 more rows", text)

    def test_pptx_slide_order(self):
        ex = extract_text(make_pptx(tmp() / "deck.pptx"))
        self.assertEqual(ex.note, "3 slides")
        self.assertLess(ex.text.index("Satu"), ex.text.index("Dua"))
        self.assertLess(ex.text.index("Dua"), ex.text.index("Sepuluh"))  # slide10 after slide2

    def test_odt_and_ods(self):
        t = 'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
        tb = 'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"'
        odt = make_zip(tmp() / "a.odt", {"content.xml": f'<doc {t}><text:h text:outline-level="2">Judul</text:h>'
                                                         '<text:p>Isi paragraf</text:p></doc>'})
        self.assertIn("## Judul\nIsi paragraf", extract_text(odt).text)
        ods = make_zip(tmp() / "a.ods", {"content.xml": f'<doc {tb} {t}><table:table table:name="Data"><table:table-row>'
                       '<table:table-cell><text:p>a</text:p></table:table-cell>'
                       '<table:table-cell table:number-columns-repeated="2"><text:p>b</text:p></table:table-cell>'
                       '</table:table-row></table:table></doc>'})
        self.assertIn("## Sheet: Data\na\tb\tb", extract_text(ods).text)

    def test_damaged_office_file(self):
        p = tmp() / "broken.docx"
        p.write_bytes(b"not a zip")
        with self.assertRaises(DocumentError):
            extract_text(p)

    def test_image_sizes(self):
        d = tmp()
        (d / "a.png").write_bytes(png_bytes(3, 2))
        self.assertEqual(image_size(d / "a.png"), ("PNG", 3, 2))
        (d / "a.gif").write_bytes(b"GIF89a" + struct.pack("<HH", 40, 30) + b"\x00" * 10)
        self.assertEqual(image_size(d / "a.gif"), ("GIF", 40, 30))
        jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" \
               b"\xff\xc0\x00\x11\x08\x01\xe0\x02\x80\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
        (d / "a.jpg").write_bytes(jpeg)
        self.assertEqual(image_size(d / "a.jpg"), ("JPEG", 640, 480))
        ex = extract_text(d / "a.png")
        self.assertEqual(ex.kind, "Image")
        self.assertIn("PNG 3×2 image", ex.note)
        self.assertIn("vision_analyze", ex.note)

    def test_kind_by_magic(self):
        d = tmp()
        make_pdf(d / "noext", ["x"])
        self.assertEqual(kind_of(d / "noext"), "document")
        (d / "notes.txt").write_text("hi")
        self.assertEqual(kind_of(d / "notes.txt"), "text")
        self.assertEqual(kind_of(make_zip(d / "x.bin", {"a": "b"})), "archive")


class ArchiveTests(unittest.TestCase):
    def test_zip_list_and_safe_extract(self):
        d = tmp()
        p = d / "evil.zip"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("ok/readme.txt", "fine")
            z.writestr("../escape.txt", "nope")
            z.writestr("/abs.txt", "nope")
            link = zipfile.ZipInfo("link")
            link.external_attr = 0o120777 << 16
            z.writestr(link, "/etc/passwd")
        names = [n for n, _ in list_archive(p)]
        self.assertIn("ok/readme.txt", names)
        out = extract_archive(p, d / "out")
        self.assertEqual(sorted(f.relative_to((d / "out").resolve()).as_posix() for f in out), ["abs.txt", "ok/readme.txt"])
        self.assertFalse((d / "escape.txt").exists())
        self.assertFalse((d / "out" / "link").exists())

    def test_tar_skips_links_and_escapes(self):
        d = tmp()
        p = d / "a.tar.gz"
        with tarfile.open(p, "w:gz") as t:
            for name, body in (("data/x.csv", b"a,b\n1,2\n"), ("../up.txt", b"no")):
                info = tarfile.TarInfo(name)
                info.size = len(body)
                t.addfile(info, io.BytesIO(body))
            sym = tarfile.TarInfo("data/passwd")
            sym.type = tarfile.SYMTYPE
            sym.linkname = "/etc/passwd"
            t.addfile(sym)
        out = extract_archive(p, d / "out")
        self.assertEqual([f.name for f in out], ["x.csv"])
        self.assertFalse((d / "out" / "data" / "passwd").exists())

    def test_single_gz(self):
        import gzip
        d = tmp()
        with gzip.open(d / "log.txt.gz", "wb") as f:
            f.write(b"line1\nline2\n")
        out = extract_archive(d / "log.txt.gz", d / "out")
        self.assertEqual(out[0].read_text(), "line1\nline2\n")

    def test_zip_bomb_and_file_limits(self):
        d = tmp()
        bomb = d / "bomb.zip"
        with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("zeros.bin", b"\x00" * (60 << 20))
        with self.assertRaises(DocumentError) as cm:
            extract_archive(bomb, d / "b")
        self.assertIn("zip bomb", str(cm.exception))
        many = make_zip(d / "many.zip", {f"f{i}.txt": "x" for i in range(5)})
        with mock.patch.object(documents, "MAX_FILES", 3):
            with self.assertRaises(DocumentError):
                extract_archive(many, d / "m")

    def test_rar_without_extractor_says_what_to_install(self):
        d = tmp()
        p = d / "a.rar"
        p.write_bytes(b"Rar!\x1a\x07\x01\x00" + b"\x00" * 32)
        with mock.patch.object(documents, "_extractors", return_value=[]):
            with self.assertRaises(DocumentError) as cm:
                list_archive(p)
        self.assertIn("unrar", str(cm.exception))


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace(tmp())
        self.reg = default_registry(self.ws, allow_http=False)

    def test_read_file_converts_documents(self):
        make_docx(self.ws.root / "laporan.docx")
        out = self.reg.run("read_file", {"path": "laporan.docx"})
        self.assertTrue(out.startswith("[Word: laporan.docx — "), out[:80])
        self.assertIn("Kopi | 340", out)
        make_xlsx(self.ws.root / "data.xlsx")
        out = self.reg.run("read_file", {"path": "data.xlsx", "offset": 2, "limit": 1})
        self.assertIn("     2\tProduk\tHarga", out)
        self.assertIn("more lines", out)
        make_pdf(self.ws.root / "inv.pdf", ["Total 99"])
        self.assertIn("Total 99", self.reg.run("read_file", {"path": "inv.pdf"}))
        (self.ws.root / "plain.txt").write_text("just text")
        self.assertEqual(self.reg.run("read_file", {"path": "plain.txt"}), "just text")

    def test_scanned_pdf_pages_land_next_to_it(self):
        if not shutil.which("pdftoppm"):
            self.skipTest("pdftoppm not installed")
        (self.ws.root / "inbox").mkdir()
        make_pdf(self.ws.root / "inbox" / "scan.pdf", [""])
        out = self.reg.run("read_file", {"path": "inbox/scan.pdf"})
        self.assertIn("inbox/scan-pages/scan-page-1.png", out)

    def test_extract_archive_tool(self):
        make_zip(self.ws.root / "kirim.zip", {"a/b.txt": "isi", "c.docx": "x"})
        out = self.reg.run("extract_archive", {"path": "kirim.zip"})
        self.assertIn("unpacked 2 files", out)
        self.assertIn("kirim/a/b.txt", out)
        self.assertEqual((self.ws.root / "kirim" / "a" / "b.txt").read_text(), "isi")
        self.assertIn("kirim.zip", self.reg.run("read_file", {"path": "kirim.zip"}) + "kirim.zip")
        self.assertIn("a/b.txt", self.reg.run("read_file", {"path": "kirim.zip"}))  # archives list their files


if __name__ == "__main__":
    unittest.main()
