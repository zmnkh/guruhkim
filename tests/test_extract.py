"""형식별 Markdown 추출과 표 단위 판정."""

import struct
import zipfile

import pytest

from docclass import Classifier, HashingEmbedder, Store
from docclass.chunking import chunk_text, table_units
from docclass.cli import main
from docclass.extract import ExtractError, hwp_section_blocks, md_table, to_markdown

COST_ROWS = [
    ["제품명", "목표 원가", "목표 판매가", "출시 시기"],
    ["오로라 허브", 42000, 129000, "2027년 3월"],
    ["오로라 센서", 8500, 24900, "2027년 6월"],
]
COST_TABLE_MD = (
    "| 제품명 | 목표 원가 | 목표 판매가 | 출시 시기 |\n"
    "| --- | --- | --- | --- |\n"
    "| 오로라 허브 | 42000 | 129000 | 2027년 3월 |\n"
    "| 오로라 센서 | 8500 | 24900 | 2027년 6월 |"
)


# ---- 표 -> 임베딩 단위 ---------------------------------------------------


def test_table_rows_become_self_describing_units():
    assert chunk_text("## 원가표\n\n" + COST_TABLE_MD + "\n") == [
        "제품명: 오로라 허브; 목표 원가: 42000; 목표 판매가: 129000; 출시 시기: 2027년 3월",
        "제품명: 오로라 센서; 목표 원가: 8500; 목표 판매가: 24900; 출시 시기: 2027년 6월",
    ]


def test_two_column_table_is_key_value():
    assert table_units([["담당 부서", "전략기획팀"], ["총 마케팅 예산", "180억 원"]]) == [
        "담당 부서: 전략기획팀",
        "총 마케팅 예산: 180억 원",
    ]


def test_wide_row_is_split_with_row_key_repeated():
    header = ["품목"] + [f"{m}월 판매량" for m in range(1, 13)]
    row = ["오로라 허브"] + [str(10000 + m) for m in range(1, 13)]
    units = table_units([header, row], max_chars=80)
    assert len(units) > 1
    assert all(len(u) <= 80 and u.startswith("품목: 오로라 허브") for u in units)
    assert "12월 판매량: 10012" in units[-1]


def test_md_table_drops_empty_rows_and_columns_and_escapes_pipes():
    table = md_table([["a", None, "b|c"], [None, None, None], [1.0, None, 2.5]])
    assert table == "| a | b\\|c |\n| --- | --- |\n| 1 | 2.5 |"
    assert chunk_text("| 구분 | 설명 |\n| --- | --- |\n| 선택 기준 | 가격\\|품질 |") == [
        "선택 기준: 가격|품질",  # "구분: 설명"은 너무 짧아 버린다
    ]


# ---- 형식별 추출 ---------------------------------------------------------


def test_xlsx(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "원가표"
    ws.append(["2027년 신제품 원가 계획"])
    ws.append([])
    for row in COST_ROWS:
        ws.append(row)
    ws2 = wb.create_sheet("분류")
    ws2.append(["구분", "품목", "수량"])
    ws2.append(["허브", "오로라 A", 10])
    ws2.append([None, "오로라 B", 20])
    ws2.merge_cells("A2:A3")
    wb.create_sheet("빈 시트")
    path = tmp_path / "plan.xlsx"
    wb.save(path)

    md = to_markdown(path)
    assert "## 시트: 원가표\n\n2027년 신제품 원가 계획\n\n" + COST_TABLE_MD in md
    assert "| 허브 | 오로라 B | 20 |" in md  # 세로 병합 셀은 아래 행에도 채운다
    assert "빈 시트" not in md


def test_docx(tmp_path):
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_heading("신제품 전략", level=1)
    document.add_paragraph("아래 표는 제품별 목표 원가와 판매가를 정리한 것입니다.")
    table = document.add_table(rows=3, cols=4)
    for r, row in enumerate(COST_ROWS):
        for c, value in enumerate(row):
            table.cell(r, c).text = str(value)
    document.add_paragraph("표 아래의 마무리 문단입니다.")
    path = tmp_path / "plan.docx"
    document.save(path)

    md = to_markdown(path)
    assert md.startswith("# 신제품 전략\n\n아래 표는")
    assert COST_TABLE_MD in md
    assert md.index(COST_TABLE_MD) < md.index("표 아래의 마무리 문단입니다.")


def test_pptx(tmp_path):
    pptx = pytest.importorskip("pptx")
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "원가 계획"
    box = slide.shapes.add_textbox(Inches(0.5), Inches(1.5), Inches(8), Inches(0.5))
    box.text_frame.text = "핵심 부품은 대만 협력사와 독점 공급 계약을 체결했습니다."
    shape = slide.shapes.add_table(3, 4, Inches(0.5), Inches(2.2), Inches(8), Inches(1.5))
    for r, row in enumerate(COST_ROWS):
        for c, value in enumerate(row):
            shape.table.cell(r, c).text = str(value)
    data = CategoryChartData()
    data.categories = ["1분기", "2분기"]
    data.add_series("판매 목표", (80000, 120000))
    slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(0.5), Inches(4), Inches(6), Inches(3), data)
    slide.notes_slide.notes_text_frame.text = "발표 때 원가는 언급하지 않는다."
    path = tmp_path / "plan.pptx"
    prs.save(path)

    md = to_markdown(path)
    assert md.startswith("## 슬라이드 1: 원가 계획\n\n핵심 부품은")
    assert md.count("원가 계획") == 1  # 제목을 본문에 다시 쓰지 않는다
    assert COST_TABLE_MD in md
    assert "| 1분기 | 80000 |" in md and "| 2분기 | 120000 |" in md
    assert "노트: 발표 때 원가는 언급하지 않는다." in md


