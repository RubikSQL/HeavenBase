"""Create and seed the registered ``demo`` workspace for ``hb dashboard``.

Registers a persistent workspace named ``demo`` (SQLite file under the
HeavenBase home) and seeds three entity types with deterministic sample rows
covering every column-statistics kind: numeric, text, bool, time, and JSON.
Rerunnable: registration updates in place and rows upsert by fixed ids. A
failed refresh restores registry metadata, but backend writes are not wrapped
in a cross-operation transaction and may remain for a later retry.
"""

from __future__ import annotations

import argparse
import random

import heavenbase as hb
from heavenbase.workspace import WorkspaceSpec
from heavenbase.utils import check_workspace_id, normalize_file_uri
from heavenbase.utils.typing import Any

WORDS = "backend vector schema index planner storage workspace query graph retrieval augmented".split()
AUTHORS = ["Chen", "Alvarez", "Okafor", "Sato", "Novak"]
TAGS = ["eco", "popular", "limited", "sale", "new", "clearance"]


class DemoPaper(hb.Entity):
    """Research paper sample rows for the demo workspace."""

    object_id: str = hb.field(hb.Identifier).pk()
    title: str = hb.field(hb.ShortText).desc("Paper title")
    author: str = hb.field(hb.ShortText).desc("First author")
    year: int = hb.field(hb.Integer).desc("Publication year")
    score: float = hb.field(hb.Float).desc("Review score 0-10")
    accepted: bool = hb.field(hb.Boolean).desc("Accepted at venue")
    published: str = hb.field(hb.Date).desc("Publication date").store(optional=True)
    abstract: str = hb.field(hb.MediumText).desc("Abstract text").store(optional=True)
    meta: dict = hb.field(hb.Json).desc("Open metadata")
    # materialized from meta.venue at seed time so agents can group/aggregate on
    # it (dotted paths only filter); a compute() lambda would not survive
    # manifest rehydration in other processes such as the dashboard server
    venue: str = hb.field(hb.ShortText).desc("Venue, also stored in meta.venue")


class DemoProduct(hb.Entity):
    """Catalog product sample rows for the demo workspace."""

    object_id: str = hb.field(hb.Identifier).pk()
    name: str = hb.field(hb.ShortText).desc("Product display name")
    category: str = hb.field(hb.ShortText).desc("Catalog category")
    price: float = hb.field(hb.Float).desc("Unit price in USD")
    stock: int = hb.field(hb.Integer).desc("Units in stock")
    tags: list = hb.field(hb.Json).desc("Free-form tag list")


class DemoDoc(hb.Entity):
    """Long-form document sample rows for the demo workspace."""

    object_id: str = hb.field(hb.Identifier).pk()
    body: str = hb.field(hb.LongText).desc("Document body")
    # Stored inline on the SQL main backend so vectors persist across processes
    # (the default vec backend is in-memory and would drop them on restart).
    embedding: list = hb.field(hb.Vector(8)).store(to="main", strategy="inline").desc("Demo embedding vector")


def seed(ws: Any) -> None:
    """Upsert deterministic sample rows into one open demo workspace."""
    random.seed(7)
    papers = [
        {
            "object_id": f"paper-{i:03d}",
            "title": f"{' '.join(random.sample(WORDS, 3)).title()} at Scale",
            "author": random.choice(AUTHORS),
            "year": 2019 + (i % 7),
            "score": round(random.uniform(2, 9.8), 2),
            "accepted": random.random() > 0.4,
            "published": f"{2019 + (i % 7)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}",
            "abstract": ("We study " + " ".join(random.choices(WORDS, k=40)) + ".") if i % 3 else None,
            "meta": {
                "venue": random.choice(["NeurIPS", "VLDB", "SIGMOD"]),
                "cites": random.randint(0, 400),
                "topics": random.sample(WORDS, 2),
                "review": {"scores": [random.randint(3, 9) for _ in range(3)]},
            },
        }
        for i in range(137)
    ]
    for row in papers:  # after the comprehension so the random call order (and data) stays identical
        row["venue"] = row["meta"]["venue"]
    ws.upsert_many(DemoPaper, papers)
    ws.upsert_many(
        DemoProduct,
        [
            {
                "object_id": f"prod-{i:03d}",
                "name": f"{random.choice(['Aero', 'Terra', 'Nimbus', 'Quartz'])} {random.choice(['Desk', 'Lamp', 'Chair', 'Shelf'])} {i}",
                "category": random.choice(["furniture", "lighting", "storage"]),
                "price": round(random.uniform(9, 499), 2),
                "stock": random.randint(0, 500),
                "tags": random.sample(TAGS, random.randint(0, 3)),
            }
            for i in range(61)
        ],
    )
    ws.upsert_many(
        DemoDoc,
        [
            {
                "object_id": f"doc-{i:03d}",
                "body": " ".join(random.choices(WORDS, k=random.randint(60, 400))) + ".",
                "embedding": [round(random.uniform(-1, 1), 4) for _ in range(8)],
            }
            for i in range(23)
        ],
    )


def materialize_demo(ws_id: str = "demo", *, context: Any = None) -> tuple[WorkspaceSpec, dict[str, int]]:
    """Create or refresh the demo with metadata rollback on failure.

    Args:
        ws_id (str): Workspace id to register. Defaults to ``"demo"``.
        context: Context that owns the workspace registry and live facade.

    Returns:
        tuple[WorkspaceSpec, dict[str, int]]: Refreshed registration and entity
        row counts.

    Notes:
        Registration metadata and active selection are restored when setup
        fails. Seed writes already committed by a backend are intentionally
        left in place; rerunning the deterministic upserts reconciles them.
    """
    from heavenbase.context import context_or_default

    selected = context_or_default(context)
    clean_id = check_workspace_id(ws_id)
    config = {
        "backends": {
            "main": {
                "type": "sqlite",
                "name": "main",
                "database": normalize_file_uri(f"file://%/workspaces/{clean_id}/main.db", home=selected.config.root),
            }
        }
    }
    try:
        previous = WorkspaceSpec.load(clean_id, context=selected)
    except KeyError:
        previous = None
    ws = None
    try:
        # An explicit empty manifest prevents stale schema rehydration while
        # preserving the registry row, creation metadata, and active context.
        spec = WorkspaceSpec.register(clean_id, config=config, manifest={}, context=selected)
        ws = spec.open_detached(context=selected)
        for entity in (DemoPaper, DemoProduct, DemoDoc):
            # The demo owns these entities; replacing lets reruns evolve the schema.
            ws.register(entity, allow_replace=True)
        seed(ws)
        counts = {entity_id: ws.count(entity_id) for entity_id in ("demo-paper", "demo-product", "demo-doc")}
        # Future processes (for example ``hb dashboard``) rehydrate the schema
        # without importing this module.
        spec = WorkspaceSpec.register(
            clean_id,
            config=config,
            manifest=ws.to_manifest(),
            context=selected,
        )
    except Exception:
        if ws is not None:
            ws.release()
        if previous is None:
            WorkspaceSpec.unregister(clean_id, context=selected)
        else:
            WorkspaceSpec.register(
                clean_id,
                config=previous.config,
                manifest=previous.manifest,
                context=selected,
            )
        raise
    return spec, counts


def main() -> None:
    """Register the ``demo`` workspace and seed sample rows."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ws-id", default="demo", help="Workspace id to register (default: demo).")
    options = parser.parse_args()

    spec, counts = materialize_demo(options.ws_id)
    print(f"workspace {spec.id!r} ready: {counts}")
    print("browse it with: hb dashboard")


if __name__ == "__main__":
    main()
