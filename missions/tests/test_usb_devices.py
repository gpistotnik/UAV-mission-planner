"""Testi varne izbire USB shrambe."""
from __future__ import annotations

import json
import subprocess

import pytest

from missions.services.usb_devices import (
    UsbDeviceError, list_usb_partitions, select_usb_partition,
)
from scripts.usb_storage_admin import _fstab_line, _without_managed_mount


def _payload() -> dict:
    return {
        "blockdevices": [
            {
                "name": "sda",
                "path": "/dev/sda",
                "type": "disk",
                "tran": "usb",
                "rm": True,
                "model": "USB Flash",
                "children": [
                    {
                        "name": "sda1",
                        "path": "/dev/sda1",
                        "type": "part",
                        "fstype": "vfat",
                        "label": "UAV",
                        "uuid": "ABCD-1234",
                        "size": 32_000_000_000,
                        "mountpoints": [None],
                    },
                ],
            },
            {
                "name": "mmcblk0",
                "path": "/dev/mmcblk0",
                "type": "disk",
                "tran": "mmc",
                "rm": False,
                "children": [],
            },
        ],
    }


def test_list_usb_partitions_excludes_system_disk() -> None:
    devices = list_usb_partitions(_payload())
    assert len(devices) == 1
    assert devices[0]["path"] == "/dev/sda1"
    assert devices[0]["filesystem"] == "vfat"
    assert devices[0]["usable"] is True


def test_select_rejects_device_outside_current_usb_list() -> None:
    with pytest.raises(UsbDeviceError, match="ni med"):
        select_usb_partition("/dev/mmcblk0p2", devices=[])


def test_select_calls_only_root_owned_helper() -> None:
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps({
                "ok": True,
                "mount_point": "/mnt/uav-data",
            }), stderr="")

    result = select_usb_partition(
        "/dev/sda1",
        devices=[{"path": "/dev/sda1"}],
        run=fake_run,
    )
    assert result["ok"] is True
    assert calls == [[
        "sudo", "-n", "/usr/local/sbin/mission-planner-usb-storage",
        "select", "/dev/sda1",
    ]]


def test_fstab_entry_is_persistent_and_user_writable() -> None:
    line = _fstab_line({
        "uuid": "ABCD-1234",
        "fstype": "vfat",
    })
    assert "UUID=ABCD-1234 /mnt/uav-data vfat" in line
    assert "uid=1000,gid=1000" in line
    assert "# mission-planner-usb" in line


def test_replacing_selection_removes_old_mount_entry() -> None:
    old = (
        "proc /proc proc defaults 0 0\n"
        "UUID=OLD /mnt/uav-data vfat defaults 0 0 # mission-planner-usb\n"
    )
    cleaned = _without_managed_mount(old)
    assert "/mnt/uav-data" not in cleaned
    assert "proc /proc" in cleaned
