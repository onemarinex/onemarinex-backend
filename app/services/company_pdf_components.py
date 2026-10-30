"""Reusable, vector PDF layout. No queries, remote resources or business rules."""
from __future__ import annotations

import base64
import io
import math
from datetime import datetime, timezone, timedelta
from html import escape
from pathlib import Path
import reportlab
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import BaseDocTemplate, Flowable, Frame, LongTable, NextPageTemplate, PageTemplate, Paragraph, TableStyle

NAVY, TEAL = colors.HexColor("#142D43"), colors.HexColor("#087F83")
INK, MUTED = colors.HexColor("#223746"), colors.HexColor("#607180")
RULE, WASH = colors.HexColor("#D9E4E9"), colors.HexColor("#F1F6F7")
PAGE_W, PAGE_H = A4
MARGIN = 39
WIDTH = PAGE_W - 2 * MARGIN
FONT, BOLD = "Helvetica", "Helvetica-Bold"
_font_dir = Path(reportlab.__file__).parent / "fonts"
if (_font_dir / "Vera.ttf").exists() and (_font_dir / "VeraBd.ttf").exists():
    FONT, BOLD = "CompanyReport", "CompanyReport-Bold"
    if FONT not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(FONT, str(_font_dir / "Vera.ttf")))
        pdfmetrics.registerFont(TTFont(BOLD, str(_font_dir / "VeraBd.ttf")))
        pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=BOLD, italic=FONT, boldItalic=BOLD)
STYLES = {
    "title": ParagraphStyle("ReportTitle", fontName=BOLD, fontSize=18, leading=23, textColor=NAVY, spaceAfter=6),
    "subtitle": ParagraphStyle("ReportSubtitle", fontName=FONT, fontSize=8, leading=12, textColor=MUTED, spaceAfter=12),
    "heading": ParagraphStyle("ReportHeading", fontName=BOLD, fontSize=11, leading=15, textColor=NAVY, spaceBefore=15, spaceAfter=8, keepWithNext=True),
    "body": ParagraphStyle("ReportBody", fontName=FONT, fontSize=8.4, leading=13, textColor=INK, spaceAfter=7, splitLongWords=True),
    "small": ParagraphStyle("ReportSmall", fontName=FONT, fontSize=7, leading=10.5, textColor=MUTED, spaceAfter=5, splitLongWords=True),
    "cell": ParagraphStyle("ReportCell", fontName=FONT, fontSize=7.4, leading=11, textColor=INK, splitLongWords=True),
    "table_head": ParagraphStyle("ReportTableHead", fontName=BOLD, fontSize=7, leading=10, textColor=NAVY, splitLongWords=True),
}


def text(value):
    if value is None:
        return "Unavailable"
    return str(value).replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-").replace("\x00", "")


def p(value, style="body"):
    return Paragraph(escape(text(value)).replace("\n", "<br/>"), STYLES[style])


class SectionHeading(Paragraph):
    """Keep a heading with the first row/card, not the whole long table.

    Paragraph.keepWithNext groups an entire LongTable and can move that table
    off an otherwise usable first page. Reserve only its first two rows here.
    """
    def __init__(self, value, following=40):
        style = ParagraphStyle("SectionHeading", parent=STYLES["heading"], keepWithNext=False)
        super().__init__(escape(text(value)), style)
        self.following = following
        self.needs_frame = False

    def wrap(self, width, height):
        w, h = super().wrap(width, height)
        self.needs_frame = height < h + self.following
        return w, height + 1 if self.needs_frame else h

    def split(self, width, height):
        return [] if self.needs_frame else super().split(width, height)


def heading(value, first=None):
    following = 40
    if isinstance(first, LongTable):
        first.wrap(WIDTH, PAGE_H - 168)
        following = sum(first._rowHeights[:2]) + 5
    elif isinstance(first, Flowable) and getattr(first, "height", None):
        following = min(first.height + 5, PAGE_H - 210)
    elif hasattr(first, "_content") and first._content:
        following = min(getattr(first._content[0], "height", 40) + 5, PAGE_H - 210)
    return SectionHeading(value, following)


def number(value, suffix="", digits=0):
    if isinstance(value, dict):
        if value.get("suppressed"):
            return "Withheld"
        value = value.get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return "Unavailable"
    separator = " " if suffix and suffix[0].isalnum() else ""
    return f"{value:,.{digits}f}{separator}{suffix}"


