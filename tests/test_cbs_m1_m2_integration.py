"""Offline Zhao TC1 M1/M2 numerical and adapter integration checks.

Run from the standalone CBS repository with Hamza's external Usecase library
available through TSN_LIBRARY_PATH and the shared-contract TC1 fixture
stored locally in tests/fixtures/cbs-paper-tc1-step17-original.json.

These checks do not establish independent NC correctness or live CNC integration.

Run: python -m unittest discover -s tests -p 'test_cbs_m1_m2_integration.py' -v
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import sys
import unittest

FRAMEWORK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FRAMEWORK))

from algorithm.cbs import CBSOptimizer  # noqa: E402
from usecase_adapter import build_usecase_from_payload  # noqa: E402

TC1 = FRAMEWORK / "tests" / "fixtures" / "cbs-paper-tc1-step17-original.json"

M1_REFERENCE = {
    ('ES1', 'Port1'): 12.77,
    ('ES2', 'Port1'): 4.94,
    ('ES3', 'Port1'): 2.47,
    ('SW1', 'P4'): 9.84,
    ('SW1', 'P5'): 9.56,
}
M2_REFERENCE = {
    ('ES1', 'Port1'): 7.93,
    ('ES2', 'Port1'): 0.72,
    ('ES3', 'Port1'): 5.11,
    ('SW1', 'P4'): 8.74,
    ('SW1', 'P5'): 4.42,
}
TOLERANCE_PP = 0.01


def modified_input(payload):
    out = deepcopy(payload)
    out['optimizationTask']['scope']['trafficClasses'] = ['M1', 'M2']
    return out


def group_map(result):
    groups = {}
    for group in result['portClassGroups']:
        key = (group['srClass'], group['node'], group['port'])
        if key in groups:
            raise AssertionError(f'Duplicate port/class: {key}')
        groups[key] = group
    return groups


class TestM1M2TC1Candidate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not TC1.is_file():
            raise FileNotFoundError(f'TC1 request not found: {TC1}. Restore the local historical fixture.')
        cls.payload = json.loads(TC1.read_text(encoding='utf-8'))
        if (cls.payload.get('schemaVersion') != '1.4'
                or cls.payload.get('operation') != 'configuration'
                or cls.payload.get('optimizationTask', {}).get('scope', {}).get('trafficClasses') != ['M1']):
            raise AssertionError('Expected verified v1.4 M1-targeted TC1 fixture')
        cls.optimizer = CBSOptimizer()
        cls.baseline = cls.optimizer.optimize(build_usecase_from_payload(cls.payload))
        cls.full_payload = modified_input(cls.payload)
        cls.candidate = cls.optimizer.optimize(build_usecase_from_payload(cls.full_payload))
        cls.baseline_groups = group_map(cls.baseline)
        cls.candidate_groups = group_map(cls.candidate)

    def test_m1_baseline_preserved(self):
        self.assertEqual(set(self.baseline_groups), {
            ('M1', *port) for port in M1_REFERENCE
        })
        for port, ref in M1_REFERENCE.items():
            original = self.baseline_groups[('M1', *port)]['sharedCBS']['idleSlopeBps']
            combined = self.candidate_groups[('M1', *port)]['sharedCBS']['idleSlopeBps']
            self.assertTrue(math.isclose(original, combined, rel_tol=0, abs_tol=1e-5), port)
            self.assertLessEqual(abs(combined / 1e6 - ref), TOLERANCE_PP, port)

    def test_m2_matches_published_tc1_reservation_table(self):
        self.assertEqual(set(self.candidate_groups), {
            (klass, *port) for klass in ('M1', 'M2') for port in M1_REFERENCE
        })
        for port, ref in M2_REFERENCE.items():
            group = self.candidate_groups[('M2', *port)]
            actual_pct = group['sharedCBS']['idleSlopeBps'] / 1e6
            self.assertLessEqual(abs(actual_pct - ref), TOLERANCE_PP, (port, actual_pct, ref))

    def test_combined_capacity_credit_bounds_and_all_eight_deadlines(self):
        m1_groups = {(n, p): group for (klass, n, p), group
                     in self.candidate_groups.items() if klass == 'M1'}
        m2_groups = {(n, p): group for (klass, n, p), group
                     in self.candidate_groups.items() if klass == 'M2'}
        for port, m1 in m1_groups.items():
            m2 = m2_groups[port]
            capacity = m1['linkSpeedBps']
            s1 = m1['sharedCBS']['idleSlopeBps']
            s2 = m2['sharedCBS']['idleSlopeBps']
            self.assertGreater(s1, 0, port)
            self.assertGreater(s2, 0, port)
            self.assertLess(s1 + s2, capacity, port)
            self.assertLess(m2['sharedCBS']['creditMinBits'], 0, port)
            self.assertGreater(m2['sharedCBS']['creditMaxBits'], 0, port)
            self.assertAlmostEqual(
                m2['sharedCBS']['sendSlopeBps'], s2-capacity, places=6
            )
        flows = {row['stream']: row for row in self.candidate['sharedStreamResults']}
        self.assertEqual(set(flows), {f'f{i}' for i in range(1, 9)})
        self.assertTrue(self.candidate['allDeadlinesSatisfied'])
        for flow, row in flows.items():
            self.assertTrue(row['deadlineSatisfied'], flow)
            self.assertLessEqual(row['sharedPathDelayNs'], row['deadlineNs'] + 1e-6, flow)

    def test_cbs_capability_advertises_client_class_support(self):
        caps = self.optimizer.get_capabilities()
        self.assertNotIn("trafficClasses", caps)
        self.assertIs(caps["inputRequirements"]["requiresPcp"], True)
        self.assertIs(caps["inputRequirements"]["acceptsClientTrafficClassIds"], True)

    def test_m2_reconfiguration_numerical_parity(self):
        """Numerical parity only, not validation of CNC reconfiguration policies."""
        payload = deepcopy(self.full_payload)
        payload['operation'] = 'reconfiguration'
        payload['optimizationTask']['changePolicy'] = {'allowQueueChanges': True}
        result = CBSOptimizer().optimize(build_usecase_from_payload(payload))
        actual = group_map(result)
        self.assertEqual(set(actual), set(self.candidate_groups))
        self.assertEqual(len(result['sharedStreamResults']), len(self.candidate['sharedStreamResults']))
        for key, expected in self.candidate_groups.items():
            self.assertAlmostEqual(
                actual[key]['sharedCBS']['idleSlopeBps'],
                expected['sharedCBS']['idleSlopeBps'], places=6,
            )

    def test_multiframe_rejected_before_m1_math(self):
        payload = deepcopy(self.full_payload)
        payload['forwardingPlane']['streams'][0]['definition']['trafficSpec']['maxIntervalFrames'] = 2
        with self.assertRaisesRegex(ValueError, 'one frame per interval'):
            CBSOptimizer().optimize(build_usecase_from_payload(payload))

    def test_response_adapter_rejects_missing_m2_queue_mapping(self):
        # Original TC1 request has M2 streams with PCP 4 but NO physical queue
        # mapping for PCP 4. Do not assume PCP == physical queue ID.
        from cbs_response_adapter import (
            CBSResponseConversionError,
            build_cbs_response_v13,
        )
        fp = self.full_payload['forwardingPlane']
        for nc in fp['configuration']['nodeConfigs']:
            for pc in nc['portConfigs']:
                table = pc.get('pcpConfig', {}).get('trafficClassTable', [])
                self.assertNotIn(4, [entry['priority'] for entry in table])

        with self.assertRaisesRegex(CBSResponseConversionError, 'PCP 4'):
            build_cbs_response_v13(self.full_payload, self.candidate)
        # This is an EXPECTED NEGATIVE test, not successful response validation.



if __name__ == '__main__':
    unittest.main()
