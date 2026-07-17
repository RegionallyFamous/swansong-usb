# SwanSong USB — MacroFab upload order

Project: SwanSong USB — Rev D

1. On the Design page, upload `01-swansong-usb-gerbers.zip`.
2. Confirm the 11 imported manufacturing layers: top/bottom copper, top/bottom solder mask, top/bottom silkscreen, top/bottom paste, board outline, PTH drill, and NPTH drill.
3. Confirm the Design Rule Check shows the Standard lane: 10 mil plated drill, at least 5 mil copper spacing, and at least 10 mil copper-to-edge clearance. This revision emits flattened final copper with a 16 mil keepout around the outside route, all nine preserved non-plated mechanical holes, and TC1's three 39 mil non-plated alignment holes. J1 has no locating pegs; all 20 of its holes are plated.
4. Upload `02-swansong-usb-bom.xlsx` when MacroFab asks for the bill of materials.
5. Upload `03-swansong-usb-placement.XYRS` when MacroFab asks for placement data.
6. Set every component to MacroFab-supplied inventory; do not select customer-supplied parts. U1 is the SOIC part `PIC16F1459-I/SO`, and J1 is the stocked GCT through-hole connector `USB4085-GF-A`.
7. Ask MacroFab for turnkey programming and functional test, using `04-swansong-usb-firmware-and-test.zip`. Pre-programming U1 before placement is preferred at prototype quantity.
8. Confirm TC1 is DNL: it must not appear as a BOM or placement item and must receive no solder paste. Its six exposed bottom pads and three asymmetric alignment holes are the solderless ICSP interface.
9. Before ordering, visually verify the bottom-side placement and rotation of J1 (USB-C), U1, RN1, and RN2. J1's mating face must project slightly into the center of the right-side accessory/headphone-adapter recess.

The firmware package contains the combined protected bootloader and gamepad
image. It uses Microchip's development VID with gamepad PID `005E` and updater
PID `005F`; obtain assigned production values before commercial release.

The combined image passes compiler, memory-boundary, configuration-word,
read-back-protocol, and CRC tests. Electrically qualify USB gamepad and update/
recovery behavior on an assembled Rev D prototype before releasing production
quantity.
