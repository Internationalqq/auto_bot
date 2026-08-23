"""Persistent, auditable price evidence shared between tenders.

The index stores only verified direct-source offers.  It deliberately keeps
position identity separate from the original estimate wording so the same
evidence can be reused for a compatible position in another tender.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import sqlite3
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from autobot.market_strategy import classify_position, market_query_name, normalize_unit
from autobot.paths import REPO_ROOT


INDEX_ROOT = REPO_ROOT / "data" / "market_index"
INDEX_DB = INDEX_ROOT / "market_prices.sqlite3"
AUDIT_ROOT = INDEX_ROOT / "audit"

_STOP_WORDS = {
    "для", "при", "под", "над", "без", "или", "как", "что", "это", "из", "от",
    "до", "по", "на", "во", "со", "за", "ед", "изм", "цена", "стоимость", "купить",
    "работ", "работы", "услуг", "услуги", "устройство", "установка", "выполнение",
}
_SUFFIXES = (
    "иями", "ями", "ами", "ого", "ему", "ому", "ыми", "ими", "ая", "яя", "ое", "ее",
    "ый", "ий", "ой", "ых", "их", "ам", "ям", "ах", "ях", "ов", "ев", "ей", "ом",
    "ем", "ую", "юю", "а", "я", "ы", "и", "у", "ю", "е", "о",
)
_KNOWN_RETAILERS = (
    "petrovich.ru", "vseinstrumenti.ru", "lemanapro.ru", "220-volt.ru", "leroymerlin.ru",
)
_MARKETPLACES = ("wildberries.ru", "wb.ru", "ozon.ru", "market.yandex.ru")
_AGGREGATORS = ("profi.ru", "youdo.com", "avito.ru")


@dataclass(frozen=True)
class PriceIdentity:
    normalized_key: str
    category_key: str
    category_tokens: tuple[str, ...]
    unit: str
    bucket: str
    position_type: str
    brand_model: str
    query_name: str


def _clean(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return "" if text.casefold() in {"nan", "none", "nat", "<na>"} else text


def _stem(token: str) -> str:
    folded = token.casefold().replace("ё", "е")
    if len(folded) <= 4:
        return folded
    for suffix in _SUFFIXES:
        if len(folded) - len(suffix) >= 4 and folded.endswith(suffix):
            return folded[: -len(suffix)]
    return folded


def _identity_tokens(value: object) -> tuple[str, ...]:
    words = re.findall(r"[0-9a-zа-яё][0-9a-zа-яё.+/-]{2,}", _clean(value).casefold())
    tokens = {_stem(word.strip(".+/-")) for word in words}
    return tuple(sorted(token for token in tokens if token and token not in _STOP_WORDS and not token.isdigit()))


def _brand_model(value: object, *, bucket: str) -> str:
    if bucket != "materials":
        return ""
    text = _clean(value)
    candidates = re.findall(r"\b(?=[0-9A-Za-zА-Яа-я-]*\d)(?=[0-9A-Za-zА-Яа-я-]*[A-Za-zА-Яа-я])[0-9A-Za-zА-Яа-я-]{3,}\b", text)
    latin = re.findall(r"\b[A-Za-z][A-Za-z0-9-]{2,}\b", text)
    return " ".join(dict.fromkeys(item.casefold() for item in candidates + latin))[:180]


def build_price_identity(name: object, unit: object = "", basis_code: object = "", section: object = "") -> PriceIdentity:
    query_name = market_query_name(name)
    position = classify_position(name, unit, basis_code, section)
    tokens = _identity_tokens(query_name)
    category_key = "-".join(tokens)
    unit_norm = normalize_unit(unit)
    brand = _brand_model(name, bucket=position.bucket)
    readable = "|".join((position.bucket, position.slug, unit_norm, category_key, brand))
    normalized_key = hashlib.sha256(readable.encode("utf-8")).hexdigest()[:32]
    return PriceIdentity(
        normalized_key=normalized_key,
        category_key=category_key,
        category_tokens=tokens,
        unit=unit_norm,
        bucket=position.bucket,
        position_type=position.slug,
        brand_model=brand,
        query_name=query_name,
    )


def source_quality(url: object, source: object = "") -> float:
    host = urlparse(_clean(url)).netloc.casefold().split(":", 1)[0]
    label = _clean(source).casefold()
    overrides_raw = (os.environ.get("MARKET_SOURCE_WEIGHT_OVERRIDES") or "").strip()
    if overrides_raw:
        try:
            overrides = json.loads(overrides_raw)
        except (TypeError, ValueError):
            overrides = {}
        if isinstance(overrides, dict):
            for domain, value in overrides.items():
                domain_text = _clean(domain).casefold().lstrip(".")
                if domain_text and (host == domain_text or host.endswith("." + domain_text)):
                    try:
                        return max(0.05, min(1.0, float(value)))
                    except (TypeError, ValueError):
                        break
    if host.endswith(".gov.ru") or host == "gov.ru":
        return 0.96
    if any(host == root or host.endswith("." + root) for root in _KNOWN_RETAILERS):
        return 0.90
    if any(host == root or host.endswith("." + root) for root in _MARKETPLACES):
        return 0.76
    if "avito" in label or host.endswith("avito.ru"):
        return 0.52
    if any(host == root or host.endswith("." + root) for root in _AGGREGATORS):
        return 0.64
    if urlparse(_clean(url)).scheme == "https":
        return 0.78
    return 0.66


def weighted_median(values: list[tuple[float, float]]) -> float | None:
    clean = sorted(
        (float(value), max(0.01, float(weight)))
        for value, weight in values
        if value is not None and weight is not None and math.isfinite(float(value)) and float(value) > 0
    )
    if not clean:
        return None
    total_weight = sum(weight for _, weight in clean)
    cursor = 0.0
    for value, weight in clean:
        cursor += weight
        if cursor >= total_weight / 2:
            return value
    return clean[-1][0]


def _connect() -> sqlite3.Connection:
    INDEX_ROOT.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(INDEX_DB, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS market_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            normalized_key TEXT NOT NULL,
            category_key TEXT NOT NULL,
            category_tokens_json TEXT NOT NULL,
            unit TEXT NOT NULL,
            bucket TEXT NOT NULL,
            position_type TEXT NOT NULL,
            brand_model TEXT NOT NULL DEFAULT '',
            original_position TEXT NOT NULL,
            tender_id TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL,
            source_host TEXT NOT NULL,
            title TEXT NOT NULL,
            price REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'RUB',
            url TEXT NOT NULL,
            confidence REAL NOT NULL,
            source_weight REAL NOT NULL,
            verification TEXT NOT NULL,
            observed_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            audit_record_path TEXT NOT NULL DEFAULT '',
            snapshot_path TEXT NOT NULL DEFAULT '',
            snapshot_sha256 TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(normalized_key, url)
        );
        CREATE INDEX IF NOT EXISTS idx_market_prices_lookup
            ON market_prices(bucket, unit, expires_at, verification);
        CREATE TABLE IF NOT EXISTS price_summaries (
            normalized_key TEXT PRIMARY KEY,
            min_price REAL NOT NULL,
            max_price REAL NOT NULL,
            median_price REAL NOT NULL,
            weighted_median_price REAL NOT NULL,
            source_count INTEGER NOT NULL,
            total_weight REAL NOT NULL,
            expires_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS parser_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tender_id TEXT NOT NULL,
            sources TEXT NOT NULL,
            total_rows INTEGER NOT NULL,
            processed_rows INTEGER NOT NULL,
            rows_with_offers INTEGER NOT NULL,
            verified_rows INTEGER NOT NULL,
            candidate_rows INTEGER NOT NULL,
            error_rows INTEGER NOT NULL,
            offer_rate REAL NOT NULL,
            baseline_rate REAL,
            degraded INTEGER NOT NULL DEFAULT 0,
            duration_sec REAL NOT NULL,
            created_at REAL NOT NULL
        );
        """
    )
    return connection


