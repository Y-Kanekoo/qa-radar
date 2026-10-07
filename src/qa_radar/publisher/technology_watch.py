"""Official-release technology watch layered on the existing qa-radar DB.

Only short public metadata already stored by the crawler is read. The release
itself remains the primary source; claims from it are labelled as announcements.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml

from qa_radar.crawler.normalize import normalize_url

WATCH_CHANNEL = "discord-technology-watch"
DEFAULT_PROFILES_PATH = Path(__file__).resolve().parents[3] / "config" / "technology_watch.yaml"
MAX_DIGEST_ITEMS = 5
LOOKBACK_SECONDS = 7 * 24 * 3600

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
    retried: bool


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
    """Select unannounced official releases, retaining failed items past lookback.

    The ordinary Discord channel is checked too, so enabling the new destination
    does not replay a release that already reached the old one.
    """
    if limit < 1 or limit > MAX_DIGEST_ITEMS:
        raise ValueError(f"limit must be 1..{MAX_DIGEST_ITEMS}")
    if not profiles:
        return ([], 0)
    current = int(time.time()) if now is None else now
    placeholders = ", ".join("?" for _ in profiles)
    rows = conn.execute(
        f"""
        SELECT a.id, a.url, a.title, a.snippet, a.published_at, s.slug,
               COALESCE(t.attempts, 0) AS attempts, s.feed_url
        FROM articles a
        JOIN sources s ON s.id = a.source_id
        LEFT JOIN technology_delivery_attempts t ON t.article_id = a.id
        WHERE s.slug IN ({placeholders}) AND a.duplicate_of IS NULL
          AND (a.fetched_at >= ? OR t.attempts > 0)
          AND NOT EXISTS (
              SELECT 1 FROM article_notifications n
              WHERE n.article_id = a.id
                AND n.channel IN (?, 'discord')
          )
        ORDER BY (t.attempts > 0) DESC, a.published_at DESC, a.id DESC
        LIMIT 500
        """,
        (*profiles, current - LOOKBACK_SECONDS, WATCH_CHANNEL),
    ).fetchall()
    by_url: dict[str, Candidate] = {}
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
                retried=existing.retried or bool(row["attempts"]),
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
            retried=bool(row["attempts"]),
        )
    selected = sorted(
        by_url.values(),
        key=lambda item: (item.retried, item.priority, item.published_at),
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


def send_payload(
    webhook_url: str,
    payload: dict[str, object],
    *,
    client: httpx.Client | None = None,
) -> tuple[bool, str]:
    """Send once, with one bounded retry for Discord 429. Never log the URL."""
    owned = client is None
    used = client if client is not None else httpx.Client(timeout=10, follow_redirects=False)
    try:
        for attempt in range(2):
            try:
                response = used.post(webhook_url, json=payload)
            except httpx.HTTPError:
                return False, "network"
            if 200 <= response.status_code < 300:
                return True, f"http_{response.status_code}"
            if response.status_code == 429 and attempt == 0:
                try:
                    delay = float(response.headers.get("Retry-After", "1"))
                except ValueError:
                    delay = 1.0
                time.sleep(min(5.0, max(0.5, delay)))
                continue
            return False, f"http_{response.status_code}"
        return False, "http_429"
    finally:
        if owned:
            used.close()


def record_delivery(
    conn: sqlite3.Connection,
    candidates: list[Candidate],
    *,
    status: str,
    success: bool,
    now: int | None = None,
) -> None:
    """Record a whole digest outcome and its per-article success in one commit."""
    timestamp = int(time.time()) if now is None else now
    ids = [article_id for item in candidates for article_id in item.article_ids]
    with conn:
        conn.executemany(
            """
            INSERT INTO technology_delivery_attempts
                (article_id, attempts, last_attempted_at, last_status)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(article_id) DO UPDATE SET
                attempts = attempts + 1,
                last_attempted_at = excluded.last_attempted_at,
                last_status = excluded.last_status
            """,
            [(article_id, timestamp, status) for article_id in ids],
        )
        if success:
            conn.executemany(
                """
                INSERT OR IGNORE INTO article_notifications
                    (article_id, channel, notified_at)
                VALUES (?, ?, ?)
                """,
                [(article_id, WATCH_CHANNEL, timestamp) for article_id in ids],
            )
