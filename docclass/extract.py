"""여러 문서 형식을 Markdown으로 변환한다.

지원: txt, md, csv, docx, xlsx/xlsm, pptx, pdf, hwpx, hwp(5.x)
표는 Markdown 표로 내보내고, 청크 분할 단계(chunking.py)가 행 단위로 다시 읽는다.
"""

from __future__ import annotations

import csv
import re
import struct
import zipfile
import zlib
from datetime import date, datetime, time
from pathlib import Path
from xml.etree import ElementTree as ET


class ExtractError(Exception):
    """문서를 읽을 수 없을 때."""


def _require(module: str, package: str):
    try:
        return __import__(module)
    except ImportError as exc:
        raise ExtractError(
            f"{package} 패키지가 필요합니다: pip install 'docclass[docs]'"
        ) from exc


# ---- 공통 도우미 --------------------------------------------------------

_SPACES = re.compile(r"[ \t\r\f\v]+")


def _clean(value) -> str:
    """셀 하나에 들어갈 한 줄짜리 문자열."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else f"{value:.10g}"
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time(0) else value.isoformat(" ", "minutes")
    if isinstance(value, (date, time)):
        return value.isoformat()
    return _SPACES.sub(" ", str(value).replace("\n", " ")).strip()


def md_table(rows: list[list]) -> str:
    """2차원 배열을 Markdown 표로. 완전히 빈 행과 열은 뺀다. 첫 행이 머리글이 된다."""
    grid = [[_clean(c) for c in row] for row in rows]
    grid = [row for row in grid if any(row)]
    if not grid:
        return ""
    width = max(len(row) for row in grid)
    grid = [row + [""] * (width - len(row)) for row in grid]
    keep = [c for c in range(width) if any(row[c] for row in grid)]
    grid = [[row[c].replace("|", "\\|") for c in keep] for row in grid]
    lines = ["| " + " | ".join(grid[0]) + " |", "|" + " --- |" * len(keep)]
    lines += ["| " + " | ".join(row) + " |" for row in grid[1:]]
    return "\n".join(lines)


def _grid_from_cells(cells: dict[tuple[int, int], tuple[str, int]]) -> list[list[str]]:
    """{(행, 열): (텍스트, 세로 병합 수)} -> 2차원 배열. 세로 병합은 아래 행에도 값을 채운다."""
    if not cells:
        return []
    filled: dict[tuple[int, int], str] = {}
    for (row, col), (text, rowspan) in cells.items():
        for r in range(row, row + max(rowspan, 1)):
            filled.setdefault((r, col), text)
    n_rows = max(r for r, _ in filled) + 1
    n_cols = max(c for _, c in filled) + 1
    return [[filled.get((r, c), "") for c in range(n_cols)] for r in range(n_rows)]


def _flatten(grid: list[list[str]]) -> str:
    """표 안의 표처럼 Markdown 표로 둘 수 없는 것을 한 줄로 편다."""
    return "; ".join(", ".join(c for c in row if c) for row in grid if any(row))


def _join(blocks: list[str]) -> str:
    text = "\n\n".join(b.strip("\n") for b in blocks if b and b.strip())
    return re.sub(r"\n{3,}", "\n\n", text) + "\n" if text else ""


# ---- 텍스트 / CSV -------------------------------------------------------


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _csv(path: Path) -> str:
    return _join([md_table(list(csv.reader(_read_text(path).splitlines())))])


# ---- DOCX ---------------------------------------------------------------


def _docx_cell_text(cell) -> str:
    parts = [p.text for p in cell.paragraphs]
    parts += [_flatten(_docx_grid(t)) for t in cell.tables]
    return " ".join(p.strip() for p in parts if p and p.strip())


def _docx_grid(table) -> list[list[str]]:
    grid = []
    for row in table.rows:
        cells, previous = [], None
        for cell in row.cells:
            # 가로로 병합된 셀은 같은 객체가 반복해서 나온다. 한 번만 쓴다.
            cells.append("" if cell._tc is previous else _docx_cell_text(cell))
            previous = cell._tc
        grid.append(cells)
    return grid


def _docx(path: Path) -> str:
    docx = _require("docx", "python-docx")
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(str(path))
    blocks: list[str] = []
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph = Paragraph(child, document)
            text = paragraph.text.strip()
            if not text:
                continue
            style = (paragraph.style.name or "") if paragraph.style is not None else ""
            match = re.match(r"(?:Heading|제목)\s*(\d)", style)
            if match:
                text = "#" * min(int(match.group(1)), 6) + " " + text
            elif style.lower() == "title":
                text = "# " + text
            blocks.append(text)
        elif tag == "tbl":
            blocks.append(md_table(_docx_grid(Table(child, document))))
    return _join(blocks)


# ---- XLSX ---------------------------------------------------------------


def _xlsx(path: Path) -> str:
    openpyxl = _require("openpyxl", "openpyxl")
    # data_only=True: 수식 대신 엑셀이 마지막으로 계산해 저장한 값을 읽는다.
    workbook = openpyxl.load_workbook(str(path), data_only=True)
    blocks: list[str] = []
    for sheet in workbook.worksheets:
        if sheet.sheet_state != "visible" or not hasattr(sheet, "iter_rows"):
            continue
        rows = [list(r) for r in sheet.iter_rows(values_only=True)]
        min_row, min_col = sheet.min_row, sheet.min_column
        for merged in sheet.merged_cells.ranges:  # 세로 병합은 아래 행에도 값을 채운다
            r0, c0 = merged.min_row - min_row, merged.min_col - min_col
            for r in range(r0 + 1, merged.max_row - min_row + 1):
                if 0 <= r < len(rows) and 0 <= c0 < len(rows[r]):
                    rows[r][c0] = rows[r0][c0]

        sheet_blocks: list[str] = []
        table: list[list] = []

        def flush() -> None:
            if table:
                sheet_blocks.append(md_table(table))
                table.clear()

        for row in rows:
            filled = [c for c in row if _clean(c)]
            if not filled:
                flush()  # 빈 행은 표를 나눈다
            elif len(filled) == 1 and not table:
                sheet_blocks.append(_clean(filled[0]))  # 표 위의 제목·설명 줄
            else:
                table.append(row)
        flush()
        if sheet_blocks:
            blocks.append(f"## 시트: {sheet.title}")
            blocks.extend(sheet_blocks)
    return _join(blocks)


# ---- PPTX ---------------------------------------------------------------


def _pptx_shape(shape, blocks: list[str], skip_id: int | None) -> None:
    if shape.shape_type == 6:  # 그룹
        for inner in sorted(shape.shapes, key=lambda s: (s.top or 0, s.left or 0)):
            _pptx_shape(inner, blocks, skip_id)
        return
    if getattr(shape, "has_table", False) and shape.has_table:
        grid = [[cell.text for cell in row.cells] for row in shape.table.rows]
        blocks.append(md_table(grid))
    elif getattr(shape, "has_chart", False) and shape.has_chart:
        try:
            chart = shape.chart
            plot = chart.plots[0]
            grid = [[""] + [s.name for s in plot.series]]
            for i, category in enumerate(plot.categories):
                grid.append([category] + [s.values[i] if i < len(s.values) else "" for s in plot.series])
            if chart.has_title:
                blocks.append(chart.chart_title.text_frame.text)
            blocks.append(md_table(grid))
        except Exception:  # 차트 종류에 따라 데이터가 없을 수 있다
            pass
    elif shape.has_text_frame and shape.shape_id != skip_id:
        lines = [p.text.strip() for p in shape.text_frame.paragraphs]
        blocks.append("\n".join(line for line in lines if line))


def _pptx(path: Path) -> str:
    pptx = _require("pptx", "python-pptx")
    presentation = pptx.Presentation(str(path))
    blocks: list[str] = []
    for number, slide in enumerate(presentation.slides, 1):
        title_shape = slide.shapes.title
        title = _clean(title_shape.text) if title_shape is not None and title_shape.has_text_frame else ""
        blocks.append(f"## 슬라이드 {number}" + (f": {title}" if title else ""))
        skip = title_shape.shape_id if title_shape is not None else None
        for shape in sorted(slide.shapes, key=lambda s: (s.top or 0, s.left or 0)):
            _pptx_shape(shape, blocks, skip)
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame
            if notes is not None and notes.text.strip():
                blocks.append("노트: " + _clean(notes.text))
    return _join(blocks)


# ---- PDF ----------------------------------------------------------------

_LINE_END = re.compile(r"[.!?。:：]\s*$")


def _unwrap(text: str) -> str:
    """PDF는 줄마다 끊겨 나온다. 문장 중간에서 끊긴 줄을 다시 잇는다."""
    lines = [line.strip() for line in text.splitlines()]
    longest = max((len(line) for line in lines), default=0)
    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        if not line:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        current.append(line)
        # 문장 부호로 끝나거나 줄이 눈에 띄게 짧으면 문단이 끝난 것으로 본다.
        if _LINE_END.search(line) or len(line) < 0.6 * longest:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs)


def _pdf(path: Path) -> str:
    pdfplumber = _require("pdfplumber", "pdfplumber")
    blocks: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            top = 0.0
            tables = sorted(page.find_tables(), key=lambda t: t.bbox[1])
            for table in tables:
                x0, y0, x1, y1 = table.bbox
                if y0 > top:  # 표 위쪽 본문
                    blocks.append(_unwrap(page.crop((0, top, page.width, y0)).extract_text() or ""))
                blocks.append(md_table(table.extract()))
                top = max(top, y1)
            if top < page.height:
                blocks.append(_unwrap(page.crop((0, top, page.width, page.height)).extract_text() or ""))
    return _join(blocks)


# ---- HWPX ---------------------------------------------------------------

_HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"


def _hwpx_text(t: ET.Element) -> str:
    parts = [t.text or ""]
    for child in t:
        if child.tag in (_HP + "tab", _HP + "lineBreak", _HP + "fwSpace", _HP + "nbSpace"):
            parts.append(" ")
        parts.append(child.tail or "")
    return "".join(parts)


def _hwpx_table(tbl: ET.Element) -> list[list[str]]:
    cells: dict[tuple[int, int], tuple[str, int]] = {}
    for tr in tbl.findall(_HP + "tr"):
        for tc in tr.findall(_HP + "tc"):
            addr, span = tc.find(_HP + "cellAddr"), tc.find(_HP + "cellSpan")
            if addr is None:
                continue
            inner: list[str] = []
            _hwpx_blocks(tc, inner, flat=True)
            key = (int(addr.get("rowAddr", 0)), int(addr.get("colAddr", 0)))
            rowspan = int(span.get("rowSpan", 1)) if span is not None else 1
            cells[key] = (" ".join(b for b in inner if b), rowspan)
    return _grid_from_cells(cells)


def _hwpx_blocks(element: ET.Element, blocks: list[str], flat: bool = False) -> None:
    """문단과 표를 문서 순서대로 모은다. flat이면 표를 한 줄 텍스트로 편다(셀 안의 표)."""
    for child in element:
        if child.tag == _HP + "p":
            text: list[str] = []
            after: list[str] = []
            for run in child.findall(_HP + "run"):
                for item in run:
                    if item.tag == _HP + "t":
                        text.append(_hwpx_text(item))
                    else:
                        _hwpx_blocks(run_wrapper(item), after, flat)
            blocks.append("".join(text).strip())
            blocks.extend(after)
        elif child.tag == _HP + "tbl":
            grid = _hwpx_table(child)
            blocks.append(_flatten(grid) if flat else md_table(grid))
        elif child.tag != _HP + "linesegarray":
            _hwpx_blocks(child, blocks, flat)


def run_wrapper(item: ET.Element) -> ET.Element:
    """run 안의 개체 하나를 _hwpx_blocks가 자식으로 순회할 수 있게 감싼다."""
    wrapper = ET.Element("wrapper")
    wrapper.append(item)
    return wrapper


def _hwpx(path: Path) -> str:
    try:
        archive = zipfile.ZipFile(str(path))
    except zipfile.BadZipFile as exc:
        raise ExtractError("HWPX 파일이 손상되었거나 암호화되어 있습니다.") from exc
    names = [n for n in archive.namelist() if re.fullmatch(r"Contents/section\d+\.xml", n)]
    names.sort(key=lambda n: int(re.search(r"\d+", n).group()))
    blocks: list[str] = []
    for name in names:
        _hwpx_blocks(ET.fromstring(archive.read(name)), blocks)
    return _join(blocks)


# ---- HWP 5.x ------------------------------------------------------------

_PARA_TEXT, _CTRL_HEADER, _LIST_HEADER, _TABLE = 67, 71, 72, 77
_CHAR_CONTROLS = {0, 10, 13, 24, 25, 26, 27, 28, 29, 30, 31}  # 1글자짜리 제어 문자


def _hwp_records(data: bytes):
    pos = 0
    while pos + 4 <= len(data):
        (header,) = struct.unpack_from("<I", data, pos)
        pos += 4
        tag, level, size = header & 0x3FF, (header >> 10) & 0x3FF, header >> 20
        if size == 0xFFF:
            (size,) = struct.unpack_from("<I", data, pos)
            pos += 4
        yield tag, level, data[pos : pos + size]
        pos += size


def _hwp_para_text(body: bytes) -> str:
    out: list[str] = []
    i, n = 0, len(body) // 2
    while i < n:
        (code,) = struct.unpack_from("<H", body, i * 2)
        if code >= 32:
            out.append(chr(code))
            i += 1
        elif code in _CHAR_CONTROLS:
            out.append(" " if code in (10, 30, 31) else "-" if code == 24 else "")
            i += 1
        else:  # 표·그림 등 개체 자리 표시: 8글자 분량을 차지한다
            out.append(" " if code == 9 else "")
            i += 8
    return "".join(out).strip()


def hwp_section_blocks(data: bytes) -> list[str]:
    """BodyText 섹션 하나(압축 해제된 레코드 스트림)를 Markdown 블록 목록으로."""
    blocks: list[str] = []
    stack: list[dict] = []  # 열려 있는 표. 표 안의 표 때문에 스택이다.

    def close() -> None:
        table = stack.pop()
        grid = _grid_from_cells({k: (" ".join(v[0]), v[1]) for k, v in table["cells"].items()})
        caption = " ".join(table["caption"])
        if stack:  # 표 안의 표는 바깥 셀에 한 줄로 넣는다
            emit(" ".join(x for x in (caption, _flatten(grid)) if x))
        else:
            blocks.extend([caption, md_table(grid)])

    def emit(text: str) -> None:
        if not text:
            return
        if not stack:
            blocks.append(text)
        elif stack[-1]["current"] is None:
            stack[-1]["caption"].append(text)
        else:
            stack[-1]["cells"][stack[-1]["current"]][0].append(text)

    for tag, level, body in _hwp_records(data):
        while stack and level <= stack[-1]["level"]:
            close()
        if tag == _CTRL_HEADER and body[:4] == b" lbt":
            stack.append({"level": level, "cells": {}, "caption": [], "current": None, "started": False})
        elif stack and level == stack[-1]["level"] + 1 and tag == _TABLE:
            stack[-1]["started"] = True
        elif stack and level == stack[-1]["level"] + 1 and tag == _LIST_HEADER:
            # 표 정보(TABLE) 레코드보다 앞에 오는 목록은 캡션이다.
            if stack[-1]["started"] and len(body) >= 16:
                col, row, _colspan, rowspan = struct.unpack_from("<4H", body, 8)
                stack[-1]["current"] = (row, col)
                stack[-1]["cells"].setdefault((row, col), ([], rowspan))
        elif tag == _PARA_TEXT:
            emit(_hwp_para_text(body))
    while stack:
        close()
    return blocks


def _hwp(path: Path) -> str:
    olefile = _require("olefile", "olefile")
    if not olefile.isOleFile(str(path)):
        raise ExtractError("HWP 5.x 형식이 아닙니다(HWP 3.x 등은 지원하지 않습니다).")
    ole = olefile.OleFileIO(str(path))
    try:
        (flags,) = struct.unpack_from("<I", ole.openstream("FileHeader").read(), 36)
        if flags & 0x2:
            raise ExtractError("암호가 걸린 HWP 문서입니다.")
        if flags & 0x4:
            raise ExtractError("배포용 HWP 문서는 지원하지 않습니다.")
        sections = sorted(
            (int(entry[1][7:]), entry)
            for entry in ole.listdir()
            if len(entry) == 2 and entry[0] == "BodyText" and entry[1].startswith("Section")
        )
        blocks: list[str] = []
        for _, entry in sections:
            data = ole.openstream(entry).read()
            if flags & 0x1:
                data = zlib.decompress(data, -15)
            blocks.extend(hwp_section_blocks(data))
        return _join(blocks)
    finally:
        ole.close()


# ---- 진입점 -------------------------------------------------------------

_HANDLERS = {
    ".txt": _read_text,
    ".md": _read_text,
    ".csv": _csv,
    ".docx": _docx,
    ".xlsx": _xlsx,
    ".xlsm": _xlsx,
    ".pptx": _pptx,
    ".pdf": _pdf,
    ".hwpx": _hwpx,
    ".hwp": _hwp,
}
_LEGACY = {".doc": "DOCX", ".xls": "XLSX", ".ppt": "PPTX"}

SUPPORTED = tuple(_HANDLERS)


def to_markdown(path: str | Path) -> str:
    """문서를 Markdown 문자열로 변환한다. 읽을 수 없으면 ExtractError."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in _LEGACY:
        raise ExtractError(f"{suffix} 형식은 지원하지 않습니다. {_LEGACY[suffix]}로 저장한 뒤 다시 시도하세요.")
    handler = _HANDLERS.get(suffix)
    if handler is None:
        raise ExtractError(f"지원하지 않는 형식입니다: {suffix or path.name}")
    try:
        return handler(path)
    except ExtractError:
        raise
    except Exception as exc:
        raise ExtractError(f"문서를 읽지 못했습니다: {exc}") from exc
