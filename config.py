"""
Crown Tech Hub - configuration.

Everything sensitive comes from environment variables (or a .env file loaded by
run.py). Nothing secret is stored in the code.
"""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _load_secret_key():
    """Use SECRET_KEY if set; otherwise create one once and keep it in instance/."""
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    instance_dir = BASE_DIR / "instance"
    instance_dir.mkdir(exist_ok=True)
    key_file = instance_dir / ".secret_key"
    if key_file.exists():
        return key_file.read_text().strip()
    key = secrets.token_hex(32)
    key_file.write_text(key)
    return key


class Config:
    # --- core -----------------------------------------------------------
    SECRET_KEY = _load_secret_key()
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", "sqlite:///inventory.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16 MB uploads
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", False)  # True behind HTTPS
    DEBUG = _env_bool("FLASK_DEBUG", False)

    # Public https address of the site, e.g. https://crowntech.co.zw
    # Paynow and PayPal send customers (and Paynow sends payment updates) back here.
    PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

    # --- business details (shown on the storefront and receipts) --------
    BUSINESS_NAME = os.environ.get("BUSINESS_NAME", "Crown Tech Hub")
    BUSINESS_ADDRESS = os.environ.get(
        "BUSINESS_ADDRESS",
        "Karimapondo Mall, Corner Robert Mugabe & Leopold Takawira, "
        "1st Floor, Workstation 25, Harare, Zimbabwe",
    )
    BUSINESS_PHONE = os.environ.get("BUSINESS_PHONE", "")
    BUSINESS_EMAIL = os.environ.get("BUSINESS_EMAIL", "")
    # International format, digits only (e.g. 263771234567). Enables "Chat on WhatsApp" links.
    BUSINESS_WHATSAPP = os.environ.get("BUSINESS_WHATSAPP", "").replace("+", "").replace(" ", "")

    # --- shop ------------------------------------------------------------
    SHOP_UTC_OFFSET_HOURS = int(os.environ.get("SHOP_UTC_OFFSET_HOURS", "2"))  # Zimbabwe = UTC+2
    SHOP_CURRENCY = os.environ.get("SHOP_CURRENCY", "USD")
    SHOP_CURRENCY_SYMBOL = os.environ.get("SHOP_CURRENCY_SYMBOL", "$")
    DELIVERY_ENABLED = _env_bool("DELIVERY_ENABLED", True)
    DELIVERY_FEE = float(os.environ.get("DELIVERY_FEE", "0"))  # flat fee; 0 = quoted by the shop
    PAY_ON_PICKUP_ENABLED = _env_bool("PAY_ON_PICKUP_ENABLED", True)
    # How long stock is held for an unpaid order
    ONLINE_PAYMENT_HOLD_MINUTES = int(os.environ.get("ONLINE_PAYMENT_HOLD_MINUTES", "45"))
    PICKUP_HOLD_HOURS = int(os.environ.get("PICKUP_HOLD_HOURS", "48"))

    # Largest discount (% of subtotal) a cashier may give. 0 = no limit. Admins are never limited.
    CASHIER_MAX_DISCOUNT_PERCENT = float(os.environ.get("CASHIER_MAX_DISCOUNT_PERCENT", "0"))

    # --- Email (Aliyun Direct Mail or any SMTP service) ----------------------------------
    # Leave MAIL_SERVER empty during development: emails are then printed to the console and saved
    # in instance/outbox/ instead of being sent.
    MAIL_SERVER = os.environ.get("MAIL_SERVER", "")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", "465"))
    MAIL_USE_SSL = _env_bool("MAIL_USE_SSL", True)        # port 465
    MAIL_USE_TLS = _env_bool("MAIL_USE_TLS", False)       # STARTTLS (e.g. port 80/25/587), when SSL is off
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "")   # the sender address you created in Direct Mail
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "")   # the SMTP password set in the Direct Mail console
    # Preferred on Render: send through the Direct Mail HTTPS API (no SMTP ports needed). Used when the key id is set.
    ALIYUN_DM_ACCESS_KEY_ID = os.environ.get("ALIYUN_DM_ACCESS_KEY_ID", "")
    ALIYUN_DM_ACCESS_KEY_SECRET = os.environ.get("ALIYUN_DM_ACCESS_KEY_SECRET", "")
    ALIYUN_DM_REGION = os.environ.get("ALIYUN_DM_REGION", "singapore").strip().lower()   # hangzhou / singapore / sydney
    ALIYUN_DM_ENDPOINT = os.environ.get("ALIYUN_DM_ENDPOINT", "").strip()                 # optional override
    ALIYUN_DM_REPLY_TO = _env_bool("ALIYUN_DM_REPLY_TO", False)  # true only if a reply-to address is verified in the console
    MAIL_TIMEOUT = int(os.environ.get("MAIL_TIMEOUT", "20"))
    MAIL_FROM = os.environ.get("MAIL_FROM", "info@crowntechhub.co.zw")
    MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", os.environ.get("BUSINESS_NAME", "Crown Tech Hub"))
    EMAIL_LINK_HOURS_VERIFY = int(os.environ.get("EMAIL_LINK_HOURS_VERIFY", "24"))
    EMAIL_LINK_MINUTES_RESET = int(os.environ.get("EMAIL_LINK_MINUTES_RESET", "60"))

    # --- Sign in with Google (customers only) ----------------------------------------
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    # Full https callback URL, exactly as listed in Google Cloud Console, e.g.
    # https://www.crowntechhub.co.zw/account/google/callback
    GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "").strip()

    # --- Paynow ----------------------------------------------------------
    PAYNOW_INTEGRATION_ID = os.environ.get("PAYNOW_INTEGRATION_ID", "")
    PAYNOW_INTEGRATION_KEY = os.environ.get("PAYNOW_INTEGRATION_KEY", "")
    # While your Paynow integration is in test mode, Paynow only accepts your own
    # merchant login email as the payer email. Set this to that email while testing.
    PAYNOW_TEST_EMAIL = os.environ.get("PAYNOW_TEST_EMAIL", "")

    # --- PayPal ----------------------------------------------------------
    PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
    PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")
    PAYPAL_MODE = os.environ.get("PAYPAL_MODE", "sandbox")  # "sandbox" or "live"

    @classmethod
    def paynow_enabled(cls):
        return bool(cls.PAYNOW_INTEGRATION_ID and cls.PAYNOW_INTEGRATION_KEY)

    @classmethod
    def paypal_enabled(cls):
        return bool(cls.PAYPAL_CLIENT_ID and cls.PAYPAL_CLIENT_SECRET)
