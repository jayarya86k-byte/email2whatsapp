"""Document generation: .docx, .pdf, .xlsx, .csv, .html and Markdown.

Everything is written with zipfile + minimal OOXML (or a tiny PDF writer) so
document automation works offline with no third-party dependency. If
python-docx / openpyxl / reportlab happen to be installed they are not
required, but the built-in writers stay the default for reproducibility.
"""

from __future__ import annotations

import csv
import io
import os
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape

from .registry import ToolRegistry

# --------------------------------------------------------------------------
# DOCX (WordprocessingML)
# --------------------------------------------------------------------------
CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:rPr><w:b/><w:sz w:val="52"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:rPr><w:b/><w:sz w:val="36"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
</w:styles>"""


def _docx_paragraph(text: str, style: str = "Normal", bold: bool = False) -> str:
    run_props = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return (
        f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
        f'<w:r>{run_props}<w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'
    )


def _docx_table(rows: Sequence[Sequence[Any]], header: bool = True) -> str:
    if not rows:
        return ""
    columns = max(len(str(cell)) for cell in [list(map(str, row)) for row in rows])
    widths = int(9360 / max(1, columns))
    out = [
        '<w:tbl><w:tblPr><w:tblW w:w="9360" w:type="dxa"/>'
        '<w:tblBorders>' + "".join(
            f'<w:{edge} w:val="single" w:sz="4" w:space="0" w:color="B0B7C3"/>'
            for edge in ("top", "left", "bottom", "right", "insideH", "insideV")
        ) + "</w:tblBorders></w:tblPr>"
    ]
    for index, row in enumerate(rows):
        cells = [str(cell) for cell in row] + [""] * (columns - len(row))
        bold = header and index == 0
        out.append("<w:tr>")
        for cell in cells:
            run_props = "<w:rPr><w:b/></w:rPr>" if bold else ""
            out.append(
                f'<w:tc><w:tcPr><w:tcW w:w="{widths}" w:type="dxa"/></w:tcPr>'
                f'<w:p><w:r>{run_props}<w:t xml:space="preserve">{escape(cell)}</w:t></w:r></w:p></w:tc>'
            )
        out.append("</w:tr>")
    out.append("</w:tbl>")
    return "".join(out)


def write_docx(path: str, blocks: Iterable[Any]) -> str:
    """blocks: list of {"type": "heading"|"paragraph"|"table"|"pagebreak", ...}"""
    body: List[str] = []
    for block in blocks:
        kind = str(block.get("type", "paragraph")).lower() if isinstance(block, dict) else "paragraph"
        if kind in ("heading", "h1", "title"):
            level = block.get("level", 1)
            style = "Title" if level == 0 else ("Heading1" if level <= 1 else "Heading2")
            body.append(_docx_paragraph(str(block.get("text", "")), style))
        elif kind == "table":
            body.append(_docx_table(block.get("rows", []), bool(block.get("header", True))))
        elif kind == "pagebreak":
            body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
        elif kind == "bullets":
            for item in block.get("items", []):
                body.append(_docx_paragraph(f"\u2022 {item}", "Normal"))
        else:
            text = str(block.get("text", ""))
            body.append(_docx_paragraph(text, "Normal", bool(block.get("bold", False))))
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(body)}"
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/></w:sectPr>'
        "</w:body></w:document>"
    )
    target = Path(os.path.expanduser(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES_XML)
        archive.writestr("_rels/.rels", ROOT_RELS)
        archive.writestr("word/_rels/document.xml.rels", DOC_RELS)
        archive.writestr("word/styles.xml", STYLES_XML)
        archive.writestr("word/document.xml", document)
    return str(target)


# --------------------------------------------------------------------------
# PDF (text-only, standard 14 fonts)
# --------------------------------------------------------------------------
def _pdf_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def write_pdf(path: str, lines: Sequence[str], title: str = "") -> str:
    """Single-weight Helvetica PDF; enough for challans, invoices, letters."""
    content: List[str] = ["BT", "/F1 11 Tf", "14 TL", "56 786 Td"]
    for line in lines:
        text = str(line)
        if text == "\f":
            content.append("ET")
            content.append("BT /F1 11 Tf 14 TL 56 786 Td")
            continue
        content.append(f"({_pdf_escape(text)}) Tj T*")
    content.append("ET")
    stream = "\n".join(content).encode("latin-1", "replace")

    objects: List[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objects.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> /Contents 4 0 R >>"
    )
    objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    if title:
        objects.append(
            b"<< /Title (" + _pdf_escape(title).encode("latin-1", "replace") + b") >>"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets: List[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF".encode()
    target = Path(os.path.expanduser(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(bytes(out))
    return str(target)


# --------------------------------------------------------------------------
# XLSX (SpreadsheetML)
# --------------------------------------------------------------------------
def write_xlsx(path: str, sheets: Dict[str, List[List[Any]]]) -> str:
    def col_name(index: int) -> str:
        name = ""
        index += 1
        while index:
            index, rem = divmod(index - 1, 26)
            name = chr(65 + rem) + name
        return name

    sheet_xml: Dict[str, str] = {}
    for sheet_name, rows in sheets.items():
        body: List[str] = []
        for r_index, row in enumerate(rows, start=1):
            cells: List[str] = []
            for c_index, value in enumerate(row):
                ref = f"{col_name(c_index)}{r_index}"
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    cells.append(f'<c r="{ref}"><v>{value}</v></c>')
                else:
                    cells.append(
                        f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">'
                        f"{escape(str(value))}</t></is></c>"
                    )
            body.append(f'<row r="{r_index}">{"".join(cells)}</row>')
        sheet_xml[sheet_name] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{''.join(body)}</sheetData></worksheet>"
        )

    entries = list(sheets) or ["Sheet1"]
    workbook_sheets = "".join(
        f'<sheet name="{escape(name)}" sheetId="{i + 1}" r:id="rId{i + 1}"/>'
        for i, name in enumerate(entries)
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{workbook_sheets}</sheets></workbook>"
    )
    workbook_rels = "".join(
        f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/'
        f'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i + 1}.xml"/>'
        for i in range(len(entries))
    ) + (
        f'<Relationship Id="rId{len(entries) + 1}" Type="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    )
    workbook_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{workbook_rels}</Relationships>"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts></styleSheet>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.spreadsheetml.styles+xml"/>'
        + "".join(
            f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for i in range(len(entries))
        )
        + "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
    )
    target = Path(os.path.expanduser(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        archive.writestr("xl/styles.xml", styles)
        for index, name in enumerate(entries, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", sheet_xml.get(name, ""))
    return str(target)


def write_csv(path: str, rows: List[List[Any]]) -> str:
    target = Path(os.path.expanduser(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(rows)
    return str(target)


# --------------------------------------------------------------------------
# Markdown -> HTML (for quick print/PDF via the browser)
# --------------------------------------------------------------------------
def markdown_to_html(markdown: str) -> str:
    import re

    def inline(text: str) -> str:
        text = escape(text)
        text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
        text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)
        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
        return text

    def table_row(cells: List[str], tag: str) -> str:
        return f"<tr>" + "".join(f"<{tag}>{inline(c)}</{tag}>" for c in cells) + "</tr>"

    def is_separator(line: str) -> bool:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        return bool(cells) and all(set(c) <= set("-: ") and c for c in cells)

    out: List[str] = []
    in_list = False
    lines = markdown.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].rstrip()
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            if in_list:
                out.append("</ul>")
                in_list = False
            rows: List[str] = []
            header: List[str] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                cells = [c.strip() for c in lines[index].strip().strip("|").split("|")]
                if not is_separator(lines[index]):
                    if not header:
                        header = cells
                        rows.append(table_row(cells, "th"))
                    else:
                        rows.append(table_row(cells, "td"))
                index += 1
            out.append(f"<table>{''.join(rows)}</table>")
            continue
        index += 1
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        bullet = re.match(r"^\s*[-*+]\s+(.*)$", line)
        if heading:
            if in_list:
                out.append("</ul>")
                in_list = False
            level = len(heading.group(1))
            out.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
        elif bullet:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(bullet.group(1))}</li>")
        elif not line.strip():
            if in_list:
                out.append("</ul>")
                in_list = False
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<p>{inline(line)}</p>")
    if in_list:
        out.append("</ul>")
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<style>body{font-family:system-ui;max-width:820px;margin:40px auto;line-height:1.55}"
        "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:6px 10px}"
        "pre{background:#f4f4f5;padding:10px;overflow:auto}</style></head><body>"
        + "".join(out)
        + "</body></html>"
    )


HTML_SHELL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<style>
 body{{font-family:'Segoe UI',system-ui,sans-serif;color:#111;margin:0;padding:40px;line-height:1.5}}
 h1,h2,h3{{color:#0f172a}} table{{border-collapse:collapse;width:100%;margin:16px 0}}
 th,td{{border:1px solid #d4d4d8;padding:8px 10px;text-align:left}}
 th{{background:#f4f4f5}} .totals td{{font-weight:bold}}
 @media print{{body{{padding:0}} .no-print{{display:none}}}}
</style></head><body>{body}</body></html>"""


