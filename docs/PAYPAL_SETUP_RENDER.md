# PayPal on Render

How it works: at checkout the customer is sent to PayPal, approves the payment, and PayPal sends them back. The site then
confirms the payment directly with PayPal (checking the amount, currency and order number) before marking the order paid.
You do **not** need to register webhooks or return URLs: they are sent with each order.
The PayPal option only appears at checkout when both PayPal variables are set.

## 0. Before you go live: can your PayPal account receive money?
This decides whether PayPal is worth switching on for real customers. Testing in the sandbox works regardless; **live
payments need a PayPal Business account that can receive funds** in the country it is registered in.

Zimbabwe has historically been a "send only" country on PayPal. The articles I found are from 2014 to 2021 and disagree:
some report Business accounts that could receive, others say the account can only send, and withdrawing to a local bank
was reported as difficult. I could not find a current official statement. Check PayPal's own "country functionality" page
for Zimbabwe and try opening a Business account before relying on it. If your own account can't receive, ask PayPal
support, or use a legitimately registered PayPal Business account in a country where receiving is supported.

Until that's settled, leave the PayPal variables empty: the PayPal button stays hidden and Paynow handles payments.

## 1. Sandbox (testing)
1. Go to developer.paypal.com and log in. Open **Apps & Credentials** and make sure the **Sandbox** toggle is on.
2. **Create App**, name it (for example "Crown Tech Hub"), type **Merchant**. Copy the **Client ID**, and show and copy the **Secret**.
3. Open **Testing tools, Sandbox accounts**. PayPal creates a test business and a test personal (buyer) account. Note the
   buyer's email and password.

## 2. Render settings
Open your web service, **Environment**, add or update, then save (Render redeploys):

| Variable | Value |
|---|---|
| `PAYPAL_CLIENT_ID` | the sandbox Client ID |
| `PAYPAL_CLIENT_SECRET` | the sandbox Secret |
| `PAYPAL_MODE` | `sandbox` |
| `PUBLIC_BASE_URL` | `https://crowntech-55z4.onrender.com` (later `https://crowntechhub.co.zw`) |
| `BEHIND_PROXY` | `true` |
| `SHOP_CURRENCY` | leave as `USD` (PayPal must support the currency on your account) |

## 3. Test checklist
1. On the shop add an item, go to checkout and choose **PayPal**. You are sent to a PayPal **sandbox** page.
2. Log in with the sandbox **buyer** account and approve. You return to the order page showing **Payment received**.
3. In **Admin, Online orders**, open the order: the payment attempt shows as paid, and a receipt exists.
4. Repeat, but press cancel on PayPal's page: the order stays unpaid and its stock is held until the hold expires.
5. Check the **Email setup** page: it shows "PayPal configured (sandbox)".

## 4. Going live
1. In the PayPal dashboard switch the toggle to **Live**, **Create App**, and copy the **live** Client ID and Secret.
2. In Render set `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` to the live values and `PAYPAL_MODE=live`. Redeploy.
3. Do one real test: a low-priced item paid from a personal PayPal account, then refund it from your PayPal dashboard and
   cancel the order in **Online orders** (tick the refund box).

Sandbox and live credentials are different; using one with the other's mode gives "PayPal rejected the API credentials".

## 5. If something goes wrong
| What you see | Cause and fix |
|---|---|
| No PayPal option at checkout | Variables empty or misspelled; set both and redeploy |
| "PayPal rejected the API credentials" | Credentials and `PAYPAL_MODE` don't match (sandbox vs live) |
| Back on the site but still unpaid | PayPal didn't confirm the capture; check the order's payment attempt note, and the PayPal dashboard |
| "Amount mismatch" or "check manually" note | The captured amount differed from the order; never ship until you've checked in PayPal |
| Customer paid but order cancelled | The 45-minute hold had expired; the site retakes the stock if available, otherwise flags it for staff in the order note |
