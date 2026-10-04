"""Testi zaznavanja USB shrambe in koordinacije aktivne seje."""
from __future__ import annotations

from pathlib import Path

import pytest

from core.storage import (
    StorageUnavailableError,
    active_session_dir,
    clear_active_session,
    find_usb_mounts,
    parse_mountinfo,
    publish_active_session,
    resolve_storage_root,
)


def test_parse_mountinfo_decodes_paths() -> None:
    mounts = parse_mountinfo(
        "36 25 8:1 / /media/dron/UAV\\040DATA rw,nosuid - vfat /dev/sda rw\n")
    assert mounts[0].mount_point == Path("/media/dron/UAV DATA")
    assert mounts[0].source == "/dev/sda"
    assert "rw" in mounts[0].options


def test_find_usb_mounts_accepts_only_writable_removable(
    tmp_path: Path,
) -> None:
    mountinfo = tmp_path / "mountinfo"
    usb_mount = tmp_path / "usb"
    fixed_mount = tmp_path / "fixed"
    usb_mount.mkdir()
    fixed_mount.mkdir()
    mountinfo.write_text(
        f"36 25 8:0 / {usb_mount} rw,nosuid - ext4 /dev/sda rw\n"
        f"37 25 8:16 / {fixed_mount} rw - ext4 /dev/sdb rw\n",
        encoding="utf-8",
    )
    sys_block = tmp_path / "block"
    (sys_block / "sda").mkdir(parents=True)
    (sys_block / "sda" / "removable").write_text("1\n", encoding="ascii")
    (sys_block / "sdb").mkdir()
    (sys_block / "sdb" / "removable").write_text("0\n", encoding="ascii")

    assert find_usb_mounts(
        mountinfo_path=mountinfo, sys_class_block=sys_block) == [usb_mount]


def test_resolve_storage_root_uses_only_candidate(tmp_path: Path) -> None:
    mount = tmp_path / "usb"
    mount.mkdir()
    resolved = resolve_storage_root("auto", mounts=[mount])
    assert resolved == mount / "mission-planner"
    assert resolved.is_dir()


def test_resolve_storage_root_rejects_missing_or_ambiguous(
    tmp_path: Path,
) -> None:
    with pytest.raises(StorageUnavailableError, match="Ni priklopljenega"):
        resolve_storage_root("auto", mounts=[])
    first = tmp_path / "usb-a"
    second = tmp_path / "usb-b"
    first.mkdir()
    second.mkdir()
    with pytest.raises(StorageUnavailableError, match="več USB"):
        resolve_storage_root("auto", mounts=[first, second])


def test_explicit_storage_root_bypasses_detection(tmp_path: Path) -> None:
    target = tmp_path / "service-override"
    assert resolve_storage_root(target) == target.resolve()
    assert target.is_dir()


def test_active_session_marker_lifecycle(tmp_path: Path) -> None:
    session = tmp_path / "20260726-120000_test"
    session.mkdir()

    publish_active_session(tmp_path, session)
    assert active_session_dir(tmp_path) == session.resolve()

    clear_active_session(tmp_path, session)
    assert active_session_dir(tmp_path) is None


def test_active_session_rejects_path_traversal(tmp_path: Path) -> None:
    (tmp_path / ".active-session").write_text("../outside", encoding="utf-8")
    assert active_session_dir(tmp_path) is None
