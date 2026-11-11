import unittest
import numpy as np
from test_record_features import record
from zds_scope.display_envelope import DisplayEnvelope


class EnvelopeTests(unittest.TestCase):
    def test_single_sample_spikes_survive_overview_and_zoom(self):
        raw = np.full(2000000, 128, dtype=np.uint8)
        raw[123457] = 255
        raw[987651] = 0
        d = record(raw=raw, start=0, dt=1e-8).channels[1]
        e = DisplayEnvelope(d)
        t, v = e.samples(pixels=1000)
        self.assertIn(123457*d.dt, t)
        self.assertIn(987651*d.dt, t)
        self.assertLess(len(t), 5000)
        t, v = e.samples(123450*d.dt, 123465*d.dt, pixels=1000)
        self.assertTrue(np.allclose(np.diff(t), d.dt))
        self.assertIn(123457*d.dt, t)
        self.assertIsNone(d._voltage_array)
        self.assertIsNone(d._time_array)

    def test_pan_returns_different_raw_region_and_outside_is_empty(self):
        d = record(raw=np.arange(256, dtype=np.uint8), start=0, dt=.001).channels[1]
        e = DisplayEnvelope(d)
        t, _ = e.samples(.2, .21)
        self.assertGreaterEqual(t[0], .198)
        t, _ = e.samples(10, 11)
        self.assertEqual(len(t), 0)


if __name__ == '__main__':
    unittest.main()
