"""
Alibaba Cloud Direct Mail over its HTTPS API (SingleSendMail, API version 2015-11-23).

This is used instead of SMTP when ALIYUN_DM_ACCESS_KEY_ID is set. It works on hosts that block SMTP ports
(such as Render's free plan) because it is an ordinary HTTPS request.
Each request is signed with HMAC-SHA1 using the AccessKey Secret (Alibaba's "RPC" signature, version 1.0).
"""
import base64
import hashlib
import hmac
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import requests

# region name -> (API host, region id)
REGIONS = {
    "hangzhou": ("dm.aliyuncs.com", "cn-hangzhou"),
    "singapore": ("dm.ap-southeast-1.aliyuncs.com", "ap-southeast-1"),
    "sydney": ("dm.ap-southeast-2.aliyuncs.com", "ap-southeast-2"),
}
API_VERSION = "2015-11-23"


class AliyunDMError(Exception):
    def __init__(self, code, message, status=None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.status = code or "Error", message or "", status


def percent_encode(value):
    """RFC 3986 encoding as Alibaba requires: space -> %20, '*' -> %2A, '~' left alone."""
    return quote(str(value), safe="~")


def string_to_sign(params, method="POST"):
    canonical = "&".join(f"{percent_encode(k)}={percent_encode(v)}" for k, v in sorted(params.items()))
    return f"{method}&{percent_encode('/')}&{percent_encode(canonical)}"


def sign(params, access_key_secret, method="POST"):
    digest = hmac.new((access_key_secret + "&").encode("utf-8"),
                      string_to_sign(params, method).encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def build_params(access_key_id, region_id, account, to, subject, html=None, text=None,
                 from_alias=None, reply_to=False, now=None, nonce=None):
    params = {
        "Action": "SingleSendMail",
        "Version": API_VERSION,
        "Format": "JSON",
        "AccessKeyId": access_key_id,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": nonce or str(uuid.uuid4()),
        "Timestamp": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "RegionId": region_id,
        "AccountName": account,          # the sender address created in the console
        "AddressType": "1",              # 1 = a sender address (0 = random account)
        "ReplyToAddress": "true" if reply_to else "false",
        "ToAddress": to,
        "Subject": subject[:256],
    }
    if from_alias:
        params["FromAlias"] = from_alias[:15]
    if html:
        params["HtmlBody"] = html
    else:
        params["TextBody"] = text or ""
    return params


def send_single(access_key_id, access_key_secret, region, account, to, subject, html=None, text=None,
                from_alias=None, reply_to=False, http=None, timeout=20, endpoint=None, now=None, nonce=None):
    """Send one email. Returns Alibaba's request id. Raises AliyunDMError."""
    host, region_id = REGIONS.get(region, REGIONS["singapore"])
    params = build_params(access_key_id, region_id, account, to, subject, html, text, from_alias, reply_to, now, nonce)
    params["Signature"] = sign({k: v for k, v in params.items()}, access_key_secret)
    http = http or requests
    try:
        resp = http.post(f"https://{endpoint or host}/", data=params, timeout=timeout)
    except requests.RequestException as exc:
        raise AliyunDMError("ConnectionError", f"Could not reach {endpoint or host}: {exc}") from exc
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code != 200 or body.get("Code"):
        raise AliyunDMError(body.get("Code") or f"HTTP{resp.status_code}", body.get("Message") or "", resp.status_code)
    return body.get("RequestId") or body.get("EnvId") or ""
