#define __USB_DESCRIPTORS_C
#include "./USB/usb.h"
#include "./USB/usb_function_hid.h"

ROM USB_DEVICE_DESCRIPTOR device_dsc = {
    0x12,
    USB_DESCRIPTOR_DEVICE,
    0x0200,
    0x00,
    0x00,
    0x00,
    USB_EP0_BUFF_SIZE,
    MY_VID,
    MY_PID,
    0x0100,
    0x01,
    0x02,
    0x00,
    0x01
};

ROM BYTE configDescriptor1[] = {
    0x09, USB_DESCRIPTOR_CONFIGURATION, DESC_CONFIG_WORD(0x0029),
    1, 1, 0, _DEFAULT, 50,

    0x09, USB_DESCRIPTOR_INTERFACE,
    0, 0, 2, HID_INTF, 0, 0, 0,

    0x09, DSC_HID, DESC_CONFIG_WORD(0x0111),
    0, HID_NUM_OF_DSC, DSC_RPT, DESC_CONFIG_WORD(HID_RPT01_SIZE),

    0x07, USB_DESCRIPTOR_ENDPOINT, HID_EP | _EP_IN, _INTERRUPT,
    DESC_CONFIG_WORD(HID_INT_IN_EP_SIZE), 0x01,

    0x07, USB_DESCRIPTOR_ENDPOINT, HID_EP | _EP_OUT, _INTERRUPT,
    DESC_CONFIG_WORD(HID_INT_OUT_EP_SIZE), 0x01
};

ROM struct { BYTE bLength; BYTE bDscType; WORD string[1]; } sd000 = {
    sizeof(sd000), USB_DESCRIPTOR_STRING, {0x0409}
};

ROM struct { BYTE bLength; BYTE bDscType; WORD string[8]; } sd001 = {
    sizeof(sd001), USB_DESCRIPTOR_STRING,
    {'S','w','a','n','S','o','n','g'}
};

ROM struct { BYTE bLength; BYTE bDscType; WORD string[19]; } sd002 = {
    sizeof(sd002), USB_DESCRIPTOR_STRING,
    {'S','w','a','n','S','o','n','g',' ','U','S','B',' ','U','p','d','a','t','e'}
};

ROM BYTE *ROM USB_CD_Ptr[] = {
    (ROM BYTE *ROM)&configDescriptor1
};

ROM BYTE *ROM USB_SD_Ptr[] = {
    (ROM BYTE *ROM)&sd000,
    (ROM BYTE *ROM)&sd001,
    (ROM BYTE *ROM)&sd002
};

/* One 64-byte vendor-defined input report and one 64-byte output report. */
ROM struct{BYTE report[HID_RPT01_SIZE];}hid_rpt01={{
    0x06, 0x00, 0xFF,
    0x09, 0x01,
    0xA1, 0x01,
    0x15, 0x00,
    0x26, 0xFF, 0x00,
    0x75, 0x08,
    0x95, 0x40,
    0x09, 0x01,
    0x81, 0x02,
    0x95, 0x40,
    0x09, 0x01,
    0x91, 0x02,
    0xC0
}};
