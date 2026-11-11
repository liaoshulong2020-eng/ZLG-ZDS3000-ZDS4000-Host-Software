import socket
import unittest
from unittest.mock import MagicMock, patch

from zds_scope.driver import ZDScopeDriver, ScopeCommunicationError


class DriverErrorTests(unittest.TestCase):
    def test_query_timeout_closes_socket_and_keeps_reason(self):
        driver = ZDScopeDriver()
        sock = MagicMock()
        sock.recv.side_effect = socket.timeout("timed out")
        driver.sock = sock
        driver._is_connected = True
        with self.assertRaises(ScopeCommunicationError):
            driver.query(":TEST?")
        self.assertFalse(driver.is_connected)
        self.assertIsNone(driver.sock)
        self.assertIn(":TEST?", driver.last_error)
        self.assertIn("timed out", driver.last_error)
        sock.close.assert_called_once()

    def test_measurements_do_not_hide_communication_failure(self):
        driver = ZDScopeDriver()
        with patch.object(driver, "query", side_effect=ScopeCommunicationError("disconnected")) as query:
            with self.assertRaises(ScopeCommunicationError):
                driver.get_measurements(1)
            query.assert_called_once()


if __name__ == "__main__":
    unittest.main()
