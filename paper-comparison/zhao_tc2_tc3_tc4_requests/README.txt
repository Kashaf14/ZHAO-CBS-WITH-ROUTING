Zhao TC2–TC4: baseline reproduction requests (38 individual JSON files)
Source: Zhao, Yan and Zhou, IEEE TII 2024, Section VII-B, Fig. 6, DOI 10.1109/TII.2023.3342466.
Format: shared OptimizerRequest v1.4, modeled after tests/request/valid/cbs-paper-tc1-full-8flow-contract.json in the shared repository.

IMPORTANT: run only against an isolated CBS benchmark instance with environment variable CBS_BENCHMARK_EGRESS_PORT=SW1/P5. The current backend implements this opt-in in cbs_service.py; it is NOT a request property. Ensure the Manager selects that instance. Do not enable this setting on a normal production/CNC integration instance. Restore the normal setting after benchmark runs.

All flows share the SW1/P5 output; distinct synthetic talkers avoid unrelated ingress bottlenecks. Physical endpoint IDs, PCP 5/4 to queues 5/4, and zero propagation delays are reconstruction choices, not paper-provided topology details. Link rate 100 Mbps, two SR classes, fixed routes, scheduling optimize. All requests use one frame per interval.

Wire-size mapping: Zhao gives 500 B payload = 43.36 us at 100 Mbps, corresponding to 542 bytes; all payloads therefore have 42 bytes added to maxFrameSize for the current CBS adapter, which reads that field directly. Overhead fields zero to avoid double counting. BE bound 1542 B is a reconstruction assumption supported by Cao's later dissertation (Sec. 9.4: 1500 B payload, 123.36 us). Zhao Sec. VII-B does not explicitly give the BE bound; do not treat it as confirmed for Zhao. TC1's 100-byte BE bound is not automatically applicable here.

TC2: 14 requests; 4 streams, period/deadline 200–1500 us by 100 us, 500 B payload.
TC3: 12 requests; 8 streams, payload 100–1200 B by 100 B, period/deadline 1000 us.
TC4: 12 requests; 8 streams, 300 B payload, period 1500 us, deadline 600–2800 us by 200 us.

Start with TC2 period 1000 us, TC3 payload 500 B, TC4 deadline 1800 us. Submit each JSON separately to the existing Manager. Save complete result and failed responses using the scenario ID. Compare SW1/P5 M1 and M2 IdleSlope (bps/100,000,000 * 100 for percent) to Zhao Fig. 6's idSlMin/NC series. Record optimizer ID/version and benchmark setting. Infeasibility is a result, not a successful configuration. These files are proposed inputs, not measured results or an exact reproduction claim. Do not change the algorithm until baseline results are reviewed. After a validated multi-frame extension, rerun identical inputs for regression; add separate N>1 cases for actual multi-frame validation.

Queue-priority correction: added explicit queuePriorityTable to every existing configured port, with queueId 0..7 and schedulingPriority 8..1 (lower is higher). M1 queue 5 rank 3; M2 queue 4 rank 4. No other JSON fields changed.
