# Attribution and licensing

SwanSong USB is a modified hardware design derived from Zwenergy's
[SwanTroller](https://github.com/zwenergy/swantroller), specifically its RP2040
PCB outline, mounting geometry, membrane contacts, and routed button matrix.
SwanTroller is distributed under GNU GPL version 3. The preserved source Gerbers,
the modified hardware generator, generated manufacturing data, and associated
hardware documentation in this repository are distributed under the same license;
see `LICENSE`.

The design was substantially modified in July 2026 to replace the plug-in RP2040
module and SNES interface with a factory-assembled PIC16F1459 USB HID circuit,
USB-C receptacle, new routing, and SwanSong USB silkscreen.

Files under `firmware/` contain Microchip USB framework code. Those files retain
their embedded Microchip notices and may be used only with Microchip PIC products
under the terms stated in their source headers. They are not relicensed by the
hardware GPL notice above.
