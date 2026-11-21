"""Regression checks for CBS response adapter device capability lookup.

Based on shared request schema v1.4: capabilities.devices[].nodeId.
Expected to expose the current 'node' vs 'nodeId' lookup defect until fixed.
Run from repository root:
  python -m unittest discover -s tests -p 'test_cbs_device_capabilities.py' -v
"""
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cbs_response_adapter import (
    _get_device_cbs_capabilities,
    _admin_idle_slope,
    CBSResponseConversionError,
)


class TestCBSDeviceCapabilities(unittest.TestCase):
    def test_device_level_unsupported_is_recognized(self):
        request = {
            "capabilities": {"devices": [{
                "nodeId": "ES1",
                "mechanisms": {"CBS": {"supported": False}},
            }]}
        }
        caps = _get_device_cbs_capabilities(request, "ES1", "Port1")
        self.assertIs(caps.get("supported"), False)

    def test_port_override_is_recognized(self):
        request = {
            "capabilities": {"devices": [{
                "nodeId": "SW1",
                "mechanisms": {"CBS": {
                    "supported": True,
                    "maxIdleSlopeBps": 5000000,
                    "supportedQueues": [5],
                }},
                "ports": {"P4": {
                    "mechanisms": {"CBS": {
                        "maxIdleSlopeBps": 3000000,
                        "supportedQueues": [4],
                    }}
                }},
            }]}
        }
        caps = _get_device_cbs_capabilities(request, "SW1", "P4")
        self.assertEqual(caps, {
            "supported": True,
            "maxIdleSlopeBps": 3000000,
            "supportedQueues": [4],
        })
        other = _get_device_cbs_capabilities(request, "SW1", "P5")
        self.assertEqual(other["supportedQueues"], [5])
        self.assertEqual(other["maxIdleSlopeBps"], 5000000)

    def test_disallowed_hardware_slope_is_rejected(self):
        request = {
            "capabilities": {"devices": [{
                "nodeId": "ES2",
                "mechanisms": {"CBS": {
                    "supported": True,
                    "maxIdleSlopeBps": 1000000,
                }},
            }]}
        }
        caps = _get_device_cbs_capabilities(request, "ES2", "Port1")
        self.assertEqual(caps.get("maxIdleSlopeBps"), 1000000)
        with self.assertRaisesRegex(CBSResponseConversionError, "exceeds hardware limit"):
            _admin_idle_slope(1200000, 100000000, caps, "ES2/Port1/M1")

    def test_nonmatching_node_has_no_capabilities(self):
        request = {
            "capabilities": {"devices": [{
                "nodeId": "ES1",
                "mechanisms": {"CBS": {"supported": True}},
            }]}
        }
        self.assertEqual(_get_device_cbs_capabilities(request, "ES9", "Port1"), {})


if __name__ == "__main__":
    unittest.main()
