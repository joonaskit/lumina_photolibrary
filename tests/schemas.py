"""A library database taken back to an older schema, for the tests of a migration."""

import sqlite3

from lumina.core import db as dbm


def rewind(path, version):
    """Take the library at `path` back to schema `version`: drop the tables, indexes and
    columns the later migrations added, keeping the rows, and set its version. What
    version N is comes from the first N migrations, so a new migration needs nothing
    here; one that does more than add is refused, naming what is left over."""
    old = sqlite3.connect(":memory:")
    for migrate in dbm._MIGRATIONS[:version]:
        migrate(old)
    want = _schema(old)
    old.close()

    db = sqlite3.connect(path)
    have = _schema(db)
    # Indexes first: a column that is indexed cannot be dropped
    for name in sorted(have["indexes"] - want["indexes"]):
        db.execute(f'DROP INDEX "{name}"')
    for table in sorted(have["tables"].keys() - want["tables"].keys()):
        db.execute(f'DROP TABLE "{table}"')
    for table, columns in want["tables"].items():
        for column in have["tables"].get(table, []):
            if column not in columns:
                db.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
    left = _schema(db)
    if left != want:
        db.close()
        raise AssertionError(
            f"schema {version} cannot be made by dropping what came later: "
            f"{left} is not {want} (tests/schemas.py)"
        )
    db.execute(f"PRAGMA user_version = {version}")
    db.commit()
    db.close()


def _schema(db):
    """The tables with their columns in order, and the names of the indexes made by a
    migration (not those SQLite makes for a key)."""
    rows = db.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {
        "tables": {
            name: [row[1] for row in db.execute(f'PRAGMA table_info("{name}")')]
            for kind, name in rows
            if kind == "table"
        },
        "indexes": {name for kind, name in rows if kind == "index"},
    }
