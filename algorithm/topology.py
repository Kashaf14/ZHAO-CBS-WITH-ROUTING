from collections import deque


def build_egress_ports(path_links):

    egress_ports = []

    for h, link in enumerate(path_links):
        egress_ports.append({
            "h": h,
            "node": link["sourceNode"],
            "port": link["sourcePort"],
            "linkSpeedBps": link["linkBw"]
        })

    return egress_ports


def find_path(links, start, end):

    queue = [(start, [start])]
    visited = set()

    while queue:

        node, path = queue.pop(0)

        if node == end:
            return path

        if node in visited:
            continue

        visited.add(node)

        for link in links:

            if link["sourceNode"] == node:

                next_node = link["destNode"]

                if next_node not in visited:
                    queue.append(
                        (
                            next_node,
                            path + [next_node]
                        )
                    )

    return None


def get_path_links(links, path):

    path_links = []

    for i in range(len(path) - 1):

        source = path[i]
        destination = path[i + 1]

        for link in links:

            if (
                link["sourceNode"] == source
                and link["destNode"] == destination
            ):
                path_links.append(link)
                break

    return path_links