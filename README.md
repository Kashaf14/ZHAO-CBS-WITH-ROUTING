# Zhao CBS Optimizer — Standalone Service

Standalone Credit-Based Shaper (CBS) optimizer service for TSN experiments. The repository contains the Zhao-based analytical solver, CBS-specific request/response adapters, an HTTP interface, and regression tests. It does **not** contain or run the OaaS Manager or OpenCNC.

## Interfaces

The service in `cbs_service.py` exposes:

- `GET /health` — basic service readiness response.
- `GET /capabilities` — CBS optimizer capabilities.
- `POST /optimize` — submit an **OptimizerRequest v1.4** JSON object and receive an **OptimizerResponse v1.3** with sparse CBS configuration changes.

The response adapter also retains a v1.2 conversion helper for historical compatibility; the current HTTP service uses v1.3. The service does not itself prove shared-contract validity or live CNC integration.

## Scope and limitations

- The N-class solver models prioritized CBS traffic classes M1–M7 under its documented analytical assumptions.
- It handles directed, bridge-based unicast scenarios with one physical listener per stream; general multicast and arbitrary routing policies are not verified.
- Missing paths can be prepared using a deterministic shortest-hop directed route when the request explicitly asks for routing optimization. This is **not** joint routing/CBS mathematical optimization.
- Generated route changes require compatible response validation and application by the caller. The existing Phase-1 CNC path does not establish their live applicability.
- Numerical regression, same-model network-calculus cross-checks, and synthetic tests do not establish hardware correctness or end-to-end CNC integration.

See `algorithm/cbs_nclass_candidate.py`, `cbs_route_preparation.py`, and the tests for further boundaries.

## Dependencies

- Docker for the provided deployment script (its image uses Python 3.14.2).
- Hamza's external TSN library, including `usecase_generator.py`. This library is **not included** in this repository.
- Python packages from `requirements.txt`.
- Historical TC1 Step 17 tests read `tests/fixtures/cbs-paper-tc1-step17-original.json` (PCP 5 only), an unchanged local copy from `tsn-optimization-contracts` at commit `9664a6a09f8a38a65362d90dae95c20eb42d094e`. The frozen regression verifies its SHA-256 against the recorded reference.
- `tests/fixtures/cbs-paper-tc1-full-8flow-contract.json` is a separate extended fixture with explicit PCP 4-to-queue 4 mappings and experimental eight-queue priority tables (`schedulingPriority = 8 - queueId`, lower rank means higher priority) at five TC1 ports. This ordering matches OpenCNC's `ApplyCNCQueuePriorities` policy; it is not a hardware measurement. Do not substitute it for the historical Step 17 input.
- Both fixtures are local. Normal tests require no sibling contract checkout or `CBS_TC1_REQUEST` variable.

The standardized JSON contracts are maintained separately in [tsn-optimization-contracts](https://github.com/Kashaf14/tsn-optimization-contracts). The JSON files under `examples/` are legacy internal-format scenarios, **not** current v1.4 HTTP request examples.

## Deploy the standalone service

From this repository's root:

```bash
export TSN_LIBRARY_HOST_PATH="/absolute/path/to/TSN_optimizer_platform-Hamza"
chmod +x deploy-zhao-cbs-standalone.sh
./deploy-zhao-cbs-standalone.sh
```

The script builds `zhao-cbs-routing:local`, tests an isolated staging container on `127.0.0.1:18001`, and then starts `zhao-cbs-routing` on `127.0.0.1:8001` after successful checks. It preserves the previous container for rollback and mounts the external Hamza library read-only. It does not deploy or change the OaaS Manager.

Once deployed, you can inspect:

```bash
curl -fsS http://127.0.0.1:8001/health
curl -fsS http://127.0.0.1:8001/capabilities
```

## Run tests

For local Python tests, install `requirements.txt` and make Hamza's library importable:

```bash
export TSN_LIBRARY_PATH="/absolute/path/to/TSN_optimizer_platform-Hamza"
# TC1 fixtures are read directly from tests/fixtures/
python3 -m unittest discover -s tests -p 'test_cbs_nclass_regression.py' -v
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Run these tests outside time-sensitive measurement windows if CPU/memory use would interfere with experiments. These commands are instructions; passing results have **not** been established by this documentation update.

## Historical regression evidence

- `tests/regression/evidence/cbs_step17_pre_dispatch_baseline.json` is the **immutable** numerical reference captured before generic N-class dispatch. Do not modify or regenerate it.
- `tests/test_cbs_nclass_regression.py` compares current CBS results against that reference and checks its checksum.
- `scripts/cbs_legacy_baseline_capture.py` is retained to explain the historical capture procedure. **Do not execute it with the current solver to recreate the baseline.**
- `tests/regression/` also holds earlier contract-v1.2 and Manager-era evidence. These are **historical artifacts**, not claims that the current standalone service uses v1.2 or that the present Manager/CNC integration was retested.
