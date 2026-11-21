"""Selection and service-order tests, loading exact source functions via AST.

Run against the patched repository. Avoid starting a Manager, CBS worker or
network process; numerical and HTTP integration require separate tests.
"""
from __future__ import annotations

import ast
from contextlib import nullcontext
from pathlib import Path
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]

import sys

sys.path.insert(0, str(ROOT / "optimizer-manager"))

from capability_catalog import request_satisfies_input_requirements
from capability_catalog import supports_requested_traffic_classes


def extract_method(path, cls_name, method_name):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(item for item in tree.body
               if isinstance(item, ast.ClassDef) and item.name == cls_name)
    func = next(item for item in cls.body
                if isinstance(item, ast.FunctionDef) and item.name == method_name)
    func.decorator_list = []
    standalone = ast.fix_missing_locations(ast.Module(body=[func], type_ignores=[]))
    namespace = {
        "request_satisfies_input_requirements": request_satisfies_input_requirements,
        "supports_requested_traffic_classes": supports_requested_traffic_classes,
    }
    exec(compile(standalone, str(path), "exec"), namespace)
    return namespace[method_name]


class TestRegistryCBSSelection(unittest.TestCase):
    def setUp(self):
        registry = ROOT / "optimizer-manager" / "optimizers" / "registry.py"
        self.select = extract_method(registry, "OptimizerRegistry", "find_compatible_with_reason")

    def test_client_names_select_only_advertised_translation_capable_cbs(self):
        class Worker:
            def __init__(self, feature):
                self.feature = feature
            def get_capabilities(self):
                return {"name": "CBS", "tasks": ["configuration"],
                        "mechanisms": ["CBS"], "trafficClasses": ["M1", "M2"],
                        "inputRequirements": {"acceptsClientTrafficClassIds": self.feature}}
        fake = types.SimpleNamespace(
            optimizers={"CBS": Worker(True)},
            request_satisfies_input_requirements=lambda c, req: (True, ["requiresPcp"]),
        )
        found, why = self.select(fake, "configuration", "CBS", ["critical", "standard"], {})
        self.assertIsNotNone(found)
        self.assertEqual(why["matchedTrafficClasses"], ["critical", "standard"])
        fake.optimizers = {"CBS": Worker(False)}
        found, why = self.select(fake, "configuration", "CBS", ["critical", "standard"], {})
        self.assertIsNone(found)

    def test_tas_selection_respects_advertised_capability(self):
        worker = types.SimpleNamespace(get_capabilities=lambda: {
            "name": "TAS", "tasks": ["configuration"], "mechanisms": ["TAS"],
            "trafficClasses": ["M1"], "inputRequirements": {"acceptsClientTrafficClassIds": True},
        })
        registry = types.SimpleNamespace(
            optimizers={"TAS": worker},
            request_satisfies_input_requirements=lambda cap, req: (True, []),
        )
        selected, _ = self.select(registry, "configuration", "TAS", ["critical"], {})
        self.assertIs(selected, worker)


class TestCBSServiceWiring(unittest.TestCase):
    def test_precheck_translate_solve_restore_response_order(self):
        # Inspect the actual function AST rather than inferring call ordering
        # from a handcrafted mock service implementation.
        src = (ROOT / "optimizer-manager" / "cbs_service.py").read_text(encoding="utf-8")
        cls = ast.parse(src)
        fn = next(f for f in cls.body if isinstance(f, ast.FunctionDef) and f.name == "optimize")
        positions_by_name = {
            n.func.id: n.lineno
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        expected = ["validate_cbs_precheck", "prepare_cbs_solver_usecase", "restore_client_classes_in_cbs_result", "build_cbs_response_v13"]
        positions = [positions_by_name[name] for name in expected]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('mapping = prepare_cbs_solver_usecase(payload, usecase, optimizer)', src)
        self.assertIn('result = restore_client_classes_in_cbs_result(result, mapping)', src)

if __name__ == "__main__":
    unittest.main()
