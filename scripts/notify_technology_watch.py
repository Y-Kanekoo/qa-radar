"""Daily official-release digest. Dry-run never sends or edits the source DB."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from qa_radar.db import init_db
from qa_radar.publisher.technology_watch import (
    DEFAULT_PROFILES_PATH,
    MAX_DIGEST_ITEMS,
    build_payload,
    load_profiles,
    record_delivery,
    select_candidates,
    send_payload,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_WEBHOOK = "DISCORD_TECH_WATCH_WEBHOOK_URL"
logger = logging.getLogger("qa_radar.technology_watch")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="QA新技術の公式リリース日次通知")
    parser.add_argument("--db-path", type=Path, default=REPO_ROOT / "data" / "articles.db")
    parser.add_argument("--profiles", type=Path, default=DEFAULT_PROFILES_PATH)
    parser.add_argument("--limit", type=int, default=MAX_DIGEST_ITEMS)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def _preview_db(path: Path, target: Path) -> sqlite3.Connection:
    # SQLite backup includes committed WAL contents. Migrations run on the copy.
    original = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        copy = sqlite3.connect(target)
        try:
            original.backup(copy)
        finally:
            copy.close()
    finally:
        original.close()
    return init_db(target)


def _run(conn: sqlite3.Connection, profiles: dict, webhook: str, *, limit: int, dry: bool) -> int:
    candidates, deferred = select_candidates(conn, profiles, limit=limit)
    logger.info("候補=%d 繰越=%d", len(candidates), deferred)
    if not candidates:
        return 0
    payload = build_payload(candidates, deferred)
    if dry:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    outcome = send_payload(webhook, payload)
    record_delivery(conn, candidates, outcome=outcome)
    logger.info(
        "新技術通知 state=%s status=%s 件数=%d", outcome.state, outcome.status, len(candidates)
    )
    return 0 if outcome.state == "confirmed" else 2


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # HTTPX's INFO request line includes the full webhook URL (and its token).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if not args.db_path.is_file():
        logger.error("DB が存在しません")
        return 1
    if not 1 <= args.limit <= MAX_DIGEST_ITEMS:
        logger.error("limit は 1..%d にしてください", MAX_DIGEST_ITEMS)
        return 1
    webhook = os.environ.get(ENV_WEBHOOK, "").strip()
    if not webhook and not args.dry_run:
        logger.warning("%s 未設定: 通知を保留します", ENV_WEBHOOK)
        return 0
    profiles = load_profiles(args.profiles)
    if args.dry_run:
        with tempfile.TemporaryDirectory(prefix="qa-tech-preview-") as directory:
            conn = _preview_db(args.db_path, Path(directory) / "preview.db")
            try:
                return _run(conn, profiles, "", limit=args.limit, dry=True)
            finally:
                conn.close()
    conn = init_db(args.db_path)
    try:
        return _run(conn, profiles, webhook, limit=args.limit, dry=False)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
