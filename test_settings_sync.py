import os
import re
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from zds_scope.driver import ZDScopeDriver
from zds_scope.ui.control_panel import ControlPanel


class SettingsSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_apply_preserves_custom_values_and_emits_no_writes(self):
        panel = ControlPanel()
        signals = []
        panel.channel_changed.connect(lambda *args: signals.append(args))
        panel.timebase_changed.connect(lambda *args: signals.append(args))
        panel.memory_depth_changed.connect(lambda *args: signals.append(args))
        config = {
            "run_state": "Stop", "timebase_scale_s": 137e-9,
            "timebase_offset_s": 3e-9, "mdepth_raw": "384000000", "sample_rate_hz": 1e9,
            "channels": {1: {"enabled": False, "scale": 0.137, "offset": 125.123456789,
                             "coupling": "GND", "bw_limit": True, "probe": 100.0},
                         3: {"enabled": True}},
        }
        panel.apply_scope_configuration(config)
        panel.apply_scope_configuration(config)
        self.assertEqual(signals, [])
        self.assertEqual(panel.combo_tb_scale.currentData(), 137e-9)
        self.assertAlmostEqual(panel.spin_tb_offset.value(), 3e-9, places=12)
        self.assertEqual(panel.combo_mdep.currentData(), "384000000")
        widgets = panel.ch_widgets[1]
        self.assertEqual(widgets["combo_scale"].currentData(), 0.137)
        self.assertAlmostEqual(widgets["spin_offset"].value(), 125.123456789, places=9)
        self.assertTrue(widgets["combo_coup"].currentText().startswith("GND"))
        self.assertFalse(widgets["cb_enable"].isChecked())
        self.assertIn("100", widgets["lbl_probe"].text())
        widgets["spin_offset"].setValue(1.25)
        self.assertEqual(signals, [(1, {"offset": 1.25})])
        panel.close()

    def test_each_user_edit_emits_only_its_changed_field(self):
        panel = ControlPanel()
        channels, timebase = [], []
        panel.channel_changed.connect(lambda ch, values: channels.append((ch, values)))
        panel.timebase_changed.connect(lambda *args: timebase.append(args))
        panel.ch_widgets[1]["combo_coup"].setCurrentIndex(2)
        self.assertEqual(channels, [(1, {"coupling": "GND"})])
        panel.spin_tb_offset.setValue(0.005)
        self.assertEqual(timebase, [(None, 0.005)])
        panel.combo_tb_scale.setCurrentIndex(7)
        self.assertEqual(timebase[-1], (2e-6, None))
        panel.close()

    def test_configuration_queries_are_read_only_and_include_disabled_channels(self):
        driver = ZDScopeDriver()
        driver.device_info = "DEVICE"
        commands = []

        def query(cmd):
            cmd = cmd.strip()
            commands.append(cmd)
            self.assertTrue(cmd.endswith("?"), cmd)
            if "RUN:STATe" in cmd:
                return "Stop"
            if "MDEPth" in cmd:
                return "384000000"
            if "SRATe" in cmd:
                return "1e9"
            if "TIMebase:SCALe" in cmd:
                return "2e-6"
            if "TIMebase:OFFSet" in cmd:
                return "3e-9"
            if ":DISPlay?" in cmd:
                return "ON" if re.search(r"CHANnel[13]:", cmd) else "OFF"
            if ":COUPling?" in cmd:
                return "GND"
            if ":BWLimit?" in cmd:
                return "20M"
            if ":SCALe?" in cmd:
                return "0.137"
            if ":OFFSet?" in cmd:
                return "0.75"
            if ":PROBe?" in cmd:
                return "100"
            if cmd == ":TRIGger:MODE?":
                return "EDGE"
            if cmd.endswith(":SOURce?"):
                return "CHANnel2"
            if cmd.endswith(":UNITs?"):
                return "VOLTage"
            if cmd.endswith(":TERMination?"):
                return "1M"
            if cmd.startswith((":ACQuire:", ":TRIG")) or cmd.endswith((":INVert?", ":DELAy?")):
                return "0"
            self.fail(cmd)

        with patch.object(driver, "query", side_effect=query), patch.object(driver, "send") as send:
            config = driver.get_scope_actual_configuration()
            send.assert_not_called()
        self.assertEqual(config["enabled_channels"], [1, 3])
        self.assertEqual(config["channels"][2]["scale"], 0.137)
        self.assertEqual(config["channels"][2]["coupling"], "GND")
        self.assertEqual(config["channels"][4]["probe"], 100)
        self.assertEqual(config["extended_settings"][":TRIGger:EDGE:SOURce"], "CHANnel2")


if __name__ == "__main__":
    unittest.main()
