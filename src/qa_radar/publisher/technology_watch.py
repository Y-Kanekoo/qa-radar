"""Official-release technology watch layered on the existing qa-radar DB.

Only short public metadata already stored by the crawler is read. The release
itself remains the primary source; claims from it are labelled as announcements.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

import httpx
import yaml

from qa_radar.crawler.normalize import normalize_url

WATCH_CHANNEL = "discord-technology-watch"
DEFAULT_PROFILES_PATH = Path(__file__).resolve().parents[3] / "config" / "technology_watch.yaml"
MAX_DIGEST_ITEMS = 5
LOOKBACK_SECONDS = 7 * 24 * 3600
PENDING_TTL_SECONDS = 30 * 24 * 3600

_NEW = re.compile(r"\b(initial release|first (public )?release|public launch)\b", re.I)
_PRERELEASE = re.compile(r"\b(alpha|beta|rc|canary|nightly|preview|dev)\b|\d+a\d+\.dev\d+", re.I)
_MAJOR_VERSION = re.compile(r"\bv?[1-9]\d*\.0\.0\b", re.I)
_IMPORTANT = re.compile(r"\b(breaking|security|critical|major release|cve-\d+|migration)\b", re.I)
_FIX = re.compile(r"\b(bug fixes?|fix(?:ed|es)?)\b", re.I)
_FEATURE = re.compile(
    r"\b(feat(?:ure)?s?|added?|introduc(?:ed|es|ing)|support(?:s|ed)?|"
    r"includes?|new)\b",
    re.I,
)
_QA_CHANGE = re.compile(
    r"\bbrowser(?:_|\b)|\b(playwright|test(?:s|ing)?|eval(?:uation)?|agent|mcp|"
    r"accessibility|vision|screenshot|recording|regression|ci|model)\b",
    re.I,
)


@dataclass(frozen=True)
class Profile:
    slug: str
    name: str
    dedicated_only: bool
    capability: str
    qa_use: str
    try_now: str
    limitations: str
    maturity: str
    license: str
    license_url: str
    pricing: str
    pricing_url: str
    docs_url: str


@dataclass(frozen=True)
class Candidate:
    article_ids: tuple[int, ...]
    profile: Profile
    title: str
    url: str
    snippet: str
    published_at: int
    kind: str
    priority: int
    queued: bool


def load_profiles(path: Path = DEFAULT_PROFILES_PATH) -> dict[str, Profile]:
    """Load a small, reviewed catalog of official projects and primary links."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("technology_watch.yaml: unsupported version")
    raw = data.get("profiles")
    if not isinstance(raw, dict) or not raw:
        raise ValueError("technology_watch.yaml: profiles are required")
    fields = tuple(
        name for name in Profile.__dataclass_fields__ if name not in {"slug", "dedicated_only"}
    )
    profiles: dict[str, Profile] = {}
    for slug, record in raw.items():
        if not isinstance(slug, str) or not isinstance(record, dict):
            raise ValueError("technology_watch.yaml: invalid profile")
        values = {field: record.get(field) for field in fields}
        if any(not isinstance(value, str) or not value.strip() for value in values.values()):
            raise ValueError(f"technology_watch.yaml: incomplete profile {slug}")
        dedicated_only = record.get("dedicated_only")
        if not isinstance(dedicated_only, bool):
            raise ValueError(f"technology_watch.yaml: {slug}.dedicated_only must be boolean")
        for field in ("license_url", "pricing_url", "docs_url"):
            if urlparse(values[field]).scheme != "https":
                raise ValueError(f"technology_watch.yaml: {slug}.{field} must be HTTPS")
        profiles[slug] = Profile(slug=slug, dedicated_only=dedicated_only, **values)
    return profiles


def classify_release(title: str, snippet: str) -> tuple[str, int] | None:
    """Prefer evidence of a launch or meaningful change over patch-version noise."""
    if _PRERELEASE.search(title):
        return None
    text = f"{title} {snippet}"
    if _NEW.search(text):
        return ("新規公開", 3)
    if _IMPORTANT.search(text) or _MAJOR_VERSION.search(title):
        return ("重要リリース", 2)
    if _FIX.search(text) and _QA_CHANGE.search(text):
        return ("QA関連修正", 1)
    if _FEATURE.search(text) and _QA_CHANGE.search(text):
        return ("機能追加", 1)
    return None


