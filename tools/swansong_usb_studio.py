#!/usr/bin/env python3
"""Safe, machine-readable SwanSong USB inspection, update, and hardware QA."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import sys
from pathlib import Path
from typing import Any, Iterable

from swansong_firmware import FirmwareError, load_application
from swansong_usb_update import (
    BOOTLOADER_PID,
    GAMEPAD_PID,
    VID,
    Transport,
    UpdateError,
    _hid_module,
    enter_bootloader,
    install,
)

DOCTOR_SCHEMA = "swansong-usb-doctor-v1"
UPDATE_PLAN_SCHEMA = "swansong-usb-update-plan-v1"
INSTALL_SCHEMA = "swansong-usb-install-report-v1"
HARDWARE_QA_SCHEMA = "swansong-usb-hardware-qa-v1"

BUTTON_NAMES = ("a", "b", "y1", "y2", "y3", "y4", "start", "sound", "power")
DIRECTION_NAMES = ("up", "right", "down", "left")
CONTROL_NAMES = DIRECTION_NAMES + BUTTON_NAMES
HAT_CONTROLS = {
    0: ("up",),
    1: ("up", "right"),
    2: ("right",),
    3: ("right", "down"),
    4: ("down",),
    5: ("down", "left"),
    6: ("left",),
    7: ("left", "up"),
    8: (),
}


class StudioUSBError(RuntimeError):
    """A bounded, user-actionable Studio USB failure."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_image(path: Path) -> dict[str, Any]:
    image = load_application(path)
    return {
        "path": str(path.resolve()),
        "sha256": _sha256(path),
        "crc16": f"{image.crc:04x}",
        "programmedWords": sum(word != 0x3FFF for word in image.words),
        "totalWords": len(image.words),
    }


def _device_summary(hid: Any) -> dict[str, Any]:
    gamepads = hid.enumerate(VID, GAMEPAD_PID)
    bootloaders = hid.enumerate(VID, BOOTLOADER_PID)
    return {
        "gamepadCount": len(gamepads),
        "bootloaderCount": len(bootloaders),
        "mode": (
            "gamepad"
            if len(gamepads) == 1 and not bootloaders
            else "bootloader"
            if len(bootloaders) == 1 and not gamepads
            else "absent"
            if not gamepads and not bootloaders
            else "ambiguous"
        ),
    }


def doctor_report(path: Path, *, require_device: bool = False, hid_module: Any | None = None) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    image: dict[str, Any] | None = None
    try:
        image = inspect_image(path)
        checks.append({"id": "firmware-image", "status": "pass", "message": "Application image is valid."})
    except (FirmwareError, OSError) as exc:
        checks.append({"id": "firmware-image", "status": "fail", "message": str(exc)})

    devices = {"gamepadCount": 0, "bootloaderCount": 0, "mode": "unavailable"}
    try:
        hid = hid_module if hid_module is not None else _hid_module()
        checks.append({"id": "hidapi", "status": "pass", "message": "HID transport is available."})
        devices = _device_summary(hid)
        if devices["mode"] in ("gamepad", "bootloader"):
            device_status = "pass"
            device_message = f"One SwanSong USB is connected in {devices['mode']} mode."
        elif devices["mode"] == "absent" and not require_device:
            device_status = "warning"
            device_message = "No SwanSong USB is connected; offline image checks remain available."
        elif devices["mode"] == "absent":
            device_status = "fail"
            device_message = "No SwanSong USB is connected."
        else:
            device_status = "fail"
            device_message = "Device selection is ambiguous; connect exactly one SwanSong USB."
        checks.append({"id": "device", "status": device_status, "message": device_message})
    except UpdateError as exc:
        checks.append({"id": "hidapi", "status": "fail", "message": str(exc)})

    checks.append(
        {
            "id": "usb-identity",
            "status": "warning",
            "message": "Prototype firmware still uses the documented engineering VID/PID; production requires an owned identity.",
        }
    )
    return {
        "schema": DOCTOR_SCHEMA,
        "ok": not any(check["status"] == "fail" for check in checks),
        "checks": checks,
        "device": devices,
        "image": image,
    }


def update_plan(path: Path, version: tuple[int, int], *, hid_module: Any | None = None) -> dict[str, Any]:
    image = inspect_image(path)
    hid = hid_module if hid_module is not None else _hid_module()
    devices = _device_summary(hid)
    ok = devices["mode"] in ("gamepad", "bootloader")
    return {
        "schema": UPDATE_PLAN_SCHEMA,
        "ok": ok,
        "image": image,
        "device": devices,
        "version": f"{version[0]}.{version[1]}",
        "requiresDeviceReset": devices["mode"] == "gamepad",
        "requiresRecoveryChordOnInterruption": True,
        "confirmationSHA256": image["sha256"],
        "message": (
            "Update is ready; installation remains disabled until the exact SHA-256 and device reset are accepted."
            if ok
            else "Connect exactly one SwanSong USB before installation."
        ),
    }


