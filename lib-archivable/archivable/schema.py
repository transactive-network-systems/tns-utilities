"""
The archive schema version: how a database says which set of table definitions wrote it.

The library has no version of its own. The application that owns the definitions numbers them (its archive schema
version, incremented whenever a shipped definition or enumeration changes), fingerprints them
(``schema_fingerprint``) and declares both on its data manager as an ``ArchiveSchema`` before ``init_archive``. A new
database gets one row in the ``archive_schema`` table; an existing database is checked against the declared schema and
refused (``ArchiveSchemaMismatch``) when its version differs. Nothing is migrated.

A database without the table that already holds tables of the application is version 1, the unversioned past.
"""
import hashlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from archivable.definitions import canonical_definitions_json

_log = logging.getLogger(__name__)

ARCHIVE_SCHEMA_TABLE = 'archive_schema'
UNVERSIONED = 1  # the version of every database written before the archive_schema table existed


def archive_schema_definition() -> dict:
    """The table that records which archive schema a database was written with: one row, written when the database is
    created. The application includes it in its versioned definitions so a reader decodes it."""
    return {
        'name': ARCHIVE_SCHEMA_TABLE,
        'columns': [
            {'name': 'version', 'type': 'Integer', 'kwargs': {'primary_key': True}},
            {'name': 'fingerprint', 'type': 'String', 'type_args': [64]},
            {'name': 'application', 'type': 'String', 'type_args': [64]},
            {'name': 'application_version', 'type': 'String', 'type_args': [32]},
            {'name': 'created', 'type': 'DateTime'},
        ],
    }


def schema_fingerprint(definitions: Sequence[Mapping], enumerations: Sequence[Mapping] = ()) -> str:
    """SHA-256 of the canonical JSON of the table definitions and enumeration definitions. Two writers with the same
    fingerprint write the same schema; a fingerprint that changes while the version does not is a definition change
    without a version bump, which an application's recorded-fingerprint test catches."""
    return hashlib.sha256(canonical_definitions_json(definitions, enumerations).encode()).hexdigest()


class ArchiveSchemaMismatch(RuntimeError):
    """An existing database was written with another archive schema version than the declared one."""

    def __init__(self, database_version: int, expected_version: int, where: str = 'The database',
                 application: str = 'this application'):
        self.database_version = database_version
        self.expected_version = expected_version
        super().__init__(
            f'{where} was written with archive schema version {database_version}; {application} writes archive schema '
            f'version {expected_version}. Nothing is migrated. Recreate the database (point the data manager at a new '
            f'URL or delete the file) or open it read-only with a {application} whose archive schema version is '
            f'{database_version}.')


@dataclass(frozen=True)
class ArchiveSchema:
    """What an application declares about the definitions it writes: its name, the version of its archive schema, the
    fingerprint of the definitions of that version, and the application's own release version (for the record)."""
    application: str
    version: int
    fingerprint: str
    application_version: str = 'unknown'

    def __post_init__(self):
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version <= UNVERSIONED:
            raise ValueError(f'An archive schema version is an integer greater than {UNVERSIONED} (version '
                             f'{UNVERSIONED} is the unversioned past); got {self.version!r}.')
        if not self.application or not isinstance(self.application, str):
            raise ValueError(f'An archive schema names its application; got {self.application!r}.')
        if not self.fingerprint or not isinstance(self.fingerprint, str):
            raise ValueError(f'An archive schema carries the fingerprint of its definitions; got {self.fingerprint!r}.')

    @property
    def writer(self) -> str:
        return f'{self.application} {self.application_version}'

    def row(self) -> dict:
        """The archive_schema row for a new database: the version, the fingerprint, the application and its version,
        and the time of creation (UTC)."""
        return {'version': self.version, 'fingerprint': self.fingerprint, 'application': self.application,
                'application_version': self.application_version, 'created': datetime.now(timezone.utc)}

    def check(self, stored: Mapping | None, where: str = 'The database') -> None:
        """
        Compare the archive_schema row an existing database holds with this schema. `stored` is the row, or None for a
        database that predates the table (version 1). A different version raises ArchiveSchemaMismatch naming both
        versions; the same version with another fingerprint is a definition change that was never given a version and
        is logged as a warning (the archive may still disagree with the code).
        """
        database_version = UNVERSIONED if stored is None else int(stored['version'])
        if database_version != self.version:
            raise ArchiveSchemaMismatch(database_version, self.version, where, self.application)
        if stored is not None and stored.get('fingerprint') != self.fingerprint:
            _log.warning(f'{where} records archive schema version {database_version} with fingerprint '
                         f'{stored.get("fingerprint")}, but the version {self.version} definitions of {self.writer} '
                         f'have fingerprint {self.fingerprint}: a table definition or enumeration changed without a '
                         f'version bump. The tables in the database may not match what is written.')
