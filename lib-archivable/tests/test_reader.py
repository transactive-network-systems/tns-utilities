"""ArchiveReader: open an existing archive without a host; reflection, typed rows, inheritance chains."""
import logging
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from uuid6 import uuid7

from archivable import LocalDataManager, enumeration_definition
from tests.conftest import DEFINITIONS, ENUMERATIONS, Colour, Mood, application_schema


@pytest.fixture
def archive(make_manager, url):
    dm = make_manager()
    plain, painted = uuid7(), uuid7()
    dm.archive_data([
        {'table_name': 'item', 'data': {'id': plain, 'kind': 'plain', 'colour': Colour.Red,
                                        'created': datetime(2026, 1, 1, tzinfo=timezone.utc)}},
        {'table_name': 'painted_item', 'data': {'id': painted, 'kind': 'painted', 'coats': 3, 'colour': Colour.Blue}},
        {'table_name': 'event', 'data': {'item_id': painted, 'mood': Mood.Calm,
                                         'when': datetime(2026, 1, 2, tzinfo=timezone.utc)}},
    ])
    with dm.engine.begin() as connection:
        connection.execute(text('CREATE TABLE agent_extra (id INTEGER PRIMARY KEY, note VARCHAR(20))'))
        connection.execute(text("INSERT INTO agent_extra VALUES (1, 'hello')"))
        connection.execute(text('CREATE TABLE keyless (note VARCHAR(20))'))
    return {'url': url, 'plain': plain, 'painted': painted}


def test_from_url_registers_known_tables_reflects_the_rest_and_is_read_only(archive):
    reader = LocalDataManager.from_url(archive['url'], DEFINITIONS, ENUMERATIONS, schema=application_schema())
    assert reader.read_only and reader.schema_version == 2
    assert {'item', 'painted_item', 'event', 'archive_schema', 'agent_extra', 'keyless'} <= set(reader.tables)
    assert reader.execute(reader.select('agent_extra')) == [{'id': 1, 'note': 'hello'}]
    assert 'keyless' not in reader.orm, 'no primary key: Core access only'
    assert reader.has_table('item') and not reader.has_table('no_such_table')
    with pytest.raises(RuntimeError, match='opened for reading'):
        reader.archive_data({'table_name': 'item', 'data': {}})
    with pytest.raises(RuntimeError, match='opened for reading'):
        reader.init_archive()
    rows = reader.execute(reader.select('item'))
    assert {r['colour'] for r in rows} == {Colour.Red, Colour.Blue} and all(
        r['created'] is None or r['created'].tzinfo == timezone.utc for r in rows)


def test_from_url_with_wire_enumerations_decodes_by_name(archive):
    reader = LocalDataManager.from_url(archive['url'], DEFINITIONS, [enumeration_definition(e) for e in ENUMERATIONS])
    assert sorted(r['colour'].name for r in reader.execute(reader.select('item'))) == ['Blue', 'Red']


def test_reflect_only_a_subset(archive):
    reader = LocalDataManager.from_url(archive['url'], DEFINITIONS, ENUMERATIONS, reflect_unknown=False)
    assert 'agent_extra' not in reader.tables
    assert reader.reflect_unknown_tables(only=['agent_extra']) == ['agent_extra']
    assert reader.reflect_unknown_tables(only=['agent_extra']) == []
    assert 'keyless' not in reader.tables
    with pytest.raises(Exception):
        reader.reflect_unknown_tables(only=['no_such_table'])


def test_another_version_is_opened_with_a_warning(archive, caplog):
    with caplog.at_level(logging.WARNING, logger='archivable.reader'):
        reader = LocalDataManager.from_url(archive['url'], DEFINITIONS, ENUMERATIONS, schema=application_schema(3))
    assert reader.schema_version == 2
    assert 'archive schema version 2' in caplog.text and 'writes version 3' in caplog.text
    with LocalDataManager.from_url(archive['url'], DEFINITIONS, ENUMERATIONS).engine.begin() as connection:
        connection.execute(text('DROP TABLE archive_schema'))
    old = LocalDataManager.from_url(archive['url'], DEFINITIONS, ENUMERATIONS)
    assert old.schema_version is None and old.archive_schema_record() is None


def test_inheritance_chains_and_typed_rows(archive):
    reader = LocalDataManager.from_url(archive['url'], DEFINITIONS, ENUMERATIONS)
    assert reader.table_chain('painted_item') == ['painted_item', 'item']
    assert reader.tables_inheriting('item') == ['painted_item']
    rows = {r['id']: r for r in reader.typed_rows('item')}
    assert rows[archive['plain']]['type'] == 'item' and 'coats' not in rows[archive['plain']]
    assert rows[archive['painted']]['type'] == 'painted_item' and rows[archive['painted']]['coats'] == 3
    assert reader.typed_rows('item', {'kind': 'painted'}) == [rows[archive['painted']]]
