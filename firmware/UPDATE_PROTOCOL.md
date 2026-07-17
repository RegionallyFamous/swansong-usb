# SwanSong USB update protocol v1

This document is the host-side contract implemented by
`bootloader/bootloader.c` and `tools/swansong_usb_update.py`. It is intentionally
simple enough to add to SwanSong Desktop without a kernel driver.

## USB modes

- Gamepad: HID `04D8:005E`, product `SwanSong USB`.
- Updater: HID `04D8:005F`, product `SwanSong USB Update`, one 64-byte interrupt
  OUT report and one 64-byte interrupt IN report, no report IDs.
- To leave gamepad mode, send the eight-byte HID feature report
  `53 53 55 50 01 42 4C A5`. The gamepad waits 25 USB frames, executes the PIC
  `RESET` instruction, and the bootloader enumerates under the updater PID.
- Holding Start + Power during connection forces updater mode.

HIDAPI expects a leading zero report-ID byte in front of both the feature report
and 64-byte OUT reports. That byte is transport metadata and is not delivered to
the PIC.

## 64-byte command report

| Byte | Meaning |
| --- | --- |
| 0 | Magic `0x53` |
| 1 | Protocol version `0x01` |
| 2 | Command |
| 3 | Sequence number, echoed by the response |
| 4–5 | Little-endian PIC program-word address |
| 6–7 | Command arguments |
| 8–39 | Up to 16 little-endian 14-bit PIC words |
| 40–63 | Reserved, send zero |

The response uses the same header, with bit 7 set in the command. Byte 4 is the
status (`0` for success). Every command has exactly one response.

| Command | Value | Operation |
| --- | --- | --- |
| Query | `0x01` | Return bootloader version, row size, bounds, and validity |
| Erase row | `0x02` | Erase one aligned 32-word application row |
| Write half | `0x03` | Stage half 0 or 1; half 1 commits and verifies the row |
| Read half | `0x04` | Return 16 words for host read-back verification |
| Finalize | `0x05` | Verify CRC, write the atomic marker, and validate again |
| Reset | `0x06` | Leave updater mode only when a valid application exists |

For Finalize, bytes 4–5 contain the final application address (`0x1FDF`), bytes
6–7 contain CRC-16/CCITT-FALSE, and bytes 8–9 contain major/minor version bytes.
CRC starts at `0xFFFF`, uses polynomial `0x1021`, and covers the low byte then
high byte of every 14-bit word from `0x1000` through `0x1FDF`; absent HEX words
are `0x3FFF`.

## Safety properties

- `CONFIG2.WRT=HALF` prevents self-write to bootloader words `0x0000–0x0FFF`.
- All bootloader erase/write commands independently reject lower-half addresses.
- The marker row (`0x1FE0–0x1FFF`) is erased before an update and committed only
  after device-side CRC verification.
- The bootloader recomputes CRC on every normal boot. A missing or bad marker
  keeps the controller in updater mode.
