"""
VolttronDataManager: the DataManager a VOLTTRON agent uses to archive through the Application Historian.

The library knows nothing about what is archived. Its host (an agent that is also the owner of some Archivable
objects) hands it table definitions, enumerations and records like any other DataManager; this one forwards them over
the message bus to the Application Historian (volttron-transactive-node-historian), which owns the database. Two
interactions:

- At start-up (``init_archive``) the table definitions the host registered, the enumerations they name and the host's
  archive schema version are registered with the historian, by the ``create_tables`` RPC or by a definitions publication
  (``registration``), and the reply is waited for. Records queued before that moment are released once the historian
  has acknowledged the tables. The historian usually runs on another platform of the federation
  (``external_platform``), and a federation connection takes some time to come up after the platforms start, so an
  unanswered call is retried every ``rpc_retry_period`` seconds, each failure logged, until ``rpc_total_timeout``
  seconds have passed; then the error is raised rather than hidden: nothing would be archived in that run and the
  operator must know. An error reply (a rejected table definition) is raised at once; retrying cannot change it.
- Every batch of records (``archive_data``) is published as a list of ``{"table_name": ..., "data": {...}}`` envelopes
  under the host's archive publish key, which the historian captures. Non-finite floats (an inelastic vertex's
  infinite price, a NaN cost) are replaced by the library's ``{"$float": "inf" | "-inf" | "nan"}`` token first
  (archivable.encoding), so a host that serialises with standard JSON only can carry them; the host serialises the rest
  (UUIDs and enumeration members as strings, datetimes as ISO 8601, durations as seconds), and the historian's
  LocalDataManager parses the values back by column type, the token included.

What the host provides: ``name`` (the publisher name the historian files the registration under), ``vip.rpc.call``,
``publish(key, message=...)`` mapping a publish key to a topic, and ``register_subscription(key, callback,
all_platforms=...)`` for the pubsub registration path.

Configuration (the ``data_manager`` block of the host's configuration; the keys are the fields of ``Config``)::

    {"class_name": "VolttronDataManager", "module_name": "volttron_archivable.data_manager",
     "historian_vip": "tns.historian", "external_platform": "central", "registration": "pubsub"}

``historian_vip`` must be the VIP identity the historian is installed under; it has no default because the two sides
have disagreed before and a silent mismatch simply archives nothing. ``external_platform`` is the instance name of the
platform the historian runs on, empty (the default) when it is the host's own platform.
"""
import logging
import time

from collections.abc import Mapping, Sequence
from typing import Literal
from uuid import uuid4

import gevent
from gevent.event import AsyncResult

from archivable.config import ConfigModel, Setting
from archivable.data_manager import DataManager
from archivable.encoding import encode_non_finite

from volttron.client.vip.agent import errors
from volttron.utils.jsonrpc import RemoteError

# What a registration attempt may fail with and be retried for: the historian unreachable or not answering. Any other
# exception is a defect and propagates at once instead of being retried for rpc_total_timeout.
TRANSPORT_ERRORS = (gevent.Timeout, TimeoutError, RemoteError, errors.VIPError)

_log = logging.getLogger(__name__)


