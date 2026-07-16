#ifndef HARDWARE_PROFILE_SWANSONG_USB_H
#define HARDWARE_PROFILE_SWANSONG_USB_H

/* SwanSong USB Rev A: PIC16F1459, crystal-free full-speed USB. */
#define USE_INTERNAL_OSC
#define DEMO_BOARD PIC16F1_LPC_USB_DEVELOPMENT_KIT
#define PIC16F1_LPC_USB_DEVELOPMENT_KIT
#define CLOCK_FREQ 48000000UL
#define _XTAL_FREQ 48000000UL

/* This is a USB bus-powered device. */
#define self_power 0
#define USB_BUS_SENSE 1

/* The production PCB has no LEDs and no separate demo switches. */
#define mInitAllLEDs() do { } while (0)
#define mInitAllSwitches() do { } while (0)
#define mLED_1_Toggle() do { } while (0)
#define mLED_1_On() do { } while (0)
#define mLED_1_Off() do { } while (0)
#define mLED_2_On() do { } while (0)
#define mLED_2_Off() do { } while (0)
#define mGetLED_1() 0
#define sw2 1
#define sw3 1

#define INPUT_PIN 1
#define OUTPUT_PIN 0

/* Active-low membrane contacts. Port A/B use internal pull-ups; the eight
   Port C controls use the two external bussed 10k arrays on the PCB. */
#define BTN_Y4      PORTAbits.RA4
#define BTN_Y3      PORTCbits.RC5
#define BTN_SOUND   PORTCbits.RC4
#define BTN_X1      PORTCbits.RC3
#define BTN_X3      PORTCbits.RC6
#define BTN_X2      PORTCbits.RC7
#define BTN_X4      PORTBbits.RB7
#define BTN_Y2      PORTBbits.RB6
#define BTN_Y1      PORTBbits.RB5
#define BTN_START   PORTBbits.RB4
#define BTN_POWER   PORTCbits.RC2
#define BTN_B       PORTCbits.RC1
#define BTN_A       PORTCbits.RC0

#endif
