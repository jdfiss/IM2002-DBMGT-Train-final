"""
TransitFlow — Neo4j Graph Database Layer
=========================================
This module handles all queries to Neo4j.

GRAPH ROLE:
  - Model the dual transit network (city metro M1–M4 + national rail NR1–NR2)
  - Find fastest routes (Dijkstra by travel_time_min via APOC)
  - Find cheapest routes (Dijkstra by fare via APOC)
  - Find alternative routes avoiding a given station
  - Find cross-network interchange paths (metro → rail or rail → metro)
  - Show delay ripple: which stations are affected within N hops

STUDENT TASK
------------
Design your graph schema (node labels, relationship types, properties)
based on the data in train-mock-data/, seed it with skeleton/seed_neo4j.py,
then implement the query_ functions below.

Functions prefixed with `query_` are called by the agent (skeleton/agent.py).
"""

from __future__ import annotations

from neo4j import GraphDatabase

from skeleton.config import NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD


def _driver():
    """Return a Neo4j driver. Caller is responsible for closing."""
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


# ── Example ───────────────────────────────────────────────────────────────────
# The block below shows the query pattern: open a session, run Cypher, return data.

def example_count_nodes() -> int:
    """Example: count all nodes currently in the graph."""
    with _driver() as driver:
        with driver.session() as session:
            result = session.run("MATCH (n) RETURN count(n) AS total")
            return result.single()["total"]

# ─────────────────────────────────────────────────────────────────────────────


# ── FASTEST ROUTE (Dijkstra by travel_time_min) ───────────────────────────────

def _infer_network(station_id: str) -> str:
    return "metro" if station_id.startswith("MS") else "rail"


def _infer_label(station_id: str) -> str:
    return "MetroStation" if station_id.startswith("MS") else "NationalRailStation"


def _node_to_dict(node) -> dict:
    return {"station_id": node["station_id"], "name": node["name"]}


def _rel_to_dict(rel) -> dict:
    props = {"travel_time_min": rel["travel_time_min"]}
    if "line" in rel:
        props["line"] = rel["line"]
    return props


def query_shortest_route(
    origin_id: str,
    destination_id: str,
    network: str = "auto",
) -> dict:
    """
    Find the fastest path between two stations, minimising total travel time.
    Uses apoc.algo.dijkstra (APOC required; enabled in docker-compose.yml).

    Args:
        origin_id:       e.g. "MS01" or "NR01"
        destination_id:  e.g. "MS09" or "NR05"
        network:         "metro", "rail", or "auto" (inferred from IDs)

    Returns:
        dict with keys: found, origin_id, destination_id,
                        total_time_min, path (list of station dicts), legs
    """
    # Infer labels per station so a direct call with mismatched IDs still finds the node.
    start_label = _infer_label(origin_id)
    end_label   = _infer_label(destination_id)

    if network == "auto":
        # WHY: unlock all edge types when network is not explicitly constrained.
        # Dijkstra with full visibility finds the global optimum; pre-filtering by
        # inferred network would hide cross-network shortcuts and produce only
        # locally optimal results.
        rel_type = "METRO_LINK|RAIL_LINK|INTERCHANGE_TO"
    else:
        rel_type = "METRO_LINK" if network == "metro" else "RAIL_LINK"

    cypher = f"""
        MATCH (start:{start_label} {{station_id: $origin}}),
              (end:{end_label}   {{station_id: $dest}})
        CALL apoc.algo.dijkstra(start, end, '{rel_type}', 'travel_time_min')
        YIELD path, weight
        RETURN path, weight
    """
    with _driver() as driver:
        with driver.session() as session:
            record = session.run(cypher, origin=origin_id, dest=destination_id).single()
    if not record:
        return {"found": False, "origin_id": origin_id, "destination_id": destination_id,
                "total_time_min": None, "path": [], "legs": []}
    path = record["path"]
    return {
        "found": True,
        "origin_id": origin_id,
        "destination_id": destination_id,
        "total_time_min": record["weight"],
        "path": [_node_to_dict(n) for n in path.nodes],
        "legs": [_rel_to_dict(r) for r in path.relationships],
    }


# ── CHEAPEST ROUTE (Dijkstra by fare) ────────────────────────────────────────

