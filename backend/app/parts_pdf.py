"""Downloadable, filtered parts reports."""

import io
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

import reportlab
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

MAX_REPORT_ROWS = 5000
FONT_DIR = Path(reportlab.__file__).parent / "fonts"
pdfmetrics.registerFont(TTFont("Parts", str(FONT_DIR / "Vera.ttf")))
pdfmetrics.registerFont(TTFont("PartsBold", str(FONT_DIR / "VeraBd.ttf")))


def _report(title, details, labels, widths, values, *, highlights=(), wide=False, empty="No products."):
    output = io.BytesIO()
    pagesize = landscape(A4) if wide else A4
    document = SimpleDocTemplate(
        output, pagesize=pagesize, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36,
        title=title, author="SalesTrackr",
    )
    normal = ParagraphStyle("PartsBody", fontName="Parts", fontSize=8.5, leading=12)
    heading = ParagraphStyle("PartsHeading", parent=normal, fontName="PartsBold", fontSize=18, leading=24)
    header = ParagraphStyle("PartsHeader", parent=normal, fontName="PartsBold", textColor=colors.white)

    def paragraph(text, style=normal):
        return Paragraph(escape(str(text)).replace("\n", "<br/>"), style)

    story = [paragraph(title, heading), Spacer(1, 10)]
    story.extend(paragraph(detail) for detail in details)
    story.append(Spacer(1, 14))
    rows = [[paragraph(label, header) for label in labels]]
    rows.extend([paragraph(value) for value in row] for row in values)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#245c4b")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#dce2dc")),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]
    for index in highlights:
        style.append(("BACKGROUND", (0, index + 1), (-1, index + 1), colors.HexColor("#eef8f0")))
    table = LongTable(rows, colWidths=[document.width * share for share in widths],
                      repeatRows=1, splitInRow=1, hAlign="LEFT")
    table.setStyle(TableStyle(style))
    story.append(table)
    if not values:
        story.extend([Spacer(1, 10), paragraph(empty)])

    def footer(canvas, _):
        canvas.setFont("Parts", 8)
        canvas.drawRightString(pagesize[0] - 36, 20, f"Page {canvas.getPageNumber()}")

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


def machine_parts_pdf(account, machine, model_name, products):
    details = [
        f"Company: {account['name']}", f"Machine: {machine['name']}", f"Model: {model_name}",
    ]
    if machine.get("serialNumber"):
        details.append(f"Serial number: {machine['serialNumber']}")
    if machine.get("manufacturingYear"):
        details.append(f"Manufacturing year: {machine['manufacturingYear']}")
    details.append(f"Compatible products: {len(products)}")
    values, sent_rows = [], []
    for index, product in enumerate(products):
        sent = product.get("lastSentOn")
        status = ("Sent - This machine" if product.get("machineSpecified")
                  else "Sent - Account delivery / machine not specified") if sent else "Not sent"
        values.append((
            product.get("partNumber") or "Not added", product["name"], status,
            date.fromisoformat(sent).strftime("%d %b %Y") if sent else "Not sent",
        ))
        if sent:
            sent_rows.append(index)
    return _report(
        "Potential Spares machine wise", details, ("Part number", "Product", "Delivery status", "Last sent"),
        (0.16, 0.40, 0.28, 0.16), values, highlights=sent_rows, empty="No compatible products.",
    )


def model_parts_pdf(model_name, products):
    return _report(
        "Master Product's List",
        [f"Model: {model_name}", "Includes Common parts for all models.", f"Products: {len(products)}"],
        ("Product", "Part number", "Notes"), (0.34, 0.18, 0.48),
        [(f"{product['name']}\nCommon" if not product.get("modelIds") else product["name"],
          product.get("partNumber") or "Not added", product.get("notes") or "Not added")
         for product in products],
        empty="No compatible products.",
    )


def sales_history_pdf(filters, entries):
    values = []
    for entry in entries:
        row = entry["data"]
        source = ("Sales record" if entry.get("sourceRecordAvailable") else "Deleted sales record") \
            if row["source"] == "sale" else "Manual"
        values.append((
            date.fromisoformat(row["sentOn"]).strftime("%d %b %Y"), row["accountName"],
            "\n".join(filter(None, [row["productName"], row.get("productPartNumber")])),
            "\n".join(filter(None, [row.get("machineName") or "Not specified",
                                   row.get("machineModelName"), row.get("serialNumber")])),
            row.get("quantity") if row.get("quantity") is not None else "Not recorded",
            source, "Active" if row["active"] else "Withdrawn",
        ))
    return _report(
        "Sales History", [*filters, f"Deliveries: {len(entries)}"],
        ("Delivered", "Account", "Product / Part number", "Machine", "Quantity", "Source", "Status"),
        (0.10, 0.17, 0.22, 0.20, 0.08, 0.13, 0.10), values, wide=True, empty="No deliveries found.",
    )
