"""
archivable: archive records of application objects into a relational database from JSON table definitions.

The public API, by concern:

- objects that archive themselves: ``Archivable``;
- where records go: ``DataManager`` (base), ``LocalDataManager`` (SQLAlchemy database), ``NullDataManager``
  (discards), ``ArchiveReader`` (the read side mixed into LocalDataManager; ``LocalDataManager.from_url`` opens an
  existing archive);
- table definitions: ``parse_table_definition``, ``definition_differences``, ``canonical_definitions_json``;
- enumerations over the wire: ``enumeration_definition(s)``, ``enumeration_from_definition``,
  ``EnumerationRegistry``;
- the archive schema version: ``ArchiveSchema``, ``ArchiveSchemaMismatch``, ``ARCHIVE_SCHEMA_TABLE``,
  ``archive_schema_definition``, ``schema_fingerprint``, ``UNVERSIONED``;
- configuration of configurable classes: ``ConfigModel``, ``Configurable``, ``Setting``, ``ConfigurationError``,
  ``ReconfigurationError``;
- helpers: ``require_aware``, ``parse_timedelta``.
"""
from archivable.archivable import Archivable
from archivable.config import ConfigModel, Configurable, ConfigurationError, ReconfigurationError, Setting
from archivable.data_manager import DataManager
from archivable.definitions import canonical_definitions_json, definition_differences, parse_table_definition
from archivable.enumerations import (EnumerationRegistry, enumeration_definition, enumeration_definitions,
                                     enumeration_from_definition)
from archivable.helpers import parse_timedelta, require_aware
from archivable.local_data_manager import DatabaseParameters, LocalDataManager
from archivable.null_data_manager import NullDataManager
from archivable.reader import ArchiveReader
from archivable.schema import (ARCHIVE_SCHEMA_TABLE, UNVERSIONED, ArchiveSchema, ArchiveSchemaMismatch,
                               archive_schema_definition, schema_fingerprint)

__all__ = ['Archivable', 'ConfigModel', 'Configurable', 'ConfigurationError', 'ReconfigurationError', 'Setting',
           'DataManager', 'LocalDataManager', 'DatabaseParameters', 'NullDataManager', 'ArchiveReader',
           'parse_table_definition', 'definition_differences', 'canonical_definitions_json',
           'EnumerationRegistry', 'enumeration_definition', 'enumeration_definitions', 'enumeration_from_definition',
           'ARCHIVE_SCHEMA_TABLE', 'UNVERSIONED', 'ArchiveSchema', 'ArchiveSchemaMismatch',
           'archive_schema_definition', 'schema_fingerprint', 'require_aware', 'parse_timedelta']
