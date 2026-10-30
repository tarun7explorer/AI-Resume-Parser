"""Local-only extraction of contact details and profile URLs; no URL fetches."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from app.report_models import ProfileLink

EMAIL_RE = re.compile(r"(?i)\b[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\b")
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d().\s-]{6,20}\d)(?!\w)")
URL_RE = re.compile(r"(?i)\b(?:https?://)?(?:www\.)?[a-z0-9.-]+\.[a-z]{2,}(?:/[a-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)?")
HOST_MARKERS = {
    "linkedin": ("linkedin.com",),
    "github": ("github.com",),
}


def extract_contact(text: str) -> dict[str, str | None]:
    emails = []
    for value in EMAIL_RE.findall(text):
        candidate = value.strip(".,;:()<>[]{}'\"").lower()
        if len(candidate) <= 254 and candidate not in emails:
            emails.append(candidate)
    phone = None
    for candidate in PHONE_RE.findall(text):
        digits = re.sub(r"\D", "", candidate)
        normalized = re.sub(r"\s+", " ", candidate).strip()
        if not 7 <= len(digits) <= 15 or len(set(digits)) <= 1:
            continue
        # Avoid common year ranges and accept short national numbers only when
        # formatting provides additional phone-like structure.
        if re.fullmatch(r"(?:19|20)\d{2}\s*[-/]\s*(?:19|20)\d{2}", normalized):
            continue
        if len(digits) < 10 and not any(mark in normalized for mark in ("+", "(", ")", ".")):
            continue
        phone = normalized
        break
    return {"email": emails[0] if emails else None, "phone": phone}


def _platform(host: str) -> str:
    normalized = host.lower().removeprefix("www.").rstrip(".")
    if normalized == "linkedin.com" or normalized.endswith(".linkedin.com"):
        return "linkedin"
    if normalized == "github.com" or normalized.endswith(".github.com"):
        return "github"
    return "portfolio"


def extract_profile_links(text: str) -> list[ProfileLink]:
    found: dict[str, str] = {}
    malformed_markers: set[str] = set()
    for raw in URL_RE.findall(text):
        cleaned = raw.rstrip(".,;:!?)]}\"")
        candidate = cleaned if cleaned.lower().startswith(("http://", "https://")) else f"https://{cleaned}"
        try:
            parsed = urlsplit(candidate)
            host = parsed.hostname or ""
            if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
                if "linkedin" in cleaned.lower(): malformed_markers.add("linkedin")
                if "github" in cleaned.lower(): malformed_markers.add("github")
                continue
            platform = _platform(host)
            # Strip fragments and tracking query strings; never request the URL.
            safe_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"[:2048]
            found.setdefault(platform, safe_url)
        except ValueError:
            if "linkedin" in cleaned.lower(): malformed_markers.add("linkedin")
            if "github" in cleaned.lower(): malformed_markers.add("github")

    text_lower = text.lower()
    if "linkedin.com" in text_lower and "linkedin" not in found:
        malformed_markers.add("linkedin")
    if "github.com" in text_lower and "github" not in found:
        malformed_markers.add("github")

    result = []
    for platform in ("linkedin", "github", "portfolio"):
        if platform in found:
            result.append(ProfileLink(platform=platform, status="found", url=found[platform]))
        elif platform in malformed_markers:
            result.append(ProfileLink(platform=platform, status="malformed", url=None))
        else:
            result.append(ProfileLink(platform=platform, status="not_detected", url=None))
    return result
