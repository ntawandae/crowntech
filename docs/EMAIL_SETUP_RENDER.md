# Email on Render with Alibaba Direct Mail (HTTPS API)

The site sends: confirm-your-email, password reset, order reserved / paid / ready, and new-staff welcome emails.
It uses the Direct Mail **API** (an ordinary HTTPS call), so it works on Render's free plan, which blocks SMTP ports.

## 1. In the Alibaba Cloud console

**a) Sender address.** You already have `info@crowntechhub.co.zw` set up and working. Note the **region** it was created
under (Singapore, China Hangzhou, or Australia Sydney). The site must call the same region.

**b) An AccessKey for the site.** Don't use your main account's key. Create a limited one:
1. RAM console, **Users**, **Create User**. Tick the option for programmatic access (AccessKey).
2. Copy the **AccessKey ID** and **AccessKey Secret** immediately. The secret is shown only once.
3. Give the user permission to send mail only. Either attach the system policy `AliyunDirectMailFullAccess`, or create a
   custom policy with just this:

```json
{ "Version": "1", "Statement": [ { "Effect": "Allow", "Action": "dm:SingleSendMail", "Resource": "*" } ] }
```

## 2. In Render
Open your web service, **Environment**, add these, then save (Render redeploys).

| Variable | Value |
|---|---|
| `ALIYUN_DM_ACCESS_KEY_ID` | the AccessKey ID from step 1b |
| `ALIYUN_DM_ACCESS_KEY_SECRET` | the AccessKey Secret from step 1b |
| `ALIYUN_DM_REGION` | `singapore`, `hangzhou` or `sydney`, matching where the sender address was created |
| `MAIL_FROM` | `info@crowntechhub.co.zw` |
| `MAIL_FROM_NAME` | `Crown Tech Hub` (Direct Mail shows at most 15 characters of the display name) |
| `PUBLIC_BASE_URL` | `https://crowntech-55z4.onrender.com` now; `https://crowntechhub.co.zw` once the domain is live |
| `BEHIND_PROXY` | `true` |
| `SESSION_COOKIE_SECURE` | `true` |
| `SECRET_KEY` | a long random string you keep (so people stay signed in across deploys) |

You do not need any `MAIL_SERVER` / `MAIL_PORT` / SMTP settings. If both are present, the API is used.

`BEHIND_PROXY=true` matters on Render: without it every visitor looks like the same address (so the login attempt limit
is shared by everyone) and links in emails may start with `http://`.

## 3. Test it
1. Sign in as an admin and open **Email setup** in the menu.
2. Check the warnings at the top, then **Send test email** to a Gmail address. The exact result shows straight away.
3. Then try the real flows: create a customer account, use "Forgot password", and place a "pay at the shop" order.

## 4. If a test fails
| Message | What to do |
|---|---|
| AccessKey ID not recognised | Re-copy `ALIYUN_DM_ACCESS_KEY_ID` with no spaces |
| AccessKey Secret does not match | Re-copy `ALIYUN_DM_ACCESS_KEY_SECRET` (if lost, create a new AccessKey) |
| Sender address not valid for this region | `MAIL_FROM` must be the address created in Direct Mail; `ALIYUN_DM_REGION` must be the region it was created in |
| Not allowed to send mail | Give the RAM user the `dm:SingleSendMail` permission (step 1b) |
| Quota / throttling | Check the daily sending quota in the Direct Mail console |
| Says sent, nothing arrives | Check spam; check the domain's DNS records show as verified in the console; try another inbox |
| Email links start with `http://` or show the wrong address | Set `PUBLIC_BASE_URL` and `BEHIND_PROXY=true`, redeploy |
| Could not reach Direct Mail | Check the region spelling; confirm the Render service has outbound internet access |

Note: Alibaba's general documentation now labels its older request-signing method ("V2") as superseded by "V3". Direct
Mail's own pages still document the older method and the site's signing matches Alibaba's published example, but if email
ever fails with a signature or version error after an Alibaba change, ask for the update to V3.

## 5. When crowntechhub.co.zw goes live
1. Add the domain as a custom domain in Render and point DNS at it.
2. Change `PUBLIC_BASE_URL` to `https://crowntechhub.co.zw` and redeploy.
3. In Google Cloud, add `https://crowntechhub.co.zw/account/google/callback` as an authorised redirect URI.

## 6. Keep your data safe on Render
The SQLite database, private contract files and thumbnails live in the `instance/` folder. On a free Render instance that
disk is wiped whenever the service redeploys or restarts, unless you attach a persistent disk or use an external database.
Make sure one is in place before real customers use the site.

## Appendix: SMTP instead (not needed if the API works)
Set `MAIL_SERVER` to the SMTP server for your region (Singapore: `smtpdm-ap-southeast-1.aliyuncs.com`; Hangzhou:
`smtpdm.aliyun.com`), `MAIL_PORT=80`, `MAIL_USE_SSL=false`, `MAIL_USE_TLS=true`, `MAIL_USERNAME` = the sender address,
`MAIL_PASSWORD` = the SMTP password from Sender Addresses, Set SMTP Password. Render's free plan blocks ports 25, 465
and 587, which is why port 80 is used.

Sources: Alibaba Cloud Direct Mail documentation (SingleSendMail API, SMTP service addresses); Render changelog on SMTP ports.
