# SwanSong USB Rev B — factory programming and test

## Programming

- Device: Microchip PIC16F1459-I/SO
- Image: `firmware/build/swansong-usb.hex`
- Preferred method: program U1 before placement to avoid fixture NRE
- Alternate method: high-voltage ICSP after assembly using the exposed pogo contacts
- VPP/MCLR: bottom pad marked `VPP` (pin 4)
- PGC: exposed B control landing (RC1 / pin 15)
- PGD: exposed A control landing (RC0 / pin 16)
- VDD/GND: USB-C VBUS and GND fixture contacts
- Configuration: MCLR disabled at runtime, LVP disabled, internal oscillator with 3x PLL

For the lowest NRE at prototype quantity, MacroFab should pre-program U1 before
placement. If MacroFab instead uses an in-circuit fixture, it should control VDD
and VPP sequencing per the PIC16F1459 programming specification. All programming
and assembly remain MacroFab-supplied; no customer-supplied parts are required.

## Functional test

1. Connect the assembled PCB to a USB 2.0 host with a normal USB-C data cable.
2. Confirm enumeration as `SwanSong USB`, generic HID gamepad, one IN endpoint.
3. Confirm neutral report is `00 00 08`.
4. Exercise X1/X2/X3/X4 and confirm hat values north/east/south/west and diagonals.
5. Exercise A, B, Y1, Y2, Y3, Y4, Start, Sound, and Power individually.
6. Confirm opposite D-pad directions cancel to neutral on that axis.
7. Flex the USB-C cable lightly and confirm there is no disconnect.

The development image uses VID/PID `04D8:005E`; replace it with an assigned
production PID before commercial release.
