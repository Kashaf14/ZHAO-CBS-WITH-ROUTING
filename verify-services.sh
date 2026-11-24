#!/usr/bin/env bash
# Post-deployment verification for the two standalone Zhao CBS variants.
# Runs against services already started by Compose (or equivalent deployment).
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIXTURE="${SMOKE_REQUEST:-$ROOT/tests/fixtures/cbs-paper-tc1-full-8flow-contract.json}"
FIXED_URL="${FIXED_URL:-http://127.0.0.1:8003}"
ROUTING_URL="${ROUTING_URL:-http://127.0.0.1:8001}"

for command in curl python3; do
  command -v "$command" >/dev/null || { echo "Missing required command: $command" >&2; exit 1; }
done
[[ -f "$FIXTURE" ]] || { echo "Smoke-test fixture not found: $FIXTURE" >&2; exit 1; }

TMPDIR_VERIFY="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_VERIFY"' EXIT

# Remove routes only within the task's selected stream/class scope. All other
# streams retain their original routes, as required by response validation.
python3 - "$FIXTURE" "$TMPDIR_VERIFY/no-routes.json" <<'PY'
import json
import sys
from pathlib import Path

request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
scope = request["optimizationTask"]["scope"]
selected_ids = set(scope.get("streamIds") or [])
selected_classes = set(scope.get("trafficClasses") or [])
if not (selected_ids or selected_classes):
    raise SystemExit("Fixture must have an explicit stream/class scope")
changed = []
for stream in request["forwardingPlane"]["streams"]:
    if stream.get("change") == "remove":
        continue
    definition = stream["definition"]
    selected = ((not selected_ids or definition["id"] in selected_ids)
                and (not selected_classes or definition.get("trafficClassId") in selected_classes))
    configuration = stream.get("configuration") or {}
    if selected:
        if not (configuration.get("path") or configuration.get("listenerPaths")):
            raise SystemExit(f"Scoped stream already has no path: {definition['id']}")
        stream.setdefault("decisionVariables", {})["routing"] = "optimize"
        stream.setdefault("configuration", {}).pop("path", None)
        stream["configuration"].pop("listenerPaths", None)
        changed.append(definition["id"])
    elif not (configuration.get("path") or configuration.get("listenerPaths")):
        raise SystemExit(f"Out-of-scope stream has no path: {definition['id']}")
if not changed:
    raise SystemExit("No selected streams available to test route generation")
Path(sys.argv[2]).write_text(json.dumps(request, indent=2), encoding="utf-8")
print("Prepared missing-route test for scoped streams:", ", ".join(changed))
PY

wait_for_health() {
  local url="$1"
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 "$url/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "Service did not become healthy: $url" >&2
  return 1
}

verify_identity_and_capabilities() {
  local url="$1" name="$2" id="$3" routes_required="$4" can_generate="$5"
  curl -fsS --max-time 5 "$url/health" -o "$TMPDIR_VERIFY/health.json"
  curl -fsS --max-time 5 "$url/capabilities" -o "$TMPDIR_VERIFY/capabilities.json"
  python3 - "$TMPDIR_VERIFY/health.json" "$TMPDIR_VERIFY/capabilities.json" \
    "$name" "$id" "$routes_required" "$can_generate" <<'PY'
import json
import sys
from pathlib import Path

health = json.loads(Path(sys.argv[1]).read_text())
caps = json.loads(Path(sys.argv[2]).read_text())
name, optimizer_id = sys.argv[3:5]
requires_routes = sys.argv[5] == "true"
can_generate = sys.argv[6] == "true"
assert health.get("status") == "ready", health
assert health.get("optimizer") == name, health
assert caps.get("name") == name, caps
assert caps.get("mechanisms") == ["CBS"], caps
requirements = caps.get("inputRequirements") or {}
assert requirements.get("requiresPcp") is True, requirements
assert requirements.get("requiresRoutes") is requires_routes, requirements
routing = caps.get("routing")
assert isinstance(routing, dict), "Missing routing capabilities; rebuild updated optimizer code"
assert routing.get("supportsRouteGeneration") is can_generate, routing
assert routing.get("supportsRouteOptimization") is False, routing
print(f"PASS: {name} health and capabilities")
PY
}

