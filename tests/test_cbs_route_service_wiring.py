"""Isolated HTTP/service sequencing tests with mocked dependencies.

No actual optimizer, shared contract, Manager or OpenCNC is invoked here.
"""
from __future__ import annotations

from copy import deepcopy
import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import psutil  # keep platform backend available across mocked imports
import unittest
from unittest.mock import patch

from test_cbs_route_preparation import HOPS, StrictCBSResolver, payload, usecase

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "optimizer-manager" / "cbs_service.py"


class FakeCBSOptimizer(StrictCBSResolver):
    name = "CBS"
    def __init__(self, calls):
        self.calls = calls
    def get_capabilities(self):
        return {"mechanisms": ["CBS"]}
    def optimize(self, uc):
        self.calls.append(("solve", deepcopy(uc.fixed_routes)))
        return {"optimizer": "CBS", "portClassGroups": []}


def make_module(name, **kwargs):
    module = ModuleType(name)
    module.__dict__.update(kwargs)
    return module


def load_service(calls):
    """Import real service.py with isolated fake boundaries, no external I/O."""
    def new_usecase(request):
        return usecase(request)
    def precheck(request, uc, optimizer):
        calls.append(("precheck", deepcopy(uc.fixed_routes)))
    def translation(request, uc, optimizer):
        calls.append(("translate", deepcopy(uc.fixed_routes)))
        return {"critical": "M1"}
    def restore(result, mapping):
        calls.append(("restore", mapping))
        return result
    def response(request, result, *, generated_routes=None):
        calls.append(("response", deepcopy(request), deepcopy(generated_routes)))
        return {"status": "ok", "result": result}

    fake_pkg = make_module("optimizers")
    fake_pkg.__path__ = []
    fakes = {
        "usecase_adapter": make_module("usecase_adapter", build_usecase_from_payload=new_usecase),
        "optimizers": fake_pkg,
        "optimizers.cbs": make_module("optimizers.cbs", CBSOptimizer=lambda: FakeCBSOptimizer(calls)),
        "cbs_response_adapter": make_module(
            "cbs_response_adapter", build_cbs_response_v13=response,
            CBSResponseConversionError=type("CBSResponseConversionError", (ValueError,), {})),
        "cbs_precheck": make_module("cbs_precheck", validate_cbs_precheck=precheck),
        "cbs_translation_boundary": make_module(
            "cbs_translation_boundary", prepare_cbs_solver_usecase=translation,
            restore_client_classes_in_cbs_result=restore),
    }
    sys.path.insert(0, str(ROOT))
    try:
        with patch.dict(sys.modules, fakes):
            spec = importlib.util.spec_from_file_location("_route_service_local_test", SERVICE)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(ROOT / "optimizer-manager"))
    return mod


class TestCBSServiceRouteWiring(unittest.TestCase):
    def test_existing_fixed_route_uses_normal_response_path(self):
        calls=[]
        svc=load_service(calls)
        request=payload(routing="fixed", hops=HOPS)
        original=deepcopy(request)
        result=svc.optimize(request)
        self.assertEqual(result["status"], "ok")
        self.assertEqual([x[0] for x in calls],
                         ["precheck", "translate", "solve", "restore", "response"])
        self.assertEqual(request, original)

    def test_missing_optimize_route_is_returned_for_cnc_application_later(self):
        from fastapi import HTTPException
        calls=[]
        svc=load_service(calls)
        request=payload()
        before=deepcopy(request)
        result = svc.optimize(request)
        self.assertEqual(result["status"], "ok")
        self.assertEqual([x[0] for x in calls],
                         ["precheck", "translate", "solve", "restore", "response"])
        self.assertEqual(calls[2][1]["f1"]["path"], HOPS)
        self.assertEqual(calls[-1][2], {"f1": HOPS})
        self.assertEqual(request, before)

    def test_missing_fixed_route_fails_before_solver(self):
        from fastapi import HTTPException
        calls=[]
        svc=load_service(calls)
        with self.assertRaises(HTTPException) as failure:
            svc.optimize(payload(routing="fixed"))
        self.assertEqual(failure.exception.detail["code"], "CBS_ROUTE_PREPARATION_FAILED")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
