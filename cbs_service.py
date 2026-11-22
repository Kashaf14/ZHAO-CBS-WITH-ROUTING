"""HTTP API for the standalone CBS optimizer service."""
import os
import time
from threading import Lock

import psutil
from fastapi import FastAPI, HTTPException

from usecase_adapter import build_usecase_from_payload
from algorithm.cbs import CBSOptimizer
from cbs_response_adapter import build_cbs_response_v13, CBSResponseConversionError
from cbs_precheck import validate_cbs_precheck
from cbs_route_preparation import prepare_cbs_routes, CBSRoutePreparationError
from cbs_translation_boundary import (
    prepare_cbs_solver_usecase,
    restore_client_classes_in_cbs_result,
)

app = FastAPI(title="CBS Optimizer Service")
optimizer = CBSOptimizer()

# Process measurements should not overlap between optimization requests.
optimization_lock = Lock()


@app.get("/health")
def health():
    return {
        "status": "ready",
        "optimizer": optimizer.name
    }


@app.get("/capabilities")
def capabilities():
    return optimizer.get_capabilities()


@app.post("/optimize")
def optimize(payload: dict):
    with optimization_lock:
        try:
            usecase = build_usecase_from_payload(payload)
            # Experimental single-output-port analysis, OFF by default.
            # Set only for isolated TC2-TC4 runs, e.g. SW1/P5.
            benchmark_port = os.environ.get("CBS_BENCHMARK_EGRESS_PORT", "").strip()
            if benchmark_port:
                parts = benchmark_port.split("/")
                if len(parts) != 2 or not all(parts):
                    raise ValueError(
                        "CBS_BENCHMARK_EGRESS_PORT must be NODE/PORT"
                    )
                usecase.cbs_benchmark_output_port = tuple(parts)
        except (KeyError, ValueError, TypeError, IndexError) as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "INVALID_SCENARIO",
                    "message": str(exc)
                }
            ) from exc

        # Resolve routing only inside the CBS adapter;
        # Fixed/preserve paths remain unchanged. For optimize with no path,
        # populate the internal Usecase with one directed bridge route.
        try:
            generated_routes = prepare_cbs_routes(payload, usecase, optimizer)
        except CBSRoutePreparationError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "CBS_ROUTE_PREPARATION_FAILED", "message": str(exc)},
            ) from exc

        # Reject hardware-inapplicable CBS requests before numerical work.
        try:
            validate_cbs_precheck(payload, usecase, optimizer)
        except (CBSResponseConversionError, ValueError, KeyError, TypeError, IndexError) as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "CBS_PRECHECK_FAILED",
                    "message": str(exc),
                },
            ) from exc

        # Keep standardized request/client IDs intact. Only the internal
        # Usecase receives Zhao's M1..M7 labels; ambiguous mappings fail closed.
        try:
            mapping = prepare_cbs_solver_usecase(payload, usecase, optimizer) # it will give us trafficClassIds mapping to M1-M7
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "CBS_CLASS_TRANSLATION_FAILED", "message": str(exc)},
            ) from exc

        process = psutil.Process()
        memory_before = process.memory_info().rss
        cpu_before = process.cpu_times()
        start_time = time.perf_counter()

        try:
            result = optimizer.optimize(usecase)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "OPTIMIZATION_FAILED",
                    "message": str(exc)
                }
            ) from exc

        execution_time = time.perf_counter() - start_time
        cpu_after = process.cpu_times()
        memory_after = process.memory_info().rss

        cpu_time = (
            (cpu_after.user - cpu_before.user)
            + (cpu_after.system - cpu_before.system)
        )

        result["kpis"] = {
            "executionTimeMs": execution_time * 1000,
            "cpuTimeMs": cpu_time * 1000,
            "memoryBeforeBytes": memory_before,
            "memoryAfterBytes": memory_after,
            "memoryDeltaBytes": memory_after - memory_before
        }

        # A route delta is returned for semantic validation; CNC must separately
        # implement its application. The native Phase-1 adapter still rejects it.

        try:
            # Restore client class IDs before the existing response adapter
            # validates each portClassGroups.srClass against the input request.
            result = restore_client_classes_in_cbs_result(result, mapping)
            return build_cbs_response_v13(payload, result, generated_routes=generated_routes)
        except (CBSResponseConversionError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "CBS_RESPONSE_MAPPING_FAILED", "message": str(exc)},
            ) from exc