def _iso_timestamp(value: object) -> float:
    text = _clean(value)
    if text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time()


def _ttl_days(identity: PriceIdentity, url: str) -> int:
    configured = os.environ.get("MARKET_INDEX_TTL_DAYS", "").strip()
    if configured:
        try:
            return max(1, int(configured))
        except ValueError:
            pass
    host = urlparse(url).netloc.casefold()
    if "avito.ru" in host:
        return 14
    if identity.bucket == "materials":
        return 30
    if identity.bucket == "works":
        return 60
    return 45


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _audit_snapshot(*, url: str, page_html: str, metadata: dict) -> tuple[str, str, str]:
    observed = float(metadata.get("observed_at") or time.time())
    instant = datetime.fromtimestamp(observed, tz=timezone.utc)
    html_bytes = page_html.encode("utf-8", errors="replace") if page_html else b""
    snapshot_sha = hashlib.sha256(html_bytes).hexdigest() if html_bytes else ""
    snapshot_path = ""
    if html_bytes:
        blob_path = AUDIT_ROOT / "blobs" / snapshot_sha[:2] / f"{snapshot_sha}.html.gz"
        if not blob_path.is_file():
            blob_path.parent.mkdir(parents=True, exist_ok=True)
            temp = blob_path.with_suffix(".tmp")
            with gzip.open(temp, "wb", compresslevel=6) as stream:
                stream.write(html_bytes)
            temp.replace(blob_path)
        snapshot_path = str(blob_path.relative_to(REPO_ROOT))
    record_basis = json.dumps({**metadata, "url": url, "snapshot_sha256": snapshot_sha}, ensure_ascii=False, sort_keys=True)
    record_id = hashlib.sha256(record_basis.encode("utf-8")).hexdigest()[:24]
    record_path = AUDIT_ROOT / "records" / instant.strftime("%Y") / instant.strftime("%m") / f"{record_id}.json"
    if not record_path.is_file():
        _write_json(
            record_path,
            {
                **metadata,
                "url": url,
                "snapshot_path": snapshot_path,
                "snapshot_sha256": snapshot_sha,
                "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        )
    return str(record_path.relative_to(REPO_ROOT)), snapshot_path, snapshot_sha


def _refresh_summary(connection: sqlite3.Connection, normalized_key: str, now: float) -> None:
    rows = connection.execute(
        """SELECT price, confidence, source_weight, expires_at FROM market_prices
           WHERE normalized_key=? AND verification='verified' AND expires_at>?""",
        (normalized_key, now),
    ).fetchall()
    if not rows:
        connection.execute("DELETE FROM price_summaries WHERE normalized_key=?", (normalized_key,))
        return
    prices = [float(row["price"]) for row in rows]
    weighted = [(float(row["price"]), float(row["confidence"]) * float(row["source_weight"])) for row in rows]
    weighted_value = weighted_median(weighted) or statistics.median(prices)
    connection.execute(
        """INSERT INTO price_summaries
           (normalized_key,min_price,max_price,median_price,weighted_median_price,source_count,total_weight,expires_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(normalized_key) DO UPDATE SET
             min_price=excluded.min_price,max_price=excluded.max_price,median_price=excluded.median_price,
             weighted_median_price=excluded.weighted_median_price,source_count=excluded.source_count,
             total_weight=excluded.total_weight,expires_at=excluded.expires_at,updated_at=excluded.updated_at""",
        (
            normalized_key,
            min(prices),
            max(prices),
            statistics.median(prices),
            weighted_value,
            len(prices),
            sum(weight for _, weight in weighted),
            min(float(row["expires_at"]) for row in rows),
            now,
        ),
    )


def record_verified_offers(
    *,
    tender_id: str,
    name: object,
    unit: object,
    basis_code: object = "",
    section: object = "",
    offers: list[dict],
) -> int:
    identity = build_price_identity(name, unit, basis_code, section)
    if not identity.unit or identity.bucket not in {"works", "materials"}:
        return 0
    now = time.time()
    stored = 0
    with _connect() as connection:
        for offer in offers:
            if _clean(offer.get("verification")).casefold() != "verified":
                continue
            url = _clean(offer.get("url"))
            try:
                price = float(offer.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if not url or price <= 0:
                continue
            observed = _iso_timestamp(offer.get("observed_at"))
            ttl_days = _ttl_days(identity, url)
            quality = source_quality(url, offer.get("source"))
            try:
                confidence = max(0.05, min(1.0, float(offer.get("confidence") or 0.5)))
            except (TypeError, ValueError):
                confidence = 0.5
            audit_record, snapshot_path, snapshot_sha = _audit_snapshot(
                url=url,
                page_html=str(offer.get("page_html") or ""),
                metadata={
                    "tender_id": _clean(tender_id),
                    "normalized_key": identity.normalized_key,
                    "identity": asdict(identity),
                    "original_position": _clean(name),
                    "title": _clean(offer.get("title")),
                    "source": _clean(offer.get("source")),
                    "price": price,
                    "currency": "RUB",
                    "confidence": confidence,
                    "source_weight": quality,
                    "observed_at": observed,
                    "expires_at": observed + ttl_days * 86400,
                },
            )
            host = urlparse(url).netloc.casefold().split(":", 1)[0]
            connection.execute(
                """INSERT INTO market_prices
                   (normalized_key,category_key,category_tokens_json,unit,bucket,position_type,brand_model,
                    original_position,tender_id,source,source_host,title,price,currency,url,confidence,
                    source_weight,verification,observed_at,expires_at,audit_record_path,snapshot_path,
                    snapshot_sha256,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(normalized_key,url) DO UPDATE SET
                     original_position=excluded.original_position,tender_id=excluded.tender_id,
                     source=excluded.source,source_host=excluded.source_host,title=excluded.title,
                     price=excluded.price,confidence=excluded.confidence,source_weight=excluded.source_weight,
                     verification=excluded.verification,observed_at=excluded.observed_at,
                     expires_at=excluded.expires_at,audit_record_path=excluded.audit_record_path,
                     snapshot_path=excluded.snapshot_path,snapshot_sha256=excluded.snapshot_sha256,
                     updated_at=excluded.updated_at""",
                (
                    identity.normalized_key,
                    identity.category_key,
                    json.dumps(identity.category_tokens, ensure_ascii=False),
                    identity.unit,
                    identity.bucket,
                    identity.position_type,
                    identity.brand_model,
                    _clean(name),
                    _clean(tender_id),
                    _clean(offer.get("source")) or host,
                    host,
                    _clean(offer.get("title")),
                    price,
                    "RUB",
                    url,
                    confidence,
                    quality,
                    "verified",
                    observed,
                    observed + ttl_days * 86400,
                    audit_record,
                    snapshot_path,
                    snapshot_sha,
                    now,
                    now,
                ),
            )
            stored += 1
        _refresh_summary(connection, identity.normalized_key, now)
    return stored


def lookup_verified_offers(
    *,
    name: object,
    unit: object,
    basis_code: object = "",
    section: object = "",
    limit: int = 5,
) -> list[dict]:
    identity = build_price_identity(name, unit, basis_code, section)
    if not identity.unit or identity.bucket not in {"works", "materials"}:
        return []
    now = time.time()
    with _connect() as connection:
        rows = connection.execute(
            """SELECT * FROM market_prices
               WHERE bucket=? AND unit=? AND verification='verified' AND expires_at>?
               ORDER BY updated_at DESC LIMIT 250""",
            (identity.bucket, identity.unit, now),
        ).fetchall()
    wanted = set(identity.category_tokens)
    matches: list[tuple[float, dict]] = []
    for row in rows:
        try:
            saved_tokens = set(json.loads(row["category_tokens_json"] or "[]"))
        except (TypeError, ValueError):
            saved_tokens = set()
        if row["normalized_key"] == identity.normalized_key:
            similarity = 1.0
        else:
            common = wanted & saved_tokens
            similarity = len(common) / max(1, len(wanted | saved_tokens))
            if len(common) < 2 or similarity < 0.55:
                continue
            if identity.position_type != row["position_type"] and identity.bucket == "works":
                continue
            saved_brand = set(_identity_tokens(row["brand_model"]))
            wanted_brand = set(_identity_tokens(identity.brand_model))
            if saved_brand and wanted_brand and not (saved_brand & wanted_brand):
                continue
        payload = dict(row)
        payload["match_score"] = round(similarity, 4)
        payload["index_hit"] = True
        matches.append((similarity * float(row["source_weight"]) * float(row["confidence"]), payload))
    matches.sort(key=lambda item: (-item[0], -float(item[1]["observed_at"])))
    return [payload for _, payload in matches[: max(1, limit)]]


def summary_for_position(name: object, unit: object = "", basis_code: object = "", section: object = "") -> dict:
    identity = build_price_identity(name, unit, basis_code, section)
    with _connect() as connection:
        row = connection.execute("SELECT * FROM price_summaries WHERE normalized_key=?", (identity.normalized_key,)).fetchone()
    return dict(row) if row else {}


def record_parser_run(
    *,
    tender_id: str,
    sources: list[str],
    total_rows: int,
    processed_rows: int,
    rows_with_offers: int,
    verified_rows: int,
    candidate_rows: int,
    error_rows: int,
    duration_sec: float,
) -> dict:
    sources_key = ",".join(sorted(set(sources)))
    offer_rate = rows_with_offers / max(1, processed_rows)
    now = time.time()
    with _connect() as connection:
        previous = connection.execute(
            """SELECT offer_rate FROM parser_runs
               WHERE sources=? AND processed_rows>=10 ORDER BY created_at DESC LIMIT 10""",
            (sources_key,),
        ).fetchall()
        baseline = statistics.mean(float(row["offer_rate"]) for row in previous) if previous else None
        degraded = bool(
            processed_rows >= 10
            and baseline is not None
            and baseline >= 0.50
            and offer_rate < baseline * 0.60
        )
        cursor = connection.execute(
            """INSERT INTO parser_runs
               (tender_id,sources,total_rows,processed_rows,rows_with_offers,verified_rows,candidate_rows,
                error_rows,offer_rate,baseline_rate,degraded,duration_sec,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                _clean(tender_id), sources_key, int(total_rows), int(processed_rows), int(rows_with_offers),
                int(verified_rows), int(candidate_rows), int(error_rows), float(offer_rate), baseline,
                1 if degraded else 0, float(duration_sec), now,
            ),
        )
        run_id = int(cursor.lastrowid)
    return {
        "id": run_id,
        "tender_id": _clean(tender_id),
        "sources": sources_key,
        "processed_rows": int(processed_rows),
        "rows_with_offers": int(rows_with_offers),
        "offer_rate": round(offer_rate, 4),
        "baseline_rate": round(baseline, 4) if baseline is not None else None,
        "degraded": degraded,
        "created_at": now,
    }


def latest_parser_health(tender_id: str = "") -> dict:
    query = "SELECT * FROM parser_runs"
    params: tuple = ()
    if _clean(tender_id):
        query += " WHERE tender_id=?"
        params = (_clean(tender_id),)
    query += " ORDER BY created_at DESC LIMIT 1"
    with _connect() as connection:
        row = connection.execute(query, params).fetchone()
    return dict(row) if row else {}


def prune_expired(*, retention_days: int = 365) -> int:
    threshold = time.time() - max(30, int(retention_days)) * 86400
    with _connect() as connection:
        cursor = connection.execute("DELETE FROM market_prices WHERE expires_at<?", (threshold,))
        removed = int(cursor.rowcount or 0)
    return removed
