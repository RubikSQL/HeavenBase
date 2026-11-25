"""Logins with table rights only, on keyword-index storage another login owns (PostgreSQL and MySQL).

The cases run against the servers that ``HVNB_POSTGRES_URL`` and ``HVNB_MYSQL_URL`` name, each an account that may create
databases and logins.
"""

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

import heavenbase as hb
from heavenbase.utils.ids import sql_index_name

pytestmark = pytest.mark.full

_WIDTH_INDEX = sql_index_name("hb_gram_keywords", "width")


class Note(hb.Entity):
    identifier = "rights-note"
    tags = hb.field(hb.Array[hb.ShortText]).store(to="notes", strategy=hb.SparseGramIndex(gram_width=2))


class PlainNote(hb.Entity):
    identifier = "rights-note"
    tags = hb.field(hb.Array[hb.ShortText]).store(to="notes")


class Other(hb.Entity):
    identifier = "rights-other"
    tags = hb.field(hb.Array[hb.ShortText]).store(to="notes", strategy=hb.SparseGramIndex(gram_width=2))


class _Server:
    """One fresh database, and logins that hold only the rights a case grants them."""

    def __init__(self, kind, url):
        self.kind, self.url = kind, make_url(url)
        self.database = f"rights_{uuid4().hex[:16]}"
        self.logins = []
        self.admin = create_engine(url, isolation_level="AUTOCOMMIT")
        self.run(self.admin, f"CREATE DATABASE {self.database}")
        self.owner = create_engine(self.url.set(database=self.database), isolation_level="AUTOCOMMIT")

    @staticmethod
    def run(engine, *statements):
        with engine.connect() as connection:
            for statement in statements:
                connection.exec_driver_sql(statement)

    def login(self, rights):
        """Create a login with ``rights`` on every table the database holds now, and return its URL."""
        name = f"hb_rights_{uuid4().hex[:10]}"
        self.logins.append(name)
        if self.kind == "postgres":
            tables = f"GRANT {rights} ON ALL TABLES IN SCHEMA public TO {name}"
            self.run(self.owner, f"CREATE USER {name} PASSWORD 'rights'", f"GRANT USAGE, CREATE ON SCHEMA public TO {name}", tables)
        else:
            self.run(self.owner, f"CREATE USER '{name}'@'%%' IDENTIFIED BY 'rights'", f"GRANT {rights} ON `{self.database}`.* TO '{name}'@'%%'")
        return self.url.set(database=self.database, username=name, password="rights").render_as_string(hide_password=False)

    def close(self):
        self.owner.dispose()
        self.run(self.admin, f"DROP DATABASE {self.database}" + (" WITH (FORCE)" if self.kind == "postgres" else ""))
        for name in self.logins:
            self.run(self.admin, f"DROP USER {name}" if self.kind == "postgres" else f"DROP USER '{name}'@'%%'")
        self.admin.dispose()


@pytest.fixture(params=["postgres", "mysql"])
def server(request):
    variable = {"postgres": "HVNB_POSTGRES_URL", "mysql": "HVNB_MYSQL_URL"}[request.param]
    url = os.environ.get(variable)
    if not url:
        pytest.skip(f"requires a test server in {variable}")
    server = _Server(request.param, url)
    try:
        yield server
    finally:
        server.close()


def _ws(tmp_path, server, name, url=None):
    route = {"type": server.kind, "url": url or server.url.set(database=server.database).render_as_string(hide_password=False)}
    main = {"type": "sqlite", "database": hb.utils.path_to_file_uri(tmp_path / f"{name}-system.db")}
    return hb.HeavenBase(server.database.replace("_", "-"), detached=True, backends={"main": main, "notes": route})


def _owner_storage(tmp_path, server):
    owner = _ws(tmp_path, server, "owner")
    try:
        owner.register(Note)
        owner.upsert(Note, {"object_id": "n1", "tags": ["invoice"]})
    finally:
        owner.release()


def _found(ws, query):
    return sorted(ws.query(Note).where(Note.tags.contained_in(query)).execute().ids)


def test_a_login_that_may_not_create_indexes_opens_storage_without_the_length_index(tmp_path, server):
    _owner_storage(tmp_path, server)
    # HeavenBase 0.1.2.2 makes the keyword tables without the index on keyword length
    table = " ON hb_gram_keywords" if server.kind == "mysql" else ""
    server.run(server.owner, f"DROP INDEX {_WIDTH_INDEX}{table}")
    ws = _ws(tmp_path, server, "login", server.login("SELECT, INSERT, UPDATE, DELETE"))
    try:
        ws.register(Note)
        ws.upsert(Note, {"object_id": "n2", "tags": ["receipt"]})
        ws.upsert(Note, {"object_id": "n1", "tags": ["credit note"]})
        assert _found(ws, "an invoice, a receipt and a credit note") == ["n1", "n2"]
        assert _found(ws, "an invoice") == []
        assert ws.delete((Note, "n2"))
    finally:
        ws.release()
    assert _WIDTH_INDEX not in {index["name"] for index in inspect(server.owner).get_indexes("hb_gram_keywords")}
    # a login that may create it adds it again
    owner = _ws(tmp_path, server, "owner-again")
    try:
        owner.register(Note)
    finally:
        owner.release()
    assert _WIDTH_INDEX in {index["name"] for index in inspect(server.owner).get_indexes("hb_gram_keywords")}


def test_an_index_that_0_1_2_2_also_makes_is_still_required(tmp_path, server):
    _owner_storage(tmp_path, server)
    table = " ON hb_gram_keywords" if server.kind == "mysql" else ""
    server.run(server.owner, f"DROP INDEX {sql_index_name('hb_gram_keywords', 'object')}{table}")
    ws = _ws(tmp_path, server, "login", server.login("SELECT, INSERT, UPDATE, DELETE"))
    try:
        with pytest.raises(DBAPIError, match="hb_gram_keywords"):
            ws.register(Note)
    finally:
        ws.release()


def test_a_login_without_update_registers_a_new_indexed_entity(tmp_path, server):
    owner = _ws(tmp_path, server, "owner")
    try:
        owner.register(Other)
        owner.upsert(Other, {"object_id": "o1", "tags": ["invoice"]})
    finally:
        owner.release()
    ws = _ws(tmp_path, server, "login", server.login("SELECT, INSERT, DELETE" + (", CREATE, INDEX" if server.kind == "mysql" else "")))
    try:
        ws.register(Note)
        ws.upsert(Note, {"object_id": "n1", "tags": ["invoice"]})
        assert _found(ws, "an invoice") == ["n1"]
        # once more, with a stored row to rebuild the index from
        ws.register(PlainNote, allow_replace=True)
        ws.register(Note, allow_replace=True)
        assert _found(ws, "an invoice") == ["n1"]
    finally:
        ws.release()


def test_a_login_that_creates_the_tables_needs_the_right_to_index_them(tmp_path, server):
    if server.kind == "postgres":
        pytest.skip("the owner of a PostgreSQL table may always index it")
    ws = _ws(tmp_path, server, "login", server.login("SELECT, INSERT, UPDATE, DELETE, CREATE"))
    try:
        with pytest.raises(DBAPIError, match="INDEX command denied"):
            ws.register(Note)
    finally:
        ws.release()
