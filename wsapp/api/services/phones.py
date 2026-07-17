import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class PhoneResult:
    original: str
    normalized: str = ""
    status: str = "invalid"
    code: str = ""
    message: str = ""
    auto_corrected: bool = False


def normalize_tanzania_phone(value) -> PhoneResult:
    if value is None:
        return PhoneResult("", status="blank", code="blank", message="Phone is blank.")
    raw = str(value).strip().replace("\u00a0", " ")
    if not raw:
        return PhoneResult(raw, status="blank", code="blank", message="Phone is blank.")
    cleaned = re.sub(r"[\s\-()]", "", raw)
    if re.fullmatch(r"\d+\.0", cleaned):
        cleaned = cleaned[:-2]
    if re.search(r"[A-Za-z]", cleaned):
        return PhoneResult(raw, code="letters", message="Letters are not allowed.")
    if cleaned.count("+") > 1 or ("+" in cleaned and not cleaned.startswith("+")):
        return PhoneResult(raw, code="malformed_plus", message="Malformed plus sign.")
    if not re.fullmatch(r"\+?\d+", cleaned):
        return PhoneResult(raw, code="malformed", message="Only phone punctuation is allowed.")
    auto = False
    if cleaned.startswith("+"):
        if not cleaned.startswith("+255"):
            return PhoneResult(raw, code="country_code", message="Only Tanzania (+255) numbers are supported.")
        normalized = cleaned
    elif cleaned.startswith("255"):
        normalized, auto = f"+{cleaned}", True
    elif cleaned.startswith("0"):
        if len(cleaned) != 10:
            return PhoneResult(raw, code="length", message="A local number must contain 10 digits.")
        normalized, auto = f"+255{cleaned[1:]}", True
    elif len(cleaned) == 9:
        normalized, auto = f"+255{cleaned}", True
    else:
        return PhoneResult(raw, code="length", message="Expected +255 followed by exactly 9 digits.")
    if not re.fullmatch(r"\+255\d{9}", normalized):
        return PhoneResult(raw, code="length", message="Expected +255 followed by exactly 9 digits.")
    prefix = normalized[4]
    if prefix not in {"6", "7"}:
        return PhoneResult(raw, normalized, "warning", "non_mobile", "Valid Tanzania length, but not a known mobile prefix.", auto)
    return PhoneResult(raw, normalized, "valid", auto_corrected=auto)


def excel_value_to_text(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, Decimal):
        try:
            return format(value.quantize(Decimal(1)), "f") if value == value.to_integral() else format(value, "f")
        except InvalidOperation:
            pass
    return str(value)
