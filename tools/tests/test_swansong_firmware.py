import tempfile
import unittest
from pathlib import Path

from swansong_firmware import (
    APP_LAST,
    APP_MARKER,
    APP_START,
    MARKER_MAGIC_0,
    MARKER_MAGIC_1,
    FirmwareError,
    build_factory,
    crc16_words,
    load_application,
    put_word,
    read_hex,
    verify_images,
    word_at,
    write_hex,
)


class FirmwareImageTests(unittest.TestCase):
    def test_hex_round_trip_and_crc_vector(self):
        memory = {0x0000: 0x34, 0x0001: 0x12, 0x1000E: 0xAA, 0x1000F: 0x2A}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "roundtrip.hex"
            write_hex(path, memory)
            self.assertEqual(read_hex(path), memory)
        self.assertEqual(crc16_words((0x1234, 0x3FFF)), 0x8B98)

    def test_factory_marker_and_write_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            boot_path = root / "boot.hex"
            app_path = root / "app.hex"
            factory_path = root / "factory.hex"

            boot = {}
            put_word(boot, 0, 0x2801)
            # CONFIG1 and CONFIG2 at PIC HEX byte address 0x1000E.
            boot.update({0x1000E: 0x84, 0x1000F: 0x0F, 0x10010: 0xCD, 0x10011: 0x1F})
            write_hex(boot_path, boot)

            app = {}
            put_word(app, APP_START, 0x2801)
            put_word(app, APP_START + 1, 0x0008)
            write_hex(app_path, app)

            build_factory(boot_path, app_path, factory_path, 2, 7)
            verify_images(boot_path, app_path, factory_path)
            factory = read_hex(factory_path)
            image = load_application(app_path)
            self.assertEqual(word_at(factory, APP_MARKER), MARKER_MAGIC_0)
            self.assertEqual(word_at(factory, APP_MARKER + 1), MARKER_MAGIC_1)
            self.assertEqual(word_at(factory, APP_MARKER + 2), APP_LAST)
            marker_crc = word_at(factory, APP_MARKER + 3) | (word_at(factory, APP_MARKER + 4) << 8)
            self.assertEqual(marker_crc, image.crc)
            self.assertEqual(word_at(factory, APP_MARKER + 5), 2)
            self.assertEqual(word_at(factory, APP_MARKER + 6), 7)

    def test_application_rejects_bootloader_addresses(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.hex"
            memory = {}
            put_word(memory, APP_START, 0x2801)
            put_word(memory, 0, 0x2801)
            write_hex(path, memory)
            with self.assertRaises(FirmwareError):
                load_application(path)


if __name__ == "__main__":
    unittest.main()