post_and_verify() {
  local url="$1" name="$2" id="$3" request_path="$4" expect_generated="$5"
  curl -fsS --max-time 120 -X POST "$url/optimize" \
    -H 'Content-Type: application/json' \
    --data-binary "@$request_path" \
    -o "$TMPDIR_VERIFY/result.json"
  python3 - "$TMPDIR_VERIFY/result.json" "$request_path" \
    "$name" "$id" "$expect_generated" <<'PY'
import json
import sys
from pathlib import Path

result = json.loads(Path(sys.argv[1]).read_text())
request = json.loads(Path(sys.argv[2]).read_text())
name, optimizer_id = sys.argv[3:5]
expect_generated = sys.argv[5] == "true"
assert result.get("schemaVersion") == "1.3", result.get("schemaVersion")
assert result.get("status") == "success", result.get("status")
assert result.get("optimizer", {}).get("name") == name, result.get("optimizer")
assert result.get("optimizer", {}).get("id") == optimizer_id, result.get("optimizer")
assert isinstance(result.get("kpis"), dict), "Missing KPIs"
solution = result.get("solution")
assert isinstance(solution, dict), "Successful response has no solution"
updates = (solution.get("changes") or {}).get("streams") or []
if expect_generated:
    scope = request["optimizationTask"]["scope"]
    ids = set(scope.get("streamIds") or [])
    classes = set(scope.get("trafficClasses") or [])
    expected = {
        (s["definition"]["streamId"]["macAddress"], s["definition"]["streamId"]["uniqueId"])
        for s in request["forwardingPlane"]["streams"]
        if s.get("change") != "remove"
        and (not ids or s["definition"]["id"] in ids)
        and (not classes or s["definition"].get("trafficClassId") in classes)
    }
    actual = {
        (s["streamId"]["macAddress"], s["streamId"]["uniqueId"])
        for s in updates
        if isinstance(s.get("configuration", {}).get("path"), list)
        and s["configuration"]["path"]
    }
    assert len(updates) == len(expected), f"Expected {len(expected)} route updates; got {len(updates)}"
    assert actual == expected, f"Generated route stream identities differ: {actual} != {expected}"
else:
    assert not updates, "Fixed-route result unexpectedly changed stream routes"
print(f"PASS: {name} optimization ({'generated routes' if expect_generated else 'fixed routes'})")
PY
}

echo "Checking Zhao CBS on $FIXED_URL..."
wait_for_health "$FIXED_URL"
verify_identity_and_capabilities "$FIXED_URL" "Zhao CBS" "zhao-cbs" true false
post_and_verify "$FIXED_URL" "Zhao CBS" "zhao-cbs" "$FIXTURE" false

# This request must be rejected by the fixed-route service, not silently routed.
http_code="$(curl -sS --max-time 20 -o "$TMPDIR_VERIFY/rejected.json" -w '%{http_code}' \
  -X POST "$FIXED_URL/optimize" -H 'Content-Type: application/json' \
  --data-binary "@$TMPDIR_VERIFY/no-routes.json")"
[[ "$http_code" == "422" ]] || {
  echo "FAIL: fixed-route service should reject missing routes with HTTP 422; got $http_code" >&2
  exit 1
}
python3 - "$TMPDIR_VERIFY/rejected.json" <<'PY'
import json
import sys
from pathlib import Path
body = json.loads(Path(sys.argv[1]).read_text())
assert body.get("detail", {}).get("code") == "CBS_ROUTE_PREPARATION_FAILED", body.get("detail")
print("PASS: fixed-route service rejects route generation")
PY

echo "Checking Zhao CBS + Routing on $ROUTING_URL..."
wait_for_health "$ROUTING_URL"
verify_identity_and_capabilities "$ROUTING_URL" "Zhao CBS + Routing" "zhao-cbs-routing" false true
post_and_verify "$ROUTING_URL" "Zhao CBS + Routing" "zhao-cbs-routing" "$TMPDIR_VERIFY/no-routes.json" true

echo "PASS: All service verification checks passed."
echo "Note: This does not validate Manager selection, shared response semantics, or CNC application."
