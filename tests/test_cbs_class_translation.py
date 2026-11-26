"""Isolated first-step CBS logical-class translation tests (stdlib only)."""
import copy
import importlib.util
import pathlib
import unittest

SOURCE = pathlib.Path(__file__).resolve().parents[1] / 'cbs_class_translation.py'
spec = importlib.util.spec_from_file_location('cbs_class_translation', SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def request(classes=('critical', 'standard'), pcps=(7, 5), mapping=((7, 0), (5, 1))):
    return {
        'schemaVersion': '1.4', 'mechanisms': [{'name': 'CBS'}],
        'optimizationTask': {'scope': {'trafficClasses': list(classes)}},
        'forwardingPlane': {
            'streams': [
                {'definition': {'id': f'f{i}', 'trafficClassId': name},
                 'configuration': {'pcp': pcp}}
                for i, (name, pcp) in enumerate(zip(classes, pcps))
            ],
            'configuration': {'nodeConfigs': [
                {'nodeId': 'SW1', 'portConfigs': [
                    {'portId': 'P1', 'pcpMappingEnabled': True,
                     'pcpConfig': {'trafficClassTable': [
                        {'priority': pcp, 'egressQueueId': tc}
                        for pcp, tc in mapping
                    ]},
                    'queuePriorityTable': [
                        {'queueId': queue, 'schedulingPriority': 8 - queue}
                        for queue in range(8)
                    ]}
                ]}
            ]},
        }
    }


def egress(classes=('critical', 'standard')):
    return {f'f{i}': [('SW1', 'P1')] for i in range(len(classes))}


class TestClassTranslation(unittest.TestCase):
    def test_explicit_mapping_prioritizes_tc_not_pcp(self):
        req = request()
        snapshot = copy.deepcopy(req)
        self.assertEqual(module.derive_zhao_class_mapping(req, egress()),
                         {'standard': 'M1', 'critical': 'M2'})
        self.assertEqual(req, snapshot)

    def test_highest_available_cbs_tc_is_m1(self):
        req = request(mapping=((7, 7), (5, 5)))
        self.assertEqual(module.derive_zhao_class_mapping(req, egress()),
                         {'critical': 'M1', 'standard': 'M2'})

    def test_one_class_can_be_m1_even_when_tc5(self):
        req = request(classes=('audio',), pcps=(5,), mapping=((5, 5),))
        self.assertEqual(module.derive_zhao_class_mapping(req, egress(('audio',))),
                         {'audio': 'M1'})

    def test_order_remains_stable_across_ports(self):
        req = request(mapping=((7, 7), (5, 4)))
        other = copy.deepcopy(req['forwardingPlane']['configuration']['nodeConfigs'][0]['portConfigs'][0])
        other['portId'] = 'P2'
        req['forwardingPlane']['configuration']['nodeConfigs'][0]['portConfigs'].append(other)
        routes = {'f0': [('SW1', 'P1'), ('SW1', 'P2')], 'f1': [('SW1', 'P2')]}
        self.assertEqual(module.derive_zhao_class_mapping(req, routes),
                         {'critical': 'M1', 'standard': 'M2'})

    def test_inconsistent_per_port_tc_rejected(self):
        req = request(mapping=((7, 7), (5, 5)))
        other = copy.deepcopy(req['forwardingPlane']['configuration']['nodeConfigs'][0]['portConfigs'][0])
        other['portId'] = 'P2'
        other['pcpConfig']['trafficClassTable'][0]['egressQueueId'] = 3
        req['forwardingPlane']['configuration']['nodeConfigs'][0]['portConfigs'].append(other)
        with self.assertRaisesRegex(module.CBSClassTranslationError, 'inconsistent'):
            module.derive_zhao_class_mapping(req, {'f0': [('SW1', 'P1'), ('SW1', 'P2')], 'f1': [('SW1', 'P1')]})

    def test_missing_mapping_rejected(self):
        req = request(mapping=((7, 0),))
        with self.assertRaisesRegex(module.CBSClassTranslationError, 'Expected exactly one'):
            module.derive_zhao_class_mapping(req, egress())

    def test_disabled_mapping_rejected(self):
        req = request()
        req['forwardingPlane']['configuration']['nodeConfigs'][0]['portConfigs'][0]['pcpMappingEnabled'] = False
        with self.assertRaisesRegex(module.CBSClassTranslationError, 'Explicit, enabled'):
            module.derive_zhao_class_mapping(req, egress())

    def test_same_tc_for_two_classes_rejected(self):
        req = request(mapping=((7, 2), (5, 2)))
        with self.assertRaisesRegex(module.CBSClassTranslationError, 'share TC'):
            module.derive_zhao_class_mapping(req, egress())

    def test_unmapped_stream_rejected(self):
        with self.assertRaisesRegex(module.CBSClassTranslationError, 'Missing resolved'):
            module.derive_zhao_class_mapping(request(), {'f0': [('SW1', 'P1')]})

    def test_invalid_pcp_rejected(self):
        req = request()
        req['forwardingPlane']['streams'][0]['configuration']['pcp'] = True
        with self.assertRaisesRegex(module.CBSClassTranslationError, 'invalid PCP'):
            module.derive_zhao_class_mapping(req, egress())

    def test_non_cbs_mechanism_rejected(self):
        req = request()
        req['mechanisms'] = [{'name': 'TAS'}]
        with self.assertRaisesRegex(module.CBSClassTranslationError, 'CBS-only'):
            module.derive_zhao_class_mapping(req, egress())

    def test_duplicate_mapping_rows_rejected(self):
        req = request()
        table = req['forwardingPlane']['configuration']['nodeConfigs'][0]['portConfigs'][0]['pcpConfig']['trafficClassTable']
        table.append(dict(table[0]))
        with self.assertRaisesRegex(module.CBSClassTranslationError, 'Expected exactly one'):
            module.derive_zhao_class_mapping(req, egress())


    def test_explicit_priority_overrides_queue_id_order(self):
        req = request(
            mapping=((7, 2), (5, 7))
        )

        port = req["forwardingPlane"]["configuration"]["nodeConfigs"][0]["portConfigs"][0]

        port["queuePriorityTable"] = [
            {"queueId": 2, "schedulingPriority": 1},
            {"queueId": 7, "schedulingPriority": 2},
        ]

        self.assertEqual(
            module.derive_zhao_class_mapping(req, egress()),
            {"critical": "M1", "standard": "M2"},
        )

if __name__ == '__main__':
    unittest.main()
