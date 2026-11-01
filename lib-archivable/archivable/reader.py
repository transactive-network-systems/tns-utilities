"""
Read side of an archive.

Opens an existing archive (SQLite file, PostgreSQL, ...) without a running host, registers the tables the caller knows
how to write so column types (Uuid, Interval, enumerations) decode correctly, reflects any table it does not know, and
offers generic access (select, execute, inheritance chains). Plain SQLAlchemy Core; results are lists of dicts. An
application adds its own canonical queries in a subclass.
"""
import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import DateTime, Select, inspect, select
from sqlalchemy.exc import ArgumentError

from archivable.schema import ARCHIVE_SCHEMA_TABLE, UNVERSIONED, ArchiveSchema

_log = logging.getLogger(__name__)


class ArchiveReader:
    """Mixin for DataManager implementations backed by a SQLAlchemy engine (self.engine, self.tables, self.definitions,
    self.enumerations, self.registry)."""

    read_only: bool = False

    # ---------------------------------------------------------------- opening an existing archive
    @classmethod
    def from_url(cls, url: str, definitions: Sequence[Mapping], enumerations: Iterable[type[Enum] | Mapping] = (),
                 schema: ArchiveSchema | None = None, reflect_unknown: bool = True):
        """
        Open an existing archive for reading. `definitions` (and the `enumerations` they name) are registered so their
        types decode correctly; if `reflect_unknown`, any other table in the database is reflected as-is. With a
        `schema`, a database written with another archive schema version is still opened, with a warning.
        """
        dm = cls(None, {'url': url})
        dm.register_enumerations(enumerations)
        dm.add_tables(list(definitions))
        if reflect_unknown:
            dm.reflect_unknown_tables()
        dm.read_only = True
        if schema is not None:
            dm.declare_schema(schema)
            version = dm.schema_version or UNVERSIONED
            if version != schema.version:
                _log.warning(f'Archive {url!r} was written with archive schema version {version} ({schema.writer} '
                             f'writes version {schema.version}); tables whose definitions changed between the two may '
                             f'not read correctly.')
        return dm

    def archive_schema_record(self) -> dict | None:
        """The archive_schema row of the database, or None when the database has no archive_schema table or the table
        is empty: an archive written before the table existed (version 1), or an empty database."""
        if ARCHIVE_SCHEMA_TABLE not in self.tables or not self.has_table(ARCHIVE_SCHEMA_TABLE):
            return None
        table = self.tables[ARCHIVE_SCHEMA_TABLE]
        rows = self.execute(select(table).order_by(table.c.version.desc()))
        return rows[0] if rows else None

    @property
    def schema_version(self) -> int | None:
        """The archive schema version the database was written with; None when the database has no archive_schema
        table, which means version 1 or a database nothing has been written to yet."""
        record = self.archive_schema_record()
        return None if record is None else record['version']

    def reflect_unknown_tables(self, only: Iterable[str] | None = None) -> list[str]:
        """
        Reflect tables the database has but no registered definition describes and make them readable through
        query_data. `only` restricts the reflection to those table names (a name that is not in the database raises);
        by default every unknown table is reflected. Returns the names added.
        """
        metadata = self.registry.metadata
        known = set(metadata.tables)
        if only is None:
            metadata.reflect(bind=self.engine, only=lambda name, _meta: name not in known)
        else:
            wanted = [name for name in only if name not in known]
            if wanted:
                metadata.reflect(bind=self.engine, only=wanted)
        added = []
        for name, table in metadata.tables.items():
            if name in self.tables:
                continue
            self.tables[name] = table
            orm_name = ''.join(word.title() for word in name.split('_')) + 'Table'
            try:
                self.orm[name] = type(orm_name, (), {})
                self.registry.map_imperatively(self.orm[name], table)
            except ArgumentError:  # no primary key: Core access only
                self.orm.pop(name, None)
            added.append(name)
        return added

    @property
    def present_tables(self) -> set[str]:
        """Tables that actually exist in the database (known tables may be registered but never created)."""
        return set(inspect(self.engine).get_table_names())

    def has_table(self, name: str) -> bool:
        return name in self.present_tables

    # ---------------------------------------------------------------- generic access
    def select(self, table_name: str) -> Select:
        """A Select over a registered table, for callers that want to add their own joins and filters."""
        return select(self.tables[table_name])

    def execute(self, statement) -> list[dict]:
        columns = list(statement.selected_columns)
        with self.engine.connect() as connection:
            return [self.decode_row(dict(row._mapping), columns) for row in connection.execute(statement)]

    @staticmethod
    def decode_row(row: dict, columns) -> dict:
        """Archive datetimes are UTC; SQLite returns them naive, so reattach UTC for DateTime-typed columns."""
        for column in columns:
            name = getattr(column, 'name', None) or getattr(column, 'key', None)
            value = row.get(name)
            if isinstance(column.type, DateTime) and isinstance(value, datetime) and value.tzinfo is None:
                row[name] = value.replace(tzinfo=timezone.utc)
        return row

    # ---------------------------------------------------------------- inheritance (base -> extension tables)
    def _parent_table(self, name: str) -> str | None:
        return (self.definitions.get(name) or {}).get('inherits')

    def table_chain(self, name: str) -> list[str]:
        """[name, parent, grandparent, ...] following `inherits` declarations."""
        chain = [name]
        while (parent := self._parent_table(chain[-1])) is not None:
            chain.append(parent)
        return chain

    def tables_inheriting(self, root: str) -> list[str]:
        return [name for name in self.definitions if name != root and root in self.table_chain(name)]

    def typed_rows(self, root: str, filters: Mapping | None = None) -> list[dict]:
        """
        Every row of the `root` table with its concrete type: `type` is the deepest extension table holding the row's
        primary key, and that table's columns (and its ancestors') are merged into the row. `filters` are equality
        conditions on the root table's columns.
        """
        root_table = self.tables[root]
        statement = select(root_table)
        for column_name, value in (filters or {}).items():
            statement = statement.where(root_table.c[column_name] == value)
        root_key = list(root_table.primary_key.columns)[0].name
        rows = {row[root_key]: dict(row, type=root) for row in self.execute(statement)}
        present = self.present_tables
        extension_rows: dict[str, dict] = {}
        for name in self.tables_inheriting(root):
            if name not in present:
                continue
            table = self.tables[name]
            key = list(table.primary_key.columns)[0].name  # the root key, or the extension's own name for it
            extension_rows[name] = {r[key]: {k: v for k, v in r.items() if k != key} for r in self.execute(select(table))}
        depth = {name: len(self.table_chain(name)) for name in extension_rows}
        for name, by_id in extension_rows.items():
            for row_id in by_id:
                if row_id in rows and depth[name] > depth.get(rows[row_id]['type'], 1):
                    rows[row_id]['type'] = name
        for row_id, row in rows.items():
            for name in reversed(self.table_chain(row['type'])):
                if name in extension_rows and row_id in extension_rows[name]:
                    row.update(extension_rows[name][row_id])
        return list(rows.values())
