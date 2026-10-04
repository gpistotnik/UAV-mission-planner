"""Seznam USB particij in varen klic privilegiranega mount helperja."""
from __future__ import annotations

import json
import subprocess
from typing import Any, Callable


ADMIN_HELPER = "/usr/local/sbin/mission-planner-usb-storage"
LSBLK_COLUMNS = (
    "NAME,PATH,TYPE,TRAN,RM,FSTYPE,LABEL,UUID,SIZE,MODEL,MOUNTPOINTS")


class UsbDeviceError(RuntimeError):
    pass


def _read_lsblk() -> dict[str, Any]:
    try:
        result = subprocess.run(
            [
                "lsblk", "--json", "--bytes", "--output", LSBLK_COLUMNS,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise UsbDeviceError(f"Seznama USB naprav ni mogoče prebrati: {exc}") from exc


def list_usb_partitions(
    payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Vrne particije removable/USB diskov v obliki za GUI."""
    data = payload if payload is not None else _read_lsblk()
    devices: list[dict[str, Any]] = []
    for disk in data.get("blockdevices", []):
        is_usb = str(disk.get("tran") or "").lower() == "usb" or bool(
            disk.get("rm"))
        if not is_usb:
            continue
        children = disk.get("children") or []
        if disk.get("type") == "part":
            children = [disk]
        for partition in children:
            if partition.get("type") != "part":
                continue
            path = str(partition.get("path") or "")
            if not path.startswith("/dev/"):
                continue
            mountpoints = [
                str(item) for item in (partition.get("mountpoints") or [])
                if item
            ]
            devices.append({
                "path": path,
                "name": partition.get("name"),
                "label": partition.get("label") or disk.get("label"),
                "model": disk.get("model"),
                "filesystem": partition.get("fstype"),
                "uuid": partition.get("uuid"),
                "size_b": int(partition.get("size") or 0),
                "mountpoints": mountpoints,
                "selected": "/mnt/uav-data" in mountpoints,
                "usable": bool(partition.get("fstype") and partition.get("uuid")),
            })
    return sorted(devices, key=lambda item: str(item["path"]))


def select_usb_partition(
    device: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    devices: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Pokliče root-owned helper; vhod je omejen na trenutno viden USB."""
    current_devices = devices if devices is not None else list_usb_partitions()
    allowed = {item["path"] for item in current_devices}
    if device not in allowed:
        raise UsbDeviceError("Izbrana naprava ni med trenutno zaznanimi USB particijami.")
    try:
        result = run(
            ["sudo", "-n", ADMIN_HELPER, "select", device],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UsbDeviceError(f"Izbira USB-ja ni uspela: {exc}") from exc
    output = (result.stdout or "").strip()
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        detail = (result.stderr or output or "helper ni vrnil odgovora").strip()
        raise UsbDeviceError(detail) from exc
    if result.returncode != 0 or not data.get("ok"):
        raise UsbDeviceError(str(data.get("error") or "Mount USB-ja ni uspel."))
    return data
