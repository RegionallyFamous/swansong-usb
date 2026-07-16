# SwanSong USB Rev D — factory programming and test

## Programming

- Device: Microchip PIC16F1459-I/SO
- Image: `swansong-usb.hex` at the programming-package root
- Preferred prototype method: program U1 before placement to avoid fixture NRE
- Standard solderless method: high-voltage ICSP through bottom footprint `TC1`
- Cable: Tag-Connect `TC2030-PKT-NL` for PICkit 3/4/5
- TC1 pin 1: VPP/MCLR
- TC1 pin 2: VDD/VBUS
- TC1 pin 3: GND
- TC1 pin 4: PGD/ICSPDAT (A / RC0 / U1 pin 16)
- TC1 pin 5: PGC/ICSPCLK (B / RC1 / U1 pin 15)
- TC1 pin 6: not connected; LVP is disabled
- TC1 is DNL: do not install a connector or apply solder paste
- Configuration: MCLR disabled at runtime, LVP disabled, internal oscillator with 3x PLL

For the lowest NRE at prototype quantity, MacroFab may pre-program U1 before
placement. If MacroFab instead uses TC1 in circuit, it should control VDD and VPP
sequencing per the PIC16F1459 programming specification. TC1 uses spring-loaded
contacts, so neither the factory nor the customer solders a programming header.
All programming and assembly remain MacroFab-supplied; no customer-supplied
parts are required.

## USB-update blocker

`swansong-usb.hex` is presently the gamepad application only.
Before ordering boards that promise USB-only field updates, replace it with a
tested combined image containing both the bootloader and relocated application.
TC1 is the solderless recovery path if that bootloader is ever corrupted.

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