def test_pdf(tmp_path):
    pytest.importorskip("pdfplumber")
    pytest.importorskip("reportlab")
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    pdfmetrics.registerFont(UnicodeCIDFont("HYGothic-Medium"))
    style = ParagraphStyle("ko", fontName="HYGothic-Medium", fontSize=11, leading=16)
    long_sentence = (
        "핵심 부품은 대만 협력사와 독점 공급 계약을 체결하여 2027년 3월부터 양산에 들어가며, "
        "상반기 안에 유통 채널 세 곳과 단독 입점 협상을 마무리할 계획입니다."
    )
    table = Table([[str(c) for c in row] for row in COST_ROWS])
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "HYGothic-Medium"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
    ]))
    path = tmp_path / "plan.pdf"
    SimpleDocTemplate(str(path), pagesize=A4).build(
        [Paragraph(long_sentence, style), Spacer(1, 12), table, Spacer(1, 12),
         Paragraph("표 아래의 마무리 문단입니다.", style)]
    )

    md = to_markdown(path)
    assert COST_TABLE_MD in md
    # 줄바꿈으로 끊긴 문장이 한 줄로 이어져야 한다
    assert any("독점 공급 계약" in line and "마무리할 계획입니다." in line for line in md.splitlines())
    assert md.index("독점 공급 계약") < md.index(COST_TABLE_MD) < md.index("표 아래의 마무리 문단입니다.")
    assert md.count("오로라 허브") == 1  # 표 내용이 본문에 중복되지 않는다


HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"
HS = "http://www.hancom.co.kr/hwpml/2011/section"


def _hwpx_p(text: str, extra: str = "") -> str:
    return f'<hp:p><hp:run><hp:t>{text}</hp:t>{extra}</hp:run><hp:linesegarray/></hp:p>'


def _hwpx_tc(row: int, col: int, text: str, rowspan: int = 1) -> str:
    return (
        f"<hp:tc><hp:subList>{_hwpx_p(text)}</hp:subList>"
        f'<hp:cellAddr colAddr="{col}" rowAddr="{row}"/><hp:cellSpan colSpan="1" rowSpan="{rowspan}"/></hp:tc>'
    )


def test_hwpx(tmp_path):
    table = (
        '<hp:tbl rowCnt="3" colCnt="3"><hp:tr>'
        + _hwpx_tc(0, 0, "구분") + _hwpx_tc(0, 1, "품목") + _hwpx_tc(0, 2, "목표 원가")
        + "</hp:tr><hp:tr>"
        + _hwpx_tc(1, 0, "허브", rowspan=2) + _hwpx_tc(1, 1, "오로라 A") + _hwpx_tc(1, 2, "42000")
        + "</hp:tr><hp:tr>"
        + _hwpx_tc(2, 1, "오로라 B") + _hwpx_tc(2, 2, "45000")
        + "</hp:tr></hp:tbl>"
    )
    section = (
        f'<?xml version="1.0" encoding="UTF-8"?><hs:sec xmlns:hs="{HS}" xmlns:hp="{HP}">'
        + _hwpx_p("표 위의 본문 문단입니다.")
        + _hwpx_p("", extra=table)
        + _hwpx_p("탭<hp:tab/>뒤의 글")
        + "</hs:sec>"
    )
    path = tmp_path / "plan.hwpx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/hwp+zip")
        z.writestr("Contents/section0.xml", section)

    assert to_markdown(path) == (
        "표 위의 본문 문단입니다.\n\n"
        "| 구분 | 품목 | 목표 원가 |\n| --- | --- | --- |\n"
        "| 허브 | 오로라 A | 42000 |\n| 허브 | 오로라 B | 45000 |\n\n"
        "탭 뒤의 글\n"
    )


def _record(tag: int, level: int, body: bytes) -> bytes:
    return struct.pack("<I", tag | (level << 10) | (len(body) << 20)) + body


def _para(level: int, text: str, control: bool = False) -> bytes:
    body = text.encode("utf-16le")
    if control:  # 표 개체 자리 표시: 제어 문자 11이 8글자 분량을 차지한다
        body += struct.pack("<8H", 11, 0, 0, 0, 0, 0, 0, 11)
    body += "\r".encode("utf-16le")
    return _record(66, level, b"\0" * 24) + _record(67, level + 1, body)


