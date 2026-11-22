"""Where :func:`utils.safe_backup` keeps its copy (issue #17)."""

from __future__ import annotations

from pathlib import Path

import pytest

from photokin import utils


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``Path.home()`` at a temporary directory."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def test_backup_goes_to_user_dir_not_beside_file(tmp_path: Path, fake_home: Path) -> None:
    # Arrange
    vocab = tmp_path / "pkg" / "vocab.toml"
    vocab.parent.mkdir()
    vocab.write_text("original", encoding="utf-8")

    # Act
    utils.safe_backup(str(vocab))

    # Assert
    backup = utils.backup_path_for(str(vocab))
    assert backup.parent == fake_home / ".photokin" / "backups"
    assert backup.read_text(encoding="utf-8") == "original"
    assert list(vocab.parent.iterdir()) == [vocab]


def test_first_backup_wins(tmp_path: Path, fake_home: Path) -> None:
    # Arrange
    vocab = tmp_path / "vocab.toml"
    vocab.write_text("first", encoding="utf-8")
    utils.safe_backup(str(vocab))
    vocab.write_text("second", encoding="utf-8")

    # Act
    utils.safe_backup(str(vocab))

    # Assert
    assert utils.backup_path_for(str(vocab)).read_text(encoding="utf-8") == "first"


def test_same_basename_in_two_folders_gets_two_backups(tmp_path: Path, fake_home: Path) -> None:
    # Arrange
    first = tmp_path / "a" / "vocab.toml"
    second = tmp_path / "b" / "vocab.toml"

    # Act / Assert
    assert utils.backup_path_for(str(first)) != utils.backup_path_for(str(second))


def test_missing_source_only_warns(tmp_path: Path, fake_home: Path, caplog: pytest.LogCaptureFixture) -> None:
    # Arrange
    missing = tmp_path / "absent.toml"

    # Act
    utils.safe_backup(str(missing))

    # Assert
    assert "Could not create backup" in caplog.text
