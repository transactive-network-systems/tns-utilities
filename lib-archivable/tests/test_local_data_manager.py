"""LocalDataManager: configuration, queueing, writing in dependency order, normalisation, quarantine, queries."""
import logging
from collections import deque
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Column, DateTime
from sqlalchemy.exc import SQLAlchemyError
from uuid6 import uuid7

from archivable import ConfigurationError, LocalDataManager, NullDataManager, DataManager
from tests.conftest import Colour, Mood


def test_database_url_is_required():
    with pytest.raises(ValueError, match='db_params\\["url"\\]'):
        LocalDataManager(None, {})
    with pytest.raises(ValueError, match='db_params\\["url"\\]'):
        LocalDataManager(None, None)
    with pytest.raises(ConfigurationError, match="unknown configuration key 'extra'"):
        LocalDataManager(None, {'url': 'sqlite://'}, extra=1)
    dm = LocalDataManager(None, {'url': 'sqlite://'}, archival_buffer_length=3)
    assert dm.db_params == {'url': 'sqlite://'} and dm.archival_buffer_length == 3
    assert LocalDataManager.Config.reconfigurable_fields() == frozenset()


def test_records_queue_until_the_archive_is_ready_and_warn_once(caplog):
    dm = LocalDataManager(None, {'url': 'sqlite://'}, archival_buffer_length=3)
    for i in range(5):
        dm.archive_data({'table_name': 't', 'data': {'i': i}})
    dm.archive_data(None)
    dm.archive_data([])
    assert [r['data']['i'] for r in dm.archival_queue] == [0, 1, 2, 3, 4], 'nothing is dropped'
    assert sum('archive is not ready' in r.message for r in caplog.records) == 1


def test_init_archive_writes_the_backlog_before_new_records(make_manager):
    dm = make_manager(ready=False)
    first, second = uuid7(), uuid7()
    dm.archive_data({'table_name': 'item', 'data': {'id': first, 'kind': 'queued', 'colour': Colour.Red}})
    dm.init_archive()
    dm.archive_data({'table_name': 'item', 'data': {'id': second, 'kind': 'new', 'colour': 'Blue'}})
    assert dm.archival_queue == deque()
    rows = dm.query_data('item', order_by=['id'])
    assert [(r['id'], r['kind'], r['colour']) for r in rows] == [(first, 'queued', Colour.Red), (second, 'new', Colour.Blue)]


def test_dependents_are_written_after_what_they_reference_whatever_the_order_given(make_manager):
    dm = make_manager()
    item = uuid7()
    painted = uuid7()
    dm.archive_data([
        {'table_name': 'event', 'data': {'item_id': painted, 'mood': Mood.Calm, 'when': datetime.now(timezone.utc)}},
        {'table_name': 'painted_item', 'data': {'id': painted, 'kind': 'leaf', 'coats': 2, 'colour': Colour.Green}},
        {'table_name': 'item', 'data': {'id': item, 'kind': 'plain'}},
    ])
    assert len(dm.query_data('event')) == 1
    assert {r['id'] for r in dm.query_data('item')} == {item, painted}, 'joined inheritance wrote the item row too'
    assert dm.query_data('painted_item')[0]['coats'] == 2


def test_values_are_normalised_by_column_type(make_manager):
    dm = make_manager()
    ident = uuid7()
    dm.archive_data({'table_name': 'item', 'data': {
        'id': str(ident), 'created': '2026-06-09T01:00:00Z', 'duration': 90, 'flag': 'yes', 'colour': 'Red'}})
    (row,) = dm.query_data('item')
    assert row['id'] == ident and row['created'] == datetime(2026, 6, 9, 1, tzinfo=timezone.utc)
    assert row['duration'] == timedelta(seconds=90) and row['flag'] is True and row['colour'] is Colour.Red
    flag = dm.tables['item'].c.flag
    assert dm._normalize_value(flag, 0) is False and dm._normalize_value(flag, None) is None
    with pytest.raises(ValueError, match="flag: 'maybe' is not a recognised Boolean"):
        dm._normalize_value(flag, 'maybe')
    with pytest.raises(ValueError, match='created must be timezone-aware'):
        dm._normalize_value(Column('created', DateTime(timezone=True)), datetime(2026, 6, 9, 1))


