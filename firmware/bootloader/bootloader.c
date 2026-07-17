#include <xc.h>
#include "GenericTypeDefs.h"
#include "Compiler.h"
#include "./USB/usb.h"
#include "./USB/usb_function_hid.h"
#include "HardwareProfile.h"
#include "../common/swansong_update.h"

/* Protect 0000-0FFF from self-write while leaving the application writable. */
#pragma config FOSC = INTOSC, WDTE = OFF, PWRTE = ON, MCLRE = OFF
#pragma config CP = OFF, BOREN = ON, CLKOUTEN = OFF, IESO = OFF, FCMEN = OFF
#pragma config WRT = HALF, CPUDIV = NOCLKDIV, USBLSCLK = 48MHz
#pragma config PLLMULT = 3x, PLLEN = ENABLED, STVREN = ON
#pragma config BORV = LO, LPBOR = OFF, LVP = OFF

#define BOOTLOADER_VERSION_MAJOR 1u
#define BOOTLOADER_VERSION_MINOR 0u

static BYTE rxPacket[SWANSONG_UPDATE_PACKET_SIZE] __at(0x2050);
static BYTE txPacket[SWANSONG_UPDATE_PACKET_SIZE] __at(0x2090);
static BYTE rowData[SWANSONG_FLASH_ROW_WORDS * 2u];
static USB_HANDLE rxHandle;
static USB_HANDLE txHandle;
static WORD stagedRow;
static BYTE stagedFirstHalf;
static BYTE resetPending;

static WORD packetAddress(void)
{
    return (WORD)rxPacket[4] | ((WORD)rxPacket[5] << 8);
}

static WORD flashReadWord(WORD address)
{
    PMADRL = (BYTE)address;
    PMADRH = (BYTE)(address >> 8);
    PMCON1bits.CFGS = 0;
    PMCON1bits.RD = 1;
    asm("nop");
    asm("nop");
    return PMDAT & 0x3FFFu;
}

static void flashUnlock(void)
{
    PMCON2 = 0x55;
    PMCON2 = 0xAA;
    PMCON1bits.WR = 1;
    asm("nop");
    asm("nop");
}

static BYTE flashEraseRow(WORD address)
{
    BYTE wasGie;

    if((address < SWANSONG_APP_START) || (address > SWANSONG_APP_MARKER))
    {
        return SWANSONG_STATUS_RANGE;
    }
    if((address & (SWANSONG_FLASH_ROW_WORDS - 1u)) != 0u)
    {
        return SWANSONG_STATUS_ALIGNMENT;
    }

    wasGie = INTCONbits.GIE;
    INTCONbits.GIE = 0;
    PMADRL = (BYTE)address;
    PMADRH = (BYTE)(address >> 8);
    PMCON1bits.CFGS = 0;
    PMCON1bits.FREE = 1;
    PMCON1bits.WREN = 1;
    flashUnlock();
    PMCON1bits.WREN = 0;
    PMCON1bits.FREE = 0;
    INTCONbits.GIE = wasGie;

    return (flashReadWord(address) == 0x3FFFu) ?
        SWANSONG_STATUS_OK : SWANSONG_STATUS_VERIFY;
}