def stamp(value, date_only=False):
    if not value:
        return "Not recorded"
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return "Not recorded"
        return dt.astimezone(timezone.utc).strftime("%d %b %Y" if date_only else "%d %b %Y %H:%M UTC")
    except (TypeError, ValueError):
        return "Not recorded"


def period(payload):
    window = payload.get("period") or {}
    if not window:
        return "Period not provided"
    end = window.get("end")
    try:
        end = end if isinstance(end, datetime) else datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        end -= timedelta(microseconds=1)
    except (TypeError, ValueError):
        pass
    return f"{stamp(window.get('start'), True)} - {stamp(end, True)}"


def safe_logo(value):
    if not isinstance(value, str) or not value.startswith(("data:image/png;base64,", "data:image/jpeg;base64,")) or len(value) > 7_000_000:
        return None
    try:
        from PIL import Image
        raw = base64.b64decode(value.split(",", 1)[1], validate=True)
        image = Image.open(io.BytesIO(raw))
        if image.format not in ("PNG", "JPEG") or image.width * image.height > 16_000_000:
            return None
        image.verify()
        return ImageReader(io.BytesIO(raw))
    except Exception:
        return None


def draw_text(canvas, value, x, y, width, size=8, bold=False, color=MUTED, max_lines=2):
    font = BOLD if bold else FONT
    lines = []
    for candidate in simpleSplit(text(value), font, size, width):
        # simpleSplit wraps at spaces. A long unbroken legal name/IMO label
        # must also stay inside the fixed header and chart-label bounds.
        while candidate and pdfmetrics.stringWidth(candidate, font, size) > width:
            lo, hi = 1, len(candidate)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if pdfmetrics.stringWidth(candidate[:mid], font, size) <= width:
                    lo = mid
                else:
                    hi = mid - 1
            lines.append(candidate[:lo])
            candidate = candidate[lo:]
        if candidate:
            lines.append(candidate)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and pdfmetrics.stringWidth(lines[-1] + "...", font, size) > width:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "..."
    canvas.setFont(font, size)
    canvas.setFillColor(color)
    for line in lines:
        canvas.drawString(x, y, line)
        y -= size * 1.35


class ReportCanvas(Canvas):
    def __init__(self, *args, report_identity, **kwargs):
        super().__init__(*args, **kwargs)
        self.report_identity, self.report_pages = report_identity, []

    def showPage(self):
        self.report_pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self.report_pages)
        for state in self.report_pages:
            self.__dict__.update(state)
            self.footer(total)
            super().showPage()
        super().save()

    def footer(self, total):
        self.saveState()
        self.setStrokeColor(RULE)
        self.setLineWidth(.6)
        self.line(MARGIN, 48, PAGE_W - MARGIN, 48)
        if self.report_identity.get("fictional"):
            draw_text(self, "Illustrative Demo Data - Not Actual Fleet Records", MARGIN, 55, WIDTH, size=7, bold=True, color=NAVY)
        suffix = " (inclusive dates, UTC)" if self.report_identity.get("period_has_end", True) else ""
        draw_text(self, self.report_identity["period"] + suffix, MARGIN, 35, WIDTH * .7, size=6.2, max_lines=1)
        self.setFont(FONT, 6.6)
        self.setFillColor(MUTED)
        self.drawRightString(PAGE_W - MARGIN, 35, f"Page {self._pageNumber} of {total}")
        draw_text(self, "Generated " + self.report_identity["generated"], MARGIN, 23, WIDTH * .65, size=6.2, max_lines=1)
        self.drawRightString(PAGE_W - MARGIN, 23, "Powered by HeyPorts")
        self.restoreState()


