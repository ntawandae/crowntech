"""
Turns the long, emoji-filled product names the shop already uses into a tidy
storefront title plus a short list of spec chips (CPU, RAM, storage...).
The database is not changed; this only affects how products are displayed.
"""
import re

# Pictographs, dingbats, flags, variation selectors, zero-width joiners...
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
    "\U0000FE00-\U0000FE0F\U0000200D\U00002700-\U000027BF\U0001F1E6-\U0001F1FF]+",
    flags=re.UNICODE,
)
_JUNK_SEPARATORS = re.compile(r"[✔✝➕✓×*]+")
_SPACES = re.compile(r"\s+")

CATEGORY_MAP = {
    "laptop": "Laptops",
    "gaming laptop": "Gaming laptops",
    "accessories": "Accessories",
    "phone": "Phones",
    "monitors": "Monitors",
    "monitor": "Monitors",
}
# Each category gets its own colour on the storefront (tiles, badges, image backdrops)
CATEGORY_TONE = {
    "Laptops": "blue",
    "Gaming laptops": "violet",
    "Phones": "teal",
    "Monitors": "green",
    "Accessories": "orange",
    "Other": "steel",
}
CATEGORY_ORDER = ["Laptops", "Gaming laptops", "Phones", "Monitors", "Accessories", "Other"]

_CUT_MARKERS = re.compile(
    r"\s(?:model\s|core\s|intel\s|ryzen\s|pentium\s|i[3579][\s\-:,]|\d+(?:st|nd|rd|th)\s?gen|"
    r"\d+\s?gb|\d+inch|\d+\.\d+inch|touch|✔|:)|[|,;—–]",
    flags=re.IGNORECASE,
)


def clean_text(text):
    text = _EMOJI.sub(" ", text or "")
    text = _JUNK_SEPARATORS.sub(" ", text)
    text = text.replace("”", '"').replace("“", '"')
    return _SPACES.sub(" ", text).strip(" -|,;:")


def normalize_category(raw):
    key = (raw or "").strip().lower()
    return CATEGORY_MAP.get(key, "Other" if not key else key.title())


def short_title(name, max_len=64):
    text = clean_text(name)
    match = _CUT_MARKERS.search(text)
    if match and match.start() >= 6:
        text = text[: match.start()].strip(" -|,;:")
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0]
    return text or clean_text(name)[:max_len]


def extract_specs(name):
    """Return up to 5 short spec chips found in the product text."""
    t = clean_text(name)
    chips = []

    cpu = (
        re.search(r"\b(?:core\s*)?(i[3579])[\s\-:,]*(?:(\d{1,2})\s*(?:st|nd|rd|th)?[\s\-]*gen)?", t, re.I)
    )
    ryzen = re.search(r"\bryzen\s*([3579])\b", t, re.I)
    apple = re.search(r"\b(?:core\s)?(m[1-4])\b", t, re.I)
    if ryzen:
        chips.append(f"Ryzen {ryzen.group(1)}")
    elif cpu:
        label = cpu.group(1).lower().replace("i", "Core i", 1)
        gen = cpu.group(2)
        if not gen:
            g2 = re.search(r"(\d{1,2})\s*(?:st|nd|rd|th)?\s*[- ]?gen(?:eration)?", t, re.I)
            gen = g2.group(1) if g2 else None
        chips.append(label + (f" · {_ordinal(gen)} gen" if gen else ""))
    elif apple:
        chips.append(f"Apple {apple.group(1).upper()}")
    elif re.search(r"pentium", t, re.I):
        chips.append("Pentium")

    ram = re.search(r"(\d{1,2})\s*gb\s*(?:ram|memory)|ram\s*(\d{1,2})\s*gb|(\d{1,2})\s*gb\s*\+", t, re.I)
    if ram:
        chips.append(f"{next(g for g in ram.groups() if g)} GB RAM")
    else:
        loose = re.search(r"\b(4|8|12|16|32|64)\s*gb\b", t, re.I)
        if loose and "laptop" not in (t.lower()[:0]):
            chips.append(f"{loose.group(1)} GB RAM")

    storage = (
        re.search(r"(?:ssd|nvme|hdd)\s*[:/]?\s*(\d{3,4}|\d)\s*(gb|tb)\b", t, re.I)
        or re.search(r"\b(\d{3,4})\s*(gb)?\s*(?:ssd|nvme|hdd)\b", t, re.I)
        or re.search(r"\b(\d)\s*(tb)\s*(?:ssd|nvme|hdd)\b", t, re.I)
        or re.search(r"\+\s*(\d{3,4})\s*(gb)\b", t, re.I)
    )
    if storage:
        num = storage.group(1)
        unit = (storage.group(2) or "GB").upper()
        chips.append(f"{num} {unit} SSD")

    if re.search(r"rtx\s?\d{4}|gtx\s?\d{4}", t, re.I):
        gpu = re.search(r"(rtx|gtx)\s?(\d{4})(?:\s?ti)?", t, re.I)
        chips.append(f"{gpu.group(1).upper()} {gpu.group(2)}")
    if re.search(r"touch", t, re.I):
        chips.append("Touchscreen")
    return chips[:5]


def _ordinal(n):
    n = int(n)
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"