static BYTE flashWriteRow(WORD address, BYTE *bytes)
{
    BYTE i;
    BYTE wasGie;
    WORD value;

    if((address < SWANSONG_APP_START) || (address > SWANSONG_APP_MARKER))
    {
        return SWANSONG_STATUS_RANGE;
    }
    if((address & (SWANSONG_FLASH_ROW_WORDS - 1u)) != 0u)
    {
        return SWANSONG_STATUS_ALIGNMENT;
    }

    wasGie = INTCONbits.GIE;
    INTCONbits.GIE = 0;
    PMCON1bits.CFGS = 0;
    PMCON1bits.FREE = 0;
    PMCON1bits.WREN = 1;

    for(i = 0; i < SWANSONG_FLASH_ROW_WORDS; i++)
    {
        value = (WORD)bytes[i * 2u] | ((WORD)bytes[(i * 2u) + 1u] << 8);
        PMADRL = (BYTE)(address + i);
        PMADRH = (BYTE)((address + i) >> 8);
        PMDAT = value & 0x3FFFu;
        PMCON1bits.LWLO = (i == (SWANSONG_FLASH_ROW_WORDS - 1u)) ? 0 : 1;
        flashUnlock();
    }

    PMCON1bits.WREN = 0;
    PMCON1bits.LWLO = 0;
    INTCONbits.GIE = wasGie;

    for(i = 0; i < SWANSONG_FLASH_ROW_WORDS; i++)
    {
        value = (WORD)bytes[i * 2u] | ((WORD)bytes[(i * 2u) + 1u] << 8);
        if(flashReadWord(address + i) != (value & 0x3FFFu))
        {
            return SWANSONG_STATUS_VERIFY;
        }
    }
    return SWANSONG_STATUS_OK;
}

static WORD crcByte(WORD crc, BYTE value)
{
    BYTE bit;
    crc ^= (WORD)value << 8;
    for(bit = 0; bit < 8u; bit++)
    {
        crc = (crc & 0x8000u) ? (WORD)((crc << 1) ^ 0x1021u) : (WORD)(crc << 1);
    }
    return crc;
}

static WORD imageCrc(WORD lastAddress)
{
    WORD address;
    WORD value;
    WORD crc = 0xFFFFu;

    for(address = SWANSONG_APP_START; address <= lastAddress; address++)
    {
        value = flashReadWord(address);
        crc = crcByte(crc, (BYTE)value);
        crc = crcByte(crc, (BYTE)(value >> 8));
    }
    return crc;
}

static BYTE applicationValid(void)
{
    WORD lastAddress;
    WORD expectedCrc;

    if((flashReadWord(SWANSONG_APP_MARKER) != SWANSONG_MARKER_MAGIC_0) ||
       (flashReadWord(SWANSONG_APP_MARKER + 1u) != SWANSONG_MARKER_MAGIC_1))
    {
        return 0;
    }
    lastAddress = flashReadWord(SWANSONG_APP_MARKER + 2u);
    if((lastAddress < SWANSONG_APP_START) || (lastAddress > SWANSONG_APP_LAST))
    {
        return 0;
    }
    expectedCrc = (flashReadWord(SWANSONG_APP_MARKER + 3u) & 0xFFu) |
        ((flashReadWord(SWANSONG_APP_MARKER + 4u) & 0xFFu) << 8);
    return imageCrc(lastAddress) == expectedCrc;
}

static BYTE writeMarker(WORD lastAddress, WORD expectedCrc, BYTE major, BYTE minor)
{
    BYTE i;
    BYTE status;

    if((lastAddress < SWANSONG_APP_START) || (lastAddress > SWANSONG_APP_LAST))
    {
        return SWANSONG_STATUS_IMAGE;
    }
    if(imageCrc(lastAddress) != expectedCrc)
    {
        return SWANSONG_STATUS_IMAGE;
    }

    for(i = 0; i < sizeof(rowData); i++)
    {
        rowData[i] = 0xFF;
    }
    rowData[0] = (BYTE)SWANSONG_MARKER_MAGIC_0;
    rowData[1] = (BYTE)(SWANSONG_MARKER_MAGIC_0 >> 8);
    rowData[2] = (BYTE)SWANSONG_MARKER_MAGIC_1;
    rowData[3] = (BYTE)(SWANSONG_MARKER_MAGIC_1 >> 8);
    rowData[4] = (BYTE)lastAddress;
    rowData[5] = (BYTE)(lastAddress >> 8);
    rowData[6] = (BYTE)expectedCrc;
    rowData[7] = 0;
    rowData[8] = (BYTE)(expectedCrc >> 8);
    rowData[9] = 0;
    rowData[10] = major;
    rowData[11] = 0;
    rowData[12] = minor;
    rowData[13] = 0;

    status = flashEraseRow(SWANSONG_APP_MARKER);
    if(status != SWANSONG_STATUS_OK)
    {
        return status;
    }
    status = flashWriteRow(SWANSONG_APP_MARKER, rowData);
    if(status != SWANSONG_STATUS_OK)
    {
        return status;
    }
    return applicationValid() ? SWANSONG_STATUS_OK : SWANSONG_STATUS_IMAGE;
}

