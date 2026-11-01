"""A database written into by several applications: the host declares no schema, registers each application's schema
as it arrives, and keeps one archive_schema row per application; an application's own manager finds its row."""
import logging

import pytest
from sqlalchemy import text

from archivable import ARCHIVE_SCHEMA_TABLE, ArchiveSchema, ArchiveSchemaMismatch, LocalDataManager
from tests.conftest import ENUMERATIONS, ITEM, application_schema

OTHER = ArchiveSchema('Other', 5, 'o' * 64, application_version='0.9')
OTHER_TABLE = {'name': 'other_thing', 'columns': [{'name': 'id', 'type': 'Integer', 'kwargs': {'primary_key': True}}]}


def host(url):
    dm = LocalDataManager(None, {'url': url})
    dm.declare_multi_application()
    dm.add_tables([{'name': 'host_own', 'columns': [{'name': 'id', 'type': 'Integer', 'kwargs': {'primary_key': True}}]}])
    dm.init_archive()
    return dm


def test_payload_round_trip_and_its_refusals():
    schema = application_schema()
    payload = schema.payload()
    assert set(payload) == set(ArchiveSchema.PAYLOAD_KEYS) == {'application', 'schema_version', 'fingerprint',
                                                                  'application_version'}
    assert ArchiveSchema.from_payload(payload) == schema
    assert ArchiveSchema.from_payload({'application': 'X', 'schema_version': 2, 'fingerprint': 'f'}).application_version == 'unknown'
    with pytest.raises(ValueError, match='states no application, fingerprint'):
        ArchiveSchema.from_payload({'schema_version': 2})
    with pytest.raises(ValueError, match="archive schema version '2', which is not an integer"):
        ArchiveSchema.from_payload({'application': 'X', 'schema_version': '2', 'fingerprint': 'f'})
    with pytest.raises(ValueError, match='is a mapping'):
        ArchiveSchema.from_payload(['definitions'])


def test_a_host_declares_no_schema_and_opens_the_archive_without_one(url):
    dm = host(url)
    assert dm.hosts_applications and not dm.schema_declared and dm.has_table('host_own')
    assert dm.archive_schema_records() == [] and dm.schema_version is None
    with pytest.raises(RuntimeError, match='declare_multi_application'):
        LocalDataManager(None, {'url': url}).archive_schema
    with pytest.raises(ValueError, match='hosts many applications'):
        dm.declare_schema(application_schema())
    single = LocalDataManager(None, {'url': url})
    single.declare_schema(application_schema())
    with pytest.raises(ValueError, match='already declares the schema of demo'):
        single.declare_multi_application()


def test_register_application_writes_then_knows_then_refuses(url, caplog):
    dm = host(url)
    demo = application_schema()
    assert dm.register_application(demo) == 'new'
    assert dm.register_application(demo) == 'known'
    assert dm.register_application(OTHER) == 'new'
    assert [(r['application'], r['version'], r['application_version']) for r in dm.archive_schema_records()] == \
        [('Other', 5, '0.9'), ('demo', 2, '1.2.3')]
    with caplog.at_level(logging.WARNING, logger='archivable.schema'):
        assert dm.register_application(ArchiveSchema('demo', 2, 'x' * 64)) == 'known'
    assert "(application 'demo')" in caplog.text and 'without a version bump' in caplog.text
    with pytest.raises(ArchiveSchemaMismatch) as error:
        dm.register_application(ArchiveSchema('demo', 3, 'y' * 64))
    message = str(error.value)
    assert "(application 'demo') was written with archive schema version 2; demo writes archive schema version 3" in message
    assert (error.value.database_version, error.value.expected_version) == (2, 3)
    assert len(dm.archive_schema_records()) == 2, 'the refusal touched nothing'
    with pytest.raises(ValueError, match="several applications \\(\\['Other', 'demo'\\]\\)"):
        dm.archive_schema_record()


def test_register_application_needs_a_host_with_an_open_archive(url):
    single = LocalDataManager(None, {'url': url})
    single.declare_schema(application_schema())
    with pytest.raises(RuntimeError, match='data manager of one application \\(demo\\)'):
        single.register_application(OTHER)
    unopened = LocalDataManager(None, {'url': url})
    unopened.declare_multi_application()
    with pytest.raises(RuntimeError, match='call init_archive first'):
        unopened.register_application(OTHER)


def test_an_application_manager_finds_its_own_row_in_a_shared_database(url):
    dm = host(url)
    dm.register_application(OTHER)
    dm.add_tables([OTHER_TABLE])
    dm.init_archive()
    demo = LocalDataManager(None, {'url': url})
    demo.declare_schema(application_schema())
    demo.register_enumerations(ENUMERATIONS)
    demo.add_tables([ITEM])
    demo.init_archive()  # the Other row and tables are not demo's: demo is new to this database
    assert demo.schema_version == 2 and demo.archive_schema_record()['application'] == 'demo'
    assert demo.archive_schema_record('Other')['version'] == 5
    assert {r['application'] for r in demo.archive_schema_records()} == {'Other', 'demo'}
    demo.init_archive()  # a restart finds its row
    with demo.engine.begin() as connection:
        connection.execute(text(f"UPDATE {ARCHIVE_SCHEMA_TABLE} SET version = 9 WHERE application = 'demo'"))
    again = LocalDataManager(None, {'url': url})
    again.declare_schema(application_schema())
    again.register_enumerations(ENUMERATIONS)
    again.add_tables([ITEM])
    with pytest.raises(ArchiveSchemaMismatch, match="\\(application 'demo'\\) was written with archive schema version 9"):
        again.init_archive()
    assert LocalDataManager(None, {'url': url}).archive_schema_record('Other')['version'] == 5, 'Other is untouched'


def test_an_applications_tables_without_its_row_are_version_one_even_beside_another_application(url):
    dm = host(url)
    dm.register_application(OTHER)
    with dm.engine.begin() as connection:
        connection.execute(text('CREATE TABLE item (id CHAR(32) PRIMARY KEY)'))  # demo's table, but no demo row
    demo = LocalDataManager(None, {'url': url})
    demo.declare_schema(application_schema())
    demo.register_enumerations(ENUMERATIONS)
    demo.add_tables([ITEM])
    with pytest.raises(ArchiveSchemaMismatch, match='version 1; demo writes archive schema version 2'):
        demo.init_archive()
