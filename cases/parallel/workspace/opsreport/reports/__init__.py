from . import audit, incidents, latency, reliability, throughput, traffic


REGISTRY = {
    "latency": latency.generate,
    "reliability": reliability.generate,
    "throughput": throughput.generate,
    "incidents": incidents.generate,
    "traffic": traffic.generate,
    "audit": audit.generate,
}