static void clearResponse(void)
{
    BYTE i;
    for(i = 0; i < SWANSONG_UPDATE_PACKET_SIZE; i++)
    {
        txPacket[i] = 0;
    }
    txPacket[0] = SWANSONG_UPDATE_MAGIC;
    txPacket[1] = SWANSONG_UPDATE_PROTOCOL;
    txPacket[2] = rxPacket[2] | SWANSONG_RESPONSE_FLAG;
    txPacket[3] = rxPacket[3];
}

static void copyHalfToResponse(WORD address, BYTE half)
{
    BYTE i;
    WORD value;
    address += (WORD)half * SWANSONG_FLASH_HALF_WORDS;
    for(i = 0; i < SWANSONG_FLASH_HALF_WORDS; i++)
    {
        value = flashReadWord(address + i);
        txPacket[8u + (i * 2u)] = (BYTE)value;
        txPacket[9u + (i * 2u)] = (BYTE)(value >> 8);
    }
}

static void processPacket(void)
{
    BYTE i;
    BYTE half;
    BYTE status = SWANSONG_STATUS_OK;
    WORD address;
    WORD expectedCrc;

    clearResponse();
    if((rxPacket[0] != SWANSONG_UPDATE_MAGIC) ||
       (rxPacket[1] != SWANSONG_UPDATE_PROTOCOL))
    {
        txPacket[4] = SWANSONG_STATUS_BAD_PACKET;
        return;
    }

    address = packetAddress();
    switch(rxPacket[2])
    {
        case SWANSONG_CMD_QUERY:
            txPacket[5] = BOOTLOADER_VERSION_MAJOR;
            txPacket[6] = BOOTLOADER_VERSION_MINOR;
            txPacket[7] = SWANSONG_FLASH_ROW_WORDS;
            txPacket[8] = (BYTE)SWANSONG_APP_START;
            txPacket[9] = (BYTE)(SWANSONG_APP_START >> 8);
            txPacket[10] = (BYTE)SWANSONG_APP_LAST;
            txPacket[11] = (BYTE)(SWANSONG_APP_LAST >> 8);
            txPacket[12] = (BYTE)SWANSONG_APP_MARKER;
            txPacket[13] = (BYTE)(SWANSONG_APP_MARKER >> 8);
            txPacket[14] = applicationValid();
            break;

        case SWANSONG_CMD_ERASE_ROW:
            stagedFirstHalf = 0;
            status = flashEraseRow(address);
            break;

        case SWANSONG_CMD_WRITE_HALF:
            half = rxPacket[6];
            if((address < SWANSONG_APP_START) || (address >= SWANSONG_APP_MARKER))
            {
                status = SWANSONG_STATUS_RANGE;
            }
            else if((address & (SWANSONG_FLASH_ROW_WORDS - 1u)) != 0u)
            {
                status = SWANSONG_STATUS_ALIGNMENT;
            }
            else if(half > 1u)
            {
                status = SWANSONG_STATUS_BAD_PACKET;
            }
            else if(half == 0u)
            {
                for(i = 0; i < 32u; i++)
                {
                    rowData[i] = rxPacket[8u + i];
                }
                stagedRow = address;
                stagedFirstHalf = 1;
            }
            else if((stagedFirstHalf == 0u) || (stagedRow != address))
            {
                status = SWANSONG_STATUS_SEQUENCE;
            }
            else
            {
                for(i = 0; i < 32u; i++)
                {
                    rowData[32u + i] = rxPacket[8u + i];
                }
                stagedFirstHalf = 0;
                status = flashWriteRow(address, rowData);
            }
            break;

        case SWANSONG_CMD_READ_HALF:
            half = rxPacket[6];
            if((address < SWANSONG_APP_START) || (address >= SWANSONG_APP_MARKER))
            {
                status = SWANSONG_STATUS_RANGE;
            }
            else if(((address & (SWANSONG_FLASH_ROW_WORDS - 1u)) != 0u) || (half > 1u))
            {
                status = SWANSONG_STATUS_ALIGNMENT;
            }
            else
            {
                txPacket[6] = half;
                copyHalfToResponse(address, half);
            }
            break;

        case SWANSONG_CMD_FINALIZE:
            expectedCrc = (WORD)rxPacket[6] | ((WORD)rxPacket[7] << 8);
            status = writeMarker(address, expectedCrc, rxPacket[8], rxPacket[9]);
            break;

        case SWANSONG_CMD_RESET:
            if(applicationValid())
            {
                resetPending = 1;
            }
            else
            {
                status = SWANSONG_STATUS_IMAGE;
            }
            break;

        default:
            status = SWANSONG_STATUS_BAD_COMMAND;
            break;
    }
    txPacket[4] = status;
}