class KPIGrid(Flowable):
    def __init__(self, items, columns=4):
        super().__init__()
        self.items, self.columns, self.width = items, columns, WIDTH
        self.height = math.ceil(len(items) / columns) * 65

    def wrap(self, *_):
        return self.width, self.height

    def draw(self):
        c, cell_w = self.canv, self.width / self.columns
        for i, (label, value, note) in enumerate(self.items):
            x, y = i % self.columns * cell_w, self.height - (i // self.columns + 1) * 65
            c.setFillColor(WASH)
            c.roundRect(x + 3, y + 5, cell_w - 6, 55, 4, fill=1, stroke=0)
            draw_text(c, value, x + 12, y + 39, cell_w - 22, size=18 if len(str(value)) < 11 else 10.5, bold=True, color=NAVY, max_lines=1)
            draw_text(c, label, x + 12, y + 24, cell_w - 22, size=7, color=INK, max_lines=1)
            draw_text(c, note, x + 12, y + 12, cell_w - 22, size=5.6, max_lines=1)


class BarChart(Flowable):
    """Missing and withheld values never become zero bars."""
    def __init__(self, items, title, unit="", description=""):
        super().__init__()
        self.items, self.title, self.unit = items[:8], title, unit
        self.description = description
        if len(items) > 8:
            self.description = f"Showing 8 of {len(items)} categories. " + description
        self.width, self.height = WIDTH, 49 + max(1, len(self.items)) * 25 + (17 if self.description else 0)

    def wrap(self, *_):
        return self.width, self.height

    def draw(self):
        c = self.canv
        draw_text(c, self.title, 0, self.height - 10, WIDTH, size=9, bold=True, color=NAVY, max_lines=1)
        valid = [i["value"] for i in self.items if isinstance(i.get("value"), (int, float)) and not isinstance(i.get("value"), bool) and math.isfinite(i["value"]) and i["value"] >= 0 and not i.get("suppressed")]
        maximum = 100 if self.unit == "%" else max(valid or [1]) or 1
        start, plot_w = 158, WIDTH - 220
        for index, item in enumerate(self.items):
            y = self.height - 39 - index * 25
            draw_text(c, item.get("label", "Not recorded"), 0, y + 3, 148, size=7.4, color=INK, max_lines=2)
            value = item.get("value")
            if item.get("suppressed"):
                display = "Withheld"
            elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                c.setFillColor(WASH)
                c.roundRect(start, y, plot_w, 12, 2, fill=1, stroke=0)
                c.setFillColor(TEAL)
                if value:
                    c.roundRect(start, y, min(value / maximum, 1) * plot_w, 12, 2, fill=1, stroke=0)
                display = number(value, self.unit, 1 if self.unit in ("%", "/5") else 0)
            else:
                display = "Unavailable"
            draw_text(c, display, start + plot_w + 9, y + 3, 61, size=6.7, color=INK, max_lines=1)
        if self.description:
            draw_text(c, self.description, 0, 8, WIDTH, size=6.5, max_lines=2)


class LineChart(Flowable):
    def __init__(self, items, title, unit="", description=""):
        super().__init__()
        self.items, self.title, self.unit, self.description = items[:12], title, unit, description
        self.width, self.height = WIDTH, 190

    def wrap(self, *_):
        return self.width, self.height

    def draw(self):
        c = self.canv
        draw_text(c, self.title, 0, 180, WIDTH, size=9, bold=True, color=NAVY, max_lines=1)
        valid = [i["value"] for i in self.items if isinstance(i.get("value"), (int, float)) and not isinstance(i.get("value"), bool) and math.isfinite(i["value"]) and i["value"] >= 0 and not i.get("suppressed")]
        maximum = 100 if self.unit == "%" else max(valid or [1]) or 1
        left, bottom, plot_w, plot_h = 36, 50, WIDTH - 52, 108
        c.setStrokeColor(RULE)
        c.setLineWidth(.4)
        for tick in range(3):
            y = bottom + tick * plot_h / 2
            c.line(left, y, left + plot_w, y)
            draw_text(c, number(maximum * tick / 2, self.unit), 0, y - 2, 33, size=6, max_lines=1)
        previous = None
        for index, item in enumerate(self.items):
            x = left + index * plot_w / max(len(self.items) - 1, 1)
            if len(self.items) <= 6 or index % 2 == 0 or index == len(self.items) - 1:
                draw_text(c, item.get("label", ""), min(max(left, x - 17), WIDTH - 49), 34, 49, size=6, max_lines=1)
            value = item.get("value")
            if item.get("suppressed") or not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
                previous = None
                continue
            y = bottom + min(value / maximum, 1) * plot_h
            c.setStrokeColor(TEAL)
            c.setLineWidth(1.8)
            if previous:
                c.line(previous[0], previous[1], x, y)
            c.setFillColor(TEAL)
            c.circle(x, y, 2.6, fill=1, stroke=0)
            previous = x, y
        draw_text(c, self.description or "Gaps indicate unavailable or withheld values.", 0, 12, WIDTH, size=6.4, max_lines=2)


def table(headers, rows, widths=None, compact=False):
    if not rows:
        return [p("No authorized records are available for this section.", "small")]
    data = [[p(h, "table_head") for h in headers]]
    data += [[p(v, "cell") for v in row] for row in rows]
    grid = LongTable(data, colWidths=widths or [WIDTH / len(headers)] * len(headers), repeatRows=1, hAlign="LEFT", splitByRow=1, splitInRow=0)
    grid.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), WASH),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FAFCFC")]),
        ("LINEBELOW", (0, 0), (-1, 0), .7, RULE),
        ("LINEBELOW", (0, 1), (-1, -1), .25, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 6 if compact else 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6 if compact else 8),
    ]))
    return [grid]


