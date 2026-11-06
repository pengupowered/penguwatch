"""Links from the page to PenguWatch's telemetry in Grafana: its dashboard, its logs (Loki) and its traces (Tempo).

PENGUWATCH_GRAFANA_URL is Grafana's base URL (e.g. https://grafana.example:3000); empty means no links. It must be
a plain http(s) base URL, without query, fragment or credentials: anything else stops the start with an error. The
Grafana dashboard is expected under the uid "penguwatch".
"""

import json
from typing import Dict, List
from urllib.parse import quote

from penguwatch.config import plain_base_url
from penguwatch.telemetry import SERVICE_NAME

DASHBOARD_UID = "penguwatch"
_links: List[Dict[str, str]] = []


def _explore(grafana: str, datasource: str, query: dict, since: str = "now-24h") -> str:
    """A Grafana Explore link (the `panes` URL format of Grafana 10 and later) running one query."""
    pane = {"wt": {"datasource": datasource,
                   "queries": [{"refId": "A", "datasource": {"type": datasource, "uid": datasource}, **query}],
                   "range": {"from": since, "to": "now"}}}
    return f"{grafana}/explore?schemaVersion=1&orgId=1&panes={quote(json.dumps(pane, separators=(',', ':')), safe='')}"


def telemetry_links(grafana: str) -> List[Dict[str, str]]:
    """The header links for a Grafana base URL; none for an empty one."""
    if not grafana:
        return []
    return [
        {"label": "Dashboard", "title": "Grafana: PenguWatch dashboard",
         "url": f"{grafana}/d/{DASHBOARD_UID}/{SERVICE_NAME}"},
        {"label": "Logs", "title": "Grafana Explore: PenguWatch log (Loki)",
         "url": _explore(grafana, "loki", {"expr": f'{{service_name="{SERVICE_NAME}"}}', "queryType": "range"})},
        {"label": "Traces", "title": "Grafana Explore: PenguWatch traces (Tempo)",
         "url": _explore(grafana, "tempo", {"queryType": "traceql", "tableType": "traces", "limit": 50,
                                            "query": f'{{resource.service.name="{SERVICE_NAME}"}}'})},
    ]


def configure(url: str) -> List[Dict[str, str]]:
    """Keeps the links for Grafana's base URL (EnvOverrides.grafana_url) for `links()`. Raises ValueError for a URL
    that is not a plain http(s) base URL."""
    _links[:] = telemetry_links(plain_base_url(url))
    return list(_links)


def links() -> List[Dict[str, str]]:
    return list(_links)
