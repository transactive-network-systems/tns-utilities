"""
The DataManager base: where an application's archive records go.

A data manager is configured by its host (a transactive node, a historian, an agent) and then told, in this order,
what it will write: ``declare_schema`` (the application's ArchiveSchema), ``register_enumerations`` (the Enum classes
or wire definitions its tables use), ``add_tables`` (the table definitions), and finally ``init_archive`` which opens
the archive and releases the records queued so far. ``archive_data`` takes {"table_name", "data"} records at any time;
before the archive is ready they queue.
"""
import abc
import logging
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from enum import Enum

from sqlalchemy import Table
from sqlalchemy.orm import registry

from archivable.config import ConfigModel, Configurable, Setting
from archivable.definitions import definition_differences, parse_table_definition
from archivable.enumerations import EnumerationRegistry
from archivable.schema import ArchiveSchema

_log = logging.getLogger(__name__)


class DataManager(Configurable):
    """Configured from its host's ``data_manager`` block (class_name and the settings of the class's Config); the host
    passes itself as `parent`."""
    class Config(ConfigModel):
        archival_buffer_length: int = Setting(10000, ge=1,
                                              description='Number of queued records above which a warning is logged '
                                                          'while the archive is not ready.')

    def __init__(self, parent, config: ConfigModel | None = None, **kwargs):
        cfg = self._configure(config, kwargs)
        self.parent = parent

        self.archive_ready = False
        # Unbounded: a bounded deque silently dropped the oldest records. The length is a warning threshold instead.
        self.archival_queue = deque()
        self.archival_buffer_length = int(cfg.archival_buffer_length)
        self._queue_warned = False
        self.definitions: dict[str, dict] = {}
        self.enumerations = EnumerationRegistry()
        self.orm: dict = {}
        self.registry = registry()
        self.tables: dict[str, Table] = {}
        self._archive_schema: ArchiveSchema | None = None

    # ---------------------------------------------------------------- what the host declares
    def declare_schema(self, schema: ArchiveSchema) -> None:
        """Declare the application's archive schema (version, fingerprint, name). Required before init_archive on a
        manager that owns or reaches a database; declaring a different schema afterwards is an error."""
        if not isinstance(schema, ArchiveSchema):
            raise TypeError(f'declare_schema needs an ArchiveSchema, not {type(schema).__name__}.')
        if self._archive_schema is not None and self._archive_schema != schema:
            raise ValueError(f'The archive schema is already declared as {self._archive_schema}; it cannot change to '
                             f'{schema} on a running data manager.')
        self._archive_schema = schema

    @property
    def archive_schema(self) -> ArchiveSchema:
        """The declared archive schema; a RuntimeError when the host has not declared one."""
        if self._archive_schema is None:
            raise RuntimeError(f'{type(self).__name__} has no archive schema: the host must call '
                               f'declare_schema(ArchiveSchema(...)) before the archive is opened or registered.')
        return self._archive_schema

    @property
    def schema_declared(self) -> bool:
        return self._archive_schema is not None

    def register_enumerations(self, enumerations: Iterable[type[Enum] | Mapping]) -> None:
        """Register the enumerations column definitions may name: Enum classes, or wire definitions
        ({"name", "members"}) for enumerations received from another application."""
        for enumeration in enumerations:
            if isinstance(enumeration, Mapping):
                self.enumerations.register_definition(enumeration)
            else:
                self.enumerations.register(enumeration)

    def enumeration_definitions(self) -> list[dict]:
        """The registered enumerations in wire form, for a registration payload."""
        return self.enumerations.definitions()

    def add_table(self, key: str, definitions: list[dict]):
        definition = next(filter(lambda d: d['name'] == key, definitions))
        if key in self.tables:
            # Every class registers the tables it shares with its ancestors, so the same definition arrives many
            # times. A different definition under a known name would silently archive into the first table
            # registered; it is refused instead.
            differences = definition_differences(self.definitions[key], definition)
            if differences:
                raise ValueError(f'Table {key!r} is already registered with a different definition: '
                                 f'{"; ".join(differences)}')
        else:
            self.definitions[key] = definition
            self.tables[key] = parse_table_definition(definition, self.registry.metadata, self.enumerations)
        if key not in self.orm:
            parent_name = definition.get('inherits')
            parent_orm = self.orm.get(parent_name)
            if parent_name and not parent_orm:
                self.add_table(parent_name, definitions)
                parent_orm = self.orm.get(parent_name)
            orm_name = ''.join([word.title() for word in key.split('_')] + ['Table'])
            bases = (parent_orm,) if parent_orm else ()
            self.orm[key] = type(orm_name, bases, {})
            mapping_params = {'inherits': parent_orm, 'polymorphic_identity': key} if parent_orm else {}
            self.registry.map_imperatively(self.orm[key],
                                           self.tables[key],
                                           **mapping_params)

    def add_tables(self, definitions: list[dict]):
        for definition in definitions:
            self.add_table(definition['name'], definitions)

    definition_differences = staticmethod(definition_differences)

    # ---------------------------------------------------------------- the archive
    def stop(self) -> None:
        """Release what the manager holds before the host is torn down (a manager that publishes or buffers overrides
        this to flush and stop its workers). The base manager holds nothing."""
        return None

    @abc.abstractmethod
    def init_archive(self):
        pass

    @abc.abstractmethod
    def archive_data(self, records: Mapping | Sequence):
        pass