def test_bad_records_are_quarantined_and_the_rest_written(make_manager, caplog):
    dm = make_manager()
    good, duplicate = uuid7(), uuid7()
    with caplog.at_level(logging.WARNING, logger='archivable.local_data_manager'):
        dm.archive_data([
            {'table_name': 'no_such_table', 'data': {'id': 1}},
            {'table_name': 'item', 'data': {'id': good, 'created': datetime(2026, 1, 1)}},  # naive: quarantined
            {'table_name': 'item', 'data': {'id': duplicate, 'kind': 'a'}},
            {'table_name': 'item', 'data': {'id': duplicate, 'kind': 'b'}},  # duplicate key: quarantined
            {'table_name': 'item', 'data': {'id': good, 'kind': 'c'}},
        ])
    rows = {r['id']: r['kind'] for r in dm.query_data('item')}
    assert rows == {duplicate: 'a', good: 'c'}
    messages = [r.getMessage() for r in caplog.records]
    assert any('non-existent table: no_such_table' in m for m in messages)
    assert sum('Quarantined an archive record' in m for m in messages) == 1
    assert sum('Quarantined an archive row' in m for m in messages) == 1


def test_an_unavailable_database_requeues_and_raises():
    dm = LocalDataManager(None, {'url': 'sqlite:////nonexistent-directory/for/sure/archive.sqlite'})
    dm.add_tables([{'name': 'plain', 'columns': [{'name': 'id', 'type': 'Integer', 'kwargs': {'primary_key': True}}]}])
    dm.archive_ready = True  # as if the archive had been opened and the database then went away
    with pytest.raises(SQLAlchemyError):
        dm.archive_data({'table_name': 'plain', 'data': {'id': 1}})
    assert [r['data']['id'] for r in dm.archival_queue] == [1], 'the record is kept for the next attempt'


def test_upsert_and_query_with_ranges_order_and_pagination(make_manager):
    dm = make_manager()
    moments = [datetime(2026, 6, 9, h, tzinfo=timezone.utc) for h in (1, 2, 3)]
    for ident, moment in enumerate(moments, start=1):
        dm.upsert_data('event', {'id': ident, 'mood': 'Tense', 'when': moment}, ['id'])
    dm.upsert_data('event', {'id': 2, 'mood': 'Calm', 'when': moments[1]}, ['id'])
    assert [r['mood'] for r in dm.query_data('event', order_by=['id'])] == [Mood.Tense, Mood.Calm, Mood.Tense]
    rows = dm.query_data('event', {'when': {'gte': '2026-06-09T01:00:00+00:00', 'lt': '2026-06-09T04:00:00+00:00'}},
                         order_by=[('when', 'desc')], limit=1, offset=1)
    assert [r['id'] for r in rows] == [2] and rows[0]['when'].tzinfo == timezone.utc
    assert dm.query_data('event', {'mood': Mood.Calm})[0]['id'] == 2
    with pytest.raises(ValueError, match='Unsupported query operator'):
        dm.query_data('event', {'id': {'between': [1, 2]}})
    with pytest.raises(KeyError, match='Unknown table'):
        dm.query_data('nothing')


def test_a_changed_redefinition_is_refused_and_an_identical_one_accepted():
    dm = LocalDataManager(None, {'url': 'sqlite://'})
    definition = {'name': 't', 'columns': [{'name': 'id', 'type': 'Integer', 'kwargs': {'primary_key': True}},
                                           {'name': 'value', 'type': 'Float'}]}
    dm.add_tables([definition])
    dm.add_tables([dict(definition, columns=list(reversed(definition['columns'])))])
    with pytest.raises(ValueError, match="Table 't' is already registered.*'extra' is new"):
        dm.add_tables([dict(definition, columns=definition['columns'] + [{'name': 'extra', 'type': 'Float'}])])
    assert DataManager.definition_differences(definition, definition) == []


def test_null_manager_discards_but_still_parses_definitions():
    dm = NullDataManager(None)
    assert isinstance(dm, DataManager) and dm.archive_ready
    dm.archive_data({'table_name': 'anything', 'data': {'id': 1}})
    dm.archive_data([{'table_name': 'anything', 'data': {'id': 2}}, {'table_name': 'other', 'data': {}}])
    dm.archive_data(None)
    dm.init_archive()
    assert dm.discarded == 3 and not dm.archival_queue
    dm.register_enumerations([Colour])
    dm.add_tables([{'name': 't', 'columns': [{'name': 'id', 'type': 'Integer', 'kwargs': {'primary_key': True}},
                                             {'name': 'c', 'enumeration': 'Colour'}]}])
    assert 't' in dm.tables
    with pytest.raises(ValueError, match='Unknown column type'):
        dm.add_tables([{'name': 'broken', 'columns': [{'name': 'x', 'type': 'NoSuchType'}]}])
    with pytest.raises(ValueError, match='Unknown enumeration'):
        dm.add_tables([{'name': 'broken2', 'columns': [{'name': 'x', 'enumeration': 'Mood'}]}])
    assert dm.stop() is None
