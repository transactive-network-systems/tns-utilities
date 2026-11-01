"""Enumerations over the wire: definitions, dynamic twins, the registry's refusals."""
from enum import Enum

import pytest

from archivable import (EnumerationRegistry, LocalDataManager, enumeration_definition, enumeration_definitions,
                        enumeration_from_definition)
from tests.conftest import Colour, Mood, application_schema


def test_definition_lists_member_names_in_order():
    assert enumeration_definition(Colour) == {'name': 'Colour', 'members': ['Red', 'Green', 'Blue']}
    assert enumeration_definitions([Colour, Mood]) == [enumeration_definition(Colour), enumeration_definition(Mood)]
    with pytest.raises(TypeError, match='Enum class'):
        enumeration_definition('Colour')


def test_a_received_definition_becomes_an_enum_decodable_by_name():
    twin = enumeration_from_definition({'name': 'Colour', 'members': ['Red', 'Green', 'Blue']})
    assert issubclass(twin, Enum) and twin.__name__ == 'Colour'
    assert [m.name for m in twin] == ['Red', 'Green', 'Blue']
    assert twin['Green'].name == Colour.Green.name


@pytest.mark.parametrize('definition, problem', [
    ({'name': 'Colour'}, 'non-empty list of member names'),
    ({'name': 'Colour', 'members': []}, 'non-empty list'),
    ({'name': 'Colour', 'members': ['Red', 'Red']}, 'twice'),
    ({'name': 'Colour', 'members': ['Red', 'not an identifier']}, 'identifiers'),
    ({'name': '1Colour', 'members': ['Red']}, 'identifier'),
    ({'name': 'Colour', 'members': ['Red'], 'values': [1]}, 'unsupported key'),
    (['Colour'], 'must be a mapping'),
])
def test_bad_definitions_are_refused(definition, problem):
    with pytest.raises(ValueError, match=problem):
        enumeration_from_definition(definition)


def test_registry_accepts_twins_and_refuses_a_different_member_list():
    registry = EnumerationRegistry()
    registry.register(Colour)
    assert registry.register_definition(enumeration_definition(Colour)) is Colour, 'the known class is kept'
    assert registry.register(Colour) is Colour and len(registry) == 1
    with pytest.raises(ValueError, match="'Colour' is already registered with members"):
        registry.register_definition({'name': 'Colour', 'members': ['Red', 'Blue']})
    with pytest.raises(KeyError, match="Unknown enumeration 'Mood'.*registered enumerations are \\['Colour'\\]"):
        registry.get('Mood')
    assert registry.definitions() == [enumeration_definition(Colour)]


def test_data_manager_resolves_columns_against_registered_enumerations_only():
    dm = LocalDataManager(None, {'url': 'sqlite://'})
    definition = {'name': 't', 'columns': [{'name': 'id', 'type': 'Integer', 'kwargs': {'primary_key': True}},
                                           {'name': 'colour', 'enumeration': 'Colour'}]}
    with pytest.raises(ValueError, match="Unknown enumeration 'Colour' in definition of column 'colour'"):
        dm.add_tables([definition])
    dm.register_enumerations([enumeration_definition(Colour)])  # as a historian receives it
    dm.add_tables([definition])
    assert dm.enumeration_definitions() == [enumeration_definition(Colour)]
    assert [e for e in dm.tables['t'].c.colour.type.enums] == ['Red', 'Green', 'Blue'], 'stored by member name'


def test_rows_decode_identically_with_the_class_and_with_its_twin(tmp_path):
    url = f'sqlite:///{tmp_path / "wire.sqlite"}'
    definition = {'name': 't', 'columns': [{'name': 'id', 'type': 'Integer', 'kwargs': {'primary_key': True}},
                                           {'name': 'colour', 'enumeration': 'Colour'}]}
    writer = LocalDataManager(None, {'url': url})
    writer.declare_schema(application_schema(definitions=[definition], enumerations=[Colour]))
    writer.register_enumerations([Colour])
    writer.add_tables([definition])
    writer.init_archive()
    writer.archive_data([{'table_name': 't', 'data': {'id': 1, 'colour': Colour.Blue}},
                         {'table_name': 't', 'data': {'id': 2, 'colour': 'Green'}}])  # the JSON form: the name

    receiver = LocalDataManager(None, {'url': url})
    receiver.declare_schema(application_schema(definitions=[definition], enumerations=[Colour]))
    receiver.register_enumerations([{'name': 'Colour', 'members': ['Red', 'Green', 'Blue']}])
    receiver.add_tables([definition])
    receiver.init_archive()
    rows = receiver.query_data('t', order_by=['id'])
    assert [r['colour'].name for r in rows] == ['Blue', 'Green']
    assert type(rows[0]['colour']) is not Colour, 'the receiver decodes with its dynamic twin'
    receiver.archive_data({'table_name': 't', 'data': {'id': 3, 'colour': 'Red'}})
    assert [r['colour'].name for r in writer.query_data('t', order_by=['id'])] == ['Blue', 'Green', 'Red']