def install_report(
    path: Path,
    version: tuple[int, int],
    *,
    confirmation_sha256: str,
    accept_device_reset: bool,
) -> dict[str, Any]:
    image_info = inspect_image(path)
    if confirmation_sha256.lower() != image_info["sha256"]:
        raise StudioUSBError("confirmation SHA-256 does not match the selected firmware image")
    if not accept_device_reset:
        raise StudioUSBError("installation requires explicit acceptance of the controller reset")

    image = load_application(path)
    output = io.StringIO()
    device = enter_bootloader()
    try:
        with contextlib.redirect_stdout(output):
            install(Transport(device), image, version[0], version[1], reset=True)
    finally:
        device.close()
    messages = [line.strip() for line in output.getvalue().splitlines() if line.strip()]
    return {
        "schema": INSTALL_SCHEMA,
        "ok": True,
        "image": image_info,
        "version": f"{version[0]}.{version[1]}",
        "verifiedReadback": True,
        "controllerRestarted": True,
        "messages": messages,
    }


def decode_report(report: bytes | bytearray | list[int]) -> set[str]:
    payload = bytes(report)
    if len(payload) == 4 and payload[0] == 0:
        payload = payload[1:]
    if len(payload) != 3:
        raise StudioUSBError(f"gamepad returned {len(payload)} bytes; expected 3")
    controls = {name for bit, name in enumerate(BUTTON_NAMES) if payload[bit // 8] & (1 << (bit % 8))}
    hat = payload[2] & 0x0F
    if hat not in HAT_CONTROLS:
        raise StudioUSBError(f"gamepad returned invalid hat value {hat}")
    controls.update(HAT_CONTROLS[hat])
    return controls


def hardware_qa_report(
    *,
    required: Iterable[str] = CONTROL_NAMES,
    max_reports: int = 30000,
    timeout_ms: int = 1,
    hid_module: Any | None = None,
) -> dict[str, Any]:
    wanted = tuple(dict.fromkeys(required))
    unknown = sorted(set(wanted) - set(CONTROL_NAMES))
    if unknown:
        raise StudioUSBError(f"unknown controls: {', '.join(unknown)}")
    if max_reports <= 0:
        raise StudioUSBError("max reports must be positive")

    hid = hid_module if hid_module is not None else _hid_module()
    devices = hid.enumerate(VID, GAMEPAD_PID)
    if len(devices) != 1:
        raise StudioUSBError("connect exactly one SwanSong USB in gamepad mode for hardware QA")
    device = hid.device()
    device.open_path(devices[0]["path"])
    seen: set[str] = set()
    neutral_seen = False
    reports_read = 0
    try:
        for _ in range(max_reports):
            raw = device.read(3, timeout_ms)
            if not raw:
                continue
            reports_read += 1
            controls = decode_report(raw)
            if not controls:
                neutral_seen = True
            seen.update(controls)
            if neutral_seen and all(control in seen for control in wanted):
                break
    finally:
        device.close()
    missing = [control for control in wanted if control not in seen]
    ok = neutral_seen and not missing
    return {
        "schema": HARDWARE_QA_SCHEMA,
        "ok": ok,
        "requiredControls": list(wanted),
        "observedControls": [control for control in CONTROL_NAMES if control in seen],
        "missingControls": missing,
        "neutralObserved": neutral_seen,
        "reportsRead": reports_read,
        "boundedReportLimit": max_reports,
    }


def _version(value: str) -> tuple[int, int]:
    try:
        major_text, minor_text = value.split(".", 1)
        major, minor = int(major_text), int(minor_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("version must look like MAJOR.MINOR") from exc
    if not (0 <= major <= 255 and 0 <= minor <= 255):
        raise argparse.ArgumentTypeError("version components must be 0-255")
    return major, minor


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="validate firmware and USB availability without changing hardware")
    doctor.add_argument("image", type=Path)
    doctor.add_argument("--require-device", action="store_true")

    plan = subparsers.add_parser("plan-update", help="produce a non-mutating, confirmable update plan")
    plan.add_argument("image", type=Path)
    plan.add_argument("--version", type=_version, default=(1, 0))

    update = subparsers.add_parser("install", help="install a previously inspected image")
    update.add_argument("image", type=Path)
    update.add_argument("--version", type=_version, default=(1, 0))
    update.add_argument("--confirm-sha256", required=True)
    update.add_argument("--accept-device-reset", action="store_true")

    qa = subparsers.add_parser("hardware-qa", help="observe every physical control through bounded HID reports")
    qa.add_argument("--required", default=",".join(CONTROL_NAMES))
    qa.add_argument("--max-reports", type=int, default=30000)
    qa.add_argument("--timeout-ms", type=int, default=1)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "doctor":
            report = doctor_report(args.image, require_device=args.require_device)
        elif args.command == "plan-update":
            report = update_plan(args.image, args.version)
        elif args.command == "install":
            report = install_report(
                args.image,
                args.version,
                confirmation_sha256=args.confirm_sha256,
                accept_device_reset=args.accept_device_reset,
            )
        else:
            required = tuple(value.strip().lower() for value in args.required.split(",") if value.strip())
            report = hardware_qa_report(required=required, max_reports=args.max_reports, timeout_ms=args.timeout_ms)
    except (FirmwareError, UpdateError, StudioUSBError, OSError) as exc:
        report = {"schema": "swansong-usb-error-v1", "ok": False, "error": str(exc)}
    json.dump(report, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
