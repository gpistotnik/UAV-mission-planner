#!/usr/bin/env python3
"""Privilegiran, ozko omejen helper za izbiro USB shrambe.

Namestitvena skripta ga kopira v ``/usr/local/sbin`` kot root-owned datoteko.
Django sme prek sudo poklicati samo ukaz ``select /dev/...``; helper nato sam
preveri, da gre za particijo fizične USB naprave z znanim datotečnim sistemom.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


MOUNT_POINT_TEXT = "/mnt/uav-data"
MOUNT_POINT = Path(MOUNT_POINT_TEXT)
FSTAB = Path("/etc/fstab")
MANAGED_COMMENT = "# mission-planner-usb"
USER_ID = 1000
GROUP_ID = 1000
FAT_FILESYSTEMS = {"vfat", "fat", "msdos", "exfat", "ntfs", "ntfs3"}
LINUX_FILESYSTEMS = {"ext2", "ext3", "ext4"}
SUPPORTED_FILESYSTEMS = FAT_FILESYSTEMS | LINUX_FILESYSTEMS


class SelectionError(RuntimeError):
    pass


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args), check=check, capture_output=True, text=True, timeout=20)


def _lsblk() -> dict[str, Any]:
    result = _run(
        "lsblk", "--json", "--bytes",
        "--output", "NAME,PATH,TYPE,TRAN,RM,FSTYPE,LABEL,UUID,SIZE,MOUNTPOINTS",
    )
    return json.loads(result.stdout)


def _usb_partitions() -> dict[str, dict[str, Any]]:
    partitions: dict[str, dict[str, Any]] = {}
    for disk in _lsblk().get("blockdevices", []):
        is_usb = str(disk.get("tran") or "").lower() == "usb" or bool(
            disk.get("rm"))
        if not is_usb:
            continue
        nodes = disk.get("children") or []
        if disk.get("type") == "part":
            nodes = [disk]
        for node in nodes:
            if node.get("type") != "part":
                continue
            path = str(node.get("path") or "")
            if path.startswith("/dev/"):
                partitions[path] = node
    return partitions


def _fstab_line(node: dict[str, Any]) -> str:
    uuid = str(node.get("uuid") or "").strip()
    filesystem = str(node.get("fstype") or "").strip().lower()
    if not uuid:
        raise SelectionError("Izbrana particija nima UUID-ja.")
    if filesystem not in SUPPORTED_FILESYSTEMS:
        raise SelectionError(
            f"Datotečni sistem '{filesystem or 'neznan'}' ni podprt.")
    if filesystem in FAT_FILESYSTEMS:
        options = (
            f"defaults,nofail,uid={USER_ID},gid={GROUP_ID},umask=0022,"
            "x-systemd.device-timeout=10"
        )
        fsck_pass = 0
    else:
        options = "defaults,nofail,x-systemd.device-timeout=10"
        fsck_pass = 2
    return (
        f"UUID={uuid} {MOUNT_POINT_TEXT} {filesystem} {options} 0 {fsck_pass} "
        f"{MANAGED_COMMENT}\n"
    )


def _without_managed_mount(text: str) -> str:
    kept: list[str] = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        fields = stripped.split()
        is_target = (
            stripped
            and not stripped.startswith("#")
            and len(fields) >= 2
            and fields[1] == MOUNT_POINT_TEXT
        )
        if MANAGED_COMMENT not in line and not is_target:
            kept.append(line)
    result = "".join(kept)
    return result if not result or result.endswith("\n") else result + "\n"


def _mounted_source(target: Path) -> str | None:
    result = _run(
        "findmnt", "--noheadings", "--raw", "--output", "SOURCE",
        "--mountpoint", str(target), check=False,
    )
    source = result.stdout.strip()
    return source or None


def select(device: str) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise SelectionError("Helper mora teči kot root.")
    partitions = _usb_partitions()
    node = partitions.get(device)
    if node is None:
        raise SelectionError("Izbrana naprava ni particija USB diska.")

    filesystem = str(node.get("fstype") or "").lower()
    fstab_line = _fstab_line(node)
    original_fstab = FSTAB.read_text(encoding="utf-8")

    _run("sync")
    current_source = _mounted_source(MOUNT_POINT)
    if current_source:
        _run("umount", str(MOUNT_POINT))
    for mounted_at in node.get("mountpoints") or []:
        if mounted_at and mounted_at != str(MOUNT_POINT):
            _run("umount", str(mounted_at))

    MOUNT_POINT.mkdir(parents=True, exist_ok=True)
    shutil.copy2(FSTAB, FSTAB.with_suffix(".mission-planner.bak"))
    temporary = FSTAB.with_suffix(".mission-planner.tmp")
    temporary.write_text(
        _without_managed_mount(original_fstab) + fstab_line,
        encoding="utf-8",
    )
    os.chmod(temporary, 0o644)
    temporary.replace(FSTAB)

    try:
        _run("mount", str(MOUNT_POINT))
        if filesystem in LINUX_FILESYSTEMS:
            os.chown(MOUNT_POINT, USER_ID, GROUP_ID)
    except Exception:
        FSTAB.write_text(original_fstab, encoding="utf-8")
        os.chmod(FSTAB, 0o644)
        _run("mount", str(MOUNT_POINT), check=False)
        raise

    app_dir = MOUNT_POINT / "mission-planner" / "flightlogs"
    app_dir.mkdir(parents=True, exist_ok=True)
    if filesystem in LINUX_FILESYSTEMS:
        os.chown(MOUNT_POINT / "mission-planner", USER_ID, GROUP_ID)
        os.chown(app_dir, USER_ID, GROUP_ID)
    return {
        "ok": True,
        "device": device,
        "mount_point": str(MOUNT_POINT),
        "log_dir": str(app_dir),
        "persistent": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("select",))
    parser.add_argument("device")
    args = parser.parse_args()
    try:
        result = select(args.device)
    except (SelectionError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
