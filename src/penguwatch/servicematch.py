"""Which configured streaming service carries a title (pure, no I/O).

Everything comes from the user's services (config.yaml `services:`, or the app's Services tab): a title rule
(`titles:`) first, then the network name against each service's name and `aliases:`. Only the ids of those
services can come out, so a title never points at a service the user does not have.

Order:
1. Title rule: the title equals one of a service's `titles` (case and surrounding spaces ignored).
2. The network equals a service's name or one of its aliases.
3. The network contains a name or alias as whole words ("FX on Hulu" contains "FX"; "Cinemax" does not contain
   "Max"). The longest such alias wins, so "BBC Two London" goes to the service listing "BBC Two" before one
   listing "BBC".
4. A name or alias contains the network as whole words ("Smithsonian" in "Smithsonian Channel"); the shortest
   such alias (the closest) wins.

Ties keep the order of the services list. Aliases shorter than two characters only match exactly.
"""

import re
from typing import Iterable, Optional

from penguwatch.models import ServiceConfig


def _norm(text: Optional[str]) -> str:
    return " ".join((text or "").split()).casefold()


def _contains_words(haystack: str, needle: str) -> bool:
    """`needle` appears in `haystack` and is not part of a longer word on either side."""
    return re.search(rf"(?<![0-9a-z]){re.escape(needle)}(?![0-9a-z])", haystack) is not None


def _names(service: ServiceConfig) -> list:
    return [n for n in dict.fromkeys(_norm(x) for x in [service.name, *service.aliases]) if n]


def match_service(services: Iterable[ServiceConfig], network: Optional[str], title: Optional[str] = None) -> Optional[str]:
    """The id of the service that carries `title` on `network`, or None when no service matches."""
    services = list(services)
    wanted_title = _norm(title)
    if wanted_title:
        for s in services:
            if any(_norm(t) == wanted_title for t in s.titles):
                return s.id

    net = _norm(network)
    if not net:
        return None
    for s in services:
        if net in _names(s):
            return s.id

    best_id, best_len = None, 0
    for s in services:
        for name in _names(s):
            if len(name) >= 2 and len(name) > best_len and _contains_words(net, name):
                best_id, best_len = s.id, len(name)
    if best_id:
        return best_id

    if len(net) < 2:
        return None
    for s in services:
        for name in _names(s):
            if (best_id is None or len(name) < best_len) and _contains_words(name, net):
                best_id, best_len = s.id, len(name)
    return best_id