def render_html(title: str, body_html: str) -> str:
    return HTML_SHELL.format(title=escape(title), body=body_html)


def register(registry: ToolRegistry, policy: Any, audit: Any) -> None:
    def create_docx(path: str, blocks: List[Dict[str, Any]], title: str = "") -> Dict[str, Any]:
        """Create a Word document. blocks: heading/paragraph/bullets/table/pagebreak."""
        target = write_docx(path, blocks)
        audit.record("doc.docx", risk=0.3, capability="fs.write",
                     detail={"path": target, "blocks": len(blocks)})
        return {"ok": True, "path": target, "format": "docx", "blocks": len(blocks)}

    def create_pdf(path: str, lines: List[str], title: str = "") -> Dict[str, Any]:
        """Create a simple text PDF (challans, letters, statements)."""
        target = write_pdf(path, lines, title)
        audit.record("doc.pdf", risk=0.3, capability="fs.write",
                     detail={"path": target, "lines": len(lines)})
        return {"ok": True, "path": target, "format": "pdf"}

    def create_xlsx(path: str, sheets: Dict[str, List[List[Any]]]) -> Dict[str, Any]:
        """Create an Excel workbook from a {sheet: rows} mapping."""
        target = write_xlsx(path, sheets)
        audit.record("doc.xlsx", risk=0.3, capability="fs.write",
                     detail={"path": target, "sheets": list(sheets)})
        return {"ok": True, "path": target, "format": "xlsx"}

    def create_csv(path: str, rows: List[List[Any]]) -> Dict[str, Any]:
        """Write rows to a CSV file."""
        target = write_csv(path, rows)
        audit.record("doc.csv", risk=0.25, capability="fs.write", detail={"path": target})
        return {"ok": True, "path": target, "format": "csv"}

    def markdown_to_doc(path: str, markdown: str, title: str = "") -> Dict[str, Any]:
        """Convert Markdown to a styled Word document (headings, bullets, tables)."""
        import re

        blocks: List[Dict[str, Any]] = []
        for raw in markdown.splitlines():
            heading = re.match(r"^(#{1,6})\s+(.*)$", raw.strip())
            bullet = re.match(r"^\s*[-*+]\s+(.*)$", raw)
            if heading:
                blocks.append({"type": "heading", "level": len(heading.group(1)) - 1,
                               "text": heading.group(2)})
            elif bullet:
                if blocks and blocks[-1].get("type") == "bullets":
                    blocks[-1]["items"].append(bullet.group(1))
                else:
                    blocks.append({"type": "bullets", "items": [bullet.group(1)]})
            elif raw.strip().startswith("|"):
                cells = [c.strip() for c in raw.strip().strip("|").split("|")]
                if all(set(c) <= set("-: ") for c in cells):
                    continue
                if blocks and blocks[-1].get("type") == "table":
                    blocks[-1]["rows"].append(cells)
                else:
                    blocks.append({"type": "table", "rows": [cells], "header": True})
            elif raw.strip():
                blocks.append({"type": "paragraph", "text": raw.rstrip()})
        target = write_docx(path, blocks)
        audit.record("doc.markdown_to_docx", risk=0.3, capability="fs.write",
                     detail={"path": target, "blocks": len(blocks)})
        return {"ok": True, "path": target, "format": "docx", "blocks": len(blocks)}

    def render_html_doc(path: str, body_html: str, title: str = "Ultron Document") -> Dict[str, Any]:
        """Save a print-ready HTML document."""
        target = Path(os.path.expanduser(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_html(title, body_html), "utf-8")
        audit.record("doc.html", risk=0.3, capability="fs.write", detail={"path": str(target)})
        return {"ok": True, "path": str(target), "format": "html"}

    def type_text(text: str, destination: str = "", append: bool = False) -> Dict[str, Any]:
        """Dictate into a text/Markdown file, or return the text to be typed."""
        if not destination:
            return {"ok": True, "text": text, "chars": len(text)}
        target = Path(os.path.expanduser(destination))
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a" if append else "w", encoding="utf-8") as handle:
            handle.write(text)
        audit.record("doc.type", risk=0.25, capability="fs.write", detail={"path": str(target)})
        return {"ok": True, "path": str(target), "chars": len(text)}

    registry.function_tool(create_docx, risk=0.3, capability="fs.write",
                           category="documents", confirm_args=["path"])
    registry.function_tool(create_pdf, risk=0.3, capability="fs.write",
                           category="documents", confirm_args=["path"])
    registry.function_tool(create_xlsx, risk=0.3, capability="fs.write",
                           category="documents", confirm_args=["path"])
    registry.function_tool(create_csv, risk=0.25, capability="fs.write",
                           category="documents", confirm_args=["path"])
    registry.function_tool(markdown_to_doc, risk=0.3, capability="fs.write",
                           category="documents", confirm_args=["path"])
    registry.function_tool(render_html_doc, risk=0.3, capability="fs.write",
                           category="documents", confirm_args=["path"])
    registry.function_tool(type_text, risk=0.2, capability="fs.write", category="documents")
