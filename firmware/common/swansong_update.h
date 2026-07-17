#ifndef SWANSONG_UPDATE_H
#define SWANSONG_UPDATE_H

/* PIC16F1459 program addresses are 14-bit words, not byte addresses. */
#define SWANSONG_APP_START             0x1000u
#define SWANSONG_APP_MARKER            0x1FE0u
#define SWANSONG_APP_LAST              (SWANSONG_APP_MARKER - 1u)
#define SWANSONG_FLASH_LAST            0x1FFFu
#define SWANSONG_FLASH_ROW_WORDS       32u
#define SWANSONG_FLASH_HALF_WORDS      16u

/* The two marker magic values fit in 14-bit PIC program words. */
#define SWANSONG_MARKER_MAGIC_0        0x2953u
#define SWANSONG_MARKER_MAGIC_1        0x155Au

#define SWANSONG_USB_VID               0x04D8u
#define SWANSONG_GAMEPAD_PID           0x005Eu
#define SWANSONG_BOOTLOADER_PID        0x005Fu

#define SWANSONG_UPDATE_PROTOCOL       1u
#define SWANSONG_UPDATE_PACKET_SIZE    64u
#define SWANSONG_UPDATE_MAGIC          0x53u

#define SWANSONG_CMD_QUERY             0x01u
#define SWANSONG_CMD_ERASE_ROW         0x02u
#define SWANSONG_CMD_WRITE_HALF        0x03u
#define SWANSONG_CMD_READ_HALF         0x04u
#define SWANSONG_CMD_FINALIZE          0x05u
#define SWANSONG_CMD_RESET             0x06u
#define SWANSONG_RESPONSE_FLAG         0x80u

#define SWANSONG_STATUS_OK             0x00u
#define SWANSONG_STATUS_BAD_PACKET     0x01u
#define SWANSONG_STATUS_BAD_COMMAND    0x02u
#define SWANSONG_STATUS_RANGE          0x03u
#define SWANSONG_STATUS_ALIGNMENT      0x04u
#define SWANSONG_STATUS_SEQUENCE       0x05u
#define SWANSONG_STATUS_VERIFY         0x06u
#define SWANSONG_STATUS_IMAGE          0x07u

/* Eight-byte feature report understood by the normal gamepad application. */
#define SWANSONG_ENTER_REPORT_SIZE     8u

#endif
