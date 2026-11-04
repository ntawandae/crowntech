# Email setup on Render with Aliyun Direct Mail

Emails sent by the site: confirm-your-email, password reset, order reserved / paid / ready, and new-staff welcome.
Until `MAIL_SERVER` is set the site does **not** send anything; it only saves a copy in `instance/outbox/`.

## 1. In the Aliyun Direct Mail console
1. **Domain:** add `crowntechhub.co.zw` and add every DNS record the console asks for at your domain registrar
   (ownership, SPF, DKIM and the others it lists). Wait until the console shows the domain as verified.
2. **Sender address:** create `info@crowntechhub.co.zw`.
3. **SMTP password:** Sender Addresses, find the address, click **Set SMTP Password**, choose a strong password and
   keep it safe. This is *not* your Alibaba Cloud login password.
4. **Region:** note which sending region the domain was added under. The SMTP server name depends on it:

| Region in the console | MAIL_SERVER |
|---|---|
| China (Hangzhou) | `smtpdm.aliyun.com` |
| Singapore | `smtpdm-ap-southeast-1.aliyuncs.com` |
| US (Virginia) | `smtpdm-us-east-1.aliyuncs.com` |
| Germany (Frankfurt) | `smtpdm-eu-central-1.aliyuncs.com` |

(Alibaba's SMTP documentation lists ports 25, 80 and 465. Check the current page if a name above stops working.)

## 2. In Render
Open the web service, then **Environment**, and add these variables. Saving redeploys the site.

| Variable | Value |
|---|---|
| `MAIL_SERVER` | the server name for your region (table above) |
| `MAIL_PORT` | `80` |
| `MAIL_USE_SSL` | `false` |
| `MAIL_USE_TLS` | `true` |
| `MAIL_USERNAME` | `info@crowntechhub.co.zw` |
| `MAIL_PASSWORD` | the SMTP password from step 1.3 |
| `MAIL_FROM` | `info@crowntechhub.co.zw` |
| `MAIL_FROM_NAME` | `Crown Tech Hub` |
| `PUBLIC_BASE_URL` | `https://crowntech-55z4.onrender.com` now; `https://crowntechhub.co.zw` once the domain points at the site |
| `BEHIND_PROXY` | `true` |
| `SESSION_COOKIE_SECURE` | `true` |
| `SECRET_KEY` | a long random string you keep (so logins survive redeploys) |

**Why port 80?** Render's free web services block outbound traffic to SMTP ports 25, 465 and 587 (Render changelog,
September 2025). Aliyun also accepts STARTTLS on port 80, which is not blocked. On a paid Render instance you can
instead use `MAIL_PORT=465`, `MAIL_USE_SSL=true`, `MAIL_USE_TLS=false`.

**Why `BEHIND_PROXY=true`?** Render sits in front of the site. Without it every visitor looks like the same address
(so the login attempt limit is shared by everyone) and links may be built with `http://`.

## 3. Test it
1. Sign in as an admin and open **Email setup** in the menu.
2. Check the warnings at the top, then use **Send test email** with a Gmail address.
3. The result shows immediately. Then try the real flows: create a customer account, use "Forgot password", and place
   a "pay at the shop" order.

## 4. If something fails
| What you see | Likely cause | Fix |
|---|---|---|
| "timed out" / could not connect | Render free plan blocks the port, or wrong server name | Use port 80 + STARTTLS; check `MAIL_SERVER` matches the region |
| "rejected the login" | Wrong username or password | Username = full sender address; password = SMTP password from the console |
| "refused the sender address" | `MAIL_FROM` differs from a verified sender | Use exactly the address created in Direct Mail |
| Says sent, nothing arrives | Spam folder, DNS records not verified, or daily quota | Check the console's domain status and quota; try another inbox |
| Email links start with `http://` or the wrong host | `PUBLIC_BASE_URL` / `BEHIND_PROXY` missing | Set both, redeploy |
| "Too many attempts" for everyone | `BEHIND_PROXY` missing | Set `BEHIND_PROXY=true` |

## 5. When crowntechhub.co.zw goes live
1. Point the domain at the Render service and add it as a custom domain there.
2. Change `PUBLIC_BASE_URL` to `https://crowntechhub.co.zw` and redeploy.
3. In Google Cloud, add `https://crowntechhub.co.zw/account/google/callback` as an authorised redirect URI (keep the
   onrender.com one until you retire it).

## 6. Keep your data safe on Render
The SQLite database, private contract files and generated thumbnails live in the `instance/` folder. On Render a free
instance's disk is wiped whenever the service redeploys or restarts, unless you attach a persistent disk or use an
external database. Make sure one of those is in place before real customers use the site.

Sources: Render changelog "Free web services will no longer allow outbound traffic to SMTP ports"; Alibaba Cloud Direct
Mail documentation on SMTP service addresses and sending over SMTP.
