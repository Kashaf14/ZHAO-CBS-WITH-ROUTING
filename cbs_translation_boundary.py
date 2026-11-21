"""CBS-only bridge between client traffic-class IDs and Zhao solver symbols.

No mutation of the standardized request. The numerical solver sees M1..M7,
while the published response uses the client's original IDs.
"""

from __future__ import annotations

from copy import deepcopy

from cbs_class_translation import (
    CBSClassTranslationError,
    derive_zhao_class_mapping,
)
from algorithm.cbs_nclass_candidate import _cbs_egress_context

def prepare_cbs_solver_usecase(request: dict, usecase, optimizer) -> dict[str, str]:
    """Return {client class: Zhao class} and translate the *internal* Usecase.

    All required mapping/route validation is completed before the Usecase is
    changed. Caller must perform the established CBS hardware precheck after
    this step; this mapper alone does not assert CBS capability or feasibility.
    """
    egress_by_stream = {}
    for stream in usecase.streams:
        name = stream["name"]
        if name in egress_by_stream:
            raise CBSClassTranslationError(f"Duplicate Usecase stream {name!r}")
        context = _cbs_egress_context(optimizer, stream, usecase)
        ports = [(p["node"], p["port"]) for p in context["egressPorts"]]
        if not ports:
            raise CBSClassTranslationError(f"No resolved egress ports for stream {name!r}")
        egress_by_stream[name] = ports

    mapping = derive_zhao_class_mapping(request, egress_by_stream)

    # Derive both new sets before mutating anything. The standard contract
    # retains the original names; the solver's Usecase holds the Zhao labels.
    translated_streams = []
    for stream in usecase.streams:
        name = stream["name"]
        client_class = stream.get("srClass")
        if client_class not in mapping:
            raise CBSClassTranslationError(
                f"Usecase class {client_class!r} of {name!r} has no CNC mapping"
            )
        translated_streams.append(mapping[client_class])

    targets = getattr(usecase, "target_traffic_classes", None)
    if not isinstance(targets, (list, tuple)) or not targets:
        raise CBSClassTranslationError("Explicit non-empty target traffic classes required")
    if len(set(targets)) != len(targets):
        raise CBSClassTranslationError("Duplicate target traffic classes")
    try:
        translated_targets = [mapping[x] for x in targets]
    except KeyError as exc:
        raise CBSClassTranslationError(
            f"Target class {exc.args[0]!r} has no CNC mapping"
        ) from exc

    for stream, mapped_class in zip(usecase.streams, translated_streams):
        stream["srClass"] = mapped_class
    usecase.target_traffic_classes = translated_targets
    return mapping


def restore_client_classes_in_cbs_result(result: dict, mapping: dict[str, str]) -> dict:
    """Restore every diagnostic srClass without changing CBS numeric values.

    Use a copy so the internal solver result remains in Zhao notation.
    The standard v1.3 response adapter checks that portClassGroups.srClass
    equals original forwardingPlane.streams[].definition.trafficClassId.
    """
    inverse = {solver: client for client, solver in mapping.items()}
    if len(inverse) != len(mapping):
        raise CBSClassTranslationError("Non-bijective client/Zhao class mapping")

    def convert(value):
        if isinstance(value, dict):
            converted = {}
            for key, member in value.items():
                if key == "srClass":
                    if member not in inverse:
                        raise CBSClassTranslationError(
                            f"Unexpected Zhao class in optimizer result: {member!r}"
                        )
                    converted[key] = inverse[member]
                else:
                    converted[key] = convert(member)
            return converted
        if isinstance(value, list):
            return [convert(item) for item in value]
        if isinstance(value, tuple):
            return tuple(convert(item) for item in value)
        return deepcopy(value)

    return convert(result)
