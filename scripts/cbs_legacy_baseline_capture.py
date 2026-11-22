#!/usr/bin/env python3
"""Step 17: capture numeric CBS outputs BEFORE generic N-class dispatch migration.

This is a regression reference from the EXISTING implementation, not an
independent network-calculus or hardware validation. It does not edit code,
contracts, requests, or OpenCNC. It refuses to overwrite an existing snapshot.

Run at repository root:
    python scripts/cbs_legacy_baseline_capture.py.py

Requires the previously passing Step 15/16 test modules and TC1 fixture.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


from algorithm.cbs import CBSOptimizer
import algorithm.cbs_nclass_candidate as generic_module
import algorithm.cbs_m3_candidate as old_m3_module
from usecase_adapter import build_usecase_from_payload
from test_cbs_nclass_parity import make_request
from test_cbs_nclass_background import append_m3

FIXTURE = Path(os.environ.get("CBS_TC1_REQUEST") or (
    ROOT.parent / "tsn-optimization-contracts" / "tests" / "request"
    / "valid" / "cbs-paper-tc1-full-8flow-contract.json"
))
OUT = ROOT / "tests" / "regression" / "evidence" / "cbs_step17_pre_dispatch_baseline.json"
CBS_FIELDS = ("idleSlopeBps", "sendSlopeBps", "creditMinBits", "creditMaxBits")
GROUP_FIELDS = (
    "linkSpeedBps", "requiredDeadlineNs", "sharedDelayNs",
    "blockingFrameBits", "maxFrameBits", "aggregateRhoBps",
)
STREAM_FIELDS = ("sharedPathDelayNs", "deadlineNs", "deadlineSatisfied")


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical_json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def checked_number(value, context):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Expected numerical {context}: {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"Non-finite {context}: {value!r}")
    return value


def compact_result(result):
    groups = []
    for g in result["portClassGroups"]:
        key = (g["srClass"], g["node"], g["port"])
        entry = {"srClass": key[0], "node": key[1], "port": key[2]}
        for field in GROUP_FIELDS:
            entry[field] = checked_number(g[field], f"{key}.{field}")
        entry["sharedCBS"] = {
            field: checked_number(g["sharedCBS"][field], f"{key}.sharedCBS.{field}")
            for field in CBS_FIELDS
        }
        groups.append(entry)
    groups.sort(key=lambda g: (g["srClass"], g["node"], g["port"]))
    group_keys = [(g["srClass"], g["node"], g["port"]) for g in groups]
    if len(group_keys) != len(set(group_keys)):
        raise ValueError("Duplicate port/class keys")

    streams = []
    for row in result["sharedStreamResults"]:
        entry = {"stream": row["stream"]}
        for field in STREAM_FIELDS:
            value = row[field]
            if field == "deadlineSatisfied":
                if not isinstance(value, bool):
                    raise ValueError(f"Expected boolean {row['stream']}.{field}")
            else:
                value = checked_number(value, f"{row['stream']}.{field}")
            entry[field] = value
        streams.append(entry)
    streams.sort(key=lambda row: row["stream"])
    names = [row["stream"] for row in streams]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate stream IDs")
    if not isinstance(result["allDeadlinesSatisfied"], bool):
        raise ValueError("Expected boolean allDeadlinesSatisfied")
    return {
        "portClassGroups": groups,
        "sharedStreamResults": streams,
        "allDeadlinesSatisfied": result["allDeadlinesSatisfied"],
        "overallWorstCaseDelayNs": checked_number(
            result["overallWorstCaseDelayNs"], "overallWorstCaseDelayNs"
        ),
    }


def capture_case(name, payload, optimizer, *, expected_class_names, expected_stream_count):
    unchanged = canonical_json(payload)
    # The generic candidate must NOT be involved: otherwise a parity snapshot
    # could falsely pass after the dispatcher is replaced.
    with patch.object(generic_module, "optimize_nclass_candidate",
                      side_effect=AssertionError(
                          "STOP: generic dispatch already active; baseline capture invalid"
                      )) as generic_call:
        result = optimizer.optimize(build_usecase_from_payload(deepcopy(payload)))
        if generic_call.call_count:
            raise AssertionError(f"STOP: generic path executed for {name}")
    if canonical_json(payload) != unchanged:
        raise AssertionError(f"Input modified: {name}")
    summary = compact_result(result)
    actual_classes = {g["srClass"] for g in summary["portClassGroups"]}
    if actual_classes != set(expected_class_names):
        raise AssertionError(f"Unexpected classes for {name}: {actual_classes}")
    if len(summary["sharedStreamResults"]) != expected_stream_count:
        raise AssertionError(f"Unexpected stream count for {name}")
    if not summary["allDeadlinesSatisfied"]:
        raise AssertionError(f"Existing implementation not feasible: {name}")
    print(f"CAPTURED {name}: {len(summary['portClassGroups'])} port/class groups, "
          f"{len(summary['sharedStreamResults'])} streams")
    return {"inputSHA256": sha(unchanged), "result": summary}


def main():
    if OUT.exists():
        raise RuntimeError(f"STOP: baseline already exists; refusing overwrite: {OUT}")
    fixture_raw = FIXTURE.read_bytes()
    original = json.loads(fixture_raw)
    cbs_source = ROOT / "algorithm" / "cbs.py"
    optimizer = CBSOptimizer()
    if optimizer.get_capabilities().get("trafficClasses") != ["M1"]:
        raise RuntimeError("STOP: Manager capability is not M1-only")

    requests = {
        "tc1_m1_with_m2_background": (
            deepcopy(original), ["M1"], 4
        ),
        "tc1_m1_m2": (
            make_request(original), ["M1", "M2"], 8
        ),
        "tc1_m1_m2_m3_100B": (
            make_request(original, with_m3=True, frame_size=100),
            ["M1", "M2", "M3"], 9
        ),
        "tc1_m1_m2_m3_300B": (
            make_request(original, with_m3=True, frame_size=300),
            ["M1", "M2", "M3"], 9
        ),
        "tc1_m1_with_m3_300B_background": (
            append_m3(deepcopy(original), frame_size=300), ["M1"], 4
        ),
    }
    # Confirm that the old class-specific M3 dispatcher really is active.
    from unittest.mock import patch as _patch
    m3_req = requests["tc1_m1_m2_m3_100B"][0]
    with _patch.object(old_m3_module, "optimize_m1_m2_m3_candidate",
                       wraps=old_m3_module.optimize_m1_m2_m3_candidate) as call:
        optimizer.optimize(build_usecase_from_payload(deepcopy(m3_req)))
        if call.call_count != 1:
            raise RuntimeError(
                "STOP: old M3 dispatch not active; cannot capture pre-migration baseline"
            )

    cases = {}
    for name, (req, classes, count) in requests.items():
        cases[name] = capture_case(
            name, req, optimizer, expected_class_names=classes,
            expected_stream_count=count,
        )
    if FIXTURE.read_bytes() != fixture_raw:
        raise AssertionError("Original TC1 request fixture changed")
    snapshot = {
        "kind": "CBS pre-generic-dispatch numerical regression reference",
        "classification": "user-executed prior implementation output; NOT independent NC validation",
        "sourceFixtureSHA256": sha(fixture_raw),
        "legacyDispatcherSourceSHA256": sha(cbs_source.read_bytes()),
        "managerAdvertisedTrafficClasses": ["M1"],
        "cases": cases,
    }
    data = json.dumps(snapshot, indent=2, sort_keys=True,
                      allow_nan=False, ensure_ascii=True) + "\n"
    # Fail safely if a competing process created the file after the check.
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("x", encoding="utf-8") as handle:
        handle.write(data)
    print(f"Saved: {OUT.relative_to(ROOT)}")
    print(f"Snapshot SHA256: {sha(data.encode('utf-8'))}")
    print("Do not overwrite. Save this snapshot as a reproducible test artifact.")


if __name__ == "__main__":
    main()
