"""Tiny documents built in memory for the Live Assistant document tests (and their
fuzzing): no files in the repo, nothing downloaded. Not a test module."""
import io
import random
import re
import zipfile
import zlib
from xml.sax.saxutils import escape

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def make_zip(parts, stored_first=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if stored_first:
            name, data = stored_first
            zf.writestr(zipfile.ZipInfo(name), data, zipfile.ZIP_STORED)
        for name, data in parts.items():
            zf.writestr(name, data)
    return buf.getvalue()


# ── PDF ──
def _pdf_escape(text):
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages, extra_objects=(), kids_override=None):
    """A valid one-font PDF; ``pages`` are strings (lines split by \\n; "" = a page with
    no text, like a scan)."""
    n = len(pages)
    font_id = 3 + 2 * n
    kids = kids_override or " ".join("%d 0 R" % (3 + 2 * i) for i in range(n))
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            ("<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, n)).encode()]
    for i, text in enumerate(pages):
        pid, cid = 3 + 2 * i, 4 + 2 * i
        objs.append(("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
                     "/Resources << /Font << /F1 %d 0 R >> >> >>" % (cid, font_id)).encode())
        if text:
            lines = text.split("\n")
            ops = "BT /F1 12 Tf 72 720 Td " + " ".join(
                "%s(%s) Tj" % ("0 -16 Td " if k else "", _pdf_escape(line))
                for k, line in enumerate(lines)) + " ET"
            stream = ops.encode("latin-1")
        else:
            stream = b""
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objs.extend(extra_objects)
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, xref))
    return bytes(out)


def make_pdf_with_bomb(zeros=150_000_000):
    """One page whose content stream inflates to ``zeros`` bytes (a few hundred KB packed)."""
    comp = zlib.compressobj(9)
    chunk = b"0" * (1 << 20)
    packed = bytearray()
    for _ in range(zeros // len(chunk)):
        packed += comp.compress(chunk)
    packed += comp.flush()
    base = make_pdf([""])
    # replace the empty content stream of page 1 (object 4) by the packed one
    obj4 = b"4 0 obj\n<< /Length 0 >>\nstream\n\nendstream\nendobj\n"
    assert obj4 in base
    new = (b"4 0 obj\n<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(packed)
           + bytes(packed) + b"\nendstream\nendobj\n")
    return base.replace(obj4, new)


def encrypt_pdf(pdf, user="secret", owner="owner", algorithm="RC4-128"):
    """``pdf`` locked with ``algorithm`` ("RC4-128", "AES-128", "AES-256"; AES needs an AES
    package for pypdf, as the reader does). user="" opens without a password (owner only)."""
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf)))
    writer.encrypt(user, owner, algorithm=algorithm)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ── Office Open XML ──
def _w_p(text):
    return "<w:p><w:r><w:t xml:space=\"preserve\">%s</w:t></w:r></w:p>" % escape(text)


def make_docx(paragraphs, table=None, extra_parts=None):
    body = "".join(_w_p(p) for p in paragraphs)
    if table:
        rows = "".join("<w:tr>%s</w:tr>" % "".join("<w:tc>%s</w:tc>" % _w_p(c) for c in row)
                       for row in table)
        body += "<w:tbl>%s</w:tbl>" % rows
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="%s"><w:body>%s</w:body></w:document>' % (W_NS, body))
    parts = {"[Content_Types].xml": "<Types/>", "word/document.xml": doc}
    parts.update(extra_parts or {})
    return make_zip(parts)


def make_pptx(slides, notes=None):
    notes = notes or {}
    sld_ids, rels, parts = [], [], {}
    for i, text in enumerate(slides, 1):
        sld_ids.append('<p:sldId id="%d" r:id="rId%d"/>' % (255 + i, i))
        rels.append('<Relationship Id="rId%d" Type="%s/slide" Target="slides/slide%d.xml"/>'
                    % (i, R_NS, i))
        paras = "".join("<a:p><a:r><a:t>%s</a:t></a:r></a:p>" % escape(t)
                        for t in text.split("\n"))
        parts["ppt/slides/slide%d.xml" % i] = (
            '<p:sld xmlns:p="%s" xmlns:a="%s"><p:cSld><p:spTree><p:sp><p:txBody>%s</p:txBody>'
            "</p:sp></p:spTree></p:cSld></p:sld>" % (P_NS, A_NS, paras))
        if i in notes:
            parts["ppt/slides/_rels/slide%d.xml.rels" % i] = (
                '<Relationships xmlns="%s"><Relationship Id="rId1" Type="%s/notesSlide" '
                'Target="../notesSlides/notesSlide%d.xml"/></Relationships>' % (REL_NS, R_NS, i))
            parts["ppt/notesSlides/notesSlide%d.xml" % i] = (
                '<p:notes xmlns:p="%s" xmlns:a="%s"><p:txBody><a:p><a:r><a:t>%s</a:t></a:r>'
                "</a:p></p:txBody></p:notes>" % (P_NS, A_NS, escape(notes[i])))
    parts["ppt/presentation.xml"] = (
        '<p:presentation xmlns:p="%s" xmlns:r="%s"><p:sldIdLst>%s</p:sldIdLst></p:presentation>'
        % (P_NS, R_NS, "".join(sld_ids)))
    parts["ppt/_rels/presentation.xml.rels"] = (
        '<Relationships xmlns="%s">%s</Relationships>' % (REL_NS, "".join(rels)))
    parts["[Content_Types].xml"] = "<Types/>"
    return make_zip(parts)