def build(title, company, story, period_text, generated, logo=None, period_has_end=True, fictional=False):
    """A prominent first-page identity and a compact continuation header.

    Frames reserve their own header space; neither a long title nor a large
    logo can push content into the header or footer. All images remain local,
    validated snapshot bytes and deterministic rendering stays unchanged.
    """
    out, image = io.BytesIO(), safe_logo(logo)
    identity = {"period": period_text, "generated": generated, "period_has_end": period_has_end, "fictional": fictional}
    later_top = 92
    half = WIDTH / 2
    brand_width = half - 38
    title_x = MARGIN + half + 21
    title_width = half - 21
    # Paragraphs preserve the complete legal name, including unbroken words.
    company_size = 22
    while True:
        company_block = Paragraph(escape(text(company)), ParagraphStyle("CoverCompany", fontName=BOLD, fontSize=company_size, leading=company_size * 1.18, textColor=NAVY, splitLongWords=True))
        _, company_height = company_block.wrap(title_width, PAGE_H)
        if company_height <= 156 or company_size <= 16:
            break
        company_size -= 1
    title_block = Paragraph(escape(text(title)), ParagraphStyle("CoverReport", fontName=BOLD, fontSize=14, leading=18, textColor=NAVY))
    _, title_height = title_block.wrap(title_width, PAGE_H)
    first_top = max(197, company_height + title_height + 115)

    def first_header(canvas, _doc):
        canvas.saveState()
        top, bottom = PAGE_H - 34, PAGE_H - first_top + 17
        middle = MARGIN + half
        canvas.setStrokeColor(colors.HexColor("#AEBECC"))
        canvas.setLineWidth(.8)
        canvas.line(middle, bottom + 15, middle, top)
        if image:
            w, h = image.getSize()
            scale = min(brand_width / w, 99 / h)
            iw, ih = w * scale, h * scale
            canvas.drawImage(image, MARGIN + (half - iw) / 2 - 7,
                             (top + bottom + 15 - ih) / 2,
                             width=iw, height=ih, mask="auto")
        y = top - 20 - company_height
        company_block.drawOn(canvas, title_x, y)
        y -= title_height + 9
        title_block.drawOn(canvas, title_x, y)
        y -= 12
        canvas.setStrokeColor(TEAL)
        canvas.setLineWidth(2.5)
        canvas.line(title_x, y, PAGE_W - MARGIN, y)
        draw_text(canvas, "COMPANY INTELLIGENCE REPORT", title_x, y - 16,
                  title_width, size=7, color=TEAL, max_lines=1)
        draw_text(canvas, period_text, title_x, y - 33,
                  title_width, size=8, color=INK, max_lines=2)
        canvas.restoreState()

    def continuation_header(canvas, _doc):
        canvas.saveState()
        draw_text(canvas, company, MARGIN, PAGE_H - 35, WIDTH * .49,
                  size=9.5, bold=True, color=NAVY, max_lines=2)
        draw_text(canvas, title, MARGIN + WIDTH * .54, PAGE_H - 35,
                  WIDTH * .46, size=8.3, bold=True, color=NAVY, max_lines=2)
        draw_text(canvas, period_text, MARGIN, PAGE_H - 65, WIDTH,
                  size=7, max_lines=1)
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(.7)
        canvas.line(MARGIN, PAGE_H - 76, PAGE_W - MARGIN, PAGE_H - 76)
        canvas.restoreState()

    def canvas_factory(*args, **kwargs):
        kwargs["invariant"] = 1
        return ReportCanvas(*args, report_identity=identity, **kwargs)

    doc = BaseDocTemplate(out, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN,
                          bottomMargin=65, title=text(title), author=text(company),
                          subject="Company-authorized management report", pageCompression=1)
    def frame(top, name):
        return Frame(MARGIN, 65, WIDTH, PAGE_H - top - 65, id=name,
                     leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([
        PageTemplate(id="First", frames=[frame(first_top, "FirstContent")], onPage=first_header),
        PageTemplate(id="Continuation", frames=[frame(later_top, "ContinuedContent")], onPage=continuation_header),
    ])
    doc.build([NextPageTemplate("Continuation"), *story], canvasmaker=canvas_factory)
    return out.getvalue()
