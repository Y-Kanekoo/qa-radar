"""scripts/publish_release.py のユニットテスト (subprocess を mock)."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# scripts/ を import path に追加
_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import publish_release  # noqa: E402


def _fake_gh_returncode(stdout: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = stdout
    return proc


# ---------------- create_data_release ----------------


def test_create_data_release_uses_timestamp_tag() -> None:
    with patch("publish_release._gh") as mock_gh:
        mock_gh.return_value = _fake_gh_returncode()
        tag = publish_release.create_data_release(Path("/tmp/articles.db"))
    assert tag.startswith("data-")
    # 引数の確認
    call_args = mock_gh.call_args.args
    assert call_args[0] == "release"
    assert call_args[1] == "create"
    assert call_args[2] == tag
    assert "--prerelease" in call_args


def test_create_data_release_with_repo() -> None:
    with patch("publish_release._gh") as mock_gh:
        mock_gh.return_value = _fake_gh_returncode()
        publish_release.create_data_release(Path("/tmp/articles.db"), repo="owner/repo")
    call_args = mock_gh.call_args.args
    assert "--repo" in call_args
    assert "owner/repo" in call_args


# ---------------- list_data_releases ----------------


def test_list_data_releases_filters_by_prefix() -> None:
    releases_json = json.dumps(
        [
            {"tagName": "v0.1.0", "createdAt": "2026-05-09T00:00:00Z"},
            {"tagName": "data-2026-05-10T0600", "createdAt": "2026-05-10T06:00:00Z"},
            {"tagName": "data-2026-05-10T1200", "createdAt": "2026-05-10T12:00:00Z"},
            {"tagName": "v0.2.0", "createdAt": "2026-05-11T00:00:00Z"},
        ]
    )
    with patch("publish_release._gh") as mock_gh:
        mock_gh.return_value = _fake_gh_returncode(stdout=releases_json)
        result = publish_release.list_data_releases()
    assert len(result) == 2
    assert all(r["tagName"].startswith("data-") for r in result)


# ---------------- cleanup_old_releases ----------------


def test_cleanup_keeps_recent_deletes_old() -> None:
    now = datetime.now(tz=UTC)
    old = (now - timedelta(days=10)).isoformat().replace("+00:00", "Z")
    recent = (now - timedelta(days=2)).isoformat().replace("+00:00", "Z")
    releases_json = json.dumps(
        [
            {"tagName": "data-old", "createdAt": old},
            {"tagName": "data-recent", "createdAt": recent},
        ]
    )
    with patch("publish_release._gh") as mock_gh:
        # 1回目: list (stdout 返却), 2回目以降: delete
        mock_gh.side_effect = [
            _fake_gh_returncode(stdout=releases_json),
            _fake_gh_returncode(),
        ]
        deleted = publish_release.cleanup_old_releases(retention_days=7)
    assert deleted == 1
    # 削除されたのは data-old
    delete_call = mock_gh.call_args_list[1]
    assert "data-old" in delete_call.args


def test_cleanup_handles_no_releases() -> None:
    with patch("publish_release._gh") as mock_gh:
        mock_gh.return_value = _fake_gh_returncode(stdout="[]")
        deleted = publish_release.cleanup_old_releases()
    assert deleted == 0


# ---------------- download_latest_db ----------------


def test_download_latest_returns_none_when_no_releases() -> None:
    with patch("publish_release._gh") as mock_gh:
        mock_gh.return_value = _fake_gh_returncode(stdout="[]")
        result = publish_release.download_latest_db(Path("/tmp/out.db"))
    assert result is None


def test_download_latest_picks_newest_by_tag_name(tmp_path: Path) -> None:
    releases_json = json.dumps(
        [
            {"tagName": "data-2026-05-09T0600", "createdAt": "2026-05-09T06:00:00Z"},
            {"tagName": "data-2026-05-10T1200", "createdAt": "2026-05-10T12:00:00Z"},
            {"tagName": "data-2026-05-10T0600", "createdAt": "2026-05-10T06:00:00Z"},
        ]
    )
    out = tmp_path / "articles.db"
    with patch("publish_release._gh") as mock_gh:
        mock_gh.side_effect = [
            _fake_gh_returncode(stdout=releases_json),
            _fake_gh_returncode(),
        ]
        result = publish_release.download_latest_db(out)
    assert result == "data-2026-05-10T1200"
    # download call で正しいタグが使われている
    download_call = mock_gh.call_args_list[1]
    assert "data-2026-05-10T1200" in download_call.args


# ---------------- main ----------------


def test_main_create_missing_db_returns_1(tmp_path: Path) -> None:
    rc = publish_release.main(
        [
            "--mode",
            "create",
            "--db-path",
            str(tmp_path / "missing.db"),
        ]
    )
    assert rc == 1


def test_main_download_no_releases_returns_2(tmp_path: Path) -> None:
    with patch("publish_release._gh") as mock_gh:
        mock_gh.return_value = _fake_gh_returncode(stdout="[]")
        rc = publish_release.main(
            [
                "--mode",
                "download",
                "--download-to",
                str(tmp_path / "out.db"),
            ]
        )
    assert rc == 2


def test_main_full_mode_calls_create_and_cleanup(tmp_path: Path) -> None:
    db = tmp_path / "articles.db"
    db.write_bytes(b"fake db")

    with (
        patch("publish_release.create_data_release") as create_mock,
        patch("publish_release.cleanup_old_releases") as cleanup_mock,
    ):
        create_mock.return_value = "data-test"
        cleanup_mock.return_value = 2
        rc = publish_release.main(
            [
                "--mode",
                "full",
                "--db-path",
                str(db),
            ]
        )
    assert rc == 0
    create_mock.assert_called_once()
    cleanup_mock.assert_called_once()


@pytest.fixture(autouse=True)
def _cleanup_module_path() -> None:
    """sys.path に追加した scripts/ を tests 終了時に維持 (他テストへの影響なし)."""
    yield