def make_xlsx(sheets, hidden=()):
    """``sheets``: {name: [[cell, ...], ...]} (strings and numbers)."""
    shared, sst_index = [], {}

    def sst(text):
        if text not in sst_index:
            sst_index[text] = len(shared)
            shared.append(text)
        return sst_index[text]

    sheet_tags, rels, parts = [], [], {}
    for n, (name, rows) in enumerate(sheets.items(), 1):
        state = ' state="hidden"' if name in hidden else ""
        sheet_tags.append('<sheet name="%s" sheetId="%d" r:id="rId%d"%s/>'
                          % (escape(name, {'"': "&quot;"}), n, n, state))
        rels.append('<Relationship Id="rId%d" Type="%s/worksheet" Target="worksheets/sheet%d.xml"/>'
                    % (n, R_NS, n))
        xml_rows = []
        for r, row in enumerate(rows, 1):
            cells = []
            for c, value in enumerate(row):
                ref = "%s%d" % (chr(65 + c), r)
                if isinstance(value, str):
                    cells.append('<c r="%s" t="s"><v>%d</v></c>' % (ref, sst(value)))
                else:
                    cells.append('<c r="%s"><v>%s</v></c>' % (ref, value))
            xml_rows.append('<row r="%d">%s</row>' % (r, "".join(cells)))
        parts["xl/worksheets/sheet%d.xml" % n] = (
            '<worksheet xmlns="%s"><sheetData>%s</sheetData></worksheet>' % (S_NS, "".join(xml_rows)))
    parts["xl/workbook.xml"] = ('<workbook xmlns="%s" xmlns:r="%s"><sheets>%s</sheets></workbook>'
                                % (S_NS, R_NS, "".join(sheet_tags)))
    parts["xl/_rels/workbook.xml.rels"] = ('<Relationships xmlns="%s">%s</Relationships>'
                                           % (REL_NS, "".join(rels)))
    parts["xl/sharedStrings.xml"] = ('<sst xmlns="%s">%s</sst>' % (
        S_NS, "".join("<si><t>%s</t></si>" % escape(s) for s in shared)))
    parts["[Content_Types].xml"] = "<Types/>"
    return make_zip(parts)


# ── OpenDocument ──
ODF_NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
          'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
          'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
          'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"')


def make_odf(kind, body):
    mime = {"odt": "application/vnd.oasis.opendocument.text",
            "odp": "application/vnd.oasis.opendocument.presentation",
            "ods": "application/vnd.oasis.opendocument.spreadsheet"}[kind]
    content = ('<?xml version="1.0" encoding="UTF-8"?><office:document-content %s>'
               "<office:scripts><text:p>MACRO SECRET</text:p></office:scripts>"
               "<office:body>%s</office:body></office:document-content>" % (ODF_NS, body))
    return make_zip({"content.xml": content}, stored_first=("mimetype", mime))


def make_odt(paragraphs):
    body = "<office:text>%s</office:text>" % "".join(
        "<text:p>%s</text:p>" % escape(p) for p in paragraphs)
    return make_odf("odt", body)


def make_odp(slides):
    pages = "".join('<draw:page draw:name="s%d"><text:p>%s</text:p></draw:page>' % (i, escape(t))
                    for i, t in enumerate(slides, 1))
    return make_odf("odp", "<office:presentation>%s</office:presentation>" % pages)


def make_ods(sheets):
    tables = ""
    for name, rows in sheets.items():
        trs = "".join("<table:table-row>%s</table:table-row>" % "".join(
            "<table:table-cell><text:p>%s</text:p></table:table-cell>" % escape(str(c)) for c in row)
            for row in rows)
        tables += '<table:table table:name="%s">%s</table:table>' % (escape(name), trs)
    return make_odf("ods", "<office:spreadsheet>%s</office:spreadsheet>" % tables)


