"""Compact, downloadable reports matching the workspace lists."""

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
        output, pagesize=pagesize, leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=30,
        title=title, author="SalesTrackr",
    )
    normal = ParagraphStyle("PartsBody", fontName="Parts", fontSize=8.5, leading=10)
    heading = ParagraphStyle("PartsHeading", parent=normal, fontName="PartsBold", fontSize=15, leading=18)
    header = ParagraphStyle("PartsHeader", parent=normal, fontName="PartsBold", textColor=colors.white)

    def paragraph(text, style=normal):
        return Paragraph(escape(str(text)).replace("\n", "<br/>"), style)

    story = [paragraph(title, heading), Spacer(1, 5)]
    story.extend(paragraph(detail) for detail in details)
    story.append(Spacer(1, 7))
    rows = [[paragraph(label, header) for label in labels]]
    rows.extend([paragraph(value) for value in row] for row in values)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#245c4b")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#dce2dc")),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
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
        canvas.drawRightString(pagesize[0] - 24, 16, f"Page {canvas.getPageNumber()}")

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


def orders_pdf(filters, records):
    def formatted(value):
        return date.fromisoformat(value).strftime("%d %b %Y") if value else "-"

    return _report(
        "Order in hand", [*filters, f"Records: {len(records)} | Newest lead acquired first | India time"],
        ("Company / location", "Product / requirement", "Lead acquired",
         "Current sales stage", "Stage date", "Next follow-up"),
        (0.23, 0.36, 0.105, 0.10, 0.105, 0.10),
        [(
            f"{row['accountName']}\n{row.get('accountLocation') or 'Location not added'}",
            row["data"].get("productNotes") or "No product notes",
            formatted(row["data"]["stageDates"]["new_lead"]), "Order in hand",
            formatted(row["data"]["stageDates"].get("order_received")),
            formatted(row["data"].get("nextFollowUpOn")),
        ) for row in records],
        wide=True, empty="No orders found.",
    )


def stock_pdf(products, *, shortages=False):
    values = []
    for product in products:
        count = product["openJobCount"]
        jobs = [f"{count} open job{'s' if count != 1 else ''}" if count else "No open jobs"]
        jobs.extend(
            f"{job['number']} / "
            f"{job['quantity'] if job.get('quantity') is not None else 'Quantity not recorded'}"
            for job in product["openJobs"]
        )
        if count > len(product["openJobs"]):
            jobs.append(f"Showing {len(product['openJobs'])} of {count} jobs")
        values.append((
            product.get("partNumber") or "Not added", product["name"],
            product["requiredQuantity"] if shortages else product["minimumStockQuantity"],
            product["stockQuantity"], product["missingQuantity"],
            *((product["salesCount"],) if shortages else ()), "\n".join(jobs),
        ))
    return _report(
        "Order in hand shortage" if shortages else "Low stock",
        [("Demand from New leads, Follow-ups and Order in hand."
          if shortages else "Products with stock below their minimum quantity."),
         f"Products: {len(products)} | Product name order"],
        ("Part number", "Product", "Required quantity" if shortages else "Minimum quantity",
         "In stock", "Short by", *(("Sales records",) if shortages else ()), "Manufacturing"),
        (0.14, 0.28, 0.10, 0.09, 0.09, 0.08, 0.22) if shortages else (0.14, 0.32, 0.10, 0.10, 0.10, 0.24),
        values, wide=True,
        empty="No order shortages." if shortages else "No low-stock products.",
    )


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


def accounts_pdf(filters, accounts, contacts):
    values = []
    for account in accounts:
        data = account["data"]
        address = "\n".join(filter(None, [
            data.get("address"),
            ", ".join(filter(None, [data.get("city"), data.get("state"), data.get("country")])),
        ]))
        people = sorted(
            contacts.get(account["entityId"], []),
            key=lambda contact: (contact["data"]["name"].casefold(), contact["entityId"]),
        )
        contact_details = []
        for person in people:
            contact = person["data"]
            contact_details.append("\n".join(filter(None, [
                contact["name"], contact.get("designation"), contact.get("email"), contact.get("phone"),
            ])))
        values.append((
            data["name"], address or "Not added", "\n\n".join(contact_details) or "No contacts added",
        ))
    return _report(
        "Accounts", [*filters, f"Accounts: {len(accounts)}"],
        ("Company name", "Address", "Contacts"), (0.25, 0.35, 0.40), values, empty="No accounts found.",
    )
