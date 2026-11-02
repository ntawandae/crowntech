"""
Minimal PayPal Orders v2 client (redirect flow) using `requests`.

Flow: create order -> send customer to PayPal -> PayPal redirects back with
?token=<order id> -> we CAPTURE server-side and verify amount/currency/reference
before marking anything paid.
"""
import time
from decimal import Decimal

import requests

BASES = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}


class PayPalError(Exception):
    pass


class PayPalClient:
    def __init__(self, client_id, client_secret, mode="sandbox", http=None, timeout=25):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base = BASES.get(mode, BASES["sandbox"])
        self.http = http or requests
        self.timeout = timeout
        self._token = None
        self._token_expiry = 0

    # -- auth ------------------------------------------------------------
    def _access_token(self):
        if self._token and time.time() < self._token_expiry - 30:
            return self._token
        try:
            resp = self.http.post(
                f"{self.base}/v1/oauth2/token",
                auth=(self.client_id, self.client_secret),
                data={"grant_type": "client_credentials"},
                headers={"Accept": "application/json"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise PayPalError(f"Could not reach PayPal: {exc}") from exc
        if resp.status_code != 200:
            raise PayPalError("PayPal rejected the API credentials.")
        body = resp.json()
        self._token = body["access_token"]
        self._token_expiry = time.time() + int(body.get("expires_in", 300))
        return self._token

    def _request(self, method, path, json=None, request_id=None):
        headers = {"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        try:
            resp = self.http.request(method, f"{self.base}{path}", json=json, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise PayPalError(f"Could not reach PayPal: {exc}") from exc
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400:
            message = body.get("message") or body.get("name") or f"HTTP {resp.status_code}"
            raise PayPalError(f"PayPal error: {message}")
        return body

    # -- orders ----------------------------------------------------------
    def create_order(self, reference, amount, currency, description, return_url, cancel_url,
                     brand_name, attempt=1):
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": reference,
                "custom_id": reference,
                "description": description[:127],
                "amount": {"currency_code": currency, "value": f"{Decimal(str(amount)):.2f}"},
            }],
            "payment_source": {"paypal": {"experience_context": {
                "brand_name": brand_name[:127],
                "shipping_preference": "NO_SHIPPING",
                "user_action": "PAY_NOW",
                "return_url": return_url,
                "cancel_url": cancel_url,
            }}},
        }
        order = self._request("POST", "/v2/checkout/orders", body, request_id=f"{reference}-{attempt}")
        approve = None
        for link in order.get("links", []):
            if link.get("rel") in ("payer-action", "approve"):
                approve = link["href"]
                break
        if not approve:
            raise PayPalError("PayPal did not return an approval link.")
        return order["id"], approve

    def capture(self, paypal_order_id):
        """Capture an approved order. Returns the capture summary dict."""
        order = self._request("POST", f"/v2/checkout/orders/{paypal_order_id}/capture",
                              json={}, request_id=f"capture-{paypal_order_id}")
        return self.summarize(order)

    def get_order(self, paypal_order_id):
        return self.summarize(self._request("GET", f"/v2/checkout/orders/{paypal_order_id}"))

    @staticmethod
    def summarize(order):
        unit = (order.get("purchase_units") or [{}])[0]
        captures = (unit.get("payments") or {}).get("captures") or []
        cap = captures[0] if captures else {}
        amount = cap.get("amount") or {}
        return {
            "order_status": order.get("status"),
            "capture_status": cap.get("status"),
            "capture_id": cap.get("id"),
            "amount": Decimal(amount["value"]) if amount.get("value") else None,
            "currency": amount.get("currency_code"),
            "reference": unit.get("custom_id") or unit.get("reference_id"),
        }

    @staticmethod
    def is_paid(summary):
        return summary.get("order_status") == "COMPLETED" and summary.get("capture_status") == "COMPLETED"
