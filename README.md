# Crown Tech Hub - web system

Your existing admin and cashier system, plus a customer storefront with online orders and
Paynow / PayPal payments.

## Run it

```bash
python -m venv venv && venv\Scripts\activate        # Windows  (macOS/Linux: source venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env                               # then fill it in (cp on macOS/Linux)
python run.py
```
Keep your existing `instance/inventory.db` in place. The new order tables are added automatically
on first start; no existing table or row is changed.

* Shop: http://127.0.0.1:5000/
* Staff sign in: http://127.0.0.1:5000/auth/login (same admin/cashier accounts as before)
* Online orders for staff: **Orders** in the admin and cashier menus

## How an order flows
1. Customer adds items to the cart and checks out (guest or with an account).
2. Stock is **reserved** immediately, so two people can't pay for the same single-unit laptop.
   Unpaid orders release their stock after 45 minutes (online payment) or 48 hours (pay at shop).
3. Payment is confirmed server-to-server (Paynow hash-verified callback + status poll; PayPal capture
   with amount, currency and reference checks). Browser redirects are never trusted.
4. On payment, a Receipt and Sale rows are written, so online sales appear in your existing sales,
   profit and daily reports. Receipt numbers look like `WEB-CTH-ABC234`.
5. Staff mark the order ready, then collected/delivered. Cancelling returns stock.

Online payments land in your Paynow/PayPal accounts, so they are **not** added to the till's
daily cash balance.

## Paynow
1. Paynow dashboard > My Account > Integrations: create an integration, copy the ID and key.
2. Put them in `.env` (`PAYNOW_INTEGRATION_ID`, `PAYNOW_INTEGRATION_KEY`).
3. While the integration is in test mode set `PAYNOW_TEST_EMAIL` to your Paynow login email.
4. `PUBLIC_BASE_URL` must be your public https address so Paynow can send payment updates.
   On localhost the customer's return page still confirms payment by polling Paynow.
5. Make sure your Paynow account accepts the currency you sell in (USD by default).

## PayPal
1. developer.paypal.com > Apps & Credentials: create a REST app (Sandbox first).
2. Set `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_MODE=sandbox`.
3. Test with a sandbox buyer account, then switch to live credentials and `PAYPAL_MODE=live`.

A method only appears at checkout when its credentials are set.

## Before you go live
* Set `SECRET_KEY`, `PUBLIC_BASE_URL`, `SESSION_COOKIE_SECURE=true`, and run behind https.
* Run with gunicorn (Linux) or waitress (Windows), not `python run.py`; set `BEHIND_PROXY=true` behind nginx.
* Do a full sandbox test of both gateways, including a cancelled and a failed payment.
* Back up `instance/inventory.db` regularly (SQLite). Consider PostgreSQL if traffic grows.
* Run `python tests/test_services.py` after any change to the payment code.

## Known issues in the original code (not changed, worth fixing next)
* `admin_quotations.html` links to `admin.edit_quotation`, which doesn't exist: the admin quotations page will error when it lists a quotation.
* Staff contract uploads are saved under `static/`, which is publicly downloadable by URL.
* Staff forms (admin/cashier) have no CSRF protection; the new storefront and order pages do.
* Cashier sales store Zimbabwe local time while reports query a UTC range, so sales after about 22:00 can land on the next day.
* The cashier panel's debug logging prints cart and form data; turn it down for production.
