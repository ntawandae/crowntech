"""
Minimal Paynow Zimbabwe client (web/redirect checkout) using `requests`.

Docs: https://developers.paynow.co.zw/docs/initiate_transaction.html
Every message in both directions carries a SHA-512 hash made from all field
values (in order, excluding `hash`) followed by the integration key. We sign
what we send and refuse anything we receive that does not verify.
"""
import hashlib
import hmac
from decimal import Decimal
from urllib.parse import parse_qsl

import requests

INITIATE_URL = "https://www.paynow.co.zw/interface/initiate-transaction"

# Paynow statuses that mean the money is secured for the merchant.
PAID_STATUSES = {"paid", "awaiting delivery", "delivered"}
FAILED_STATUSES = {"cancelled", "refunded", "disputed"}


class PaynowError(Exception):
    pass


def make_hash(values, integration_key):
    joined = "".join(str(v) for v in values) + integration_key
    return hashlib.sha512(joined.encode("utf-8")).hexdigest().upper()


def parse_message(text):
    """Parse an urlencoded Paynow reply into an ordered list of (lowercase key, value)."""
    return [(k.lower(), v) for k, v in parse_qsl(text, keep_blank_values=True)]


def verify_pairs(pairs, integration_key):
    """pairs: ordered (key, value) list as received. True only if the hash matches."""
    received = None
    values = []
    for key, value in pairs:
        if key.lower() == "hash":
            received = value
        else:
            values.append(value)
    if not received:
        return False
    expected = make_hash(values, integration_key)
    return hmac.compare_digest(expected, received.upper())


def is_paid(status):
    return (status or "").strip().lower() in PAID_STATUSES


def is_failed(status):
    return (status or "").strip().lower() in FAILED_STATUSES


class PaynowClient:
    def __init__(self, integration_id, integration_key, http=None, timeout=25):
        self.integration_id = str(integration_id)
        self.integration_key = integration_key
        self.http = http or requests
        self.timeout = timeout

    def initiate(self, reference, amount, additional_info, return_url, result_url, payer_email):
        """Start a payment. Returns dict with browserurl, pollurl."""
        fields = [
            ("id", self.integration_id),
            ("reference", reference),
            ("amount", f"{Decimal(str(amount)):.2f}"),
            ("additionalinfo", additional_info[:120]),
            ("returnurl", return_url),
            ("resulturl", result_url),
            ("authemail", payer_email),
            ("status", "Message"),
        ]
        fields.append(("hash", make_hash([v for _, v in fields], self.integration_key)))
        try:
            resp = self.http.post(INITIATE_URL, data=dict(fields), timeout=self.timeout)
        except requests.RequestException as exc:
            raise PaynowError(f"Could not reach Paynow: {exc}") from exc

        pairs = parse_message(resp.text)
        data = dict(pairs)
        if data.get("status", "").lower() != "ok":
            raise PaynowError(data.get("error") or "Paynow rejected the request.")
        if not verify_pairs(pairs, self.integration_key):
            raise PaynowError("Paynow response failed hash verification.")
        if not data.get("browserurl") or not data.get("pollurl"):
            raise PaynowError("Paynow response was missing payment links.")
        return {"browserurl": data["browserurl"], "pollurl": data["pollurl"]}

    def poll(self, poll_url):
        """Ask Paynow directly for the current status. Returns verified dict (lowercase keys)."""
        if not poll_url.startswith("https://www.paynow.co.zw/"):
            raise PaynowError("Unexpected poll URL.")
        try:
            resp = self.http.post(poll_url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise PaynowError(f"Could not reach Paynow: {exc}") from exc
        pairs = parse_message(resp.text)
        if not verify_pairs(pairs, self.integration_key):
            raise PaynowError("Paynow status failed hash verification.")
        return dict(pairs)
