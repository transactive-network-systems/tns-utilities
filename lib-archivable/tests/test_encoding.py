"""Non-finite floats in records that travel as JSON: the token, the round trip, what the database keeps."""
import json
import math

import pytest
from uuid6 import uuid7

from archivable import NON_FINITE_KEY, LocalDataManager, decode_non_finite, encode_non_finite
from tests.conftest import application_schema

PRICED = {'name': 'priced', 'columns': [{'name': 'id', 'type': 'Uuid', 'kwargs': {'primary_key': True}},
                                        {'name': 'price', 'type': 'Float'}, {'name': 'cost', 'type': 'Float'},
                                        {'name': 'label', 'type': 'String', 'type_args': [16]}]}


def test_encoding_replaces_non_finite_floats_at_any_depth_and_nothing_else():
    record = {'table_name': 'vertex', 'data': {'price': math.inf, 'cost': -math.inf, 'quantity': 1.5, 'flags': [math.nan, 2],
                                               'label': 'Infinity', 'nested': {'x': math.inf}}}
    encoded = encode_non_finite([record])
    assert encoded == [{'table_name': 'vertex', 'data': {'price': {NON_FINITE_KEY: 'inf'}, 'cost': {NON_FINITE_KEY: '-inf'},
                                                         'quantity': 1.5, 'flags': [{NON_FINITE_KEY: 'nan'}, 2],
                                                         'label': 'Infinity', 'nested': {'x': {NON_FINITE_KEY: 'inf'}}}}]
    assert record['data']['price'] == math.inf, 'the original is not modified'
    json.dumps(encoded, allow_nan=False)  # standard JSON now
    with pytest.raises(ValueError):
        json.dumps([record], allow_nan=False)


def test_decoding_is_exact_and_refuses_what_is_not_a_token():
    assert decode_non_finite({NON_FINITE_KEY: 'inf'}) == math.inf
    assert decode_non_finite({NON_FINITE_KEY: '-inf'}) == -math.inf
    assert math.isnan(decode_non_finite({NON_FINITE_KEY: 'nan'}))
    assert decode_non_finite(1.5) == 1.5 and decode_non_finite('Infinity') == 'Infinity'
    assert decode_non_finite({'other': 1}) == {'other': 1}
    with pytest.raises(ValueError, match='not a non-finite float token'):
        decode_non_finite({NON_FINITE_KEY: 'infinity'})
    with pytest.raises(ValueError, match='not a non-finite float token'):
        decode_non_finite({NON_FINITE_KEY: 'inf', 'extra': 1})


def test_round_trip_through_json_into_the_database(url):
    dm = LocalDataManager(None, {'url': url})
    dm.declare_schema(application_schema(definitions=[PRICED], enumerations=[]))
    dm.add_tables([PRICED])
    dm.init_archive()
    rows = [{'table_name': 'priced', 'data': {'id': str(uuid7()), 'price': math.inf, 'cost': 0.5, 'label': 'inelastic'}},
            {'table_name': 'priced', 'data': {'id': str(uuid7()), 'price': -math.inf, 'cost': math.nan, 'label': 'odd'}},
            {'table_name': 'priced', 'data': {'id': str(uuid7()), 'price': 0.1, 'cost': 2.0, 'label': 'Infinity'}}]
    received = json.loads(json.dumps(encode_non_finite(rows), allow_nan=False))  # what the historian gets off the bus
    dm.archive_data(received)
    stored = {r['label']: r for r in dm.query_data('priced')}
    assert stored['inelastic']['price'] == math.inf and stored['inelastic']['cost'] == 0.5
    assert stored['odd']['price'] == -math.inf
    assert stored['odd']['cost'] is None, 'SQLite has no NaN and keeps NULL; PostgreSQL would keep NaN'
    assert stored['Infinity']['price'] == 0.1 and stored['Infinity']['label'] == 'Infinity', 'a text value is text'


def test_a_bad_token_quarantines_the_record_not_the_batch(url, caplog):
    dm = LocalDataManager(None, {'url': url})
    dm.declare_schema(application_schema(definitions=[PRICED], enumerations=[]))
    dm.add_tables([PRICED])
    dm.init_archive()
    dm.archive_data([{'table_name': 'priced', 'data': {'id': str(uuid7()), 'price': {NON_FINITE_KEY: 'huge'}, 'label': 'bad'}},
                     {'table_name': 'priced', 'data': {'id': str(uuid7()), 'price': 1.0, 'label': 'good'}}])
    assert [r['label'] for r in dm.query_data('priced')] == ['good']
    assert 'Quarantined' in caplog.text and 'not a non-finite float token' in caplog.text