def query_cheapest_route(
    origin_id: str,
    destination_id: str,
    network: str = "auto",
    fare_class: str = "standard",
) -> dict:
    """
    Find the cheapest path between two stations, minimising total estimated fare.
    Uses APOC Dijkstra weighted by cost_standard or cost_first stored on each edge,
    so fare_class visibly affects both the edge weights and the path selection.

    Args:
        origin_id:       e.g. "NR01"
        destination_id:  e.g. "NR05"
        network:         "metro", "rail", or "auto"
        fare_class:      "standard" or "first" (national rail only)

    Returns:
        dict with found, total_fare_usd, stops_travelled, path, legs
    """
    start_label = _infer_label(origin_id)
    end_label   = _infer_label(destination_id)
    # WHY cost is stored on edges at seed time: apoc.algo.dijkstra requires a static
    # property name and cannot evaluate expressions at query time, so fare-class weights
    # must be pre-computed and written to cost_standard / cost_first during seeding.
    weight_prop = "cost_first" if fare_class.lower() == "first" else "cost_standard"

    if network == "auto":
        # WHY: unlock all edge types when network is not explicitly constrained.
        # Dijkstra with full visibility finds the global optimum; pre-filtering by
        # inferred network would hide cross-network shortcuts and produce only
        # locally optimal results.
        rel_type = "METRO_LINK|RAIL_LINK|INTERCHANGE_TO"
    else:
        rel_type = "METRO_LINK" if network == "metro" else "RAIL_LINK"

    cypher = f"""
        MATCH (start:{start_label} {{station_id: $origin}}),
              (end:{end_label}   {{station_id: $dest}})
        CALL apoc.algo.dijkstra(start, end, '{rel_type}', '{weight_prop}')
        YIELD path, weight
        RETURN path, weight
    """
    with _driver() as driver:
        with driver.session() as session:
            record = session.run(cypher, origin=origin_id, dest=destination_id).single()
    if not record:
        return {"found": False, "origin_id": origin_id, "destination_id": destination_id,
                "total_fare_usd": None, "path": [], "legs": []}
    path  = record["path"]
    stops = len(list(path.relationships))
    return {
        "found": True,
        "origin_id": origin_id,
        "destination_id": destination_id,
        "fare_class": fare_class,
        "stops_travelled": stops,
        "total_fare_usd": round(record["weight"], 2),
        "path": [_node_to_dict(n) for n in path.nodes],
        "legs": [_rel_to_dict(r) for r in path.relationships],
    }


# ── ALTERNATIVE ROUTES (avoiding a station) ───────────────────────────────────

def query_alternative_routes(
    origin_id: str,
    destination_id: str,
    avoid_station_id: str,
    network: str = "auto",
    max_routes: int = 3,
) -> list[list[dict]]:
    """
    Find paths between two stations that avoid a specific intermediate station.
    Useful for routing around a delayed or closed station.

    Args:
        origin_id:         e.g. "NR01"
        destination_id:    e.g. "NR05"
        avoid_station_id:  e.g. "NR03"
        network:           "metro", "rail", or "auto"
        max_routes:        max number of alternatives to return

    Returns:
        List of routes, each route is a list of leg dicts
    """
    start_label = _infer_label(origin_id)
    end_label   = _infer_label(destination_id)

    if network == "auto":
        # WHY: unlock all edge types when network is not explicitly constrained.
        # Dijkstra with full visibility finds the global optimum; pre-filtering by
        # inferred network would hide cross-network shortcuts and produce only
        # locally optimal results.
        rel_type = "METRO_LINK|RAIL_LINK|INTERCHANGE_TO"
    else:
        rel_type = "METRO_LINK" if network == "metro" else "RAIL_LINK"

    cypher = f"""
        MATCH (start:{start_label} {{station_id: $origin}}),
              (end:{end_label}   {{station_id: $dest}})
        MATCH path = (start)-[:{rel_type}*..8]->(end)
        
        WHERE ALL(n IN nodes(path) WHERE n.station_id <> $avoid)
          AND size(nodes(path)) = size(apoc.coll.toSet(nodes(path)))
        WITH DISTINCT path,
             reduce(t = 0, r IN relationships(path) | t + r.travel_time_min) AS total_time
        ORDER BY total_time
        RETURN path, total_time
        LIMIT $max_routes
    """
    with _driver() as driver:
        with driver.session() as session:
            records = list(session.run(
                cypher, origin=origin_id, dest=destination_id,
                avoid=avoid_station_id, max_routes=max_routes,
            ))
    routes = []
    for rec in records:
        path = rec["path"]
        legs = []
        nodes = list(path.nodes)
        rels  = list(path.relationships)
        for i, rel in enumerate(rels):
            legs.append({
                "from": _node_to_dict(nodes[i]),
                "to":   _node_to_dict(nodes[i + 1]),
                **_rel_to_dict(rel),
            })
        routes.append(legs)
    return routes


# ── CROSS-NETWORK INTERCHANGE PATH ───────────────────────────────────────────

