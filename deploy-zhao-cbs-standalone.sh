
#!/usr/bin/env bash
set -Eeuo pipefail

# Standalone Zhao CBS deployment.
# Tests new image before replacing the running container.
# Does not modify real-cbs or other services.

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HAMZA_LIB="${TSN_LIBRARY_HOST_PATH:-}"

IMAGE="zhao-cbs-routing:local"
CONTAINER="zhao-cbs-routing"
STAGE="zhao-cbs-routing-stage"
PORT=8001
STAGE_PORT=18001

SMOKE_REQUEST="${SMOKE_REQUEST:-/tmp/zhao-cbs-smoke-request.json}"
EXPECTED_NAME="Zhao CBS + Routing"

if [[ ! -f "$PROJECT_ROOT/Dockerfile" ]]; then
  echo "Error: Dockerfile not found" >&2
  exit 1
fi

if [[ -z "$HAMZA_LIB" || ! -f "$HAMZA_LIB/usecase_generator.py" ]]; then
  echo "Error: Set TSN_LIBRARY_HOST_PATH correctly" >&2
  exit 1
fi

if [[ ! -f "$SMOKE_REQUEST" ]]; then
  echo "Error: Smoke-test request not found: $SMOKE_REQUEST" >&2
  exit 1
fi

docker info >/dev/null

if docker container inspect "$STAGE" >/dev/null 2>&1; then
  echo "Error: Staging container already exists: $STAGE" >&2
  exit 1
fi

BACKUP=""
CUTOVER=0
RESULT_FILE="$(mktemp)"

cleanup() {
  local rc=$?
  trap - EXIT

  docker rm -f "$STAGE" >/dev/null 2>&1 || true
  rm -f "$RESULT_FILE"

  if (( rc != 0 )); then
    echo "Deployment failed." >&2

    if (( CUTOVER == 1 )); then
      docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    fi

    if [[ -n "$BACKUP" ]]; then
      echo "Attempting to restore previous CBS container..." >&2
      if docker rename "$BACKUP" "$CONTAINER"; then
        if [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" != "true" ]]; then
          docker start "$CONTAINER" || true
        fi
      else
        echo "Automatic rollback failed; check container $BACKUP" >&2
      fi
    fi
  fi
}

trap cleanup EXIT

wait_healthy() {
  local port="$1"

  for attempt in {1..30}; do
    if curl -fsS --max-time 2 \
      "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done

  echo "Health check failed on port $port" >&2
  return 1
}

verify_capabilities() {
  local port="$1"

  curl -fsS "http://127.0.0.1:${port}/capabilities" |
    python3 -c '
import json, sys
caps = json.load(sys.stdin)
assert caps["name"] == "Zhao CBS + Routing", caps
assert caps["mechanisms"] == ["CBS"], caps
assert caps["inputRequirements"]["requiresRoutes"] is False, caps
print("Capabilities verified:", caps["name"])
'
}

smoke_test() {
  local port="$1"

  curl -fsS \
    -X POST "http://127.0.0.1:${port}/optimize" \
    -H "Content-Type: application/json" \
    --data-binary @"$SMOKE_REQUEST" \
    -o "$RESULT_FILE"

  python3 - "$RESULT_FILE" <<'PY'
import json
import sys

with open(sys.argv[1]) as f:
    result = json.load(f)

assert result["schemaVersion"] == "1.3", result
assert result["status"] == "success", result
assert result["optimizer"]["name"] == "Zhao CBS + Routing", result

print("Optimization smoke test: PASSED")
PY
}

cd "$PROJECT_ROOT"

echo "Building latest CBS image..."
docker build -f Dockerfile -t "$IMAGE" .

echo "Starting isolated staging container..."
docker run -d \
  --name "$STAGE" \
  -p "127.0.0.1:${STAGE_PORT}:${PORT}" \
  --mount "type=bind,source=${HAMZA_LIB},target=/opt/hamza,readonly" \
  -e TSN_LIBRARY_PATH=/opt/hamza \
  "$IMAGE"

wait_healthy "$STAGE_PORT"
verify_capabilities "$STAGE_PORT"
smoke_test "$STAGE_PORT"

echo "Staging tests passed."

# Remove staging container before production cutover.
docker rm -f "$STAGE" >/dev/null

# Preserve the existing deployment for rollback.
if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
  BACKUP="${CONTAINER}-rollback-$(date +%Y%m%d%H%M%S)"

  docker rename "$CONTAINER" "$BACKUP"
  docker stop "$BACKUP"
fi

CUTOVER=1

echo "Starting new CBS container on port $PORT..."
docker run -d \
  --name "$CONTAINER" \
  -p "127.0.0.1:${PORT}:${PORT}" \
  --mount "type=bind,source=${HAMZA_LIB},target=/opt/hamza,readonly" \
  -e TSN_LIBRARY_PATH=/opt/hamza \
  "$IMAGE"

wait_healthy "$PORT"
verify_capabilities "$PORT"
smoke_test "$PORT"

echo "Deployment successful."
echo "CBS available at http://127.0.0.1:${PORT}"

if [[ -n "$BACKUP" ]]; then
  echo "Previous container preserved as: $BACKUP"
fi
