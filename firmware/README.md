# SwanSong USB firmware

This is a crystal-free, full-speed USB HID gamepad firmware for the
PIC16F1459-I/SO on SwanSong USB Rev C.

## Controls

- D-pad: X1 up, X2 right, X3 down, X4 left
- Buttons 1–9: A, B, Y1, Y2, Y3, Y4, Start, Sound, Power
- Input reports: 3 bytes, one 8-way hat plus nine buttons
- Debounce: 5 ms, sampled from USB Start-of-Frame timing

## Build

From this directory:

```sh
make
```

`XC8` defaults to `xc8-cc` on `PATH`. If the compiler cannot locate the
PIC12-16F1xxx Device Family Pack automatically, provide its `xc8` directory:

```sh
make XC8=/path/to/xc8-cc DFP=/path/to/PIC12-16F1xxx_DFP/version/xc8
```

The factory image is `build/swansong-usb.hex`. The checked-in build was made
with MPLAB XC8 4.00 and PIC12-16F1xxx DFP 1.9.258.

## USB identity warning

The current HEX uses Microchip's demo VID/PID `04D8:005E` for engineering
prototypes. It must not be used as a commercial product identity. Before sale,
obtain a PID through Microchip's USB VID sublicense program (or use an owned
VID/PID), update `src/usb_config.h`, rebuild, and give MacroFab the new HEX.
