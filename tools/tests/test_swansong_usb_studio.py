import hashlib
import unittest
from pathlib import Path

from swansong_usb_studio import (
    CONTROL_NAMES,
    StudioUSBError,
    decode_report,
    doctor_report,
    hardware_qa_report,
    inspect_image,
    install_report,
    update_plan,
)
from swansong_usb_update import GAMEPAD_PID


ROOT = Path(__file__).resolve().parents[2]
IMAGE = ROOT / "firmware" / "build" / "swansong-usb-app.hex"


class FakeDevice:
    def __init__(self, reports):
        self.reports = list(reports)
        self.opened = None
        self.closed = False

    def open_path(self, path):
        self.opened = path

    def read(self, size, timeout_ms):
        if self.reports:
            return self.reports.pop(0)
        return []

    def close(self):
        self.closed = True


class FakeHID:
    def __init__(self, *, gamepads=1, bootloaders=0, reports=()):
        self.gamepads = gamepads
        self.bootloaders = bootloaders
        self.instance = FakeDevice(reports)

    def enumerate(self, vid, pid):
        count = self.gamepads if pid == GAMEPAD_PID else self.bootloaders
        return [{"path": f"device-{index}".encode()} for index in range(count)]

    def device(self):
        return self.instance


class StudioUSBContractTests(unittest.TestCase):
    def test_image_report_is_content_bound(self):
        report = inspect_image(IMAGE)
        self.assertEqual(report["sha256"], hashlib.sha256(IMAGE.read_bytes()).hexdigest())
        self.assertGreater(report["programmedWords"], 0)
        self.assertGreater(report["totalWords"], report["programmedWords"])

    def test_doctor_allows_offline_inspection_but_can_require_device(self):
        offline = doctor_report(IMAGE, hid_module=FakeHID(gamepads=0))
        required = doctor_report(IMAGE, require_device=True, hid_module=FakeHID(gamepads=0))
        self.assertTrue(offline["ok"])
        self.assertFalse(required["ok"])
        self.assertEqual(offline["device"]["mode"], "absent")

    def test_update_plan_is_non_mutating_and_requires_exactly_one_device(self):
        ready = update_plan(IMAGE, (1, 2), hid_module=FakeHID())
        absent = update_plan(IMAGE, (1, 2), hid_module=FakeHID(gamepads=0))
        ambiguous = update_plan(IMAGE, (1, 2), hid_module=FakeHID(gamepads=2))
        self.assertTrue(ready["ok"])
        self.assertEqual(ready["version"], "1.2")
        self.assertEqual(ready["confirmationSHA256"], ready["image"]["sha256"])
        self.assertFalse(absent["ok"])
        self.assertFalse(ambiguous["ok"])

    def test_install_rejects_unconfirmed_image_before_opening_hardware(self):
        with self.assertRaisesRegex(StudioUSBError, "confirmation SHA-256"):
            install_report(
                IMAGE,
                (1, 2),
                confirmation_sha256="0" * 64,
                accept_device_reset=True,
            )
        with self.assertRaisesRegex(StudioUSBError, "explicit acceptance"):
            install_report(
                IMAGE,
                (1, 2),
                confirmation_sha256=inspect_image(IMAGE)["sha256"],
                accept_device_reset=False,
            )

    def test_report_decoder_covers_buttons_directions_and_neutral(self):
        self.assertEqual(decode_report(bytes((0, 0, 8))), set())
        self.assertEqual(decode_report(bytes((0b00000001, 0, 0))), {"a", "up"})
        self.assertEqual(decode_report(bytes((0, 1, 5))), {"power", "down", "left"})
        with self.assertRaises(StudioUSBError):
            decode_report(bytes((0, 0, 15)))

    def test_hardware_qa_requires_neutral_and_every_requested_control(self):
        reports = [
            [0, 0, 8],
            [0b00111111, 0, 0],
            [0b11000000, 1, 4],
            [0, 0, 2],
            [0, 0, 6],
        ]
        hid = FakeHID(reports=reports)
        report = hardware_qa_report(hid_module=hid, max_reports=10)
        self.assertTrue(report["ok"])
        self.assertEqual(tuple(report["observedControls"]), CONTROL_NAMES)
        self.assertTrue(hid.instance.closed)

    def test_hardware_qa_reports_missing_controls_without_guessing(self):
        report = hardware_qa_report(
            required=("a", "b"),
            hid_module=FakeHID(reports=([0, 0, 8], [1, 0, 8])),
            max_reports=2,
        )
        self.assertFalse(report["ok"])
        self.assertEqual(report["missingControls"], ["b"])


if __name__ == "__main__":
    unittest.main()
