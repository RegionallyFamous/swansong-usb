# SwanSong USB Rev D — factory programming and test

## Programming

- Device: Microchip PIC16F1459-I/SO
- Image: `swansong-usb.hex` at the programming-package root (combined bootloader + gamepad)
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
- Self-write protection: `0000h–0FFFh` protected; USB updates can write only `1000h–1FFFh`

For the lowest NRE at prototype quantity, MacroFab may pre-program U1 before
placement. If MacroFab instead uses TC1 in circuit, it should control VDD and VPP
sequencing per the PIC16F1459 programming specification. TC1 uses spring-loaded
contacts, so neither the factory nor the customer solders a programming header.
All programming and assembly remain MacroFab-supplied; no customer-supplied
parts are required.

## USB update and recovery

`swansong-usb.hex` contains the protected HID bootloader, relocated gamepad, and
validated application marker. After this one ICSP operation, normal releases use
`swansong-usb-app.hex` through the USB-C port. Holding Start + Power while USB is
connected forces recovery/update mode. An interrupted update leaves the marker
invalid and the controller in its bootloader. TC1 remains the solderless recovery
path if external ICSP ever damages the bootloader itself.

The image is compiler- and structure-verified but must be electrically qualified
on an assembled Rev D prototype before a production quantity is released.

## Functional test

1. Connect the assembled PCB to a USB 2.0 host with a normal USB-C data cable.
2. Confirm enumeration as `SwanSong USB`, generic HID gamepad, one IN endpoint.
3. Confirm neutral report is `00 00 08`.
4. Exercise X1/X2/X3/X4 and confirm hat values north/east/south/west and diagonals.
5. Exercise A, B, Y1, Y2, Y3, Y4, Start, Sound, and Power individually.
6. Confirm opposite D-pad directions cancel to neutral on that axis.
7. Flex the USB-C cable lightly and confirm there is no disconnect.
8. Hold Start + Power while reconnecting and confirm enumeration as
   `SwanSong USB Update`, then reconnect normally and confirm gamepad mode.

The development image uses VID/PID `04D8:005E` for the gamepad and `04D8:005F`
for the updater; replace both PIDs with assigned production values before sale.
