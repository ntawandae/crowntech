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

Online sales are counted in your sales, profit and daily balance like any other sale (the daily
balance is rebuilt from all recorded sales, income and expenses).

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

## Till: discounts and delivery fee
* Discount can be a $ amount or a percentage. It is checked on the server (never negative, never more than
  the subtotal, percentages 0-100). Set `CASHIER_MAX_DISCOUNT_PERCENT` to cap what cashiers can give.
* The discount is shared across the items to the cent, so each sale line and its profit are exact.
  (Before, profit ignored discounts.)
* A delivery fee can be added to any sale, with an optional address. It is on the receipt, included in the
  total, and booked as extra income ("Delivery fee - receipt number") so reports pick it up.
* Reversing a sale line returns stock but does not reverse the delivery fee; adjust that income manually.

## Time
All sale, receipt, expense and income times are stored in Zimbabwe local time (UTC+2). Reports use the plain
local day, so a sale at 23:00 appears on that day. Old records are not changed.

## Contracts
Contract files now live in `instance/contracts/` (not under `static/`) and are served only to the owner or
an admin. Any old files found in `static/uploads/contracts` are moved there on first start.

## Accounts, email and Google sign-in
* **Customers** confirm their email by link before they can sign in, and can reset a forgotten password by email.
  Orders placed as a guest with the same email join the account once it is confirmed.
* **Sign in with Google** (customers only) appears automatically once `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`
  are set. If a Google email matches an admin or cashier account it is refused, so staff always use their password.
* **Staff** accounts are created by an admin in Users. The admin sets a temporary password; the staff member must
  choose their own at first sign-in (also after every admin password reset).
* **Order emails:** reservation received, payment received (with receipt link), and order ready.
* **Sending:** set the `MAIL_*` values (Aliyun Direct Mail SMTP). With `MAIL_SERVER` empty, emails are printed to the
  console and saved in `instance/outbox/`, so every flow can be tested before the domain is live.
* Links in emails use `PUBLIC_BASE_URL`, so set it to `https://crowntechhub.co.zw` in production.

## Tests
`python tests/test_services.py` - payment clients, pricing, discount sharing, product text helpers.
