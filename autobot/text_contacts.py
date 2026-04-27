"""
URL и телефоны из текста ответов Алисы (без тяжёлых зависимостей).
Домены без схемы нормализуются к https:// для ссылок в отчётах.
"""

from __future__ import annotations

import re

URL_RE = re.compile(r"https?://[^\s<>\")']+", re.IGNORECASE)

PHONE_RE = re.compile(
    r"(?:\+?7|8)(?:[\s\-]?(?:\(?\d{3}\)?))(?:[\s\-]?\d{3}){1,2}[\s\-]?\d{2}[\s\-]?\d{2}"
    r"|(?:\+?7|8|7)(?:[\s\-]?\d){9,14}\d"
    r"|(?:\+?7|8)[\s\-]?(?:\(?\d{3}\)?)[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"
    r"|\b\d{3}[\s\-]\d{3}[\s\-]\d{2}[\s\-]\d{2}\b",
    re.IGNORECASE,
)

_DOMAIN_HOST_RE = re.compile(
    r"(?<![@\w/])(?:https?://)?(?:www\.)?"
    r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})(?::\d{1,5})?"
    r"(?:/[^\s)\]<>\"',;]*)?",
    re.IGNORECASE,
)


def normalize_http_url(s: str) -> str:
    u = (s or "").strip().strip("<>").rstrip(").,;\"'")
    if not u or "\n" in u or "\t" in u:
        return ""
    if re.match(r"^https?://", u, re.IGNORECASE):
        return u
    if re.match(r"^www\.", u, re.IGNORECASE):
        return "https://" + u
    if "." in u and ".." not in u and not u.startswith("."):
        if re.match(r"^[a-z0-9._\-/]+$", u, re.IGNORECASE):
            return "https://" + u.lstrip("/")
    return ""


def _host_key(url: str) -> str:
    u = re.sub(r"^https?://", "", (url or "").strip(), flags=re.IGNORECASE)
    return u.split("/")[0].lower()


def collect_urls(text: str, *, limit: int = 24) -> list[str]:
    if not text:
        return []
    t = str(text)
    full = list(dict.fromkeys(URL_RE.findall(t)))
    hosts = {_host_key(u) for u in full}
    out = list(full)
    for m in _DOMAIN_HOST_RE.finditer(t):
        raw = m.group(0).strip().rstrip(").,;\"'")
        norm = normalize_http_url(raw)
        if not norm:
            continue
        hk = _host_key(norm)
        if hk in hosts:
            continue
        if any(norm.startswith(u.rstrip("/") + "/") or u.startswith(norm.rstrip("/") + "/") for u in full):
            continue
        hosts.add(hk)
        out.append(norm)
        if len(out) >= limit:
            break
    return out[:limit]


def collect_phones(text: str, *, limit: int = 24) -> list[str]:
    if not text:
        return []
    raw = PHONE_RE.findall(str(text))
    seen: set[str] = set()
    out: list[str] = []
    for p in raw:
        n = re.sub(r"\s+", " ", (p or "").strip())
        digits = re.sub(r"\D", "", n)
        if len(digits) < 10:
            continue
        key = digits[-10:] if len(digits) >= 10 else digits
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
        if len(out) >= limit:
            break
    return out
