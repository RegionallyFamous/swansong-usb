# SwanSong USB firmware

This directory builds a recoverable, two-stage firmware system for the
PIC16F1459-I/SO on SwanSong USB Rev D:

- a generic USB HID gamepad (nine buttons and an eight-way hat);
- a driverless USB HID updater;
- a combined image for the first PICkit/Tag-Connect programming operation.

## Release images

| File | Purpose |
| --- | --- |
| `build/swansong-usb.hex` | Combined factory image; use this for a blank chip |
| `build/swansong-usb-factory.hex` | Same combined image, with an explicit name |
| `build/swansong-usb-app.hex` | USB-update image; never use this on a blank chip |
| `build/swansong-usb-bootloader.hex` | Protected bootloader service image |

The lower half of program flash (`0x0000–0x0FFF`) contains the bootloader and is
hardware self-write-protected by `CONFIG2.WRT=HALF`. The gamepad occupies
`0x1000–0x1FDF`. The last 32-word row contains the application boundary, version,
and CRC. That marker is written only after full read-back verification, so an
interrupted update remains in the bootloader.

## Build and verification

From this directory:

```sh
make
make verify
make test
```

`XC8` defaults to `xc8-cc` on `PATH`. If the compiler cannot locate the
PIC12-16F1xxx Device Family Pack automatically, provide its `xc8` directory:

```sh
make XC8=/path/to/xc8-cc DFP=/path/to/PIC12-16F1xxx_DFP/version/xc8
```

The checked-in release was built with MPLAB XC8 4.00 and PIC12-16F1xxx DFP
1.9.258. `make verify` checks both flash boundaries, the reset vectors, the
hardware write-protection bits, the merged application data, and the marker CRC.

## Updating through USB-C

Install the one host dependency from the repository root:

```sh
python3 -m venv .venv
.venv/bin/pip install -r tools/requirements.txt
```

Then connect the controller and run:

```sh
.venv/bin/python tools/swansong_usb_update.py firmware/build/swansong-usb-app.hex
```

The tool validates the HEX before touching the device, switches the gamepad into
update mode, erases/programs/verifies every application row, commits the CRC
marker, and restarts the controller. No soldering or programmer is needed after
the initial combined image has been installed. See `UPDATE_PROTOCOL.md` for the
wire format used by the CLI and future SwanSong Desktop integration.

Recovery options:

- Hold **Start + Power** while connecting USB to force the HID bootloader.
- If the protected bootloader itself is ever damaged by external ICSP, use a
  PICkit 5 and `TC2030-PKT-NL` cable to reinstall `swansong-usb.hex`.

## Controls

- D-pad: X1 up, X2 right, X3 down, X4 left
- Buttons 1–9: A, B, Y1, Y2, Y3, Y4, Start, Sound, Power
- Input reports: 3 bytes, one 8-way hat plus nine buttons
- Debounce: 5 ms, sampled from USB Start-of-Frame timing

## USB identity warning

The engineering build uses Microchip's demo VID with application PID `005E` and
bootloader PID `005F`. These values are suitable only for prototypes. Obtain a
licensed/owned VID and PIDs, update the shared constants in
`common/swansong_update.h`, rebuild, and re-run all checks before commercial sale.
