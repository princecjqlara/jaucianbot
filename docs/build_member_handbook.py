"""Build the shareable member PDF from its editable Markdown source.

Requires reportlab. Run from the repository root with a document-tools Python
environment; this dependency is intentionally separate from the bot runtime.
"""
from pathlib import Path
import html
import re

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.pagesizes import letter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle, Preformatted, KeepTogether

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "member-handbook.md"
OUTPUT = ROOT / "Veo-and-Suno-Member-Handbook.pdf"


def register_fonts():
    directory = Path("C:/Windows/Fonts")
    for name, file in (("Handbook", "calibri.ttf"), ("Handbook-Bold", "calibrib.ttf"),
                       ("Handbook-Italic", "calibrii.ttf"), ("Handbook-Code", "consola.ttf")):
        pdfmetrics.registerFont(TTFont(name, str(directory / file)))
    pdfmetrics.registerFontFamily("Handbook", normal="Handbook", bold="Handbook-Bold", italic="Handbook-Italic", boldItalic="Handbook-Bold")


def inline(value):
    value = html.escape(value)
    value = re.sub(r"\[([^\]]+)\]\((https://[^)]+)\)", r'<a href="\2" color="#2E74B5"><u>\1</u></a>', value)
    value = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", value)
    value = re.sub(r"`([^`]+)`", r'<font name="Handbook-Code" size="9">\1</font>', value)
    return value


def create_styles():
    base = dict(fontName="Handbook", fontSize=11, leading=13.75, textColor=colors.HexColor("#182432"),
                alignment=TA_LEFT, spaceBefore=0, spaceAfter=6)
    styles = {"body": ParagraphStyle("Body", **base)}
    styles["title"] = ParagraphStyle("Title", parent=styles["body"], fontName="Handbook-Bold", fontSize=26, leading=30,
        alignment=TA_CENTER, textColor=colors.HexColor("#0B2545"), spaceBefore=0, spaceAfter=6)
    styles["subtitle"] = ParagraphStyle("Subtitle", parent=styles["body"], fontSize=14, leading=18,
        alignment=TA_CENTER, textColor=colors.HexColor("#2E74B5"), spaceAfter=10)
    styles["h1"] = ParagraphStyle("Heading1", parent=styles["body"], fontName="Handbook-Bold", fontSize=16, leading=20,
        textColor=colors.HexColor("#2E74B5"), spaceBefore=0, spaceAfter=10, keepWithNext=True)
    styles["h2"] = ParagraphStyle("Heading2", parent=styles["body"], fontName="Handbook-Bold", fontSize=13, leading=16,
        textColor=colors.HexColor("#2E74B5"), spaceBefore=14, spaceAfter=7, keepWithNext=True)
    styles["bullet"] = ParagraphStyle("Bullet", parent=styles["body"], leftIndent=27, firstLineIndent=0,
        bulletIndent=13.5, spaceAfter=4)
    styles["table"] = ParagraphStyle("Table", parent=styles["body"], fontSize=9.5, leading=11.8, spaceAfter=0)
    styles["tablehead"] = ParagraphStyle("TableHeading", parent=styles["table"], fontName="Handbook-Bold", textColor=colors.HexColor("#0B2545"))
    styles["code"] = ParagraphStyle("Code", parent=styles["body"], fontName="Handbook-Code", fontSize=9.5, leading=12,
        backColor=colors.HexColor("#F4F6F9"), borderPadding=8, spaceBefore=4, spaceAfter=12, leftIndent=8, rightIndent=8)
    return styles


def table(rows, styles):
    count = len(rows[0])
    widths = ([86] + [382 / 6] * 6) if count == 7 else ([125, 343] if count == 2 else [468 / count] * count)
    cells = [[Paragraph(inline(value.strip()), styles["tablehead"] if i == 0 else styles["table"]) for value in row] for i, row in enumerate(rows)]
    obj = Table(cells, colWidths=widths, repeatRows=1, hAlign="LEFT")
    obj.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF5")),
        ("GRID", (0, 0), (-1, -1), .4, colors.HexColor("#CCD4DD")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return obj


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Handbook", 8)
    canvas.setFillColor(colors.HexColor("#657281"))
    canvas.drawString(72, 36, "Veo + Suno / Trabawho | Member handbook | 14 Oct 2026")
    canvas.drawRightString(540, 36, str(doc.page))
    canvas.restoreState()


def build():
    register_fonts()
    styles = create_styles()
    lines = SOURCE.read_text(encoding="utf-8").splitlines()
    story = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line == "<!-- page -->":
            story.append(PageBreak())
            i += 1
            continue
        if line.startswith("```"):
            block = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                block.append(lines[i])
                i += 1
            story.append(Preformatted("\n".join(block), styles["code"]))
            i += 1
            continue
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                row = lines[i].strip("|").split("|")
                if not all(re.fullmatch(r"\s*[-:]+\s*", value) for value in row):
                    rows.append(row)
                i += 1
            story.extend([table(rows, styles), Spacer(1, 8)])
            continue
        if line.startswith("# "):
            story.append(Paragraph(inline(line[2:]), styles["title"] if not story else styles["h1"]))
        elif line.startswith("## "):
            story.append(Paragraph(inline(line[3:]), styles["subtitle"]))
        elif line.startswith("### "):
            story.append(Paragraph(inline(line[4:]), styles["h2"]))
        elif line.startswith("- "):
            story.append(Paragraph(inline(line[2:]), styles["bullet"], bulletText="•"))
        else:
            story.append(Paragraph(inline(line), styles["body"]))
        i += 1
    doc = SimpleDocTemplate(str(OUTPUT), pagesize=letter, rightMargin=72, leftMargin=72, topMargin=54, bottomMargin=54,
        title="Veo and Suno Member Handbook", author="Team Operations", subject="Simple member instructions for client ownership and team workflows")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    print(OUTPUT)


if __name__ == "__main__":
    build()
