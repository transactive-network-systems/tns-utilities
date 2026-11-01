# tns-utilities

Shared Python libraries of the transactive node system (TNS). One git repository, several distributions, each in its
own directory with its own `pyproject.toml` (PEP 621, Python 3.11 or newer):

| Directory | Distribution | Import package | Holds |
|---|---|---|---|
| `lib-archivable/` | `lib-archivable` | `archivable` | The archive machinery: `Archivable`, the `DataManager` base, `LocalDataManager` (SQLAlchemy), `NullDataManager`, `ArchiveReader`, table and enumeration definitions, the archive schema version. No dependency on TENT or VOLTTRON. |
| `volttron-lib-archivable/` | `volttron-lib-archivable` | `volttron_archivable` | `VolttronDataManager`: a data manager that registers its host's tables with the Application Historian over the VOLTTRON message bus and publishes the records. Depends on `lib-archivable` and `volttron-core`. |

Each distribution is installed from its directory:

```shell
pip install -e lib-archivable
pip install -e volttron-lib-archivable
```

and tested from its directory:

```shell
cd lib-archivable && python -m pytest tests -q
cd volttron-lib-archivable && python -m pytest tests -q
```

The `volttron-lib-archivable` tests run without a VOLTTRON platform (its `tests/conftest.py` installs fake `volttron`
and `gevent` modules when the real ones are absent).

## Who uses what

- TENT (`code/TENT`) depends on `lib-archivable`: `tent.data_manager` keeps TENT's own things (its archive schema
  version, the definitions and enumerations it ships, `open_archive`) and takes everything else from `archivable`.
- The TransactiveNodeAgent (`code/TransactiveNodeAgent`) depends on `volttron-lib-archivable` for its data manager.
- The Application Historian (`code/volttron-transactive-node-historian`) still imports `tent.data_manager`; its
  migration to `archivable` follows. Until then the historian and a node of the same TENT agree on the archive schema
  through TENT.

## The registration payload

A `VolttronDataManager` registers its host's tables with the historian (RPC `create_tables`, or the pubsub
`archive_definitions` message) as

```json
{"definitions": [table definitions], "enumerations": [{"name": "MarketState", "members": ["Initial", "Active"]}],
 "schema_version": 3}
```

The historian registers the enumerations with its data manager (`register_enumerations`), which builds a Python Enum
for each, then the tables. Enumeration values are stored by member name on both sides.