def make_epub(chapters):
    manifest = "".join('<item id="c%d" href="ch%d.xhtml" media-type="application/xhtml+xml"/>'
                       % (i, i) for i in range(1, len(chapters) + 1))
    spine = "".join('<itemref idref="c%d"/>' % i for i in range(1, len(chapters) + 1))
    parts = {
        "META-INF/container.xml": (
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>'),
        "OEBPS/content.opf": ('<package xmlns="http://www.idpf.org/2007/opf"><manifest>%s</manifest>'
                              "<spine>%s</spine></package>" % (manifest, spine)),
    }
    for i, text in enumerate(chapters, 1):
        parts["OEBPS/ch%d.xhtml" % i] = (
            "<html><head><title>x</title><style>p{color:red}</style></head><body>"
            "<h1>Chapter %d</h1><p>%s</p><script>alert('x')</script></body></html>"
            % (i, escape(text)))
    return make_zip(parts, stored_first=("mimetype", "application/epub+zip"))


# ── the rest ──
def make_rtf(text, with_picture=True):
    pict = "{\\pict\\pngblip 89504e470d0a1a0a0000000d49484452}" if with_picture else ""
    uni = "".join("\\u%d?" % (ord(c) if ord(c) < 32768 else ord(c) - 65536) for c in text)
    return ("{\\rtf1\\ansi\\ansicpg1252{\\fonttbl{\\f0 Arial;}}{\\*\\generator Secret;}"
            "%s\\pard Hello \\'e9t\\'e9 world\\par %s\\par}" % (pict, uni)).encode("latin-1")


def make_png(size=(40, 30), mode="RGB", color=(200, 30, 30)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, "PNG")
    return buf.getvalue()


def make_image(fmt, size=(64, 48)):
    from PIL import Image
    buf = io.BytesIO()
    img = Image.new("RGB", size, (20, 120, 200))
    img.save(buf, fmt)
    return buf.getvalue()


def make_big_header_png(width, height):
    """A PNG that CLAIMS a huge size (IHDR) with almost no data: must be refused from
    the header, before anything is decoded."""
    import struct

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00" * 10)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


# ── hostile ──
ENTITY_BOMB = (
    '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
    '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
    '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>'
    '<w:document xmlns:w="%s"><w:body><w:p><w:r><w:t>&lol3;</w:t></w:r></w:p></w:body>'
    "</w:document>" % W_NS)
XXE = ('<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///C:/Windows/win.ini">]>'
       '<w:document xmlns:w="%s"><w:body><w:p><w:r><w:t>&x;</w:t></w:r></w:p></w:body>'
       "</w:document>" % W_NS)


def make_zip_bomb(declared=60_000_000):
    """A .docx whose document.xml is `declared` bytes of repeated markup, a few KB packed."""
    xml = ('<w:document xmlns:w="%s"><w:body>' % W_NS).encode()
    filler = b"<w:p><w:r><w:t>aaaaaaaaaaaaaaaaaaaaaaaa</w:t></w:r></w:p>"
    big = xml + filler * (declared // len(filler))
    return make_zip({"[Content_Types].xml": "<Types/>", "word/document.xml": big})


def make_many_entries_zip(count=6000):
    return make_zip({"f%d.txt" % i: b"x" for i in range(count)})


def deep_xml(depth=5000):
    return ('<w:document xmlns:w="%s"><w:body>%s<w:p/>%s</w:body></w:document>'
            % (W_NS, "<w:x>" * depth, "</w:x>" * depth)).encode()


# ── fuzzing ──
def mutate(data, rng):
    """One random corruption of ``data`` (bytes)."""
    data = bytearray(data)
    if not data:
        return bytes(rng.getrandbits(8) for _ in range(rng.randint(1, 16)))
    kind = rng.randint(0, 7)
    if kind == 0:                               # flip some bytes
        for _ in range(rng.randint(1, 8)):
            data[rng.randrange(len(data))] = rng.getrandbits(8)
    elif kind == 1:                             # truncate
        del data[rng.randrange(len(data)):]
    elif kind == 2:                             # delete a slice
        a = rng.randrange(len(data))
        del data[a:a + rng.randint(1, 64)]
    elif kind == 3:                             # insert junk
        a = rng.randrange(len(data) + 1)
        data[a:a] = bytes(rng.getrandbits(8) for _ in range(rng.randint(1, 64)))
    elif kind == 4:                             # duplicate a slice
        a = rng.randrange(len(data))
        b = min(len(data), a + rng.randint(1, 256))
        data[a:a] = data[a:b]
    elif kind == 5:                             # zero a slice
        a = rng.randrange(len(data))
        data[a:a + rng.randint(1, 64)] = b"\x00" * min(64, len(data) - a)
    elif kind == 6:                             # swap two bytes ranges
        a, b = rng.randrange(len(data)), rng.randrange(len(data))
        data[a], data[b] = data[b], data[a]
    else:                                       # prepend / append garbage
        if rng.random() < 0.5:
            data[:0] = bytes(rng.getrandbits(8) for _ in range(rng.randint(1, 32)))
        else:
            data += bytes(rng.getrandbits(8) for _ in range(rng.randint(1, 32)))
    return bytes(data)


def mutate_zip_parts(data, rng):
    """Mutate the CONTENT of one part of a zip and re-zip it (a byte flip in a
    compressed stream only gives a CRC error; this reaches the XML/HTML parsers)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            parts = [(zi.filename, zf.read(zi.filename)) for zi in zf.infolist()]
    except Exception:
        return mutate(data, rng)
    i = rng.randrange(len(parts))
    name, body = parts[i]
    parts[i] = (name, mutate(body, rng))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, b in parts:
            zf.writestr(n, b)
    return buf.getvalue()


def seeded(seed):
    return random.Random(seed)


__all__ = [n for n in dir() if n.startswith(("make_", "mutate", "encrypt_", "deep_", "ENTITY", "XXE", "seeded"))]
