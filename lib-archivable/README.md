# lib-archivable

Archive records of application objects into a relational database from JSON table definitions. Import package
`archivable`. Depends on SQLAlchemy 2, pydantic 2 and uuid6; nothing else.

## What it does

An application declares, per class, the tables it writes as JSON table definitions, and each object produces its row
(`Archivable.get_record`). A `DataManager` turns the definitions into SQLAlchemy tables and takes the records:
`LocalDataManager` writes them to a database named by a SQLAlchemy URL (SQLite, PostgreSQL, ...), `NullDataManager`
accepts and discards them (tests, dry runs), and other packages add managers that forward them elsewhere
(`volttron-lib-archivable`). `LocalDataManager.from_url` opens an existing archive for reading.

```python
from archivable import ArchiveSchema, LocalDataManager, schema_fingerprint, enumeration_definitions

definitions = [...]             # the application's table definitions
enumerations = [MarketState]    # the Enum classes its columns name

dm = LocalDataManager(host, {'url': 'sqlite:////data/archive.sqlite'})
dm.declare_schema(ArchiveSchema('TENT', 3, schema_fingerprint(definitions, enumeration_definitions(enumerations)),
                                application_version='3.0.0'))
dm.register_enumerations(enumerations)
dm.add_tables(definitions)
dm.init_archive()               # creates missing tables, checks the archive schema, releases queued records
dm.archive_data({'table_name': 'market', 'data': {...}})
```

The order matters: the schema is declared and the enumerations registered before the tables that use them, and
`init_archive` refuses to run without a declared schema.

## Table definitions

```json
{"name": "market", "inherits": "component",
 "columns": [{"name": "id", "type": "Uuid", "foreign_key": "component.id", "kwargs": {"primary_key": true}},
             {"name": "state", "enumeration": "MarketState"},
             {"name": "duration", "type": "Interval"}],
 "indices": [{"name": "ix_market_state", "columns": ["state"]}],
 "constraints": [{"foreign_key": {"columns": ["a", "b"], "references": ["t.a", "t.b"]}}]}
```

`type` is a SQLAlchemy type name (`Integer`, `String`, `DateTime`, `Uuid`, `Interval`, `Boolean`, `Float`, `Text`,
...), `type_args` its positional arguments. `DateTime` columns are timezone-aware and stored as UTC; a naive datetime
is refused. `inherits` names a parent table: the row of the child carries the parent's columns (joined inheritance), and
a record addressed to the child table writes both rows. A definition registered twice must be identical apart from
column order (`definition_differences` lists what differs).

## Enumerations

A column with `"enumeration": "MarketState"` stores the member name. The name resolves against the enumerations
registered with the data manager: the application registers its Enum classes; a receiver without the class (a
historian) registers the wire form, `{"name": "MarketState", "members": ["Initial", "Active", ...]}`, and gets a
dynamically created Enum that decodes by name. `enumeration_definitions(classes)` produces the wire form;
`DataManager.enumeration_definitions()` lists what is registered. An unknown enumeration in a column definition, or a
known name with another member list, is an error.

## The archive schema version

The library has no version of its own. The application numbers its set of definitions (incremented whenever a shipped
definition or enumeration changes), fingerprints them with `schema_fingerprint(definitions, enumerations)` and
declares both as an `ArchiveSchema`. `init_archive` writes one row into the `archive_schema` table (`version`,
`fingerprint`, `application`, `application_version`, `created`) of a new database and refuses an existing database of
another version with `ArchiveSchemaMismatch` naming both versions; nothing is migrated. A database with tables but no
`archive_schema` table is version 1 (`UNVERSIONED`). The same version with another fingerprint is a warning: a
definition changed without a version bump.

## Configuration

Data managers are configurable classes: a nested `Config` model (`ConfigModel`, fields declared with `Setting`)
validates the constructor's keyword arguments, unknown keys are errors naming the class and the key, and fields marked
`reconfigurable=True` may be changed on a running object with `reconfigure`. `archivable.config` holds the contract
(`ConfigModel`, `Configurable`, `Setting`, `ConfigurationError`, `ReconfigurationError`); applications with richer
conventions subclass it.

## Reading

`LocalDataManager.from_url(url, definitions, enumerations, schema=None, reflect_unknown=True)` opens an archive
read-only, registers the definitions so types decode, reflects any other table, and reports `schema_version`.
`ArchiveReader` offers `select`, `execute` (lists of dicts, UTC datetimes), `table_chain`, `tables_inheriting` and
`typed_rows` (rows of a base table with their deepest extension table merged in). Applications add their own queries in
a subclass.

## Tests

```shell
pip install -e .[dev]
python -m pytest tests -q
```
