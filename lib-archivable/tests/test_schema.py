"""The archive schema version: the declared schema, the row a new database gets, and the refusal of other versions."""
import logging
from datetime import timezone

import pytest
from sqlalchemy import text

from archivable import (ARCHIVE_SCHEMA_TABLE, UNVERSIONED, ArchiveSchema, ArchiveSchemaMismatch, LocalDataManager,
                        archive_schema_definition, schema_fingerprint)
from tests.conftest import DEFINITIONS, ITEM, Colour, application_schema


def test_schema_validates_its_parts():
    schema = application_schema()
    assert schema.writer == 'demo 1.2.3' and len(schema.fingerprint) == 64
    with pytest.raises(ValueError, match='greater than 1'):
        ArchiveSchema('demo', UNVERSIONED, 'abc')
    with pytest.raises(ValueError, match='integer'):
        ArchiveSchema('demo', '2', 'abc')
    with pytest.raises(ValueError, match='names its application'):
        ArchiveSchema('', 2, 'abc')
    with pytest.raises(ValueError, match='fingerprint'):
        ArchiveSchema('demo', 2, '')


def test_fingerprint_changes_with_definitions_and_enumerations():
    base = schema_fingerprint(DEFINITIONS, [{'name': 'Colour', 'members': ['Red']}])
    assert base == schema_fingerprint(list(reversed(DEFINITIONS)), [{'name': 'Colour', 'members': ['Red']}])
    assert base != schema_fingerprint(DEFINITIONS[:-1], [{'name': 'Colour', 'members': ['Red']}])
    assert base != schema_fingerprint(DEFINITIONS, [{'name': 'Colour', 'members': ['Red', 'Blue']}])


def test_check_accepts_same_version_and_refuses_others(caplog):
    schema = application_schema(version=3)
    schema.check({'version': 3, 'fingerprint': schema.fingerprint})
    with pytest.raises(ArchiveSchemaMismatch, match='version 1; demo writes archive schema version 3') as error:
        schema.check(None)
    assert (error.value.database_version, error.value.expected_version) == (1, 3)
    with pytest.raises(ArchiveSchemaMismatch, match='Historian database was written with archive schema version 2'):
        schema.check({'version': 2, 'fingerprint': 'x'}, where='Historian database')
    with caplog.at_level(logging.WARNING, logger='archivable.schema'):
        schema.check({'version': 3, 'fingerprint': 'other'})
    assert 'without a version bump' in caplog.text and 'other' in caplog.text


def test_init_archive_needs_a_declared_schema(url):
    dm = LocalDataManager(None, {'url': url})
    dm.register_enumerations([Colour])
    dm.add_tables([ITEM])
    with pytest.raises(RuntimeError, match='declare_schema'):
        dm.init_archive()
    with pytest.raises(TypeError):
        dm.declare_schema({'version': 2})
    dm.declare_schema(application_schema())
    dm.declare_schema(application_schema())  # the same schema again is fine
    with pytest.raises(ValueError, match='already declared'):
        dm.declare_schema(application_schema(version=3))


def test_new_database_gets_the_row(make_manager):
    dm = make_manager()
    record = dm.archive_schema_record()
    assert record['version'] == 2 == dm.schema_version
    assert record['fingerprint'] == dm.archive_schema.fingerprint
    assert (record['application'], record['application_version']) == ('demo', '1.2.3')
    assert record['created'].tzinfo == timezone.utc
    assert dm.has_table('item') and dm.has_table('event')


def test_reopening_with_the_same_version_keeps_one_row(make_manager):
    make_manager()
    dm = make_manager(ready=False)
    dm.init_archive()  # a restart
    dm.init_archive()  # more tables registered during a run
    assert dm.execute(dm.select(ARCHIVE_SCHEMA_TABLE)) == [dm.archive_schema_record()]


def test_database_of_another_version_is_refused(make_manager):
    make_manager()
    with pytest.raises(ArchiveSchemaMismatch) as error:
        make_manager(version=3)
    message = str(error.value)
    assert 'version 2' in message and 'version 3' in message and 'Recreate the database' in message
    assert 'read-only with a demo' in message


def test_database_without_the_table_is_version_one(make_manager):
    dm = make_manager()
    with dm.engine.begin() as connection:
        connection.execute(text(f'DROP TABLE {ARCHIVE_SCHEMA_TABLE}'))
    with pytest.raises(ArchiveSchemaMismatch, match='version 1;.*version 2'):
        make_manager()


def test_other_applications_tables_do_not_make_a_database_old(make_manager):
    dm = make_manager(ready=False)
    with dm.engine.begin() as connection:
        connection.execute(text('CREATE TABLE somebody_elses (id INTEGER PRIMARY KEY)'))
    dm.init_archive()
    assert dm.schema_version == 2


def test_archive_schema_definition_is_parseable():
    dm = LocalDataManager(None, {'url': 'sqlite://'})
    dm.add_tables([archive_schema_definition()])
    assert {c.name for c in dm.tables[ARCHIVE_SCHEMA_TABLE].columns} == {'version', 'fingerprint', 'application',
                                                                           'application_version', 'created'}
    assert [c.name for c in dm.tables[ARCHIVE_SCHEMA_TABLE].primary_key.columns] == ['application'], \
        'one row per application'
