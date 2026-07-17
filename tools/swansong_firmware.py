#!/usr/bin/env python3
"""Build and validate SwanSong USB PIC16F1459 firmware images."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

APP_START = 0x1000
APP_MARKER = 0x1FE0
APP_LAST = APP_MARKER - 1
FLASH_LAST = 0x1FFF
ROW_WORDS = 32
MARKER_MAGIC_0 = 0x2953
MARKER_MAGIC_1 = 0x155A
CONFIG_BYTES = range(0x1000E, 0x10012)


class FirmwareError(ValueError):
    pass


def _record(address: int, record_type: int, data: bytes = b"") -> str:
    payload = bytes((len(data), (address >> 8) & 0xFF, address & 0xFF, record_type)) + data
    checksum = (-sum(payload)) & 0xFF
    return ":" + (payload + bytes((checksum,))).hex().upper()


def read_hex(path: Path | str) -> dict[int, int]:
    memory: dict[int, int] = {}
    base = 0
    eof_seen = False
    for line_number, raw_line in enumerate(Path(path).read_text().splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise FirmwareError(f"{path}:{line_number}: Intel HEX record must start with ':'")
        try:
            record = bytes.fromhex(line[1:])
        except ValueError as exc:
            raise FirmwareError(f"{path}:{line_number}: invalid hexadecimal data") from exc
        if len(record) < 5 or len(record) != record[0] + 5:
            raise FirmwareError(f"{path}:{line_number}: invalid record length")
        if sum(record) & 0xFF:
            raise FirmwareError(f"{path}:{line_number}: checksum mismatch")

        count = record[0]
        address = (record[1] << 8) | record[2]
        record_type = record[3]
        data = record[4 : 4 + count]
        if record_type == 0x00:
            for offset, value in enumerate(data):
                absolute = base + address + offset
                previous = memory.get(absolute)
                if previous is not None and previous != value:
                    raise FirmwareError(f"{path}:{line_number}: conflicting byte at 0x{absolute:X}")
                memory[absolute] = value
        elif record_type == 0x01:
            eof_seen = True
            break
        elif record_type == 0x02:
            if count != 2:
                raise FirmwareError(f"{path}:{line_number}: bad extended-segment record")
            base = int.from_bytes(data, "big") << 4
        elif record_type == 0x04:
            if count != 2:
                raise FirmwareError(f"{path}:{line_number}: bad extended-linear record")
            base = int.from_bytes(data, "big") << 16
        elif record_type not in (0x03, 0x05):
            raise FirmwareError(f"{path}:{line_number}: unsupported record type 0x{record_type:02X}")
    if not eof_seen:
        raise FirmwareError(f"{path}: missing end-of-file record")
    return memory


def write_hex(path: Path | str, memory: dict[int, int]) -> None:
    lines: list[str] = []
    addresses = sorted(memory)
    index = 0
    active_upper: int | None = None
    while index < len(addresses):
        start = addresses[index]
        upper = start >> 16
        if upper != active_upper:
            lines.append(_record(0, 0x04, upper.to_bytes(2, "big")))
            active_upper = upper

        data = bytearray((memory[start],))
        index += 1
        while index < len(addresses) and len(data) < 16:
            current = addresses[index]
            if (current >> 16) != upper or current != start + len(data):
                break
            data.append(memory[current])
            index += 1
        lines.append(_record(start & 0xFFFF, 0x00, bytes(data)))
    lines.append(_record(0, 0x01))
    Path(path).write_text("\n".join(lines) + "\n")


def word_at(memory: dict[int, int], word_address: int, default: int = 0x3FFF) -> int:
    byte_address = word_address * 2
    low = memory.get(byte_address)
    high = memory.get(byte_address + 1)
    if low is None and high is None:
        return default
    if low is None or high is None:
        raise FirmwareError(f"incomplete PIC word at 0x{word_address:04X}")
    value = low | (high << 8)
    if value > 0x3FFF:
        raise FirmwareError(f"non-14-bit PIC word 0x{value:04X} at 0x{word_address:04X}")
    return value


def put_word(memory: dict[int, int], word_address: int, value: int) -> None:
    if not 0 <= value <= 0x3FFF:
        raise FirmwareError(f"word 0x{value:X} does not fit the PIC16F1459")
    byte_address = word_address * 2
    memory[byte_address] = value & 0xFF
    memory[byte_address + 1] = value >> 8


def crc16_words(words: Iterable[int]) -> int:
    crc = 0xFFFF
    for word in words:
        for value in (word & 0xFF, (word >> 8) & 0xFF):
            crc ^= value << 8
            for _ in range(8):
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


@dataclass(frozen=True)
class ApplicationImage:
    words: tuple[int, ...]
    crc: int

    def row(self, address: int) -> tuple[int, ...]:
        offset = address - APP_START
        return self.words[offset : offset + ROW_WORDS]


def load_application(path: Path | str) -> ApplicationImage:
    memory = read_hex(path)
    app_first_byte = APP_START * 2
    app_end_byte = (APP_MARKER * 2) - 1
    outside = [address for address in memory if not app_first_byte <= address <= app_end_byte]
    if outside:
        raise FirmwareError(
            f"application image writes outside 0x{APP_START:04X}-0x{APP_LAST:04X}; "
            f"first byte is 0x{min(outside):X}"
        )
    if word_at(memory, APP_START) == 0x3FFF:
        raise FirmwareError("application reset vector is blank")
    words = tuple(word_at(memory, address) for address in range(APP_START, APP_LAST + 1))
    return ApplicationImage(words=words, crc=crc16_words(words))


def _merge(target: dict[int, int], source: dict[int, int], label: str) -> None:
    for address, value in source.items():
        previous = target.get(address)
        if previous is not None and previous != value:
            raise FirmwareError(f"{label} conflicts at byte address 0x{address:X}")
        target[address] = value


def build_factory(boot_path: Path, app_path: Path, output: Path, major: int, minor: int) -> None:
    if not 0 <= major <= 255 or not 0 <= minor <= 255:
        raise FirmwareError("firmware version components must be 0-255")
    boot = read_hex(boot_path)
    app_memory = read_hex(app_path)
    image = load_application(app_path)

    boot_app_bytes = [address for address in boot if APP_START * 2 <= address <= FLASH_LAST * 2 + 1]
    if boot_app_bytes:
        raise FirmwareError(f"bootloader overlaps application at byte 0x{min(boot_app_bytes):X}")

    factory: dict[int, int] = {}
    _merge(factory, boot, "bootloader")
    _merge(factory, app_memory, "application")
    marker_words = [0x3FFF] * ROW_WORDS
    marker_words[0] = MARKER_MAGIC_0
    marker_words[1] = MARKER_MAGIC_1
    marker_words[2] = APP_LAST
    marker_words[3] = image.crc & 0xFF
    marker_words[4] = image.crc >> 8
    marker_words[5] = major
    marker_words[6] = minor
    for offset, value in enumerate(marker_words):
        put_word(factory, APP_MARKER + offset, value)
    write_hex(output, factory)


def verify_images(boot_path: Path, app_path: Path, factory_path: Path) -> None:
    boot = read_hex(boot_path)
    app = load_application(app_path)
    factory = read_hex(factory_path)

    if word_at(boot, 0) == 0x3FFF:
        raise FirmwareError("bootloader reset vector is blank")
    if any(APP_START * 2 <= address <= FLASH_LAST * 2 + 1 for address in boot):
        raise FirmwareError("bootloader crosses the protected 0x1000 boundary")
    if not all(address in boot for address in CONFIG_BYTES):
        raise FirmwareError("bootloader configuration words are missing")
    config2 = boot[0x10010] | (boot[0x10011] << 8)
    if config2 & 0x0003 != 0x0001:
        raise FirmwareError("CONFIG2 does not hardware-write-protect 0x0000-0x0FFF")

    if word_at(factory, APP_MARKER) != MARKER_MAGIC_0 or word_at(factory, APP_MARKER + 1) != MARKER_MAGIC_1:
        raise FirmwareError("factory image marker magic is missing")
    if word_at(factory, APP_MARKER + 2) != APP_LAST:
        raise FirmwareError("factory image marker has the wrong application boundary")
    marker_crc = (word_at(factory, APP_MARKER + 3) & 0xFF) | ((word_at(factory, APP_MARKER + 4) & 0xFF) << 8)
    if marker_crc != app.crc:
        raise FirmwareError(f"factory marker CRC 0x{marker_crc:04X} != application CRC 0x{app.crc:04X}")
    for address in range(APP_START, APP_LAST + 1):
        app_word = app.words[address - APP_START]
        if word_at(factory, address) != app_word:
            raise FirmwareError(f"factory application differs at word 0x{address:04X}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    factory = subparsers.add_parser("factory", help="merge bootloader and application images")
    factory.add_argument("--boot", type=Path, required=True)
    factory.add_argument("--app", type=Path, required=True)
    factory.add_argument("--output", type=Path, required=True)
    factory.add_argument("--version", default="1.0")

    verify = subparsers.add_parser("verify", help="validate all release images")
    verify.add_argument("--boot", type=Path, required=True)
    verify.add_argument("--app", type=Path, required=True)
    verify.add_argument("--factory", type=Path, required=True)

    inspect = subparsers.add_parser("inspect", help="inspect a USB-update application image")
    inspect.add_argument("image", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "factory":
            try:
                major_text, minor_text = args.version.split(".", 1)
                major, minor = int(major_text), int(minor_text)
            except ValueError as exc:
                raise FirmwareError("version must look like MAJOR.MINOR") from exc
            build_factory(args.boot, args.app, args.output, major, minor)
            image = load_application(args.app)
            print(f"Wrote {args.output} (application CRC 0x{image.crc:04X})")
        elif args.command == "verify":
            verify_images(args.boot, args.app, args.factory)
            image = load_application(args.app)
            print(f"Firmware images verified (application CRC 0x{image.crc:04X})")
        else:
            image = load_application(args.image)
            programmed = sum(word != 0x3FFF for word in image.words)
            print(f"PIC16F1459 application: {programmed}/{len(image.words)} programmed words, CRC 0x{image.crc:04X}")
    except (FirmwareError, OSError) as exc:
        raise SystemExit(f"error: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
