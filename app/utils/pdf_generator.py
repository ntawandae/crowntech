from flask import render_template
import io
from xhtml2pdf import pisa

from .receipts import static_link_callback
from ..services.quotes import generate_quotation_number  # noqa: F401  (kept for older imports)


def generate_pdf(template_name, context):
    html = render_template(template_name, **context)
    result = io.BytesIO()
    pisa.CreatePDF(io.StringIO(html), dest=result, link_callback=static_link_callback)
    result.seek(0)
    return result
