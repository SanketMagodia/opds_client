"""Turn a plain .txt book into a small EPUB 2 file.

EPUB 2 with a toc.ncx is the format simple e-ink readers handle most reliably.
Chapters are detected from heading lines like "CHAPTER IV." and long sections
are split so each XHTML file stays small enough for low-memory devices.
"""

import io
import re
import uuid
import zipfile
from html import escape

MAX_SECTION_CHARS = 40_000
HEADING = re.compile(
    r"^(chapter|book|part|prologue|epilogue|preface|introduction)\b.{0,60}$",
    re.IGNORECASE,
)


def decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1")


def paragraphs(text: str) -> list[str]:
    """Blank lines separate paragraphs; hard-wrapped lines inside are joined."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if lines:
            out.append(" ".join(lines))
    return out


def sections(paras: list[str], title: str) -> list[tuple[str, list[str]]]:
    """Group paragraphs into (heading, paragraphs) chapters, then split big ones."""
    chapters: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] = (title, [])
    for p in paras:
        if len(p) <= 80 and HEADING.match(p):
            if current[1]:
                chapters.append(current)
            current = (p, [])
        else:
            current[1].append(p)
    if current[1] or not chapters:
        chapters.append(current)

    result = []
    for heading, body in chapters:
        part, size, n = [], 0, 1
        for p in body:
            if part and size + len(p) > MAX_SECTION_CHARS:
                result.append((heading if n == 1 else f"{heading} ({n})", part))
                part, size, n = [], 0, n + 1
            part.append(p)
            size += len(p)
        result.append((heading if n == 1 else f"{heading} ({n})", part))
    return result


def xhtml(heading: str, body: list[str]) -> str:
    paras = "\n".join(f"<p>{escape(p)}</p>" for p in body)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>{escape(heading)}</title><link rel="stylesheet" type="text/css" href="style.css"/></head>
<body>
<h2>{escape(heading)}</h2>
{paras}
</body>
</html>
"""


def txt_to_epub(data: bytes, title: str, author: str = "Unknown") -> bytes:
    secs = sections(paragraphs(decode_text(data)), title)
    book_id = f"urn:uuid:{uuid.uuid4()}"
    files = [(f"text/s{i:03d}.xhtml", heading, body) for i, (heading, body) in enumerate(secs, 1)]

    manifest = "\n".join(
        f'<item id="s{i}" href="{path}" media-type="application/xhtml+xml"/>'
        for i, (path, _, _) in enumerate(files, 1)
    )
    spine = "\n".join(f'<itemref idref="s{i}"/>' for i in range(1, len(files) + 1))
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="bookid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
<dc:title>{escape(title)}</dc:title>
<dc:creator opf:role="aut">{escape(author)}</dc:creator>
<dc:language>en</dc:language>
<dc:identifier id="bookid">{book_id}</dc:identifier>
</metadata>
<manifest>
<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
<item id="css" href="text/style.css" media-type="text/css"/>
{manifest}
</manifest>
<spine toc="ncx">
{spine}
</spine>
</package>
"""
    nav_points = "\n".join(
        f'<navPoint id="n{i}" playOrder="{i}"><navLabel><text>{escape(heading)}</text></navLabel>'
        f'<content src="{path}"/></navPoint>'
        for i, (path, heading, _) in enumerate(files, 1)
    )
    ncx = f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
<head><meta name="dtb:uid" content="{book_id}"/><meta name="dtb:depth" content="1"/>
<meta name="dtb:totalPageCount" content="0"/><meta name="dtb:maxPageNumber" content="0"/></head>
<docTitle><text>{escape(title)}</text></docTitle>
<navMap>
{nav_points}
</navMap>
</ncx>
"""
    container = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""
    css = "p { text-indent: 1.2em; margin: 0 0 0.4em 0; }\nh2 { text-align: center; }\n"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        # The EPUB spec requires "mimetype" first and uncompressed.
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/toc.ncx", ncx, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/text/style.css", css, compress_type=zipfile.ZIP_DEFLATED)
        for path, heading, body in files:
            z.writestr(f"OEBPS/{path}", xhtml(heading, body), compress_type=zipfile.ZIP_DEFLATED)
    return buf.getvalue()
