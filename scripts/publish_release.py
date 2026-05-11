"""articles.db を GitHub Releases に push し、古い data-* release を削除する.

cron ワークフロー (Phase 7) で `gh` CLI と一緒に使う想定. ローカルでも
`GH_TOKEN` を渡せば実行可能.

タグ命名規則:
    data-YYYY-MM-DDTHHMM (UTC、prerelease=True で latest にしない)

retention:
    既定 7 日以上経過した data-* タグは自動削除する.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

DATA_TAG_PREFIX = "data-"
DEFAULT_RETENTION_DAYS = 7
REPO_ROOT = Path(__file__).resolve().parent.parent


def _gh(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    """gh CLI を呼ぶラッパ. 失敗で例外."""
    return subprocess.run(
        ["gh", *args],
        check=True,
        text=True,
        capture_output=capture,
    )


def create_data_release(db_path: Path, repo: str | None = None) -> str:
    """data-<timestamp> タグで新規 release を作成し、tag 名を返す."""
    ts = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H%M")
    tag = f"{DATA_TAG_PREFIX}{ts}"
    title = f"Data snapshot {ts} UTC"

    args = [
        "release",
        "create",
        tag,
        str(db_path),
        "--title",
        title,
        "--notes",
        "qa-radar データスナップショット (cron 自動生成). 7日後に自動削除されます.",
        "--prerelease",  # latest 扱いにしない (コードリリース v*.*.* との分離)
    ]
    if repo:
        args.extend(["--repo", repo])
    _gh(*args)
    return tag


def list_data_releases(repo: str | None = None) -> list[dict[str, str]]:
    """data-* タグの release を tag/createdAt のリストで返す."""
    args = ["release", "list", "--limit", "200", "--json", "tagName,createdAt"]
    if repo:
        args.extend(["--repo", repo])
    proc = _gh(*args, capture=True)
    releases: list[dict[str, str]] = json.loads(proc.stdout)
    return [r for r in releases if r["tagName"].startswith(DATA_TAG_PREFIX)]


def delete_release(tag: str, repo: str | None = None) -> bool:
    """release を削除する. tag も同時に削除. 失敗は False を返す."""
    args = ["release", "delete", tag, "--yes", "--cleanup-tag"]
    if repo:
        args.extend(["--repo", repo])
    try:
        _gh(*args)
        return True
    except subprocess.CalledProcessError:
        return False


def cleanup_old_releases(
    repo: str | None = None, retention_days: int = DEFAULT_RETENTION_DAYS
) -> int:
    """指定日数より古い data-* release を削除. 削除件数を返す."""
    cutoff = datetime.now(tz=UTC) - timedelta(days=retention_days)
    releases = list_data_releases(repo=repo)
    deleted = 0
    for r in releases:
        created = datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00"))
        if created < cutoff and delete_release(r["tagName"], repo=repo):
            deleted += 1
    return deleted


def download_latest_db(output_path: Path, repo: str | None = None) -> str | None:
    """最新の data-* release から articles.db をダウンロード. tag 名を返す.

    過去 release が存在しない場合は None を返す.
    """
    releases = list_data_releases(repo=repo)
    if not releases:
        return None
    # tag 名は data-YYYY-MM-DDTHHMM なので辞書順 = 新しい順
    latest = max(releases, key=lambda r: r["tagName"])["tagName"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "release",
        "download",
        latest,
        "--pattern",
        "articles.db",
        "--output",
        str(output_path),
        "--clobber",
    ]
    if repo:
        args.extend(["--repo", repo])
    _gh(*args)
    return latest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="qa-radar DB を GH Releases に publish")
    parser.add_argument("--db-path", type=Path, default=REPO_ROOT / "data" / "articles.db")
    parser.add_argument("--repo", default=None, help="owner/repo. 省略時は cwd の repo")
    parser.add_argument(
        "--retention-days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help=f"古い data-* release の削除対象日数 (既定 {DEFAULT_RETENTION_DAYS})",
    )
    parser.add_argument(
        "--mode",
        choices=["create", "cleanup", "download", "full"],
        default="full",
        help="full=create+cleanup",
    )
    parser.add_argument(
        "--download-to",
        type=Path,
        default=None,
        help="--mode=download 時の出力先 (既定 --db-path)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("qa_radar.publish_release")

    if args.mode in ("create", "full"):
        if not args.db_path.exists():
            log.error("DB が存在しません: %s", args.db_path)
            return 1
        tag = create_data_release(args.db_path, repo=args.repo)
        log.info("作成: %s", tag)

    if args.mode in ("cleanup", "full"):
        deleted = cleanup_old_releases(repo=args.repo, retention_days=args.retention_days)
        log.info("削除: %d 件 (>%d日)", deleted, args.retention_days)

    if args.mode == "download":
        target = args.download_to or args.db_path
        tag = download_latest_db(target, repo=args.repo)
        if tag:
            log.info("復元: %s → %s", tag, target)
        else:
            log.warning("過去 release なし")
            return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
