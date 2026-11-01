# volttron-lib-archivable

`VolttronDataManager`: a `lib-archivable` data manager that archives through the Application Historian
(`volttron-transactive-node-historian`) on the VOLTTRON message bus. Import package `volttron_archivable`. Depends on
`lib-archivable`, `volttron-core` (modular platform) and `gevent`.

## What it does

The host agent configures the data manager, declares its archive schema, registers its enumerations and tables, and
calls `init_archive`. The manager then registers everything with the historian and publishes the records the host
hands it in batches. It knows nothing about what is archived.

What the host provides:

| Member | Used for |
|---|---|
| `name` | The `publisher` the registration is filed under. |
| `vip.rpc.call(peer, method, *args, external_platform=...)` | The `create_tables` RPC (registration `rpc`). |
| `publish(key, message=...)` | Records (key `archive`, configurable) and the registration request (key `archive_definitions`); the host maps keys to topics. |
| `register_subscription(key, callback, all_platforms=...)` | The historian's answers on key `archive_status` (registration `pubsub`). |

## Configuration

The keys are the fields of `VolttronDataManager.Config`:

| Key | Default | Meaning |
|---|---|---|
| `historian_vip` | required | VIP identity of the Application Historian. |
| `registration` | `rpc` | `rpc` (the `create_tables` RPC) or `pubsub` (a definitions publication answered on the status topic, for a federation without cross-platform RPC). |
| `external_platform` | `""` | Platform the historian runs on, for `rpc` in a federation; empty means the local platform. |
| `rpc_timeout` | `30` | Seconds to wait for one answer. Reconfigurable. |
| `rpc_retry_period` | `10` | Seconds between registration attempts while unanswered. Reconfigurable. |
| `rpc_total_timeout` | `300` | Seconds after which an unanswered registration is given up with an error; nothing is archived in that run. Reconfigurable. |
| `publish_interval` | `1.0` | Seconds between publications of the queued records. Reconfigurable. |
| `max_batch_records` | `1000` | Largest number of records in one archive message. Reconfigurable. |
| `archive_key` | `archive` | Publish key the records go out under. |
| `archival_buffer_length` | `10000` | Queue length above which a warning is logged while the archive is not ready. |

## The registration

Both paths send the same payload:

```json
{"definitions": [table definitions],
 "enumerations": [{"name": "MarketState", "members": ["Initial", "Active", "..."]}],
 "schema_version": 3}
```

`schema_version` is the version of the host's declared `ArchiveSchema`. The pubsub message adds `publisher` (the
host's name) and `request_id`; the historian answers on the host's status topic with the same `request_id`. Both paths
yield the status mapping

```json
{"request_id": "...", "publisher": "...", "status": "success | partial | error",
 "registered_tables": [...], "rejected_tables": {"table": {"message": "...", "differences": "..."}},
 "message": "...", "schema_version": 3}
```

`success` turns the archive on and publishes the backlog; `partial` does too and drops every record addressed to a
rejected table (one error log per table); `error` is raised and nothing is archived. A historian of another archive
schema version answers `error` naming both versions. An unanswered registration is retried every `rpc_retry_period`
seconds and raised after `rpc_total_timeout`; a rejected registration is raised at once.

## Records

Records are queued and published as lists of `{"table_name": ..., "data": {...}}` envelopes: the backlog when the
registration succeeds, then everything queued every `publish_interval` seconds, in messages of at most
`max_batch_records`. A failed publication is logged and its records kept for the next flush; a chunk that cannot be
serialised is dropped with an error. `stop()` flushes once more and ends the flusher.

## Tests

```shell
pip install -e ../lib-archivable -e .[dev]
python -m pytest tests -q
```

The tests run without a platform: `tests/conftest.py` installs fake `volttron` modules and, when gevent is absent, a
fake `gevent`.
