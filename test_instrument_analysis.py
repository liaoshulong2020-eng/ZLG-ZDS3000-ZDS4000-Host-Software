import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import threading
import unittest
from unittest.mock import MagicMock
from PySide6.QtWidgets import QApplication
from zds_scope.ui.instrument_analysis import InstrumentAnalysisThread, MathPanel, DecodePanel, parse_events

class AnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def driver(self):
        d = MagicMock(); d.io_lock = threading.RLock()
        return d

    def test_apply_explicitly_writes_instrument_and_reads_mode(self):
        d=self.driver(); d.query.return_value='BASIc'
        t=InstrumentAnalysisThread(d,'math_apply',dict(operator='SUBTract',a=1,b=4,invert=False));t.run()
        self.assertIsNone(t.error)
        self.assertEqual([c.args[0] for c in d.send.call_args_list], [':MATH:BASIc:SA CHANnel1', ':MATH:BASIc:SB CHANnel4', ':MATH:BASIc:OPERator SUBTract', ':MATH:BASIc:INVErt OFF', ':MATH:MODE BASIc'])
        d.query.assert_called_once_with(':MATH:MODE?')

    def test_unary_does_not_write_unused_source(self):
        d=self.driver()
        t=InstrumentAnalysisThread(d,'math_apply',dict(operator='INTEgral',a=2,b=1,invert=True));t.run()
        self.assertFalse(any(':SB' in c.args[0] for c in d.send.call_args_list))

    def test_decode_read_only_preserves_fields(self):
        d=self.driver(); raw='0.001，123，AA BB;0.002,456,CC DD;'
        d.query.return_value=raw
        t=InstrumentAnalysisThread(d,'decode');t.run()
        d.send.assert_not_called();d.query.assert_called_once_with(':DECOde:PLUG:EVENt?')
        self.assertEqual(t.result,raw)
        self.assertEqual(parse_events(raw)[1],['0.002','456','CC DD'])
        p=DecodePanel();p.show_events(raw)
        self.assertEqual(p.table.rowCount(),2); self.assertEqual(p.raw,raw)
        p.show_events('');self.assertEqual(p.table.rowCount(),0);self.assertFalse(p.save.isEnabled())
        p.close()

    def test_query_failure_and_idle_panel(self):
        d=self.driver();d.query.side_effect=TimeoutError('timeout')
        t=InstrumentAnalysisThread(d,'decode');t.run();self.assertEqual(t.error,'timeout')
        p=MathPanel();p.operator.setCurrentIndex(4);self.assertFalse(p.b.isEnabled());p.close()

if __name__=='__main__':unittest.main()
