"""Text-based management PDFs with repeatable table headers and safe text."""

import base64, io
from html import escape
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    LongTable,
    TableStyle,
    Image,
)
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4


def render(title, company, rows, metadata, logo=None):
    out = io.BytesIO()
    styles = getSampleStyleSheet()
    body = []
    if logo:
        try:
            raw = base64.b64decode(logo.split(",")[-1], validate=True)
            image = Image(io.BytesIO(raw))
            image._restrictSize(110, 60)
            body += [image, Spacer(1, 12)]
        except (ValueError, OSError):
            pass
    body += [
        Paragraph(escape(company), styles["Title"]),
        Paragraph(escape(title), styles["Heading1"]),
        Paragraph(escape(metadata), styles["Normal"]),
        Spacer(1, 18),
    ]
    table = [
        [Paragraph("Field", styles["Heading4"]), Paragraph("Value", styles["Heading4"])]
    ]
    for key, value in rows:
        table.append(
            [
                Paragraph(escape(str(key)), styles["Normal"]),
                Paragraph(escape(str(value)).replace("\n", "<br/>"), styles["Normal"]),
            ]
        )
    t = LongTable(
        table, colWidths=[155, 340], repeatRows=1, hAlign="LEFT", splitInRow=1
    )
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#edf3fa")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d6e0eb")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 9),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
            ]
        )
    )
    body.append(t)

    def footer(canvas, doc):
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.drawString(40, 25, "HeyPorts · Company-authorized report")
        canvas.drawRightString(A4[0] - 40, 25, f"Page {doc.page}")

    SimpleDocTemplate(
        out, pagesize=A4, leftMargin=40, rightMargin=40, topMargin=40, bottomMargin=45
    ).build(body, onFirstPage=footer, onLaterPages=footer)
    return out.getvalue()
