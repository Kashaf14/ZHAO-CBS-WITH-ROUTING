
#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

IMAGE="zhao-cbs:local"
CONTAINER="zhao-cbs"
HOST_PORT=8003
CONTAINER_PORT=8001
HAMZA_LIB="${TSN_LIBRARY_HOST_PATH:-}"
SMOKE_REQUEST="${SMOKE_REQUEST:-/tmp/zhao-cbs-smoke-request.json}"

if [[ -z "$HAMZA_LIB" || ! -f "$HAMZA_LIB/usecase_generator.py" ]]; then
  echo "Error: Set TSN_LIBRARY_HOST_PATH correctly" >&2
  exit 1
fi

if [[ ! -f "$SMOKE_REQUEST" ]]; then
  echo "Error: Smoke-test request not found: $SMOKE_REQUEST" >&2
  exit 1
fi

if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "Error: Container '$CONTAINER' already exists." >&2
  echo "Existing container was not modified." >&2
  exit 1
fi

EXPECTED_NAME="$(CBS_VARIANT=fixed python3 -c 'from optimizer_metadata import NAME; print(NAME)')"
EXPECTED_ID="$(CBS_VARIANT=fixed python3 -c 'from optimizer_metadata import OPTIMIZER_ID; print(OPTIMIZER_ID)')"
export EXPECTED_NAME EXPECTED_ID

echo "Building Zhao CBS image..."
docker build -t "$IMAGE" .

echo "Starting Zhao CBS without route generation..."
docker run -d \
  --name "$CONTAINER" \
  -p "127.0.0.1:${HOST_PORT}:${CONTAINER_PORT}" \
  --mount "type=bind,source=${HAMZA_LIB},target=/opt/hamza,readonly" \
  -e TSN_LIBRARY_PATH=/opt/hamza \
  -e CBS_VARIANT=fixed \
  "$IMAGE"

echo "Checking service health..."
healthy=false

for attempt in {1..30}; do
  if curl -fsS --max-time 2 \
    "http://127.0.0.1:${HOST_PORT}/health" >/dev/null 2>&1; then
    healthy=true
    break
  fi
  sleep 2
done

if [[ "$healthy" != true ]]; then
  echo "Health check failed. Container retained for inspection." >&2
  docker logs "$CONTAINER" --tail 40 >&2
  exit 1
fi

echo "Verifying capabilities..."
curl -fsS "http://127.0.0.1:${HOST_PORT}/capabilities" |
  python3 -c '
import json, os, sys
caps = json.load(sys.stdin)
assert caps["name"] == os.environ["EXPECTED_NAME"], caps
assert caps["mechanisms"] == ["CBS"], caps
assert caps["inputRequirements"]["requiresRoutes"] is True, caps
print("Capabilities verified:", caps["name"])
'

echo "Testing fixed-route optimization..."
RESULT_FILE="$(mktemp)"
trap 'rm -f "$RESULT_FILE"' EXIT

curl -fsS \
  -X POST "http://127.0.0.1:${HOST_PORT}/optimize" \
  -H "Content-Type: application/json" \
  --data-binary @"$SMOKE_REQUEST" \
  -o "$RESULT_FILE"

python3 - "$RESULT_FILE" <<'PY'
import json
import os
import sys

with open(sys.argv[1]) as f:
    result = json.load(f)

assert result["schemaVersion"] == "1.3", result
assert result["status"] == "success", result
assert result["optimizer"]["id"] == os.environ["EXPECTED_ID"], result
assert result["optimizer"]["name"] == os.environ["EXPECTED_NAME"], result

print("Optimization smoke test: PASSED")
PY

echo "Zhao CBS deployed successfully on port ${HOST_PORT}."
