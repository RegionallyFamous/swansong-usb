#!/usr/bin/env python3
"""Install SwanSong USB application firmware through the controller's USB-C port."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

from swansong_firmware import (
    APP_LAST,
    APP_MARKER,
    APP_START,
    ROW_WORDS,
    ApplicationImage,
    FirmwareError,
    load_application,
)

VID = 0x04D8
GAMEPAD_PID = 0x005E
BOOTLOADER_PID = 0x005F
PACKET_SIZE = 64
PROTOCOL = 1
MAGIC = 0x53

CMD_QUERY = 0x01
CMD_ERASE_ROW = 0x02
CMD_WRITE_HALF = 0x03
CMD_READ_HALF = 0x04
CMD_FINALIZE = 0x05
CMD_RESET = 0x06

STATUS_NAMES = {
    0x00: "ok",
    0x01: "bad packet",
    0x02: "unsupported command",
    0x03: "address outside application flash",
    0x04: "misaligned address",
    0x05: "write halves arrived out of order",
    0x06: "flash read-back verification failed",
    0x07: "application CRC or marker is invalid",
}

ENTER_REPORT = bytes((0x53, 0x53, 0x55, 0x50, 0x01, 0x42, 0x4C, 0xA5))


class UpdateError(RuntimeError):
    pass


def _hid_module() -> Any:
    try:
        import hid  # type: ignore
    except ImportError as exc:
        raise UpdateError("hidapi is not installed; run: python3 -m pip install -r tools/requirements.txt") from exc
    return hid


def _first_device(hid: Any, pid: int) -> dict[str, Any] | None:
    devices = hid.enumerate(VID, pid)
    return devices[0] if devices else None


def _open_path(hid: Any, info: dict[str, Any]) -> Any:
    device = hid.device()
    device.open_path(info["path"])
    return device


def enter_bootloader(timeout: float = 10.0) -> Any:
    hid = _hid_module()
    info = _first_device(hid, BOOTLOADER_PID)
    if info is not None:
        return _open_path(hid, info)

    gamepad = _first_device(hid, GAMEPAD_PID)
    if gamepad is None:
        raise UpdateError(
            "No SwanSong USB found. Connect it directly, or hold Start + Power while plugging it in."
        )

    device = _open_path(hid, gamepad)
    try:
        # HIDAPI reserves byte zero for a report ID; this descriptor has ID zero.
        device.send_feature_report(bytes((0,)) + ENTER_REPORT)
    except OSError:
        # A reset can race the completion return on some HIDAPI/macOS versions.
        pass
    finally:
        device.close()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = _first_device(hid, BOOTLOADER_PID)
        if info is not None:
            return _open_path(hid, info)
        time.sleep(0.1)
    raise UpdateError("The controller did not reappear in update mode; hold Start + Power while reconnecting it.")


class Transport:
    def __init__(self, device: Any):
        self.device = device
        self.sequence = 0

    def command(
        self,
        command: int,
        *,
        address: int = 0,
        argument: int = 0,
        data: bytes = b"",
        timeout_ms: int = 2000,
    ) -> bytes:
        if len(data) > 56:
            raise UpdateError("internal error: command payload is too large")
        packet = bytearray(PACKET_SIZE)
        packet[0] = MAGIC
        packet[1] = PROTOCOL
        packet[2] = command
        packet[3] = self.sequence
        packet[4] = address & 0xFF
        packet[5] = (address >> 8) & 0xFF
        packet[6] = argument & 0xFF
        packet[8 : 8 + len(data)] = data

        written = self.device.write(bytes((0,)) + packet)
        if written not in (PACKET_SIZE, PACKET_SIZE + 1):
            raise UpdateError(f"short HID write ({written} bytes)")
        response = bytes(self.device.read(PACKET_SIZE, timeout_ms))
        if len(response) == PACKET_SIZE + 1 and response[0] == 0:
            response = response[1:]
        if len(response) != PACKET_SIZE:
            raise UpdateError("controller timed out during update")
        if response[0] != MAGIC or response[1] != PROTOCOL:
            raise UpdateError("controller returned an invalid update packet")
        if response[2] != (command | 0x80) or response[3] != self.sequence:
            raise UpdateError("controller response did not match the command")
        status = response[4]
        if status:
            raise UpdateError(f"controller rejected command 0x{command:02X}: {STATUS_NAMES.get(status, f'status 0x{status:02X}')}")
        self.sequence = (self.sequence + 1) & 0xFF
        return response


def _words_to_bytes(words: tuple[int, ...]) -> bytes:
    output = bytearray()
    for word in words:
        output.extend((word & 0xFF, (word >> 8) & 0xFF))
    return bytes(output)


def query(transport: Transport) -> dict[str, int]:
    response = transport.command(CMD_QUERY)
    return {
        "boot_major": response[5],
        "boot_minor": response[6],
        "row_words": response[7],
        "app_start": response[8] | (response[9] << 8),
        "app_last": response[10] | (response[11] << 8),
        "marker": response[12] | (response[13] << 8),
        "valid": response[14],
    }


def _validate_target(info: dict[str, int]) -> None:
    expected = {"row_words": ROW_WORDS, "app_start": APP_START, "app_last": APP_LAST, "marker": APP_MARKER}
    for name, value in expected.items():
        if info[name] != value:
            raise UpdateError(f"bootloader {name} is 0x{info[name]:X}, expected 0x{value:X}")


def install(transport: Transport, image: ApplicationImage, major: int, minor: int, reset: bool = True) -> None:
    info = query(transport)
    _validate_target(info)
    print(f"Bootloader {info['boot_major']}.{info['boot_minor']} detected")

    erase_rows = list(range(APP_START, APP_MARKER + 1, ROW_WORDS))
    print(f"Erasing {len(erase_rows)} rows...", flush=True)
    for index, address in enumerate(erase_rows, 1):
        transport.command(CMD_ERASE_ROW, address=address)
        if index % 16 == 0 or index == len(erase_rows):
            print(f"  erased {index}/{len(erase_rows)}", flush=True)

    app_rows = list(range(APP_START, APP_MARKER, ROW_WORDS))
    programmed_rows = [address for address in app_rows if any(word != 0x3FFF for word in image.row(address))]
    print(f"Programming {len(programmed_rows)} nonblank rows...", flush=True)
    for index, address in enumerate(programmed_rows, 1):
        row_bytes = _words_to_bytes(image.row(address))
        transport.command(CMD_WRITE_HALF, address=address, argument=0, data=row_bytes[:32])
        transport.command(CMD_WRITE_HALF, address=address, argument=1, data=row_bytes[32:])
        if index % 16 == 0 or index == len(programmed_rows):
            print(f"  programmed {index}/{len(programmed_rows)}", flush=True)

    print(f"Verifying {len(app_rows)} rows...", flush=True)
    for index, address in enumerate(app_rows, 1):
        expected = _words_to_bytes(image.row(address))
        first = transport.command(CMD_READ_HALF, address=address, argument=0)[8:40]
        second = transport.command(CMD_READ_HALF, address=address, argument=1)[8:40]
        if first + second != expected:
            raise UpdateError(f"read-back mismatch at program word 0x{address:04X}")
        if index % 16 == 0 or index == len(app_rows):
            print(f"  verified {index}/{len(app_rows)}", flush=True)

    finalize_data = bytes((major & 0xFF, minor & 0xFF))
    # CRC occupies the command's argument byte and byte 7; version starts at byte 8.
    packet = bytearray(PACKET_SIZE)
    packet[0] = MAGIC
    packet[1] = PROTOCOL
    packet[2] = CMD_FINALIZE
    packet[3] = transport.sequence
    packet[4] = APP_LAST & 0xFF
    packet[5] = APP_LAST >> 8
    packet[6] = image.crc & 0xFF
    packet[7] = image.crc >> 8
    packet[8:10] = finalize_data
    written = transport.device.write(bytes((0,)) + packet)
    if written not in (PACKET_SIZE, PACKET_SIZE + 1):
        raise UpdateError(f"short HID write ({written} bytes)")
    response = bytes(transport.device.read(PACKET_SIZE, 3000))
    if len(response) == PACKET_SIZE + 1 and response[0] == 0:
        response = response[1:]
    if len(response) != PACKET_SIZE or response[:4] != bytes((MAGIC, PROTOCOL, CMD_FINALIZE | 0x80, transport.sequence)):
        raise UpdateError("controller returned an invalid finalize response")
    if response[4]:
        raise UpdateError(f"controller rejected final image: {STATUS_NAMES.get(response[4], 'unknown status')}")
    transport.sequence = (transport.sequence + 1) & 0xFF

    info = query(transport)
    if not info["valid"]:
        raise UpdateError("controller did not mark the verified image valid")
    print(f"Installed firmware {major}.{minor}; CRC 0x{image.crc:04X}")
    if reset:
        transport.command(CMD_RESET)
        print("Controller restarted in gamepad mode")


def _version(value: str) -> tuple[int, int]:
    try:
        major_text, minor_text = value.split(".", 1)
        major, minor = int(major_text), int(minor_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("version must look like MAJOR.MINOR") from exc
    if not (0 <= major <= 255 and 0 <= minor <= 255):
        raise argparse.ArgumentTypeError("version components must be 0-255")
    return major, minor


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, help="swansong-usb-app.hex")
    parser.add_argument("--version", type=_version, default=(1, 0), metavar="MAJOR.MINOR")
    parser.add_argument("--check", action="store_true", help="validate the HEX without connecting hardware")
    parser.add_argument("--no-reset", action="store_true", help="leave the board in update mode after flashing")
    args = parser.parse_args()

    try:
        image = load_application(args.image)
        print(f"Validated {args.image}: {len(image.words)} words, CRC 0x{image.crc:04X}")
        if args.check:
            return 0
        device = enter_bootloader()
        try:
            install(Transport(device), image, args.version[0], args.version[1], not args.no_reset)
        finally:
            device.close()
    except (FirmwareError, UpdateError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
