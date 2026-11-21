from .topology import (
    find_path,
    get_path_links,
    build_egress_ports
)

class CBSOptimizer:
    name = "CBS"

    def get_capabilities(self):
        return {
            "name": "CBS",
            "tasks": ["configuration", "reconfiguration"],
            "mechanisms": ["CBS"],        
            "objectives": [
                "latency",
                "bandwidth"
            ],
            "supportsIncremental": False,
            "supportsReconfiguration": True,
            "inputRequirements": {
                "requiresPcp": True,
                "acceptsClientTrafficClassIds": True, # zhao's CBS-specific adapter translates arbitrary client labels to M1..M7.
            }
        }
  

    # limitations: This is a targeted replacement for directed, bridge-based unicast paths such as TC1. 
    # current CBS implementation processes only the first listener; general multicast, direct end-station paths, and more complex routing policies require separate handling.
    def resolve_stream_path(self, stream, links, fixed_routes=None):
        talker = stream["tlkrPssblPlcmnt"][0]
        listener = stream["lstnsrPssblPlcmnt"][0][0]
        stream_id = stream["name"]

        # No fixed routing: optimizer determines the path.
        if fixed_routes is None or stream_id not in fixed_routes:
            path = find_path(links, talker, listener)

            if not path:
                raise ValueError(
                    f"No path found for {stream_id}: {talker} -> {listener}"
                )

            return path, get_path_links(links, path)

        configuration = fixed_routes[stream_id]

        # Contract v1.2: one explicit OpenCNC path.
        hops = configuration.get("path")

        # Contract v1.2: listener-specific paths.
        if hops is None:
            matches = [
                item for item in configuration.get("listenerPaths", [])
                if item["listenerId"] == listener
            ]
            if len(matches) != 1:
                raise ValueError(
                    f"Expected one fixed path for {stream_id} -> {listener}"
                )
            hops = matches[0]["path"]

        if not hops:
            raise ValueError(f"Empty fixed path for stream {stream_id}")

        hops = sorted(hops, key=lambda hop: hop["hopNumber"])

        # Resolve each directed physical link using node and port IDs.
        def find_link(source, source_port, destination, destination_port):
            matches = [
                link for link in links
                if link["sourceNode"] == source
                and link["sourcePort"] == source_port
                and link["destNode"] == destination
                and link["destPort"] == destination_port
            ]

            if len(matches) != 1:
                raise ValueError(
                    f"Expected one link: {source}/{source_port} -> "
                    f"{destination}/{destination_port}; found {len(matches)}"
                )

            return matches[0]

        # Source to first bridge.
        # The source egress port is determined by the topology.
        first = hops[0]
        source_links = [
            link for link in links
            if link["sourceNode"] == talker
            and link["destNode"] == first["bridgeId"]
            and link["destPort"] == first["ingressPortId"]
        ]

        if len(source_links) != 1:
            raise ValueError(
                f"Cannot uniquely resolve source link for {stream_id}"
            )

        path_links = [source_links[0]]

        # Bridge-to-bridge connections.
        for previous, current in zip(hops, hops[1:]):
            path_links.append(find_link(
                previous["bridgeId"],
                previous["egressPortId"],
                current["bridgeId"],
                current["ingressPortId"]
            ))

        # Last bridge to destination.
        last = hops[-1]
        destination_links = [
            link for link in links
            if link["sourceNode"] == last["bridgeId"]
            and link["sourcePort"] == last["egressPortId"]
            and link["destNode"] == listener
        ]

        if len(destination_links) != 1:
            raise ValueError(
                f"Cannot uniquely resolve destination link for {stream_id}"
            )

        path_links.append(destination_links[0])

        path = [talker] + [
            link["destNode"] for link in path_links
        ]

        if len(path) != len(set(path)):
            raise ValueError(f"Cyclic fixed route for {stream_id}")

        return path, path_links

    # builds the basic context for a stream: talker, listener, path, egress ports, SR class, and frame size
    def build_stream_topology_context(
        self,
        stream,
        links,
        fixed_routes=None
    ):
        talker = stream["tlkrPssblPlcmnt"][0]
        listener = stream["lstnsrPssblPlcmnt"][0][0]

        path, path_links = self.resolve_stream_path(
            stream,
            links,
            fixed_routes
        )

        egress_ports = build_egress_ports(
            path_links
        )

        return {
            "stream": stream["name"],
            "srClass": stream["srClass"],
            "maxFrameBits": (
                stream["maxFrameSize"] * 8
            ),
            "path": path,
            "egressPorts": egress_ports
        }

    # it groups streams which share the same egress port + SR class and aggregates their arrival rate, burst, deadlines, and upstream (predecessar) information
    def group_streams_by_port_class(self, stream_contexts):

        groups = {}

        for context in stream_contexts:

            for port in context["egressPorts"]:

                key = (
                    port["node"],
                    port["port"],
                    context["srClass"]
                )

                if key not in groups:
                    groups[key] = {
                        "node": port["node"],
                        "port": port["port"],
                        "srClass": context["srClass"],
                        "streams": [],
                        "aggregateRhoBps": 0,
                        "aggregateBurstBits": 0,
                        "maxFrameBits": 0,
                        "linkSpeedBps": port["linkSpeedBps"],
                        "streamDeadlines": [],
                        "beMaxFrameBits": 0,
                        "streamBursts": [],
                        "streamPositions": [],
                    }

                groups[key]["streams"].append(
                    context["stream"]
                )

                groups[key]["aggregateRhoBps"] += (
                    context["rhoBps"]
                )

                groups[key]["aggregateBurstBits"] += (
                    context["burstBits"]
                )

                groups[key]["maxFrameBits"] = max(
                    groups[key]["maxFrameBits"],
                    context["burstBits"]
                )

                groups[key]["streamDeadlines"].append({
                    "stream": context["stream"],
                    "deadlineNs": context["localDeadlinesNs"][port["h"]]
                })

                groups[key]["beMaxFrameBits"] = max(
                    groups[key]["beMaxFrameBits"],
                    context["beMaxFrameBits"]
                )

                groups[key]["streamBursts"].append({
                    "stream": context["stream"],
                    "burstBits": context["portBurstsBits"][port["h"]]
                })

                h = port["h"]

                predecessor = None

                if h > 0:
                    previous_port = context["egressPorts"][h - 1]

                    predecessor = {
                        "node": previous_port["node"],
                        "port": previous_port["port"],
                        "srClass": context["srClass"]
                    }

                groups[key]["streamPositions"].append({
                    "stream": context["stream"],
                    "h": h,
                    "predecessor": predecessor
                })

        for group in groups.values():

            group["requiredDeadlineNs"] = min(
                item["deadlineNs"]
                for item in group["streamDeadlines"]
            )

            group["aggregatePortBurstBits"] = sum(
                item["burstBits"]
                for item in group["streamBursts"]
            )

        return list(groups.values())

    # implements the current Th-1 / C1 + C2 logic at class/network level and calculates local deadlines for each relevant egress port
    def calculate_class_local_deadlines(
        self,
        stream_contexts,
        port_class_groups,
        sr_class
    ):
        # Streams and port groups belonging to this SR class
        class_streams = [
            context
            for context in stream_contexts
            if context["srClass"] == sr_class
        ]

        class_groups = [
            group
            for group in port_class_groups
            if group["srClass"] == sr_class
        ]

        if not class_groups:
            return {}

        context_map = {
            context["stream"]: context
            for context in class_streams
        }

        group_map = {
            (
                group["node"],
                group["port"],
                group["srClass"]
            ): group
            for group in class_groups
        }

        # Find source groups
        source_groups = []

        for group in class_groups:

            positions = group.get(
                "streamPositions",
                []
            )

            if positions and all(
                position.get("predecessor") is None
                for position in positions
            ):
                source_groups.append(group)

        if not source_groups:
            raise ValueError(
                f"No source egress port found for SR class {sr_class}"
            )

        # Deterministic benchmark source port (d0 in Theorem 1)
        benchmark_group = sorted(
            source_groups,
            key=lambda group: (
                group["node"],
                group["port"]
            )
        )[0]

        benchmark_burst_bits = (
            benchmark_group["aggregateBurstBits"]
        )

        benchmark_rho_per_ns = (
            benchmark_group["aggregateRhoBps"]
            / 1_000_000_000
        )

        if benchmark_rho_per_ns <= 0:
            raise ValueError(
                f"Invalid traffic rate for SR class {sr_class}"
            )

        # Calculate all port deadlines for a candidate d0
        def build_deadlines(d0_ns):

            deadlines = {}
            pending = set(group_map.keys())

            benchmark_key = (
                benchmark_group["node"],
                benchmark_group["port"],
                sr_class
            )

            deadlines[benchmark_key] = d0_ns
            pending.remove(benchmark_key)

            while pending:

                progress = False

                for key in list(pending):

                    group = group_map[key]

                    propagated_burst_bits = 0
                    dependencies_ready = True

                    for position in group["streamPositions"]:

                        stream_name = position["stream"]
                        context = context_map[stream_name]

                        burst_bits = context["burstBits"]
                        rho_per_ns = (
                            context["rhoBps"]
                            / 1_000_000_000
                        )

                        # All egress ports before this port
                        previous_delay_ns = 0

                        current_h = position["h"]

                        for previous_port in (
                            context["egressPorts"][:current_h]
                        ):

                            previous_key = (
                                previous_port["node"],
                                previous_port["port"],
                                sr_class
                            )

                            if previous_key not in deadlines:
                                dependencies_ready = False
                                break

                            previous_delay_ns += (
                                deadlines[previous_key]
                            )

                        if not dependencies_ready:
                            break

                        propagated_burst_bits += (
                            burst_bits
                            + rho_per_ns * previous_delay_ns
                        )

                    if not dependencies_ready:
                        continue

                    group_rho_per_ns = (
                        group["aggregateRhoBps"]
                        / 1_000_000_000
                    )

                    ratio = (
                        benchmark_burst_bits
                        / benchmark_rho_per_ns
                    ) * (
                        group_rho_per_ns
                        / propagated_burst_bits
                    )

                    deadlines[key] = (
                        d0_ns / ratio
                    )

                    pending.remove(key)
                    progress = True

                if not progress:
                    raise ValueError(
                        "Unable to resolve deadline dependencies. "
                        "Check stream paths for cycles or invalid topology."
                    )

            return deadlines

        # C2: every stream must satisfy its end-to-end deadline
        def satisfies_end_to_end(deadlines):

            for context in class_streams:

                total_delay_ns = 0

                for port in context["egressPorts"]:

                    key = (
                        port["node"],
                        port["port"],
                        sr_class
                    )

                    total_delay_ns += deadlines[key]

                if total_delay_ns > context["deadline"]:
                    return False

            return True

        # Find maximum feasible d0
        low = 0
        high = max(
            context["deadline"]
            for context in class_streams
        )

        for _ in range(100):

            mid = (low + high) / 2

            deadlines = build_deadlines(mid)

            if satisfies_end_to_end(deadlines):
                low = mid
            else:
                high = mid

        final_deadlines = build_deadlines(low)

        # Store deadline on each port/class group
        for key, deadline_ns in final_deadlines.items():
            group_map[key]["theorem1DeadlineNs"] = (
                deadline_ns
            )

        return final_deadlines

    # recalculates the burst seen at downstream ports using the delays assigned by Th-1.
    def update_group_bursts_from_theorem1(
        self,
        stream_contexts,
        port_class_groups
    ):

        context_map = {
            context["stream"]: context
            for context in stream_contexts
        }

        group_map = {
            (
                group["node"],
                group["port"],
                group["srClass"]
            ): group
            for group in port_class_groups
        }

        for group in port_class_groups:

            updated_stream_bursts = []

            for position in group["streamPositions"]:

                stream_name = position["stream"]
                context = context_map[stream_name]
                h = position["h"]

                rho_per_ns = (
                    context["rhoBps"]
                    / 1_000_000_000
                )

                previous_delay_ns = 0

                # Sum Theorem-1 deadlines before this port
                for previous_port in context["egressPorts"][:h]:

                    previous_key = (
                        previous_port["node"],
                        previous_port["port"],
                        context["srClass"]
                    )

                    previous_group = group_map[
                        previous_key
                    ]

                    previous_delay_ns += (
                        previous_group[
                            "theorem1DeadlineNs"
                        ]
                    )

                propagated_burst_bits = (
                    context["burstBits"]
                    + rho_per_ns * previous_delay_ns
                )

                updated_stream_bursts.append({
                    "stream": stream_name,
                    "rhoBps": context["rhoBps"],
                    "burstBits": propagated_burst_bits,
                    "maxFrameBits": context["frameSize"] * 8
                })

            group["streamBursts"] = (
                updated_stream_bursts
            )

            group["aggregatePortBurstBits"] = sum(
                item["burstBits"]
                for item in updated_stream_bursts
            )

        return port_class_groups

    # it uses all network streams
    # their actual paths
    # same egress port
    # lower-priority classes only
    # largest individual frame
    # compare with BE frame
    # blockingFrameBits

    # finds the largest possible lower-priority/BE frame that can cause non-preemptive blocking at each port.
    def calculate_blocking_frames(
        self,
        stream_contexts,
        port_class_groups,
        class_priority
    ):

        # Map all network streams to their actual egress ports
        streams_by_port = {}

        for context in stream_contexts:

            for port in context["egressPorts"]:

                key = (
                    port["node"],
                    port["port"]
                )

                streams_by_port.setdefault(
                    key,
                    []
                ).append(context)

        for group in port_class_groups:

            port_key = (
                group["node"],
                group["port"]
            )

            current_class = group["srClass"]

            current_priority = class_priority[
                current_class
            ]

            # BE traffic is always a possible blocker
            blocking_frame_bits = (
                group["beMaxFrameBits"]
            )

            # Check lower-priority SR traffic
            # using the same egress port
            for context in streams_by_port.get(
                port_key,
                []
            ):

                stream_class = context["srClass"]

                if stream_class not in class_priority:
                    continue

                stream_priority = class_priority[
                    stream_class
                ]

                if stream_priority > current_priority:

                    blocking_frame_bits = max(
                        blocking_frame_bits,
                        context["maxFrameBits"]
                    )

            group["blockingFrameBits"] = (
                blocking_frame_bits
            )

        return port_class_groups

      # it  now accepts any number of predecessor groups, not one fixed previous_group
   
    # builds the aggregate arrival-curve information for a port using traffic coming from all predecessor groups.
    def build_shared_m1_arrival_curve(
        self,
        current_group,
        predecessor_groups
    ):

        if not predecessor_groups:
            raise ValueError(
                f"No predecessor groups for "
                f"{current_group['node']}/{current_group['port']}"
            )

        # Stream data available at the current egress port
        stream_data = {
            item["stream"]: item
            for item in current_group["streamBursts"]
        }

        predecessor_curves = []

        for predecessor_group in predecessor_groups:

            predecessor_key = (
                predecessor_group["node"],
                predecessor_group["port"],
                predecessor_group["srClass"]
            )

            # Streams that actually travel from this predecessor
            # to the current egress port
            subset_streams = []

            for position in current_group["streamPositions"]:

                predecessor = position.get("predecessor")

                if predecessor is None:
                    continue

                position_predecessor_key = (
                    predecessor["node"],
                    predecessor["port"],
                    predecessor["srClass"]
                )

                if position_predecessor_key == predecessor_key:
                    subset_streams.append(
                        position["stream"]
                    )

            if not subset_streams:
                continue

            subset_rho_bps = sum(
                stream_data[stream]["rhoBps"]
                for stream in subset_streams
            )

            subset_burst_bits = sum(
                stream_data[stream]["burstBits"]
                for stream in subset_streams
            )

            subset_max_frame_bits = max(
                stream_data[stream]["maxFrameBits"]
                for stream in subset_streams
            )

            previous_cbs = predecessor_group["sharedCBS"]

            predecessor_curves.append({
                "predecessor": {
                    "node": predecessor_group["node"],
                    "port": predecessor_group["port"],
                    "srClass": predecessor_group["srClass"]
                },

                "streams": subset_streams,

                # Flow arrival bound
                "flowSlopeBps": subset_rho_bps,
                "flowInterceptBits": subset_burst_bits,

                # Link shaping bound
                "linkSlopeBps":
                    predecessor_group["linkSpeedBps"],
                "linkInterceptBits":
                    subset_max_frame_bits,

                # CBS shaping bound
                "cbsSlopeBps":
                    previous_cbs["idleSlopeBps"],
                "cbsInterceptBits": (
                    previous_cbs["creditMaxBits"]
                    - previous_cbs["creditMinBits"]
                    + subset_max_frame_bits
                )
            })

        if not predecessor_curves:
            raise ValueError(
                f"No valid predecessor traffic found for "
                f"{current_group['node']}/{current_group['port']}"
            )

        return {
            "predecessorCurves": predecessor_curves
        }
   
    # finds candidate breakpoints (x, y) on the arrival curve
    def find_xy_point(self, curve, idle_slope):

        # New multi-predecessor curve
        if "predecessorCurves" in curve:

            predecessor_curves = curve["predecessorCurves"]

            candidate_x_values = set()

            for predecessor_curve in predecessor_curves:

                lines = [
                    (
                        predecessor_curve["flowSlopeBps"],
                        predecessor_curve["flowInterceptBits"]
                    ),
                    (
                        predecessor_curve["linkSlopeBps"],
                        predecessor_curve["linkInterceptBits"]
                    ),
                    (
                        predecessor_curve["cbsSlopeBps"],
                        predecessor_curve["cbsInterceptBits"]
                    )
                ]

                for i in range(len(lines)):
                    for j in range(i + 1, len(lines)):

                        m1, b1 = lines[i]
                        m2, b2 = lines[j]

                        if m1 == m2:
                            continue

                        x_seconds = (
                            (b2 - b1) / (m1 - m2)
                        )

                        if x_seconds >= 0:
                            candidate_x_values.add(
                                x_seconds
                            )

            candidates = []

            for x_seconds in sorted(candidate_x_values):

                total_y_bits = 0

                for predecessor_curve in predecessor_curves:

                    values = [
                        (
                            predecessor_curve["flowSlopeBps"]
                            * x_seconds
                            + predecessor_curve["flowInterceptBits"]
                        ),
                        (
                            predecessor_curve["linkSlopeBps"]
                            * x_seconds
                            + predecessor_curve["linkInterceptBits"]
                        ),
                        (
                            predecessor_curve["cbsSlopeBps"]
                            * x_seconds
                            + predecessor_curve["cbsInterceptBits"]
                        )
                    ]

                    total_y_bits += min(values)

                candidates.append({
                    "xNs": x_seconds * 1_000_000_000,
                    "yBits": total_y_bits
                })

            return candidates

        # Original single three-line curve
        lines = [
            (
                curve["flowSlopeBps"],
                curve["flowInterceptBits"]
            ),
            (
                curve["linkSlopeBps"],
                curve["linkInterceptBits"]
            ),
            (
                curve["cbsSlopeBps"],
                curve["cbsInterceptBits"]
            )
        ]

        candidates = []

        for i in range(len(lines)):
            for j in range(i + 1, len(lines)):

                m1, b1 = lines[i]
                m2, b2 = lines[j]

                if m1 == m2:
                    continue

                x_seconds = (
                    (b2 - b1) / (m1 - m2)
                )

                if x_seconds >= 0:

                    y_bits = (
                        m1 * x_seconds + b1
                    )

                    candidates.append({
                        "xNs": (
                            x_seconds
                            * 1_000_000_000
                        ),
                        "yBits": y_bits
                    })

        return candidates
     
    # Theorem 1
    # 2 main conditions: C1 & C2 
    def calculate_local_deadlines(self, burst_bits, rho_bps, deadline_ns, port_count):

        # Convert rate from bits/sec to bits/ns
        rho_per_ns = rho_bps / 1_000_000_000

        def build_deadlines(d0):
            deadlines = [d0]

            for _ in range(1, port_count):
                previous_delay = sum(deadlines)

                dh = d0 * (
                    burst_bits + rho_per_ns * previous_delay
                ) / burst_bits

                deadlines.append(dh)

            return deadlines

        # Find maximum d0 satisfying end-to-end deadline
        low = 0
        high = deadline_ns

        for _ in range(100):
            mid = (low + high) / 2

            deadlines = build_deadlines(mid)

            if sum(deadlines) <= deadline_ns:
                low = mid
            else:
                high = mid

        return build_deadlines(low)
    
    def calculate_port_bursts(
        self,
        burst_bits,
        rho_bps, # arrival rate
        local_deadlines
    ):
        port_bursts = []
        previous_delay_ns = 0

        for deadline_ns in local_deadlines:

            burst_at_port = (
                burst_bits
                + rho_bps * (previous_delay_ns / 1_000_000_000)
            )

            port_bursts.append(burst_at_port)

            previous_delay_ns += deadline_ns

        return port_bursts


    def calculate_shared_stream_delays(
        self,
        stream_contexts,
        port_class_groups
    ):

        group_map = {
            (
                group["node"],
                group["port"],
                group["srClass"]
            ): group
            for group in port_class_groups
        }

        results = []

        for context in stream_contexts:

            total_delay_ns = 0
            port_delays = []

            for port in context["egressPorts"]:

                key = (
                    port["node"],
                    port["port"],
                    context["srClass"]
                )

                group = group_map[key]
                delay_ns = group["sharedDelayNs"]

                total_delay_ns += delay_ns

                port_delays.append({
                    "node": port["node"],
                    "port": port["port"],
                    "delayNs": delay_ns
                })

            results.append({
                "stream": context["stream"],
                "sharedPathDelayNs": total_delay_ns,
                "deadlineNs": context["deadline"],
                "deadlineSatisfied": (
                    total_delay_ns <= context["deadline"] + 1e-6
                ),
                "portDelays": port_delays
            })

        return results
    
 
    def optimize(self, usecase):
        """Compute CBS with one priority-ordered N-class algorithm.

        Supported analytical domain and fail-closed preconditions are checked
        by optimize_nclass_candidate. Manager still advertises M1 only until
        physical queue mapping and integration are independently checked.
        """
        print(
            "[CBS.py] Received stream classes:",
            [(s.get("name"), s.get("srClass")) for s in usecase.streams],
            "| Target classes:",
            getattr(usecase, "target_traffic_classes", None),
            flush=True,
        )
        from .cbs_nclass_candidate import optimize_nclass_candidate
        return optimize_nclass_candidate(self, usecase)
