"""Verify Postgres + MySQL connectivity at three layers.

Layer 1: raw SQLAlchemy.
Layer 2: heavenbase ``Database``.
Layer 3: heavenbase ``SQLBackend``.

Run:  python scripts/verify_db_stack.py
"""

import sys
from heavenbase.utils.files import get_file_dir, pj

ROOT = pj(get_file_dir(get_file_dir(__file__)), abs=True)
sys.path.insert(0, pj(ROOT, "src"))

import sqlalchemy as sa  # noqa: E402
from heavenbase.database import Database  # noqa: E402

_TARGETS = [
    dict(
        label="postgres",
        provider="pg",
        username="heavenbase",
        password="heavenbase",
        host="localhost",
        port=5432,
        database="heavenbase",
        raw_url="postgresql+psycopg2://heavenbase:heavenbase@localhost:5432/heavenbase",
    ),
    dict(
        label="mysql",
        provider="mysql",
        username="heavenbase",
        password="heavenbase",
        host="localhost",
        port=3306,
        database="heavenbase",
        raw_url="mysql+pymysql://heavenbase:heavenbase@localhost:3306/heavenbase",
    ),
]


def layer1_sqlalchemy(t):
    eng = sa.create_engine(t["raw_url"], pool_pre_ping=True)
    with eng.connect() as c:
        assert c.execute(sa.text("SELECT 1")).scalar() == 1
    eng.dispose()
    print(f"  [L1 sqlalchemy] {t['label']:8s} OK")


def layer2_database(t):
    db = Database(provider=t["provider"], database=t["database"], host=t["host"], port=t["port"], username=t["username"], password=t["password"])
    with db.engine.connect() as c:
        assert c.execute(sa.text("SELECT 1")).scalar() == 1
    print(f"  [L2 database  ] {t['label']:8s} OK")
    return db


def layer3_heavenbase(t, db):
    import heavenbase as hb
    from heavenbase.backends.sql import SQLBackend

    class _Ping(hb.Entity):
        id: hb.Identifier
        name: hb.ShortText
        price: hb.Float

    _Ping._backends = {}
    sql = SQLBackend(name="sql", db=db)
    _Ping.configure(backends=[sql])
    for pid in ("p-1", "p-2"):
        try:
            _Ping.delete(pid)
        except Exception:
            pass
    _Ping.upsert(id="p-1", name="widget", price=9.5)
    _Ping.upsert(id="p-2", name="gizmo", price=19.5)
    ids = sorted(_Ping.query().where(_Ping.price > 10).execute().ids)
    assert ids == ["p-2"], ids
    _Ping.delete("p-1")
    _Ping.delete("p-2")
    print(f"  [L3 heavenbase] {t['label']:8s} OK")


def main() -> int:
    for t in _TARGETS:
        print(f"[{t['label']}]")
        try:
            layer1_sqlalchemy(t)
            db = layer2_database(t)
            layer3_heavenbase(t, db)
        except Exception as e:
            print(f"  FAIL at {t['label']}: {type(e).__name__}: {e}")
            return 1
    print("all layers green.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
