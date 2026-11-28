"""Check CBS response capacity guards and existing physical queue reservations.

Run:
    python -m unittest discover -s tests -p 'test_cbs_response_capacity_guard.py' -v
"""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

from cbs_response_adapter import CBSResponseConversionError, build_cbs_response_v13 as _map_response
from algorithm.cbs_configuration import prepare_cbs_configuration
import test_cbs_nclass_queue_boundary as boundary
from usecase_adapter import build_usecase_from_payload


class TestStep20CapacityGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        boundary.TestNClassPhysicalQueueBoundary.setUpClass()
        cls.original = boundary.TestNClassPhysicalQueueBoundary.original
        cls.optimizer = boundary.TestNClassPhysicalQueueBoundary.optimizer

    def case(self):
        request = boundary.mapped_request(self.original, 1)
        legacy = self.optimizer.optimize(build_usecase_from_payload(deepcopy(request)))
        self.assertTrue(legacy['allDeadlinesSatisfied'])
        return request, legacy

    def test_existing_untouched_cbs_queue_counts_toward_capacity(self):
        request, legacy = self.case()
        node = next(n for n in request['forwardingPlane']['configuration']['nodeConfigs']
                    if n['nodeId'] == 'ES3')
        port = next(p for p in node['portConfigs'] if p['portId'] == 'Port1')
        # Queue 2 is not touched by the M1 optimization (mapped to queue 1).
        port['queueConfigs'] = [{
            'queueId': 2, 'scheduling': 'CREDIT_BASED_SHAPER',
            'cbs': [{'egressQueueId': 2, 'adminState': 'ENABLED',
                     'adminIdleSlopeBps': 99_000_000}],
        }]
        with self.assertRaisesRegex(CBSResponseConversionError, 'Aggregate|link speed'):
            build_cbs_response_v13(request, legacy)

    def test_previous_slope_on_replaced_target_queue_not_double_counted(self):
        request, legacy = self.case()
        node = next(n for n in request['forwardingPlane']['configuration']['nodeConfigs']
                    if n['nodeId'] == 'ES3')
        port = next(p for p in node['portConfigs'] if p['portId'] == 'Port1')
        # Queue 1 *is* overwritten by M1 and old 99Mbps should not count.
        port['queueConfigs'] = [{
            'queueId': 1, 'scheduling': 'CREDIT_BASED_SHAPER',
            'cbs': [{'egressQueueId': 1, 'adminState': 'ENABLED',
                     'adminIdleSlopeBps': 99_000_000}],
        }]
        response = build_cbs_response_v13(request, legacy)
        self.assertEqual(response['status'], 'success')

    def test_disabled_existing_cbs_does_not_count(self):
        request, legacy = self.case()
        node = next(n for n in request['forwardingPlane']['configuration']['nodeConfigs']
                    if n['nodeId'] == 'ES3')
        port = next(p for p in node['portConfigs'] if p['portId'] == 'Port1')
        port['queueConfigs'] = [{
            'queueId': 2, 'scheduling': 'CREDIT_BASED_SHAPER',
            'cbs': [{'egressQueueId': 2, 'adminState': 'DISABLED',
                     'adminIdleSlopeBps': 99_000_000}],
        }]
        response = build_cbs_response_v13(request, legacy)
        self.assertEqual(response['status'], 'success')

    def test_ambiguous_active_existing_cbs_is_rejected(self):
        request, legacy = self.case()
        node = next(n for n in request['forwardingPlane']['configuration']['nodeConfigs']
                    if n['nodeId'] == 'ES3')
        port = next(p for p in node['portConfigs'] if p['portId'] == 'Port1')
        port['queueConfigs'] = [{
            'queueId': 2, 'scheduling': 'CREDIT_BASED_SHAPER',
            'cbs': [{'egressQueueId': 2, 'adminState': 'ENABLED'}],
        }]
        with self.assertRaisesRegex(CBSResponseConversionError, 'Invalid existing CBS idleSlope'):
            build_cbs_response_v13(request, legacy)



def build_cbs_response_v13(request, legacy):
    """Exercise the explicit configuration-to-response boundary."""
    return _map_response(request, legacy, prepared=prepare_cbs_configuration(request, legacy))


if __name__ == '__main__':
    unittest.main()
