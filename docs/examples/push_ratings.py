#!/usr/bin/env python3
"""Push ratings from a CSV file into a PenguWatch rating source.

    PENGUWATCH_URL=http://localhost:8080 PENGUWATCH_TOKEN=<your token> \
        python push_ratings.py letterboxd my-ratings.csv

The CSV has a header row with `title` and `rating`, and optionally `year`, `imdb_id`, `tmdb_id` and `votes`:

    title,year,rating
    Heat,1995,4.5
    Alien,1979,5

A title is matched by its IMDb or TMDB id when the file has one, else by its name and year. The rating source is
created on the first run (a 0 to 5 scale, shown on the cards); anything that did not match is printed at the end.
Needs only `httpx` (pip install httpx).
"""

import csv
import os
import sys
from typing import Any, Dict, Iterable, Iterator, List, Optional

import httpx

BATCH = 500  # the API takes up to 1000 per request


def make_client(url: str, token: str) -> httpx.Client:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(base_url=url.rstrip("/"), headers=headers, timeout=60)


def ensure_source(client: httpx.Client, source: str, name: Optional[str] = None) -> None:
    """Creates the rating source unless it exists (a source defined in config.yaml is simply found)."""
    known = client.get("/api/rating-sources")
    known.raise_for_status()
    if any(s["id"] == source for s in known.json()):
        return
    made = client.post("/api/rating-sources", json={
        "id": source, "name": name or source.title(), "label": source[:3].upper(),
        "scale_min": 0, "scale_max": 5, "decimals": 1, "show_on_cards": True,
    })
    made.raise_for_status()


def read_items(rows: Iterable[Dict[str, str]]) -> Iterator[Dict[str, Any]]:
    """CSV rows -> import items: {"match": {...}, "value": 4.5, ...}."""
    for row in rows:
        if row.get("imdb_id"):
            match: Dict[str, Any] = {"imdb": row["imdb_id"].strip()}
        elif row.get("tmdb_id"):
            match = {"tmdb": int(row["tmdb_id"])}
        else:
            match = {"title": row["title"].strip()}
            if row.get("year"):
                match["year"] = int(row["year"])
        item: Dict[str, Any] = {"match": match, "value": float(row["rating"])}
        if row.get("votes"):
            item["votes"] = int(row["votes"])
        yield item


def push(client: httpx.Client, source: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Sends the items in batches and adds up the answers."""
    total: Dict[str, Any] = {"updated": 0, "unchanged": 0, "unmatched": [], "ambiguous": []}
    for start in range(0, len(items), BATCH):
        answer = client.put(f"/api/ratings/{source}", json={"items": items[start:start + BATCH]})
        if answer.status_code == 422:  # a value outside the scale: the whole batch was refused
            sys.exit(f"Refused: {answer.json()['detail']}")
        answer.raise_for_status()
        part = answer.json()
        total["updated"] += part["updated"]
        total["unchanged"] += part["unchanged"]
        total["unmatched"] += [items[start + u["index"]]["match"] for u in part["unmatched"]]
        total["ambiguous"] += [items[start + a["index"]]["match"] for a in part["ambiguous"]]
    return total


def main(argv: List[str], environ=os.environ, client: Optional[httpx.Client] = None) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    source, path = argv[1], argv[2]
    client = client or make_client(environ.get("PENGUWATCH_URL", "http://localhost:8080"), environ.get("PENGUWATCH_TOKEN", ""))
    with open(path, newline="", encoding="utf-8") as f:
        items = list(read_items(csv.DictReader(f)))
    ensure_source(client, source)
    result = push(client, source, items)
    print(f"{result['updated']} updated, {result['unchanged']} unchanged, "
          f"{len(result['unmatched'])} not found, {len(result['ambiguous'])} ambiguous")
    for label, matches in (("not found", result["unmatched"]), ("ambiguous (add a year or an id)", result["ambiguous"])):
        for match in matches:
            print(f"  {label}: {match}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
