from flask import render_template
import io
from xhtml2pdf import pisa
from app import db  # <-- fix this line depending on your app structure
from app.models import Quotation


def generate_pdf(template_name, context):
    html = render_template(template_name, **context)
    result = io.BytesIO()
    pisa.CreatePDF(io.StringIO(html), dest=result)
    result.seek(0)
    return result

# utils.py (for generating unique quotation numbers)
def generate_quotation_number():
    count = Quotation.query.count() + 1
    return f"CTHQ{count:00}"