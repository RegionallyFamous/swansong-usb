import contextlib
import io
import unittest

from swansong_firmware import (
    APP_LAST,
    APP_MARKER,
    APP_START,
    ROW_WORDS,
    ApplicationImage,
    crc16_words,
)
from swansong_usb_update import (
    BOOTLOADER_PID,
    CMD_ERASE_ROW,
    CMD_FINALIZE,
    CMD_QUERY,
    CMD_READ_HALF,
    CMD_RESET,
    CMD_WRITE_HALF,
    MAGIC,
    PACKET_SIZE,
    PROTOCOL,
    Transport,
    install,
)


class FakeBootloader:
    def __init__(self):
        self.words = [0x3FFF] * (APP_MARKER + ROW_WORDS - APP_START)
        self.response = bytes(PACKET_SIZE)
        self.staged = None
        self.valid = False
        self.reset = False

    def write(self, report):
        packet = bytes(report)[1:]
        response = bytearray(PACKET_SIZE)
        response[:4] = bytes((MAGIC, PROTOCOL, packet[2] | 0x80, packet[3]))
        address = packet[4] | (packet[5] << 8)
        command = packet[2]

        if command == CMD_QUERY:
            response[5:15] = bytes((1, 0, ROW_WORDS, 0x00, 0x10, 0xDF, 0x1F, 0xE0, 0x1F, int(self.valid)))
        elif command == CMD_ERASE_ROW:
            offset = address - APP_START
            self.words[offset : offset + ROW_WORDS] = [0x3FFF] * ROW_WORDS
            if address == APP_MARKER:
                self.valid = False
        elif command == CMD_WRITE_HALF:
            half = packet[6]
            values = tuple(packet[8 + i] | (packet[9 + i] << 8) for i in range(0, 32, 2))
            if half == 0:
                self.staged = (address, values)
            else:
                if self.staged is None or self.staged[0] != address:
                    response[4] = 5
                else:
                    offset = address - APP_START
                    self.words[offset : offset + ROW_WORDS] = self.staged[1] + values
                    self.staged = None
        elif command == CMD_READ_HALF:
            offset = address - APP_START + packet[6] * 16
            for index, word in enumerate(self.words[offset : offset + 16]):
                response[8 + index * 2] = word & 0xFF
                response[9 + index * 2] = word >> 8
        elif command == CMD_FINALIZE:
            expected = packet[6] | (packet[7] << 8)
            actual = crc16_words(self.words[: APP_LAST - APP_START + 1])
            if expected == actual:
                self.valid = True
            else:
                response[4] = 7
        elif command == CMD_RESET:
            if self.valid:
                self.reset = True
            else:
                response[4] = 7
        else:
            response[4] = 2
        self.response = bytes(response)
        return len(report)

    def read(self, size, timeout_ms):
        self.assert_read = (size, timeout_ms)
        return list(self.response)


class UpdaterProtocolTests(unittest.TestCase):
    def test_complete_install_erases_programs_verifies_and_resets(self):
        words = [0x3FFF] * (APP_LAST - APP_START + 1)
        words[0:4] = [0x2801, 0x0008, 0x1234, 0x0000]
        words[ROW_WORDS + 3] = 0x0555
        image = ApplicationImage(tuple(words), crc16_words(words))
        device = FakeBootloader()

        with contextlib.redirect_stdout(io.StringIO()):
            install(Transport(device), image, 1, 2)

        self.assertEqual(device.words[: len(words)], words)
        self.assertTrue(device.valid)
        self.assertTrue(device.reset)


if __name__ == "__main__":
    unittest.main()
