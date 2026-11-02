"""
Tests for the payment clients and storefront text helpers.
They load the service modules directly, so they run without Flask/SQLAlchemy installed:
    python tests/test_services.py     (or: pytest tests)
"""
import importlib.util
import pathlib
import sys
from decimal import Decimal
from urllib.parse import urlencode

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "app" / "services" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


paynow = load("paynow")
paypal = load("paypal")
specs = load("specs")
KEY = "test-integration-key"


class Resp:
    def __init__(self, text="", status=200, js=None):
        self.text, self.status_code, self._js = text, status, js

    def json(self):
        if self._js is None:
            raise ValueError
        return self._js


def signed(fields, key=KEY):
    """Build a Paynow-style signed urlencoded message."""
    values = [v for _, v in fields]
    return urlencode(fields + [("hash", paynow.make_hash(values, key))])


# ---------------------------------------------------------------- Paynow
def test_paynow_initiate_signs_request_and_verifies_reply():
    sent = {}

    class Http:
        def post(self, url, data=None, timeout=None):
            sent.update(data)
            return Resp(signed([("status", "Ok"), ("browserurl", "https://www.paynow.co.zw/Payment/ConfirmPayment/1"),
                                ("pollurl", "https://www.paynow.co.zw/Interface/CheckPayment/?guid=abc")]))

    c = paynow.PaynowClient("1234", KEY, http=Http())
    out = c.initiate("CTH-ABC234", 450, "order", "https://x/return", "https://x/result", "a@b.co")
    assert out["browserurl"].startswith("https://www.paynow.co.zw/")
    assert sent["amount"] == "450.00"
    # request hash = SHA512(all values in order + key)
    order = ["id", "reference", "amount", "additionalinfo", "returnurl", "resulturl", "authemail", "status"]
    assert sent["hash"] == paynow.make_hash([sent[k] for k in order], KEY)


def test_paynow_rejects_tampered_or_error_replies():
    good = signed([("status", "Ok"), ("browserurl", "https://www.paynow.co.zw/a"), ("pollurl", "https://www.paynow.co.zw/b")])

    forged_pairs = [(k, "https://www.paynow.co.zw/evil" if k == "browserurl" else v)
                    for k, v in paynow.parse_message(good)]  # keeps the old hash

    class Tampered:
        def post(self, *a, **k):
            return Resp(urlencode(forged_pairs))

    class Err:
        def post(self, *a, **k):
            return Resp(urlencode([("status", "Error"), ("error", "Invalid Integration ID")]))

    for http, exc_text in ((Tampered(), "hash"), (Err(), "Integration")):
        try:
            paynow.PaynowClient("1", KEY, http=http).initiate("R", 1, "i", "u", "u", "e@x.co")
            raise AssertionError("should have raised")
        except paynow.PaynowError as exc:
            assert exc_text in str(exc)


def test_paynow_callback_verification_and_status_helpers():
    msg = signed([("reference", "CTH-ABC234"), ("paynowreference", "99"), ("amount", "450.00"),
                  ("status", "Paid"), ("pollurl", "https://www.paynow.co.zw/Interface/CheckPayment/?guid=abc")])
    pairs = paynow.parse_message(msg)
    assert paynow.verify_pairs(pairs, KEY)
    assert not paynow.verify_pairs(pairs, "wrong-key")
    forged = [(k, "1.00" if k == "amount" else v) for k, v in pairs]
    assert not paynow.verify_pairs(forged, KEY)
    assert not paynow.verify_pairs([p for p in pairs if p[0] != "hash"], KEY)  # no hash at all
    assert paynow.is_paid("Paid") and paynow.is_paid("awaiting delivery") and not paynow.is_paid("Created")
    assert paynow.is_failed("Cancelled") and not paynow.is_failed("Sent")