def query_interchange_path(origin_id: str, destination_id: str) -> dict:
    """
    Find a path between a metro station and a national rail station (or vice versa)
    crossing the network boundary via interchange relationships.

    Args:
        origin_id:       e.g. "MS03" (metro) or "NR05" (national rail)
        destination_id:  e.g. "NR05" (national rail) or "MS09" (metro)

    Returns:
        dict with found, stations list, interchange points, total_time_min
    """
    # WHY no label constraint on MATCH: interchange queries span both MetroStation and
    # NationalRailStation nodes, so omitting the label lets a single query find either
    # node type by station_id without duplicating the query for each direction.
    cypher = """
        MATCH (start {station_id: $origin}), (end {station_id: $dest})
        CALL apoc.algo.dijkstra(start, end, 'METRO_LINK|RAIL_LINK|INTERCHANGE_TO', 'travel_time_min')
        YIELD path, weight
        RETURN path, weight
    """
    with _driver() as driver:
        with driver.session() as session:
            record = session.run(cypher, origin=origin_id, dest=destination_id).single()
    if not record:
        return {"found": False, "origin_id": origin_id, "destination_id": destination_id,
                "total_time_min": None, "path": [], "interchange_points": []}
    path = record["path"]
    nodes = list(path.nodes)
    rels  = list(path.relationships)
    interchange_points = [
        _node_to_dict(nodes[i + 1])
        for i, rel in enumerate(rels)
        if rel.type == "INTERCHANGE_TO"
    ]
    return {
        "found": True,
        "origin_id": origin_id,
        "destination_id": destination_id,
        "total_time_min": record["weight"],
        "path": [_node_to_dict(n) for n in nodes],
        "interchange_points": interchange_points,
    }


# ── DELAY RIPPLE ANALYSIS ─────────────────────────────────────────────────────

def query_delay_ripple(delayed_station_id: str, hops: int = 2) -> list[dict]:
    """
    Find all stations within N hops of a delayed or disrupted station.
    Works on both metro and national rail networks.

    Args:
        delayed_station_id: e.g. "NR03" or "MS01"
        hops:               how many connections out to search (default 2)

    Returns:
        List of dicts: {station_id, name, hops_away, lines_affected}
    """
    # hops=0 means "show only the disrupted station itself" — no traversal needed.
    if hops == 0:
        cypher = "MATCH (s {station_id: $sid}) RETURN s.station_id AS station_id, s.name AS name, s.lines AS lines_affected"
        with _driver() as driver:
            with driver.session() as session:
                record = session.run(cypher, sid=delayed_station_id).single()
        if not record:
            return []
        return [{"station_id": record["station_id"], "name": record["name"],
                 "hops_away": 0, "lines_affected": record["lines_affected"]}]

    # Neo4j does not accept parameters in relationship range literals (*1..$hops),
    # so the hop count is injected directly into the query string.
    # INTERCHANGE_TO included so delays at hub stations ripple across network boundaries.
    cypher = f"""
        MATCH path = ({{station_id: $sid}})-[:METRO_LINK|RAIL_LINK|INTERCHANGE_TO*1..{hops}]-(affected)
        WHERE affected.station_id <> $sid
        WITH affected, min(length(path)) AS hops_away
        RETURN affected.station_id AS station_id,
               affected.name       AS name,
               affected.lines      AS lines_affected,
               hops_away
        ORDER BY hops_away, station_id
    """
    with _driver() as driver:
        with driver.session() as session:
            records = list(session.run(cypher, sid=delayed_station_id))
    return [
        {
            "station_id":    r["station_id"],
            "name":          r["name"],
            "hops_away":     r["hops_away"],
            "lines_affected": r["lines_affected"],
        }
        for r in records
    ]


# ── STATION CONNECTIONS ───────────────────────────────────────────────────────

def query_station_connections(station_id: str) -> list[dict]:
    """
    List all direct connections from a given station.

    Args:
        station_id: e.g. "MS01" or "NR01"
    """
    cypher = """
        MATCH ({station_id: $sid})-[r:METRO_LINK|RAIL_LINK|INTERCHANGE_TO]->(neighbor)
        RETURN neighbor.station_id AS station_id,
               neighbor.name       AS name,
               type(r)             AS rel_type,
               r.travel_time_min   AS travel_time_min,
               r.line              AS line
        ORDER BY rel_type, travel_time_min
    """
    with _driver() as driver:
        with driver.session() as session:
            records = list(session.run(cypher, sid=station_id))
    return [
        {
            "station_id":     r["station_id"],
            "name":           r["name"],
            "connection_type": r["rel_type"],
            "travel_time_min": r["travel_time_min"],
            "line":            r["line"],
        }
        for r in records
    ]
