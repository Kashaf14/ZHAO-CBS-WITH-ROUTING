#!/usr/bin/env bash
set -Eeuo pipefail

# Save this script as containers/cbs/redeploy-real-cbs.sh in TSN-OPTIMIZER-FRAMEWORK.
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HAMZA_LIB="${TSN_LIBRARY_HOST_PATH:-/Users/kashaf/Desktop/THESIS/TSN_optimizer_platform-Hamza}"
IMAGE="real-cbs:local"
CONTAINER="real-cbs"

if [[ ! -f "$PROJECT_ROOT/containers/cbs/Dockerfile" ]]; then
  echo "Error: CBS Dockerfile not found under $PROJECT_ROOT" >&2
  exit 1
fi
if [[ ! -f "$HAMZA_LIB/usecase_generator.py" ]]; then
  echo "Error: usecase_generator.py not found in $HAMZA_LIB" >&2
  exit 1
fi
docker info >/dev/null

cd "$PROJECT_ROOT"
docker build -f containers/cbs/Dockerfile -t "$IMAGE" .

if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
  docker rm -f "$CONTAINER"
fi

docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  -p 127.0.0.1:8001:8001 \
  --mount "type=bind,source=$HAMZA_LIB,target=/opt/hamza,readonly" \
  -e TSN_LIBRARY_PATH=/opt/hamza \
  "$IMAGE"

for attempt in {1..30}; do
  if curl --fail --silent --show-error --max-time 2 http://127.0.0.1:8001/health; then
    echo
    curl --fail --silent --show-error http://127.0.0.1:8001/capabilities | python3 -m json.tool
    echo "Real CBS redeployed successfully."
    exit 0
  fi
  sleep 2
done

echo "Real CBS did not become healthy. Container logs:" >&2
docker logs "$CONTAINER" --tail 100 >&2
exit 1
