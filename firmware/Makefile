XC8 ?= xc8-cc
DFP ?=
DFP_FLAG := $(if $(strip $(DFP)),-mdfp=$(DFP),)

TARGET := build/swansong-usb.elf
SOURCES := \
	src/main.c \
	src/usb_descriptors.c \
	usb/usb_device.c \
	usb/usb_function_hid.c

.PHONY: all clean

all: $(TARGET)

$(TARGET): $(SOURCES) src/usb_config.h src/HardwareProfile.h
	mkdir -p build
	$(XC8) -mcpu=16F1459 $(DFP_FLAG) -DHI_TECH_C \
		-Isrc -Iinclude $(SOURCES) -o $(TARGET)

clean:
	rm -rf build
