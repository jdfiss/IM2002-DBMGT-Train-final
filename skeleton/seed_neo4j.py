"""
TransitFlow — Neo4j Seeder
Run once after starting Docker:
    python skeleton/seed_neo4j.py

Loads station and network data from train-mock-data/:
  - metro_stations.json         — city metro stations and adjacencies
  - national_rail_stations.json — national rail stations and adjacencies

Design your graph schema (node labels, relationship types, properties)
based on the data in these files, then implement the seed() function below.
"""

import json
import os
import sys

sys.path.insert(0, ".")

from neo4j import GraphDatabase
from skeleton.config import NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD

_DATA_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "train-mock-data")
)


def _load(filename):
    with open(os.path.join(_DATA_DIR, filename), encoding="utf-8") as f:
        return json.load(f)


def seed():
    metro_stations = _load("metro_stations.json")
    rail_stations  = _load("national_rail_stations.json")

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with driver.session() as session:

        session.run("MATCH (n) DETACH DELETE n")
        print("  Cleared existing graph data")

        # Unique constraints on station_id: enables index-backed MERGE (O(log N) instead
        # of full-label scan), and enforces node identity at the database level.
        session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (m:MetroStation) REQUIRE m.station_id IS UNIQUE")
        session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (r:NationalRailStation) REQUIRE r.station_id IS UNIQUE")
        print("  Unique constraints ensured")

        # MetroStation nodes
        for s in metro_stations:
            session.run(
                "MERGE (m:MetroStation {station_id: $sid}) SET m.name = $name, m.lines = $lines",
                sid=s["station_id"], name=s["name"], lines=s["lines"],
            )
        print(f"  MetroStation nodes: {len(metro_stations)}")

        # NationalRailStation nodes
        for s in rail_stations:
            session.run(
                "MERGE (r:NationalRailStation {station_id: $sid}) SET r.name = $name, r.lines = $lines",
                sid=s["station_id"], name=s["name"], lines=s["lines"],
            )
        print(f"  NationalRailStation nodes: {len(rail_stations)}")

        # METRO_LINK relationships
        # MERGE on structure only, SET properties separately: prevents duplicate
        # relationships when properties change on re-seed.
        count = 0
        for s in metro_stations:
            for adj in s["adjacent_stations"]:
                session.run(
                    """
                    MATCH (a:MetroStation {station_id: $from_id})
                    MATCH (b:MetroStation {station_id: $to_id})
                    MERGE (a)-[r:METRO_LINK]->(b)
                    SET r.line = $line, r.travel_time_min = $time,
                        r.cost_standard = $time * 0.5,
                        r.cost_first    = $time * 0.5
                    """,
                    from_id=s["station_id"], to_id=adj["station_id"],
                    line=adj["line"], time=adj["travel_time_min"],
                )
                count += 1
        print(f"  METRO_LINK relationships: {count}")

        # RAIL_LINK relationships
        count = 0
        for s in rail_stations:
            for adj in s["adjacent_stations"]:
                session.run(
                    """
                    MATCH (a:NationalRailStation {station_id: $from_id})
                    MATCH (b:NationalRailStation {station_id: $to_id})
                    MERGE (a)-[r:RAIL_LINK]->(b)
                    SET r.line = $line, r.travel_time_min = $time,
                        r.cost_standard = $time * 0.5,
                        r.cost_first    = $time * 1.5
                    """,
                    from_id=s["station_id"], to_id=adj["station_id"],
                    line=adj["line"], time=adj["travel_time_min"],
                )
                count += 1
        print(f"  RAIL_LINK relationships: {count}")

        # INTERCHANGE_TO relationships (metro ↔ rail, 5 min transfer)
        count = 0
        for s in metro_stations:
            rail_id = s.get("interchange_national_rail_station_id")
            if s["is_interchange_national_rail"] and rail_id:
                session.run(
                    """
                    MATCH (m:MetroStation        {station_id: $metro_id})
                    MATCH (r:NationalRailStation {station_id: $rail_id})
                    MERGE (m)-[r1:INTERCHANGE_TO]->(r)
                    SET r1.travel_time_min = 5,
                        r1.cost_standard   = 0,
                        r1.cost_first      = 0
                    MERGE (r)-[r2:INTERCHANGE_TO]->(m)
                    SET r2.travel_time_min = 5,
                        r2.cost_standard   = 0,
                        r2.cost_first      = 0
                    """,
                    metro_id=s["station_id"], rail_id=rail_id,
                )
                count += 2
        print(f"  INTERCHANGE_TO relationships: {count}")

    driver.close()
    print("\nNeo4j graph seeded successfully.")
    print("   Open http://localhost:7475 to explore the graph.")


if __name__ == "__main__":
    print("Connecting to Neo4j...")
    seed()