static BYTE recoveryButtonsHeld(void)
{
    TRISBbits.TRISB4 = 1;
    TRISCbits.TRISC2 = 1;
    WPUBbits.WPUB4 = 1;
    OPTION_REGbits.nWPUEN = 0;
    return (!BOOT_BUTTON_START) && (!BOOT_BUTTON_POWER);
}

static void jumpToApplication(void)
{
    USBModuleDisable();
    INTCONbits.GIE = 0;
    INTCONbits.PEIE = 0;
    /* This function was called, but the application starts with a jump rather
       than a return. Discard that bootloader return address first. */
    STKPTR = 0;
    asm("ljmp 0x1000");
}

static void initializeBootloader(void)
{
    ANSELA = 0;
    ANSELB = 0;
    ANSELC = 0;
    OSCTUNE = 0;
    OSCCON = 0xFC;
    ACTCON = 0x90;
    stagedFirstHalf = 0;
    resetPending = 0;
    rxHandle = 0;
    txHandle = 0;
    USBDeviceInit();
}

int main(void)
{
    BYTE requested = !PCONbits.nRI;
    PCONbits.nRI = 1;

    ANSELA = 0;
    ANSELB = 0;
    ANSELC = 0;
    OSCTUNE = 0;
    OSCCON = 0xFC;

    if(!requested && !recoveryButtonsHeld() && applicationValid())
    {
        jumpToApplication();
    }

    initializeBootloader();
    while(1)
    {
        USBDeviceTasks();
        if((USBDeviceState < CONFIGURED_STATE) || (USBSuspendControl == 1))
        {
            continue;
        }

        if((rxHandle != 0) && !HIDRxHandleBusy(rxHandle) && !HIDTxHandleBusy(txHandle))
        {
            processPacket();
            txHandle = HIDTxPacket(HID_EP, txPacket, sizeof(txPacket));
            rxHandle = HIDRxPacket(HID_EP, rxPacket, sizeof(rxPacket));
        }
        if(resetPending && !HIDTxHandleBusy(txHandle))
        {
            resetPending = 0;
            jumpToApplication();
        }
    }
}

void USBCBInitEP(void)
{
    USBEnableEndpoint(HID_EP,
        USB_IN_ENABLED | USB_OUT_ENABLED | USB_HANDSHAKE_ENABLED | USB_DISALLOW_SETUP);
    rxHandle = HIDRxPacket(HID_EP, rxPacket, sizeof(rxPacket));
    txHandle = 0;
}

void USBCBCheckOtherReq(void)
{
    USBCheckHIDRequest();
}

BOOL USER_USB_CALLBACK_EVENT_HANDLER(int event, void *pdata, WORD size)
{
    (void)pdata;
    (void)size;
    switch(event)
    {
        case EVENT_CONFIGURED:
            USBCBInitEP();
            break;
        case EVENT_EP0_REQUEST:
            USBCBCheckOtherReq();
            break;
        default:
            break;
    }
    return TRUE;
}
