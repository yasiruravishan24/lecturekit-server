"""The notes and the quiz as a proper PDF (title, headings, summaries, key points, quiz with answers)."""
import io
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import HRFlowable, KeepTogether, ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

BLUE = colors.HexColor("#1976d2")
TINT = colors.HexColor("#eaf2fb")
GREY = colors.HexColor("#5f6b7a")
INK = colors.HexColor("#1f2933")


def _fonts() -> tuple[str, str]:
    """DejaVu Sans (installed in the Docker image) shows formulas and non-English letters; the built-in Helvetica only Latin-1."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    d = Path("/usr/share/fonts/truetype/dejavu")
    try:
        pdfmetrics.registerFont(TTFont("LK", str(d / "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("LK-Bold", str(d / "DejaVuSans-Bold.ttf")))
        return "LK", "LK-Bold"
    except Exception:
        return "Helvetica", "Helvetica-Bold"


def _styles() -> dict:
    base = getSampleStyleSheet()["BodyText"]
    regular, bold = _fonts()
    body = ParagraphStyle("body", parent=base, fontName=regular, fontSize=10.5, leading=15.5, textColor=INK, alignment=TA_LEFT)
    return {
        "title": ParagraphStyle("title", parent=body, fontName=bold, fontSize=24, leading=29, spaceAfter=4),
        "meta": ParagraphStyle("meta", parent=body, fontSize=9.5, textColor=GREY, spaceAfter=10),
        "h1": ParagraphStyle("h1", parent=body, fontName=bold, fontSize=16, leading=20, textColor=BLUE, spaceBefore=14, spaceAfter=6),
        "h2": ParagraphStyle("h2", parent=body, fontName=bold, fontSize=13, leading=17, spaceBefore=14, spaceAfter=9),
        "label": ParagraphStyle("label", parent=body, fontName=bold, fontSize=8.5, leading=11, textColor=BLUE, spaceBefore=6, spaceAfter=2),
        "body": body,
        "summary": ParagraphStyle("summary", parent=body, backColor=TINT, borderPadding=(7, 9, 7, 9), spaceBefore=4, spaceAfter=10),
        "q": ParagraphStyle("q", parent=body, fontName=bold, spaceBefore=10, spaceAfter=3),
        "opt": ParagraphStyle("opt", parent=body, leftIndent=14),
        "ans": ParagraphStyle("ans", parent=body, fontSize=9.5, textColor=GREY, leftIndent=14, spaceBefore=2),
    }


def _p(text, style) -> Paragraph:
    return Paragraph(escape(str(text or "")).replace("\n", "<br/>"), style)


def _footer(title: str):
    def draw(canvas, doc):
        canvas.saveState()
        canvas.setFont(_fonts()[0], 8.5)
        canvas.setFillColor(GREY)
        canvas.drawString(20 * mm, 12 * mm, title[:80])
        canvas.drawRightString(A4[0] - 20 * mm, 12 * mm, f"Page {doc.page}")
        canvas.setStrokeColor(colors.HexColor("#d5dbe3"))
        canvas.line(20 * mm, 16 * mm, A4[0] - 20 * mm, 16 * mm)
        canvas.restoreState()
    return draw


def notes_pdf(title: str, meta: str, full_notes: list[dict], short_notes: list[str], questions: list[dict]) -> bytes:
    """`questions`: [{"question", "options", "answer_index", "explanation"}]. Returns the PDF file as bytes."""
    s = _styles()
    story = [_p(title, s["title"]), _p(meta, s["meta"]), HRFlowable(width="100%", thickness=1.2, color=BLUE, spaceAfter=4)]

    story.append(_p("Notes", s["h1"]))
    for i, t in enumerate(full_notes, 1):
        block = [_p(f"{i}. {t.get('heading', '')}", s["h2"]), _p(t.get("summary", ""), s["summary"])]
        points = [k for k in t.get("key_points", []) if k]
        if points:
            block.append(_p("KEY POINTS", s["label"]))
            block.append(ListFlowable([ListItem(_p(k, s["body"]), leftIndent=14) for k in points], bulletType="bullet", start="•",
                                      bulletColor=BLUE, leftIndent=14, bulletFontSize=10))
        story.append(KeepTogether(block[:2]))
        story.extend(block[2:])

    if short_notes:
        story.append(_p("Short notes", s["h1"]))
        story.append(ListFlowable([ListItem(_p(x, s["body"]), leftIndent=14) for x in short_notes], bulletType="bullet", start="•",
                                  bulletColor=BLUE, leftIndent=14))

    if questions:
        story.append(_p("Quiz", s["h1"]))
        for n, q in enumerate(questions, 1):
            block = [_p(f"{n}. {q['question']}", s["q"])]
            block += [_p(f"{'ABCD'[j]}.  {o}", s["opt"]) for j, o in enumerate(q["options"])]
            story.append(KeepTogether(block))
        story.append(_p("Answers", s["h1"]))
        for n, q in enumerate(questions, 1):
            right = 'ABCD'[q["answer_index"]]
            story.append(KeepTogether([_p(f"<b>{n}. {right}</b> - {escape(str(q['options'][q['answer_index']]))}", s["body"]),
                                       _p(q.get("explanation", ""), s["ans"])]))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=22 * mm,
                            title=title, author="Lecture Assistant")
    foot = _footer(title)
    doc.build(story, onFirstPage=foot, onLaterPages=foot)
    return buf.getvalue()
