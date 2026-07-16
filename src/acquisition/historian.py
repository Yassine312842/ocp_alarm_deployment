"""
Layer 1 — Real historian connectors (STUBS).

When OCP's data becomes available, implement one of these against the same
`DataSource` interface and swap it in `scripts/generate_demo_data.py`.
Nothing else in the platform changes.

PI Web API sketch:
    GET {base}/streams/{webId}/recorded?startTime=...&endTime=...
    -> map each sample to ProcessSample; map the PI alarm/event frames to
       AlarmEvent.

OPC UA sketch (asyncua):
    client = Client(url); await client.connect()
    node = client.get_node(nodeid); history = await node.read_raw_history(...)
"""
from __future__ import annotations

from .base import AlarmEvent, DataSource, ProcessSample


class PIHistorianDataSource(DataSource):
    def __init__(self, base_url: str, web_ids: dict[str, str], start, end):
        self.base_url, self.web_ids, self.start, self.end = base_url, web_ids, start, end

    def process_samples(self) -> list[ProcessSample]:
        raise NotImplementedError("Implement PI Web API 'recorded' pull here.")

    def alarm_events(self) -> list[AlarmEvent]:
        raise NotImplementedError("Implement PI event-frame pull here.")


class OpcUaDataSource(DataSource):
    def __init__(self, endpoint: str, node_map: dict[str, str], start, end):
        self.endpoint, self.node_map, self.start, self.end = endpoint, node_map, start, end

    def process_samples(self) -> list[ProcessSample]:
        raise NotImplementedError("Implement asyncua read_raw_history here.")

    def alarm_events(self) -> list[AlarmEvent]:
        raise NotImplementedError("Implement OPC UA alarm/condition subscription here.")
