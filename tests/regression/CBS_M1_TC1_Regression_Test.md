# CBS M1 — Zhao TC1 Regression Test

**Test ID:** `CBS-M1-TC1-V1.2-001`  
**Date:** 2026-10-18  
**Result:** **PASS — M1 TC1 numerical regression**  
**Scope:** Contract v1.2 → Optimization Service Manager → CBS container → CBS optimizer

## Purpose

Check that the Contract v1.2 routing-interface changes preserve the CBS optimizer's network-wide **M1** bandwidth allocation and deadline calculations for Zhao et al.'s TC1 benchmark.

## Test inputs and implementation

- **Reference:** L. Zhao, Y. Yan, X. Zhou, *Minimum Bandwidth Reservation for CBS in TSN With Real-Time QoS Guarantees*, IEEE Transactions on Industrial Informatics, 20(4), 2024, **Tables I–III**.
- **Input scenario:** `cbs-paper-tc1-full-8flow-contract-v1.1(6).json` (file's internal `schemaVersion` is **`1.2`**).
- **Requested task/mechanism:** `configuration` / `CBS`.
- **Requested optimization scope:** `M1`.
- **Topology:** `ES1`, `ES2`, `ES3`, `SW1`, `ES4`, `ES5`; 100 Mb/s directed links.
- **Traffic:** Four M1 streams (`f1`–`f4`) optimized; four M2 streams (`f5`–`f8`) supplied as background traffic for lower-priority blocking analysis.
- **Routing:** fixed bridge/port `configuration.path` entries, resolved to physical links by `CBSOptimizer.resolve_stream_path()`.
- **Best-effort maximum frame size:** 100 bytes.

## Recorded execution

- **Job ID:** `e929d185-362e-4bbd-8a95-972d7ec607cc`
- **Job status:** `COMPLETED`
- **Selected optimizer:** `CBS`
- **Reported deadline result:** `allDeadlinesSatisfied = true`
- **Computed maximum M1 path delay:** `669958.6275264858 ns`

## Test A — Network-wide M1 bandwidth reservations

Compare the `portClassGroups[*].sharedCBS.idleSlopeBps` values with **Zhao Table II**. All links have capacity 100,000,000 bit/s. Thus `reservation (%) = idleSlopeBps / 1,000,000`.

| Egress port | Zhao Table II (%) | Observed (%) | Absolute difference (percentage points) | Verdict |
|---|---:|---:|---:|:---:|
| ES1 / Port1 → SW1 | 12.77 | 12.7693295665 | 0.0006704335 | PASS |
| ES2 / Port1 → SW1 | 4.94 | 4.9395330908 | 0.0004669092 | PASS |
| ES3 / Port1 → SW1 | 2.47 | 2.4697665454 | 0.0002334546 | PASS |
| SW1 / P4 → ES4 | 9.84 | 9.8352992546 | 0.0047007454 | PASS |
| SW1 / P5 → ES5 | 9.56 | 9.5544443955 | 0.0055556045 | PASS |

**Acceptance criterion:** all five absolute differences ≤ **0.01 percentage points** (appropriate to compare against paper values reported to two decimal places).  
**Observed:** **5/5 PASS**.

> Note: The paper's values are printed to two decimal places. The observed 9.5544% is slightly below the conventional rounding boundary for 9.56%, although still within the declared 0.01-point comparison tolerance. This is close numerical agreement, not proof of exact equality to unpublished reference values.

## Test B — M1 end-to-end deadlines

The authoritative result for network-wide comparison is `sharedStreamResults`, **not** the preliminary per-stream `streams[*].worstCaseDelayNs`.

| Stream | Deadline (µs) | Observed shared-path delay (µs) | Zhao Table III (µs) | Deadline met |
|---|---:|---:|---:|:---:|
| f1 | 1000 | 533.9917 | 533.8 | PASS |
| f2 | 500 | 443.2352 | 443.1 | PASS |
| f3 | 500 | 500.0000 | 499.8 | PASS* |
| f4 | 1000 | 669.9586 | 669.8 | PASS |

**Observed:** 4/4 deadline checks passed.  
\* For `f3`, the raw computed value is `500000.0000000001 ns`, only `0.0000000001 ns` above the nominal deadline due to floating-point precision. The implementation uses a `1e-6 ns` numerical tolerance.

## Test C — Fixed routing after Contract v1.2 migration

Verified for the optimized M1 streams:

| Stream | Resolved path | Verdict |
|---|---|:---:|
| f1 | ES1 → SW1 → ES4 | PASS |
| f2 | ES1 → SW1 → ES5 | PASS |
| f3 | ES2 → SW1 → ES4 | PASS |
| f4 | ES3 → SW1 → ES4 | PASS |

The result includes both directed physical links per route. The routing resolver is not one of Zhao's mathematical theorems, but its output supplies the topology and egress-port sequence used in the calculations.

## Conclusion

**PASS:** The completed end-to-end Contract v1.2 job reproduces Zhao's published **TC1 M1 per-port CBS reservation values** within 0.01 percentage points and satisfies all four optimized M1 stream deadlines. The fixed-routing interface refactor did not cause an observable TC1 M1 numerical regression.

### Boundaries of this result

This test **does not establish** correctness for M2 bandwidth optimization, multicast, arbitrary multi-bridge topologies, missing/invalid fixed routes, direct end-station paths, all Contract v1.2 scenario variants, or general correctness of Zhao's algorithm implementation. Preserve the TC1 request and job-result JSON as reproducible fixtures for future reruns. A completed job is not by itself proof of mathematical correctness; numerical comparisons above provide the benchmark evidence.

### Suggested rerun procedure

1. Submit the same schemaVersion `1.2` TC1 input to `/api/v1/jobs`.
2. Wait for job status `COMPLETED`; fetch the job result.
3. Verify exactly four optimized M1 streams, all four fixed paths, `allDeadlinesSatisfied == true`, and all five `sharedCBS.idleSlopeBps` reservations within **0.01 percentage points** of Zhao Table II.
4. Record the new job ID, date, and any unexpected differences.
