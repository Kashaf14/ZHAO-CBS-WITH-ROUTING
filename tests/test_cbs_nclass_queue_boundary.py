"""Step 20: offline N-class CBS response/physical queue boundary.

Run from repository root after placing in tests/:
    python -m unittest discover -s tests -p 'test_cbs_nclass_queue_boundary.py' -v

These PCP/queue assignments are synthetic and explicitly NOT real CNC hardware
mappings. No request fixture is modified, no Manager or CNC process is run.
The granularity-capacity test intentionally checks a potential fail-open case;
a failure is a defect report, NOT a reason to weaken the assertion.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

from cbs_response_adapter import CBSResponseConversionError, build_cbs_response_v13
from algorithm.cbs import CBSOptimizer
from test_cbs_nclass_candidate import synth_request
from tsn_contract_validator.response_validator import validate_optimizer_response
from usecase_adapter import build_usecase_from_payload

TC1 = ROOT / "tests" / "fixtures" / "cbs-paper-tc1-step17-original.json"


def mapped_request(original: dict, n: int) -> dict:
    """Make N-class request with explicit, non-identity, synthetic mapping.

    M1..M7: PCP 7..1 -> physical queues 1..7. Queue 0 and PCP 0 are
    left unused by these SR streams (illustrative BE allocation only).
    This does not assert the hardware scheduler implements Zhao priority order.
    """
    req = deepcopy(original) if n == 1 else synth_request(original, classes=n)
    for stream in req['forwardingPlane']['streams']:
        class_id = stream['definition']['trafficClassId']
        if class_id.startswith('M') and class_id[1:].isdigit():
            i = int(class_id[1:])
            if 1 <= i <= 7:
                stream['configuration']['pcp'] = 8 - i
    for node in req['forwardingPlane']['configuration']['nodeConfigs']:
        for port in node['portConfigs']:
            if port.get('pcpMappingEnabled') is not True:
                raise AssertionError(f"PCP mapping disabled: {node['nodeId']}/{port['portId']}")
            port['pcpConfig']['trafficClassTable'] = [
                {'priority': 8 - i, 'egressQueueId': i} for i in range(1, 8)
            ]
    return req


def port_mapping(req: dict, node_id: str, port_id: str) -> list:
    return next(
        port['pcpConfig']['trafficClassTable']
        for node in req['forwardingPlane']['configuration']['nodeConfigs']
        if node['nodeId'] == node_id
        for port in node['portConfigs'] if port['portId'] == port_id
    )


def topology_port(req: dict, node_id: str, port_id: str) -> dict:
    return next(
        port
        for node in req['forwardingPlane']['topology']['nodes']
        if node['name'] == node_id
        for port in node['ports'] if port['id'] == port_id
    )


def output_queues(response: dict) -> dict:
    result = {}
    changes = response['solution']['changes']
    for node in changes.get('configuration', {}).get('nodeConfigs', []):
        for port in node['portConfigs']:
            key = (node['nodeId'], port['portId'])
            if key in result:
                raise AssertionError(f'Duplicate output port: {key}')
            result[key] = {}
            for queue in port['queueConfigs']:
                qid = queue['queueId']
                if qid in result[key]:
                    raise AssertionError(f'Duplicate output queue: {key}/{qid}')
                result[key][qid] = queue
    return result


class TestNClassPhysicalQueueBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_bytes = TC1.read_bytes()
        cls.original = json.loads(cls.fixture_bytes)
        cls.optimizer = CBSOptimizer()

    def solve(self, n: int):
        req = mapped_request(self.original, n)
        before = deepcopy(req)
        legacy = self.optimizer.optimize(build_usecase_from_payload(deepcopy(req)))
        self.assertEqual(req, before, 'Optimizer mutated request')
        self.assertTrue(legacy['allDeadlinesSatisfied'])
        return req, legacy

    def test_all_n_1_to_7_produce_distinct_physical_queue_updates(self):
        for n in range(1, 8):
            with self.subTest(n=n):
                req, legacy = self.solve(n)
                before = deepcopy(req)
                response = build_cbs_response_v13(req, legacy)
                self.assertEqual(response['status'], 'success')
                self.assertEqual(response['schemaVersion'], '1.3')
                self.assertEqual(response['solution']['representation'], 'partial')
                by_port = output_queues(response)
                expected = {}
                for group in legacy['portClassGroups']:
                    k = (group['node'], group['port'])
                    queue_id = int(group['srClass'][1:])
                    expected.setdefault(k, set()).add(queue_id)
                self.assertEqual({k: set(v) for k, v in by_port.items()}, expected)
                for k, queues in by_port.items():
                    self.assertLessEqual(len(queues), 7, (n, k))
                    for qid, item in queues.items():
                        self.assertGreaterEqual(qid, 1)  # Queue 0 left for BE.
                        self.assertLess(qid, topology_port(req, *k)['number_of_queues'])
                        self.assertEqual(item['scheduling'], 'CREDIT_BASED_SHAPER')
                        self.assertEqual(len(item['cbs']), 1)
                        self.assertEqual(item['cbs'][0]['egressQueueId'], qid)
                        self.assertGreater(item['cbs'][0]['adminIdleSlopeBps'], 0)
                self.assertEqual(req, before, 'Response converter mutated request')

    def test_seven_class_response_passes_shared_v13_validator(self):
        req, legacy = self.solve(7)
        baseline = deepcopy(req['forwardingPlane'])
        response = build_cbs_response_v13(req, legacy)
        report = validate_optimizer_response(
            response, request=req, baseline=baseline,
            expected_job_id='synthetic-offline', result_job_id='synthetic-offline',
        )
        self.assertTrue(report.applicable, report)
        self.assertEqual(req['forwardingPlane'], baseline)
        self.assertTrue(report.not_verified)  # No independent hardware check.

    def test_missing_mapping_for_targeted_class_is_rejected(self):
        req, legacy = self.solve(7)
        table = port_mapping(req, 'ES3', 'Port1')
        table[:] = [row for row in table if row['priority'] != 1]  # M7 PCP.
        with self.assertRaisesRegex(CBSResponseConversionError, 'PCP 1'):
            build_cbs_response_v13(req, legacy)

    def test_two_classes_on_same_physical_queue_are_rejected(self):
        req, legacy = self.solve(7)
        table = port_mapping(req, 'ES3', 'Port1')
        next(row for row in table if row['priority'] == 1)['egressQueueId'] = 6
        with self.assertRaisesRegex(CBSResponseConversionError, 'More than one CBS class'):
            build_cbs_response_v13(req, legacy)

    def test_insufficient_physical_queue_count_is_rejected(self):
        req, legacy = self.solve(7)
        topology_port(req, 'ES3', 'Port1')['number_of_queues'] = 7
        with self.assertRaisesRegex(CBSResponseConversionError, 'outside'):
            build_cbs_response_v13(req, legacy)

    def test_device_supported_queue_restriction_is_rejected(self):
        req, legacy = self.solve(7)
        device = next(d for d in req['capabilities']['devices'] if d['nodeId'] == 'ES3')
        device['mechanisms']['CBS']['supportedQueues'] = [1, 2, 3, 4, 5, 6]
        with self.assertRaisesRegex(CBSResponseConversionError, 'does not support CBS'):
            build_cbs_response_v13(req, legacy)

    def test_granularity_rounding_cannot_overcommit_link_capacity(self):
        """Every raw reservation is feasible; hardware rounding must stay feasible.

        ES3 has all M1..M7. 20 Mbps granularity would round all seven
        positive idleSlopes to at least 20 Mbps = >=140 Mbps on 100 Mbps.
        An adapter that checks only INDIVIDUAL slope < speed will fail this test.
        """
        req, legacy = self.solve(7)
        device = next(d for d in req['capabilities']['devices'] if d['nodeId'] == 'ES3')
        device['mechanisms']['CBS']['idleSlopeGranularityBps'] = 20_000_000
        with self.assertRaisesRegex(
            CBSResponseConversionError, '(?i)aggregate|capacity|overcommit|link speed|reserved'
        ):
            build_cbs_response_v13(req, legacy)

    def test_source_fixture_and_manager_advertisement_are_unchanged(self):
        self.assertEqual(TC1.read_bytes(), self.fixture_bytes)
        caps = self.optimizer.get_capabilities()
        self.assertNotIn("trafficClasses", caps)
        self.assertIs(caps["inputRequirements"]["requiresPcp"], True)
        self.assertIs(caps["inputRequirements"]["acceptsClientTrafficClassIds"], True)


if __name__ == '__main__':
    unittest.main()
