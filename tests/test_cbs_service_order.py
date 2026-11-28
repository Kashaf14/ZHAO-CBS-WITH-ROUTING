"""Statically check the ordering of CBS service processing steps.

Inspects cbs_service.py using Python's AST; does not start a service,
invoke the Manager, or establish HTTP or numerical integration.
"""
from __future__ import annotations

import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class TestCBSServiceWiring(unittest.TestCase):
    def test_precheck_translate_solve_restore_response_order(self):
        # Inspect the actual function AST rather than inferring call ordering
        # from a handcrafted mock service implementation.
        src = (ROOT / "cbs_service.py").read_text(encoding="utf-8")
        cls = ast.parse(src)
        fn = next(f for f in cls.body if isinstance(f, ast.FunctionDef) and f.name == "optimize")
        positions_by_name = {
            n.func.id: n.lineno
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        expected = ["validate_cbs_precheck", "prepare_cbs_solver_usecase", "restore_client_classes_in_cbs_result", "prepare_cbs_configuration", "build_cbs_response_v13"]
        positions = [positions_by_name[name] for name in expected]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('mapping = prepare_cbs_solver_usecase(payload, usecase, optimizer)', src)
        self.assertIn('result = restore_client_classes_in_cbs_result(result, mapping)', src)

if __name__ == "__main__":
    unittest.main()