def _cell(level: int, row: int, col: int, text: str, rowspan: int = 1) -> bytes:
    header = struct.pack("<II4H", 1, 0, col, row, 1, rowspan) + b"\0" * 22
    return _record(72, level, header) + _para(level, text)


def test_hwp_records():
    data = (
        _para(0, "표 위의 본문 문단입니다.")
        + _para(0, "", control=True)
        + _record(71, 1, b" lbt" + b"\0" * 42)
        + _record(72, 2, b"\0" * 30) + _para(2, "표 1 원가 계획")  # 캡션은 표 정보보다 앞에 온다
        + _record(77, 2, struct.pack("<IHH", 0, 3, 2) + b"\0" * 16)
        + _cell(2, 0, 0, "구분") + _cell(2, 0, 1, "목표 원가")
        + _cell(2, 1, 0, "허브", rowspan=2) + _cell(2, 1, 1, "42000")
        + _cell(2, 2, 1, "45000")
        + _para(0, "표 아래 문단입니다.")
    )
    assert hwp_section_blocks(data) == [
        "표 위의 본문 문단입니다.",
        "표 1 원가 계획",
        "| 구분 | 목표 원가 |\n| --- | --- |\n| 허브 | 42000 |\n| 허브 | 45000 |",
        "표 아래 문단입니다.",
    ]


def test_csv_and_cp949_text(tmp_path):
    (tmp_path / "a.csv").write_text("품목,원가\n오로라,42000\n", encoding="utf-8-sig")
    assert to_markdown(tmp_path / "a.csv") == "| 품목 | 원가 |\n| --- | --- |\n| 오로라 | 42000 |\n"
    (tmp_path / "b.txt").write_bytes("한글 완성형 문서입니다.".encode("cp949"))
    assert to_markdown(tmp_path / "b.txt") == "한글 완성형 문서입니다."


def test_unsupported_formats(tmp_path):
    for name, message in [("a.xls", "XLSX로 저장"), ("a.zip", "지원하지 않는 형식")]:
        (tmp_path / name).write_bytes(b"x")
        with pytest.raises(ExtractError, match=message):
            to_markdown(tmp_path / name)
    (tmp_path / "broken.xlsx").write_bytes(b"not a zip")
    with pytest.raises(ExtractError, match="읽지 못했습니다"):
        to_markdown(tmp_path / "broken.xlsx")


# ---- 표 데이터로 중요/일반 판정 ------------------------------------------


def test_table_row_classification_end_to_end(tmp_path, capsys):
    openpyxl = pytest.importorskip("openpyxl")

    def save(name, rows):
        wb = openpyxl.Workbook()
        for row in rows:
            wb.active.append(row)
        wb.save(tmp_path / name)
        return str(tmp_path / name)

    public_row = ["스마트홈 시장 전체", 80000, 95000, "공개 통계 기준"]
    important = save("원가계획.xlsx", COST_ROWS + [public_row])
    general = save("공개자료.xlsx", [COST_ROWS[0], public_row])
    # 열 순서가 달라도 행 단위로는 같은 내용이다. 여기서는 같은 구조로 옮겨 적은 경우를 본다.
    leak = save("유출.xlsx", [COST_ROWS[0], COST_ROWS[1]])
    harmless = save("인용.xlsx", [COST_ROWS[0], public_row])

    base = ["--store", str(tmp_path / "s")]
    main(base + ["--embedder", "hashing", "register", "--label", "important", important])
    main(base + ["register", "--label", "general", general])
    capsys.readouterr()
    main(base + ["classify", leak, harmless])
    out = capsys.readouterr().out
    assert "유출.xlsx: 중요 문서" in out and "정확히 일치" in out
    assert "인용.xlsx: 일반 문서" in out

    store = Store(tmp_path / "s", HashingEmbedder())
    # 같은 값이 문장으로 풀려 있어도 의미 유사도로 잡는다
    result = Classifier(store).classify("제품명 오로라 허브, 목표 원가 42000, 목표 판매가 129000, 출시 시기 2027년 3월")
    assert result.verdict == "important" and result.chunks[0].reason == "semantic"


def test_cli_convert_and_skip_unreadable(tmp_path, capsys):
    (tmp_path / "a.csv").write_text("품목,원가\n오로라,42000\n", encoding="utf-8")
    (tmp_path / "b.xls").write_bytes(b"x")
    with pytest.raises(SystemExit) as exit_info:
        main(["convert", "-o", str(tmp_path / "out"), str(tmp_path / "a.csv"), str(tmp_path / "b.xls")])
    assert exit_info.value.code == 1
    assert (tmp_path / "out" / "a.csv.md").read_text(encoding="utf-8").startswith("| 품목 | 원가 |")
    assert "b.xls: 건너뜀" in capsys.readouterr().err
