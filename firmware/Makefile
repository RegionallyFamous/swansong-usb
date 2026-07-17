XC8 ?= xc8-cc
DFP ?=
PYTHON ?= python3
DFP_FLAG := $(if $(strip $(DFP)),-mdfp=$(DFP),)

BUILD_DIR := build
APP_ELF := $(BUILD_DIR)/swansong-usb-app.elf
APP_HEX := $(BUILD_DIR)/swansong-usb-app.hex
BOOT_ELF := $(BUILD_DIR)/swansong-usb-bootloader.elf
BOOT_HEX := $(BUILD_DIR)/swansong-usb-bootloader.hex
FACTORY_HEX := $(BUILD_DIR)/swansong-usb-factory.hex
LEGACY_HEX := $(BUILD_DIR)/swansong-usb.hex

USB_SOURCES := usb/usb_device.c usb/usb_function_hid.c
APP_SOURCES := src/main.c src/usb_descriptors.c $(USB_SOURCES)
BOOT_SOURCES := bootloader/bootloader.c bootloader/usb_descriptors.c $(USB_SOURCES)
COMMON_HEADERS := common/swansong_update.h include/Compiler.h

.PHONY: all clean verify test

all: $(FACTORY_HEX) $(LEGACY_HEX)

$(BUILD_DIR):
	mkdir -p $@

$(APP_ELF): $(APP_SOURCES) src/usb_config.h src/HardwareProfile.h $(COMMON_HEADERS) | $(BUILD_DIR)
	$(XC8) -mcpu=16F1459 $(DFP_FLAG) -DHI_TECH_C \
		-DSWANSONG_BOOTLOADED_APPLICATION -Isrc -Iinclude \
		-mcodeoffset=0x1000 '-mreserve=rom@1FE0:1FFF' \
		$(APP_SOURCES) -o $@

$(BOOT_ELF): $(BOOT_SOURCES) bootloader/usb_config.h bootloader/HardwareProfile.h $(COMMON_HEADERS) | $(BUILD_DIR)
	$(XC8) -mcpu=16F1459 $(DFP_FLAG) -DHI_TECH_C \
		-Ibootloader -Iinclude '-mreserve=rom@1000:1FFF' \
		$(BOOT_SOURCES) -o $@

$(APP_HEX): $(APP_ELF)
	@test -f $@

$(BOOT_HEX): $(BOOT_ELF)
	@test -f $@

$(FACTORY_HEX): $(BOOT_HEX) $(APP_HEX) ../tools/swansong_firmware.py
	$(PYTHON) ../tools/swansong_firmware.py factory \
		--boot $(BOOT_HEX) --app $(APP_HEX) --output $@ --version 1.0

$(LEGACY_HEX): $(FACTORY_HEX)
	cp $(FACTORY_HEX) $(LEGACY_HEX)

verify: all
	$(PYTHON) ../tools/swansong_firmware.py verify \
		--boot $(BOOT_HEX) --app $(APP_HEX) --factory $(FACTORY_HEX)

test:
	PYTHONPATH=../tools $(PYTHON) -m unittest discover -s ../tools/tests -v

clean:
	rm -rf $(BUILD_DIR)