class VolttronDataManager(DataManager):
    """Archives through the Application Historian on the message bus.

    Before anything is archived the host's table definitions are registered with the historian, together with the
    enumerations they name and the host's archive schema (the declared ArchiveSchema: application, version,
    fingerprint, application version), by one of two paths selected with `registration`:

    - "rpc" (default): the historian's `create_tables` RPC with `{"publisher", "definitions": [...],
      "enumerations": [...], "application", "schema_version", "fingerprint", "application_version"}` (the publisher is
      the host's name, the last four come from the declared ArchiveSchema), addressed with `external_platform` when the
      historian runs on another platform. The call is
      retried every `rpc_retry_period` seconds until it is answered or `rpc_total_timeout` has passed.
    - "pubsub": for federations without cross-platform RPC. The same payload is published under the host's
      `archive_definitions` key with a `request_id` beside it; the
      historian answers on the host's status topic (the `archive_status` key, subscribed on all platforms) and the
      answer carrying that request id is awaited for `rpc_timeout` seconds. Without an answer the request is published
      again with a new id every `rpc_retry_period` seconds, and given up after `rpc_total_timeout`.

    Both paths yield the same reply: status "success" (archive on), "partial" (archive on; records for the rejected
    tables are dropped, each table reported once) or "error" (raised; nothing is archived). The historian refuses a
    host whose archive schema version differs from the one it recorded for the application with an "error" naming
    both versions; the reply carries the version the historian holds for the application as `schema_version`, which
    is logged.

    Records are never published one by one: archive_data only queues them. Once the archive is on, the start-up backlog
    is published and a flusher greenlet publishes whatever has queued every `publish_interval` seconds, as one message
    per flush split into chunks of at most `max_batch_records` records, in order. A failed publish is logged and the
    records go back to the front of the queue for the next flush. stop() ends the flusher after a final flush.
    """
    ARCHIVE_KEY = 'archive'                      # publish key the host maps to its records topic
    DEFINITIONS_KEY = 'archive_definitions'      # publish key for the registration request (pubsub path)
    STATUS_KEY = 'archive_status'                # subscribe key for the historian's answers (pubsub path)

    class Config(DataManager.Config):
        historian_vip: str = Setting(..., min_length=1,
                                     description='VIP identity of the Application Historian.')
        registration: Literal['rpc', 'pubsub'] = Setting(
            'rpc', description='How the table definitions are registered: the historian\'s create_tables RPC, or a '
                               'definitions publication answered on the host\'s status topic (for a historian on '
                               'another platform of a federation without cross-platform RPC).')
        external_platform: str = Setting('', description='Platform the historian runs on, for the rpc path in a '
                                                         'federation; empty means the local platform.')
        rpc_timeout: float = Setting(30.0, gt=0, reconfigurable=True,
                                     description='[s] wait for one answer (an RPC reply or a status message).')
        rpc_retry_period: float = Setting(10.0, gt=0, reconfigurable=True,
                                          description='[s] between registration attempts while unanswered.')
        rpc_total_timeout: float = Setting(300.0, ge=0, reconfigurable=True,
                                           description='[s] after which an unanswered registration is given up.')
        publish_interval: float = Setting(1.0, gt=0, reconfigurable=True,
                                          description='[s] between publications of the queued archive records.')
        max_batch_records: int = Setting(1000, ge=1, reconfigurable=True,
                                         description='Largest number of records in one archive message.')
        archive_key: str = Setting('archive', min_length=1,
                                   description='Publish key the records go out under (the host maps it to a topic).')

    def __init__(self, parent, config: ConfigModel | None = None, **kwargs):
        # parent is the host agent; a TENT node passes itself positionally.
        cfg = self._configure(config, kwargs)
        super(VolttronDataManager, self).__init__(parent, config=cfg)
        self.historian_vip: str = cfg.historian_vip
        self.registration: str = cfg.registration
        self.external_platform: str = cfg.external_platform
        self.rpc_timeout: float = cfg.rpc_timeout
        self.rpc_retry_period: float = cfg.rpc_retry_period
        self.rpc_total_timeout: float = cfg.rpc_total_timeout
        self.publish_interval: float = cfg.publish_interval
        self.max_batch_records: int = cfg.max_batch_records
        self.archive_key: str = cfg.archive_key
        self.rejected_tables: dict[str, Mapping] = {}   # table name -> {"message", "differences"} from the historian
        self._dropped_tables_reported: set[str] = set()
        self._pending_request_id: str | None = None
        self._pending_reply: AsyncResult | None = None
        self._flusher = None          # greenlet publishing the queue every publish_interval once the archive is on
        self._flusher_stopped = False

    @property
    def historian_address(self) -> str:
        return f'{self.historian_vip}@{self.external_platform}' if self.external_platform else self.historian_vip

    def _call_historian(self, method: str, *args):
        kwargs = {'external_platform': self.external_platform} if self.external_platform else {}
        return self.parent.vip.rpc.call(self.historian_vip, method, *args, **kwargs).get(timeout=self.rpc_timeout)

    def registration_payload(self) -> dict:
        """What the historian is told: the publisher name the registration is filed under (and which a historian with
        publisher bindings binds to the caller), the registered table definitions, the enumerations they name, and the
        declared archive schema (application, schema_version, fingerprint, application_version)."""
        return {'publisher': self.parent.name,
                'definitions': list(self.definitions.values()),
                'enumerations': self.enumeration_definitions(),
                **self.archive_schema.payload()}

    def init_archive(self):
        """Register the host's tables with the historian, then release whatever was queued while waiting."""
        payload = self.registration_payload()  # fails here, before any attempt, when no schema is declared
        _log.info(f'Registering {len(payload["definitions"])} archive tables and {len(payload["enumerations"])} '
                  f'enumerations with historian {self.historian_address!r} by {self.registration}.')
        if self.registration == 'pubsub':
            self.parent.register_subscription(self.STATUS_KEY, self._receive_status, all_platforms=True)
        deadline = time.monotonic() + self.rpc_total_timeout
        attempt = 0
        while True:
            attempt += 1
            try:
                if self.registration == 'pubsub':
                    reply = self._request_registration_by_pubsub(payload)
                else:
                    reply = self._call_historian('create_tables', payload)
                break
            except TRANSPORT_ERRORS as error:  # unreachable peer, no federation route yet, or no reply in time
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(f'Historian {self.historian_address!r} did not answer the table registration in '
                                       f'{self.rpc_total_timeout:.0f} s ({attempt} attempts); nothing will be archived '
                                       f'in this run.') from error
                _log.error(f'Table registration with historian {self.historian_address!r} failed (attempt {attempt}: '
                           f'{error!r}); retrying in {self.rpc_retry_period:.0f} s for up to {remaining:.0f} s more.')
                gevent.sleep(self.rpc_retry_period)
        self._apply_registration_reply(reply, attempt)
        self.flush()
        self._flusher_stopped = False
        self._flusher = gevent.spawn(self._flush_periodically)

    def _request_registration_by_pubsub(self, payload: Mapping) -> Mapping:
        """Publish one registration request and wait rpc_timeout seconds for the status that answers it."""
        request_id = str(uuid4())
        self._pending_request_id = request_id
        self._pending_reply = AsyncResult()
        self.parent.publish(self.DEFINITIONS_KEY, message={'request_id': request_id, **payload})
        try:
            return self._pending_reply.get(timeout=self.rpc_timeout)
        except gevent.Timeout:
            raise TimeoutError(f'no status for request {request_id} within {self.rpc_timeout:.0f} s')
        finally:
            self._pending_request_id = None
            self._pending_reply = None

    @property
    def pending_request_id(self) -> str | None:
        return self._pending_request_id

    def _receive_status(self, time_received, subscribe_key, message):
        """Subscription callback for the historian's status topic. Only the answer to the pending request is taken."""
        if not isinstance(message, Mapping):
            _log.warning(f'Ignoring a status message that is not a mapping: {message!r}')
            return
        request_id = message.get('request_id')
        if self._pending_reply is None or request_id != self._pending_request_id:
            _log.debug(f'Ignoring status for request {request_id!r}; pending request is {self._pending_request_id!r}.')
            return
        self._pending_reply.set(message)

    def _apply_registration_reply(self, reply, attempts: int):
        if not isinstance(reply, Mapping):
            raise RuntimeError(f'Historian {self.historian_address!r} answered the table registration with '
                               f'{reply!r}, not a status mapping.')
        status = reply.get('status')
        rejected = reply.get('rejected_tables') or {}
        version = self.archive_schema.version
        if status in ('success', 'partial'):
            _log.info(f'Historian {self.historian_address!r} writes archive schema version '
                      f'{reply.get("schema_version")!r}; this host writes version {version}.')
        if status == 'success':
            _log.info(f'Historian {self.historian_address!r} registered the archive tables after {attempts} attempt(s).')
        elif status == 'partial':
            for table, detail in rejected.items():
                detail = detail if isinstance(detail, Mapping) else {'message': str(detail), 'differences': None}
                self.rejected_tables[table] = detail
                _log.error(f'Historian {self.historian_address!r} rejected archive table {table!r}: '
                           f'{detail.get("message")}; differences: {detail.get("differences")}. '
                           f'Records for this table will be dropped.')
            _log.warning(f'Historian {self.historian_address!r} registered the archive tables except '
                         f'{sorted(self.rejected_tables)} after {attempts} attempt(s).')
        elif status == 'error':
            # Includes the historian refusing this host's archive schema version; its message names both versions.
            raise RuntimeError(f'Historian {self.historian_address!r} did not register the archive tables: '
                               f'{reply.get("message")!r}; rejected tables: {dict(rejected)!r}; historian archive '
                               f'schema version {reply.get("schema_version")!r}, this host\'s {version}')
        else:
            raise RuntimeError(f'Historian {self.historian_address!r} answered the table registration with an unknown '
                               f'status {status!r}: {reply!r}')
        self.archive_ready = True

    def archive_data(self, records: Mapping | Sequence | None = None):
        """Queue records for the flusher. Records for tables the historian rejected are dropped, with one error per
        table. Nothing is published here."""
        if records is None:
            return
        self.archival_queue.extend(r for r in ([records] if isinstance(records, Mapping) else records)
                                   if self._accepted(r))

    def _accepted(self, record) -> bool:
        """False for a record addressed to a table the historian rejected; the first drop per table is an error log."""
        table = record.get('table_name') if isinstance(record, Mapping) else None
        if table not in self.rejected_tables:
            return True
        if table not in self._dropped_tables_reported:
            self._dropped_tables_reported.add(table)
            _log.error(f'Dropping records for archive table {table!r}, which historian {self.historian_address!r} '
                       f'rejected: {self.rejected_tables[table].get("message")}')
        return False

    def flush(self):
        """Publish everything queued, in order, as messages of at most max_batch_records records each. Nothing goes out
        before the historian has the tables. If a publish fails, its records and the rest return to the front of the
        queue for the next flush; the failure is logged, not raised."""
        if not self.archive_ready or not self.archival_queue:
            return
        # Records queued before the historian answered are filtered here; later ones were filtered as they arrived.
        pending = [r for r in self.archival_queue if self._accepted(r)]
        self.archival_queue.clear()
        if not pending:
            return
        _log.debug(f'Publishing {len(pending)} archive records in chunks of {self.max_batch_records}.')
        for start in range(0, len(pending), self.max_batch_records):
            chunk = pending[start:start + self.max_batch_records]
            try:
                # Infinities and NaN become {"$float": ...} tokens: standard JSON carries them, the historian decodes.
                self.parent.publish(self.archive_key, message=encode_non_finite(chunk))
            except (TypeError, ValueError):
                # A record that still cannot be serialised (a value of a type no encoder knows) would fail on every
                # flush; requeueing it would stop the archive for good. Drop the chunk, naming its records, and go on.
                named = [f'{r.get("table_name")}({r.get("data", {}).get("id")!r})' if isinstance(r, Mapping) else repr(r)
                         for r in chunk]
                _log.exception(f'{len(chunk)} archive records cannot be serialised and are dropped: {named}')
            except Exception:
                _log.exception(f'Publishing {len(chunk)} archive records failed; they are requeued for the next flush.')
                self.archival_queue.extendleft(reversed(pending[start:]))
                return

    def _flush_periodically(self):
        while not self._flusher_stopped:
            gevent.sleep(self.publish_interval)
            self.flush()

    def stop(self):
        """End the periodic flushing and publish what is still queued. Called when the host is torn down."""
        self._flusher_stopped = True
        flusher, self._flusher = self._flusher, None
        if flusher is not None:
            flusher.kill()
        self.flush()
