import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, time, timedelta, timezone
from uuid import UUID

from pydantic import field_validator
from sqlalchemy import create_engine, select, Interval
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.schema import sort_tables
from sqlalchemy.sql.sqltypes import DateTime, Uuid, Boolean, Float, Time

from archivable.config import ConfigModel, Setting
from archivable.data_manager import DataManager
from archivable.encoding import decode_non_finite
from archivable.helpers import parse_timedelta, require_aware
from archivable.reader import ArchiveReader
from archivable.schema import ARCHIVE_SCHEMA_TABLE, archive_schema_definition

_log = logging.getLogger(__name__)


class DatabaseParameters(ConfigModel):
    """The ``db_params`` block of a LocalDataManager."""
    url: str = Setting(..., min_length=1,
                       description='SQLAlchemy URL of the archive database, such as "sqlite:////home/user/db/node.sqlite" '
                                   'or "postgresql://user:password@host/db". The archive location is always configured: '
                                   'a default relative to the working directory put it wherever the host was started.')


class LocalDataManager(ArchiveReader, DataManager):
    """Archives to a SQLAlchemy database (SQLite, PostgreSQL, ...) named by db_params.url."""
    class Config(DataManager.Config):
        db_params: DatabaseParameters = Setting(..., description='The database connection: url.')

        @field_validator('db_params', mode='before')
        @classmethod
        def _needs_url(cls, value):
            if value is None or (isinstance(value, Mapping) and not value.get('url')):
                raise ValueError('LocalDataManager needs db_params["url"], a SQLAlchemy URL such as '
                                 '"sqlite:////home/user/db/node.sqlite" or "postgresql://user:password@host/db".')
            return value

    def __init__(self, parent, db_params: Mapping | None = None, config: ConfigModel | None = None, **kwargs):
        if config is None:
            kwargs = {**kwargs, 'db_params': db_params}
        cfg = self._configure(config, kwargs)
        super(LocalDataManager, self).__init__(parent, config=cfg)
        self.db_params = cfg.db_params.model_dump()
        self.engine = create_engine(cfg.db_params.url)

    def init_archive(self):
        """
        Create the registered tables that are missing and release the queued records. Before creating anything, the
        archive schema of the declared application is checked against the database: an application that is new to the
        database (none of the registered tables exists yet, no archive_schema row for it) gets its archive_schema row;
        one whose row has the same version is accepted; one with another version, or one that has registered tables
        but no archive_schema row (version 1), raises ArchiveSchemaMismatch naming both versions. Nothing is migrated.
        Called again after more tables are registered, it creates them and checks the row again. A multi-application
        host (declare_multi_application) creates its tables and checks nothing: it registers each application's schema
        as the application arrives (register_application).
        """
        if self.read_only:
            raise RuntimeError('This data manager was opened for reading (LocalDataManager.from_url); it does not '
                               'create tables.')
        schema = None if self.hosts_applications else self.archive_schema  # neither declared: fails here
        if ARCHIVE_SCHEMA_TABLE not in self.tables:
            self.add_tables([archive_schema_definition()])
        new_application = False
        if schema is not None:
            present = self.present_tables
            stored = self.archive_schema_record(schema.application)
            new_application = stored is None and not (present & (set(self.tables) - {ARCHIVE_SCHEMA_TABLE}))
            if not new_application:
                schema.check(stored, where=self._where(schema.application))
        self.registry.metadata.create_all(self.engine, checkfirst=True)
        if new_application:
            self._write_schema_row(schema)
        self.archive_ready = True
        self.archive_data()

    def _where(self, application: str) -> str:
        return f'Archive {self.redacted_url!r} (application {application!r})'

    def _write_schema_row(self, schema):
        row = schema.row()
        with Session(self.engine) as session:
            session.add(self.orm[ARCHIVE_SCHEMA_TABLE](**row))
            session.commit()
        _log.info(f'Archive {self.redacted_url!r} records archive schema version {row["version"]} for {schema.writer} '
                  f'(fingerprint {row["fingerprint"][:12]}).')

    def register_application(self, schema) -> str:
        """
        For a multi-application host: record or check the archive schema of an application that registers its tables.
        No row for the application: its row is written and 'new' returned. A row of the same version: 'known' (another
        fingerprint at the same version is a warning naming the application). A row of another version raises
        ArchiveSchemaMismatch naming the application and both versions; nothing else in the database is touched. The
        archive must be open (init_archive).
        """
        if not self.hosts_applications:
            raise RuntimeError(f'{type(self).__name__} is the data manager of one application ('
                               f'{self.archive_schema.application if self.schema_declared else "undeclared"}); only a '
                               f'multi-application host registers applications.')
        if not self.archive_ready or self.read_only:
            raise RuntimeError('register_application needs an open, writable archive: call init_archive first.')
        stored = self.archive_schema_record(schema.application)
        if stored is None:
            self._write_schema_row(schema)
            return 'new'
        schema.check(stored, where=self._where(schema.application))
        return 'known'

    @property
    def redacted_url(self) -> str:
        return self.engine.url.render_as_string(hide_password=True)

    def archive_data(self, records: Mapping | Sequence = None):
        if self.read_only:
            raise RuntimeError('This data manager was opened for reading (LocalDataManager.from_url); it does not archive.')
        records = records if records else []
        records = [records] if isinstance(records, Mapping) else records
        if not self.archive_ready:
            self.archival_queue.extend(records)
            if (len(self.archival_queue) > self.archival_buffer_length) and not self._queue_warned:
                self._queue_warned = True
                _log.warning(f'{len(self.archival_queue)} archive records are queued and the archive is not ready; '
                             f'nothing is dropped, but check that init_archive() is reached.')
            return
        # Drain the queue oldest-first (self-references point backwards in time), then insert table by table in
        # foreign-key dependency order. The ORM only orders INSERTs across mappers when relationships are declared,
        # and these imperative mappings declare none, so the order is made explicit here.
        pending = []
        while self.archival_queue:
            pending.append(self.archival_queue.popleft())
        pending.extend(records)
        # Normalise every record before touching the database, so a bad record is quarantined by itself (logged with its
        # table and key) and cannot take the whole batch down with it.
        by_table: dict[str, list] = {}
        for rec in pending:
            table_name = rec.get('table_name')
            if table_name not in self.orm:
                _log.warning(f'Attempted to archive data to non-existent table: {table_name}')
                continue
            try:
                row = self._normalize_data(table_name, rec['data'])
            except Exception as error:  # a record the host built wrongly; the rest of the batch must still be written
                _log.error(f'Quarantined an archive record for {table_name} ({rec.get("data", {}).get("id")!r}): {error}')
                continue
            by_table.setdefault(table_name, []).append(row)
        if not by_table:
            return
        # Records name their leaf table, and joined inheritance writes the ancestor rows as part of that insert. A
        # table must therefore be flushed before anything that references any table in its inheritance chain: rank
        # each leaf by the earliest position of its chain in the dependency sort.
        position = {table.name: i for i, table in enumerate(sort_tables(self.registry.metadata.tables.values()))}

        def rank(name):
            chain = self.table_chain(name) if name in self.definitions else [name]
            return (min(position.get(t, len(position)) for t in chain), position.get(name, len(position)))

        order = sorted(by_table, key=rank)
        ordered_rows = [(table_name, row) for table_name in order for row in by_table[table_name]]
        try:
            with Session(self.engine) as session:
                for table_name, row in ordered_rows:
                    # The archive is append-only: rows are inserted, never updated. Rows whose state evolves carry a
                    # record time in their primary key.
                    session.add(self.orm[table_name](**row))
                session.commit()
        except IntegrityError as error:
            # One row violates a constraint (a duplicate key, a dangling reference). Write the batch row by row inside
            # savepoints so every valid row is kept and only the offenders are quarantined.
            _log.warning(f'Archive batch rejected ({error.orig}); writing row by row.')
            with Session(self.engine) as session:
                for table_name, row in ordered_rows:
                    try:
                        with session.begin_nested():
                            session.add(self.orm[table_name](**row))
                            session.flush()
                    except IntegrityError as row_error:
                        _log.error(f'Quarantined an archive row for {table_name} ({row.get("id")!r}): {row_error.orig}')
                session.commit()
        except SQLAlchemyError as error:
            # The database itself is unavailable or broken: keep every record for the next attempt and say so.
            self.archival_queue.extendleft(reversed(pending))
            _log.error(f'Archive write failed; {len(pending)} records re-queued: {error}')
            raise

    def upsert_data(self, table_name: str, data: Mapping, match_columns: Sequence[str]) -> dict:
        """Insert a row or update the row identified by match_columns."""
        table_orm = self._get_orm(table_name)
        normalized = self._normalize_data(table_name, data)
        filters = {column: normalized[column] for column in match_columns}

        with Session(self.engine) as session:
            record = session.execute(select(table_orm).filter_by(**filters)).scalar_one_or_none()
            if record is None:
                record = table_orm(**normalized)
                session.add(record)
            else:
                for key, value in normalized.items():
                    setattr(record, key, value)
            session.commit()
            session.refresh(record)
            return self._record_to_dict(table_name, record)

    def query_data(self, table_name: str, conditions: Mapping = None, order_by: Sequence = None,
                   limit: int = None, offset: int = None) -> list[dict]:
        """Query a registered table using simple equality and range conditions."""
        table_orm = self._get_orm(table_name)
        table = self.tables[table_name]
        statement = select(table_orm)

        for column_name, condition in (conditions or {}).items():
            column = table.c[column_name]
            if isinstance(condition, Mapping):
                for operator, value in condition.items():
                    value = self._normalize_value(column, value)
                    comparisons = {
                        'eq': lambda: column == value,
                        'ne': lambda: column != value,
                        'gt': lambda: column > value,
                        'gte': lambda: column >= value,
                        'lt': lambda: column < value,
                        'lte': lambda: column <= value,
                    }
                    try:
                        statement = statement.where(comparisons[operator]())
                    except KeyError as exc:
                        raise ValueError(f'Unsupported query operator: {operator}') from exc
            else:
                statement = statement.where(column == self._normalize_value(column, condition))

        for item in order_by or []:
            column_name, direction = item if isinstance(item, (list, tuple)) else (item, 'asc')
            column = table.c[column_name]
            statement = statement.order_by(column.desc() if str(direction).lower() == 'desc' else column.asc())
        if offset:
            statement = statement.offset(offset)
        if limit is not None:
            statement = statement.limit(limit)

        with Session(self.engine) as session:
            return [self._record_to_dict(table_name, row) for row in session.scalars(statement)]

    def _get_orm(self, table_name: str):
        table_orm = self.orm.get(table_name)
        if table_orm is None:
            raise KeyError(f'Unknown table: {table_name}')
        return table_orm

    def _normalize_data(self, table_name: str, data: Mapping) -> dict:
        orm = self._get_orm(table_name)
        return {
            key: self._normalize_value(getattr(orm, key), value)
            for key, value in data.items()
        }

    @staticmethod
    def _normalize_value(column, value):
        if value is None:
            return None
        value = decode_non_finite(value)  # {"$float": "inf" | "-inf" | "nan"}: a non-finite float that travelled as JSON
        if isinstance(column.type, Boolean):
            if isinstance(value, bool):
                return value
            if value in ['T', 'True', 't', 'true', 'TRUE', '1', '1.0', 1, 1.0, 'y', 'Y', 'yes', 'Yes', 'YES']:
                return True
            if value in ['F', 'False', 'FALSE', 'f', 'false', '0', '0.0', 0, 0.0, 'no', 'No', 'NO']:
                return False
            raise ValueError(f'{column.name}: {value!r} is not a recognised Boolean value.')
        if isinstance(column.type, Float) and isinstance(value, bool):
            # Boolean-valued measurements may share a Float column with numbers. PostgreSQL will not coerce bool to
            # double precision.
            return 1.0 if value else 0.0
        if isinstance(column.type, Time) and isinstance(value, str):
            return time.fromisoformat(value.replace('Z', '+00:00'))
        if isinstance(column.type, DateTime):
            if isinstance(value, str):
                value = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if isinstance(value, datetime):
                # One convention in the archive: UTC. A naive value would be read back as UTC by SQLite and as the
                # session zone by PostgreSQL, so it is refused rather than guessed at.
                return require_aware(value, f'{column.name}').astimezone(timezone.utc)
            return value
        if isinstance(column.type, Uuid) and isinstance(value, str):
            return UUID(value)
        if isinstance(column.type, Interval) and not isinstance(value, timedelta):
            return parse_timedelta(value)  # seconds
        return value

    def _record_to_dict(self, table_name: str, record) -> dict:
        return self.decode_row({column.name: getattr(record, column.name) for column in self.tables[table_name].columns},
                               self.tables[table_name].columns)