def select_candidates(
    conn: sqlite3.Connection,
    profiles: dict[str, Profile],
    *,
    now: int | None = None,
    limit: int = MAX_DIGEST_ITEMS,
) -> tuple[list[Candidate], int]:
    """Queue all eligible releases before applying the daily digest limit.

    The ordinary Discord channel is checked too, so enabling the new destination
    does not replay a release that already reached the old one. Pending items
    persist beyond the seven-day discovery window, then expire visibly at 30 days.
    """
    if limit < 1 or limit > MAX_DIGEST_ITEMS:
        raise ValueError(f"limit must be 1..{MAX_DIGEST_ITEMS}")
    if not profiles:
        return ([], 0)
    current = int(time.time()) if now is None else now
    with conn:
        conn.execute(
            """
            UPDATE technology_delivery_attempts
            SET last_status = 'expired', excluded_reason = 'pending_ttl_30d'
            WHERE expires_at <= ? AND excluded_reason IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM article_notifications n
                WHERE n.article_id = technology_delivery_attempts.article_id
                  AND n.channel = ?
              )
            """,
            (current, WATCH_CHANNEL),
        )
    placeholders = ", ".join("?" for _ in profiles)
    rows = conn.execute(
        f"""
        SELECT a.id, a.url, a.title, a.snippet, a.published_at, s.slug,
               a.fetched_at, t.article_id AS pending_id, s.feed_url
        FROM articles a
        JOIN sources s ON s.id = a.source_id
        LEFT JOIN technology_delivery_attempts t ON t.article_id = a.id
        WHERE s.slug IN ({placeholders}) AND a.duplicate_of IS NULL
          AND (a.fetched_at >= ? OR (t.article_id IS NOT NULL AND t.expires_at > ?))
          AND (t.article_id IS NULL OR (t.excluded_reason IS NULL AND t.expires_at > ?))
          AND NOT EXISTS (
              SELECT 1 FROM article_notifications n
              WHERE n.article_id = a.id
                AND n.channel IN (?, 'discord')
          )
        ORDER BY (t.article_id IS NOT NULL) DESC, a.published_at DESC, a.id DESC
        """,
        (*profiles, current - LOOKBACK_SECONDS, current, current, WATCH_CHANNEL),
    ).fetchall()
    by_url: dict[str, Candidate] = {}
    pending_to_insert: list[tuple[int, int]] = []
    for row in rows:
        slug = str(row["slug"])
        url = normalize_url(str(row["url"]))
        official_root = str(row["feed_url"]).removesuffix("/releases.atom")
        if not url.startswith(official_root + "/releases/tag/"):
            continue
        title = str(row["title"])
        snippet = str(row["snippet"])
        classification = classify_release(title, snippet)
        if classification is None:
            continue
        pending_to_insert.append((int(row["id"]), int(row["fetched_at"]) + PENDING_TTL_SECONDS))
        key = url.lower()
        existing = by_url.get(key)
        if existing is not None:
            by_url[key] = Candidate(
                article_ids=(*existing.article_ids, int(row["id"])),
                profile=existing.profile,
                title=existing.title,
                url=existing.url,
                snippet=existing.snippet,
                published_at=existing.published_at,
                kind=existing.kind,
                priority=existing.priority,
                queued=existing.queued or row["pending_id"] is not None,
            )
            continue
        kind, priority = classification
        by_url[key] = Candidate(
            article_ids=(int(row["id"]),),
            profile=profiles[slug],
            title=title,
            url=url,
            snippet=snippet,
            published_at=int(row["published_at"]),
            kind=kind,
            priority=priority,
            queued=row["pending_id"] is not None,
        )
    with conn:
        conn.executemany(
            """
            INSERT OR IGNORE INTO technology_delivery_attempts
                (article_id, attempts, last_attempted_at, last_status, expires_at)
            VALUES (?, 0, 0, 'pending', ?)
            """,
            pending_to_insert,
        )
    selected = sorted(
        by_url.values(),
        key=lambda item: (item.queued, item.priority, item.published_at),
        reverse=True,
    )
    return selected[:limit], max(0, len(selected) - limit)


def build_payload(candidates: list[Candidate], deferred: int = 0) -> dict[str, object]:
    """Build one Discord request from up to five items; never include full bodies."""
    if not candidates or len(candidates) > MAX_DIGEST_ITEMS:
        raise ValueError("digest requires 1..5 candidates")
    embeds: list[dict[str, object]] = []
    for item in candidates:
        profile = item.profile
        difference = item.snippet[:100] or "公式リリースノートで差分を確認"
        embeds.append(
            {
                "title": f"{profile.name}: {item.title}"[:256],
                "url": item.url,
                "description": (
                    f"**区分:** {item.kind} / 公式告知・動作未検証\n"
                    f"**何ができるか:** {profile.capability}\n"
                    f"**QA用途:** {profile.qa_use}\n"
                    f"**今回の差分（公式告知冒頭）:** {difference}\n"
                    f"**すぐ試せるか:** {profile.try_now}\n"
                    f"**制限・成熟度:** {profile.limitations} {profile.maturity}\n"
                    f"**ライセンス:** [{profile.license}]({profile.license_url}) / "
                    f"**料金:** [{profile.pricing}]({profile.pricing_url})\n"
                    f"**一次情報:** [リリース]({item.url}) · "
                    f"[公式ドキュメント]({profile.docs_url})"
                )[:4000],
                "color": 0x2DA44E,
            }
        )
    suffix = f"（残り{deferred}件は次回へ）" if deferred else ""
    return {
        "content": f"QA新技術ウォッチ: 公式リリース {len(candidates)}件{suffix}",
        "embeds": embeds,
        "allowed_mentions": {"parse": []},
    }