def test_paynow_poll_refuses_foreign_urls():
    try:
        paynow.PaynowClient("1", KEY, http=object()).poll("https://evil.example/steal")
        raise AssertionError("should have raised")
    except paynow.PaynowError:
        pass


# ---------------------------------------------------------------- PayPal
class PayPalHttp:
    def __init__(self, capture_body=None):
        self.calls, self.capture_body = [], capture_body

    def post(self, url, **kw):
        self.calls.append(("POST", url))
        return Resp(js={"access_token": "tok", "expires_in": 3000})

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append((method, url, json, headers))
        if url.endswith("/v2/checkout/orders"):
            return Resp(js={"id": "PPORDER1", "links": [{"rel": "self", "href": "x"},
                                                        {"rel": "payer-action", "href": "https://www.sandbox.paypal.com/checkoutnow?token=PPORDER1"}]})
        return Resp(js=self.capture_body)


def test_paypal_create_order_builds_correct_payload():
    http = PayPalHttp()
    c = paypal.PayPalClient("id", "secret", "sandbox", http=http)
    oid, url = c.create_order("CTH-ABC234", 450, "USD", "Crown Tech Hub order", "https://x/ret", "https://x/cancel", "Crown Tech Hub")
    assert oid == "PPORDER1" and "paypal.com" in url
    method, _, body, headers = http.calls[-1]
    unit = body["purchase_units"][0]
    assert unit["amount"] == {"currency_code": "USD", "value": "450.00"}
    assert unit["custom_id"] == "CTH-ABC234" and body["intent"] == "CAPTURE"
    assert headers["Authorization"] == "Bearer tok" and headers["PayPal-Request-Id"] == "CTH-ABC234-1"


def test_paypal_capture_summary():
    done = {"status": "COMPLETED", "purchase_units": [{"custom_id": "CTH-ABC234", "payments": {"captures": [
        {"id": "CAP1", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "450.00"}}]}}]}
    c = paypal.PayPalClient("id", "secret", http=PayPalHttp(done))
    s = c.capture("PPORDER1")
    assert paypal.PayPalClient.is_paid(s)
    assert s["amount"] == Decimal("450.00") and s["currency"] == "USD" and s["reference"] == "CTH-ABC234"

    pending = {"status": "APPROVED", "purchase_units": [{"custom_id": "CTH-ABC234"}]}
    s2 = paypal.PayPalClient("id", "secret", http=PayPalHttp(pending)).capture("X")
    assert not paypal.PayPalClient.is_paid(s2)


def test_paypal_errors_are_readable():
    class Bad(PayPalHttp):
        def request(self, *a, **k):
            return Resp(status=422, js={"name": "UNPROCESSABLE_ENTITY", "message": "Instrument declined"})

    try:
        paypal.PayPalClient("id", "secret", http=Bad()).capture("X")
        raise AssertionError("should have raised")
    except paypal.PayPalError as exc:
        assert "declined" in str(exc)


# ---------------------------------------------------------------- storefront text
def test_specs_and_titles():
    name = "HP EliteBook 830 G7 x360 Touch 💻core i5 10th Gen | 16GB RAM | 512GB SSD 🔁 2-in-1 Flip Design"
    assert specs.short_title(name) == "HP EliteBook 830 G7 x360"
    assert specs.extract_specs(name) == ["Core i5 · 10th gen", "16 GB RAM", "512 GB SSD", "Touchscreen"]
    assert specs.extract_specs("💻💻HP Pro X2 Core i5-7th gen RAM 8gb SSD 256gb Touch Screen")[2] == "256 GB SSD"
    assert specs.extract_specs("Watch straps") == []
    assert specs.short_title("AirPods Pro") == "AirPods Pro"
    assert specs.normalize_category("accessories ") == "Accessories"
    assert specs.normalize_category("Gaming Laptop ") == "Gaming laptops"
    assert specs.normalize_category("") == "Other"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print("PASS", name)
    print(f"{len(tests)} tests passed")
