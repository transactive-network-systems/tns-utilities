"""Table definition parsing and comparison."""
import pytest
from sqlalchemy import Boolean, DateTime, Enum, Integer, Interval, MetaData, String, Uuid

from archivable import EnumerationRegistry, canonical_definitions_json, definition_differences, parse_table_definition
from archivable.definitions import parse_column_definition
from tests.conftest import DEFINITIONS, EVENT, ITEM, Colour, Mood, PAINTED_ITEM


@pytest.fixture
def enumerations():
    registry = EnumerationRegistry()
    registry.register(Colour)
    registry.register(Mood)
    return registry


def test_columns_take_their_sqlalchemy_types(enumerations):
    table = parse_table_definition(ITEM, MetaData(), enumerations)
    types = {c.name: type(c.type) for c in table.columns}
    assert types == {'id': Uuid, 'created': DateTime, 'colour': Enum, 'duration': Interval, 'flag': Boolean,
                     'kind': String}
    assert table.c.created.type.timezone is True, 'archive datetimes are aware'
    assert table.c.kind.type.length == 16 and table.c.id.primary_key
    assert table.c.colour.type.enums == ['Red', 'Green', 'Blue']


def test_foreign_keys_identity_indices_and_constraints(enumerations):
    metadata = MetaData()
    parse_table_definition(ITEM, metadata, enumerations)
    event = parse_table_definition(EVENT, metadata, enumerations)
    assert {fk.target_fullname for fk in event.c.item_id.foreign_keys} == {'item.id'}
    assert event.c.id.identity is not None and type(event.c.id.type) is Integer
    assert [index.name for index in event.indexes] == ['ix_event_item']
    composite = parse_table_definition({
        'name': 'link',
        'columns': [{'name': 'a', 'type': 'Integer', 'kwargs': {'primary_key': True}},
                    {'name': 'b', 'type': 'Uuid', 'identity': True}],
        'constraints': [{'name': 'fk_link', 'foreign_key': {'columns': ['b'], 'references': ['item.id']}}],
    }, metadata, enumerations)
    assert composite.c.b.identity is None, 'identity is only for integer columns'
    assert any(getattr(c, 'name', None) == 'fk_link' for c in composite.constraints)


def test_unknown_type_enumeration_and_constraint_are_errors(enumerations):
    with pytest.raises(ValueError, match="Unknown column type 'NoSuchType' in definition of column 'x'"):
        parse_column_definition({'name': 'x', 'type': 'NoSuchType'}, enumerations)
    with pytest.raises(ValueError, match="Unknown enumeration 'Size' in definition of column 'x'.*Register it"):
        parse_column_definition({'name': 'x', 'enumeration': 'Size'}, enumerations)
    with pytest.raises(ValueError, match='Unsupported table constraint'):
        parse_table_definition({'name': 't', 'columns': [{'name': 'id', 'type': 'Integer'}],
                                'constraints': [{'check': 'id > 0'}]}, MetaData(), enumerations)


def test_differences_ignore_column_order_and_report_everything_else():
    reordered = dict(ITEM, columns=list(reversed(ITEM['columns'])))
    assert definition_differences(ITEM, reordered) == []
    changed = dict(ITEM, inherits='base', columns=[c for c in ITEM['columns'] if c['name'] != 'flag']
                   + [{'name': 'kind', 'type': 'Text'}, {'name': 'extra', 'type': 'Integer'}])
    changed['columns'] = [c for c in changed['columns'] if not (c['name'] == 'kind' and 'type_args' in c)]
    differences = definition_differences(ITEM, changed)
    assert any(d.startswith("column 'flag' is missing") for d in differences)
    assert any(d.startswith("column 'extra' is new") for d in differences)
    assert any(d.startswith("column 'kind' differs") for d in differences)
    assert any(d.startswith("'inherits' differs") for d in differences)


def test_canonical_json_is_order_independent_and_covers_enumerations():
    enumerations = [{'name': 'Colour', 'members': ['Red', 'Green', 'Blue']}, {'name': 'Mood', 'members': ['Calm']}]
    shuffled = [dict(d, columns=list(reversed(d['columns']))) for d in reversed(DEFINITIONS)]
    assert canonical_definitions_json(shuffled, list(reversed(enumerations))) == \
        canonical_definitions_json(DEFINITIONS, enumerations)
    assert canonical_definitions_json(DEFINITIONS, enumerations) != canonical_definitions_json(DEFINITIONS, [])
    member_order = [{'name': 'Colour', 'members': ['Blue', 'Green', 'Red']}]
    assert canonical_definitions_json([], member_order) != canonical_definitions_json([], enumerations[:1]), \
        'member order is part of an enumeration'
    assert PAINTED_ITEM['inherits'] == 'item'