@dataclass(frozen=True)
class DeliveryResult:
    state: str  # confirmed | retryable | uncertain
    status: str
    message_id: str | None = None


@contextmanager
def _quiet_transport_logs():
    """HTTPX INFO records include the full webhook URL, so suppress them."""
    loggers = [logging.getLogger(name) for name in ("httpx", "httpcore")]
    previous = [logger.level for logger in loggers]
    try:
        for logger in loggers:
            if logger.getEffectiveLevel() < logging.WARNING:
                logger.setLevel(logging.WARNING)
        yield
    finally:
        for logger, level in zip(loggers, previous, strict=True):
            logger.setLevel(level)


def _confirmed_url(webhook_url: str) -> str:
    parts = urlsplit(webhook_url)
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError("invalid webhook URL")
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "wait"
    ]
    query.append(("wait", "true"))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def send_payload(
    webhook_url: str,
    payload: dict[str, object],
    *,
    client: httpx.Client | None = None,
) -> DeliveryResult:
    """Confirm persistence with wait=true and a Discord message ID.

    Uncertain outcomes are held for manual reconciliation rather than retried
    automatically, because a timeout or malformed success may already be saved.
    """
    try:
        endpoint = _confirmed_url(webhook_url)
    except ValueError:
        return DeliveryResult("retryable", "invalid_webhook_url")
    owned = client is None
    used = client if client is not None else httpx.Client(timeout=10, follow_redirects=False)
    try:
        with _quiet_transport_logs():
            for attempt in range(2):
                try:
                    response = used.post(endpoint, json=payload)
                except httpx.ConnectError:
                    return DeliveryResult("retryable", "connect_error")
                except httpx.InvalidURL:
                    return DeliveryResult("retryable", "invalid_webhook_url")
                except httpx.HTTPError:
                    return DeliveryResult("uncertain", "network_uncertain")
                status = response.status_code
                if 200 <= status < 300:
                    try:
                        body = response.json()
                    except ValueError:
                        body = None
                    message_id = body.get("id") if isinstance(body, dict) else None
                    if isinstance(message_id, str) and message_id.isdecimal():
                        return DeliveryResult("confirmed", f"http_{status}", message_id)
                    return DeliveryResult("uncertain", f"unconfirmed_http_{status}")
                if status == 429 and attempt == 0:
                    try:
                        delay = float(response.headers.get("Retry-After", "1"))
                    except ValueError:
                        delay = 1.0
                    time.sleep(min(5.0, max(0.5, delay)))
                    continue
                if status >= 500:
                    return DeliveryResult("uncertain", f"http_{status}")
                return DeliveryResult("retryable", f"http_{status}")
            return DeliveryResult("retryable", "http_429")
    finally:
        if owned:
            used.close()


def record_delivery(
    conn: sqlite3.Connection,
    candidates: list[Candidate],
    *,
    outcome: DeliveryResult,
    now: int | None = None,
) -> None:
    """Record a whole digest outcome and its per-article success in one commit."""
    if outcome.state not in {"confirmed", "retryable", "uncertain"}:
        raise ValueError("unknown delivery state")
    if outcome.state == "confirmed" and not outcome.message_id:
        raise ValueError("confirmed delivery requires a message ID")
    timestamp = int(time.time()) if now is None else now
    ids = [article_id for item in candidates for article_id in item.article_ids]
    exclusion = "manual_reconciliation" if outcome.state == "uncertain" else None
    with conn:
        conn.executemany(
            """
            INSERT INTO technology_delivery_attempts
                (article_id, attempts, last_attempted_at, last_status,
                 expires_at, last_message_id, excluded_reason)
            VALUES (?, 1, ?, ?, ?, ?, ?)
            ON CONFLICT(article_id) DO UPDATE SET
                attempts = attempts + 1,
                last_attempted_at = excluded.last_attempted_at,
                last_status = excluded.last_status,
                last_message_id = excluded.last_message_id,
                excluded_reason = excluded.excluded_reason
            """,
            [
                (
                    article_id,
                    timestamp,
                    outcome.status,
                    timestamp + PENDING_TTL_SECONDS,
                    outcome.message_id,
                    exclusion,
                )
                for article_id in ids
            ],
        )
        if outcome.state == "confirmed":
            conn.executemany(
                """
                INSERT OR IGNORE INTO article_notifications
                    (article_id, channel, notified_at)
                VALUES (?, ?, ?)
                """,
                [(article_id, WATCH_CHANNEL, timestamp) for article_id in ids],
            )
