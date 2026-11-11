import os,unittest
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from zds_scope.driver import ZDScopeDriver,ScopeCommunicationError
from zds_scope.ui.advanced_settings import AdvancedSettings
from PySide6.QtWidgets import QApplication,QPushButton,QLabel

class MeasurementActivationTests(unittest.TestCase):
    def test_missing_measurements_are_enabled_then_read(self):
        driver=ZDScopeDriver(); enabled=set()
        def send(command): enabled.add(command.strip())
        def query(command):
            setter=command.replace(':CURRent?','').strip()
            return '1.25' if setter in enabled else '3.40282e+38'
        with patch.object(driver,'send',side_effect=send) as sending, patch.object(driver,'query',side_effect=query):
            measured=driver.get_measurements(2,wait_seconds=0,activate_missing=True)
        self.assertTrue(all(value==1.25 for value in measured.values()))
        self.assertIn(':MEASure:VRMS DC,DISPlay,CHANnel2',enabled)
        self.assertTrue(all(command.startswith(':MEASure:') and 'CHANnel2' in command for command in enabled))
        self.assertEqual(sending.call_count,11)
    def test_valid_results_do_not_enable_or_change_anything(self):
        driver=ZDScopeDriver()
        with patch.object(driver,'query',return_value='2.5'),patch.object(driver,'send') as sending:
            result=driver.get_measurements(1)
        sending.assert_not_called();self.assertEqual(result['vpp'],2.5)
    def test_activation_failure_is_reported(self):
        driver=ZDScopeDriver()
        with patch.object(driver,'query',return_value='3.40282e+38'),patch.object(driver,'send',side_effect=ScopeCommunicationError('broken')):
            with self.assertRaises(ScopeCommunicationError):driver.get_measurements(1,wait_seconds=0,activate_missing=True)

class SettingsFormTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def test_loading_ch2_and_opening_form_never_writes(self):
        form=AdvancedSettings();writes=[];form.write_requested.connect(lambda *args:writes.append(args))
        config={'extended_settings':{':TRIGger:MODE':'EDGE',':TRIGger:EDGE:SOURce':'CHANnel2',':TRIGger:EDGE:LEVel':'1.024',':TRIGger:SWEep':'AUTO'}}
        form.set_configuration(config,True);form.set_filter((':TRIG',))
        self.assertEqual(form.editors[':TRIGger:EDGE:SOURce'].currentData(),'CHANNEL2')
        self.assertEqual(writes,[])
        labels=' '.join(label.text() for label in form.findChildren(QLabel))
        self.assertNotIn(':TRIG',labels);self.assertNotIn('SCPI',labels)
        source=form.editors[':TRIGger:EDGE:SOURce'];source.setCurrentIndex(2)
        self.assertEqual(writes,[])
        source.parentWidget().findChild(QPushButton).click()
        self.assertEqual(writes,[(':TRIGger:EDGE:SOURce','CHANNEL3')])
        form.close()

if __name__=='__main__':unittest.main()
