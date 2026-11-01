"""Shared fixtures: a small application schema with an enumeration, and a factory for writable data managers."""
from enum import Enum

import pytest

from archivable import ArchiveSchema, LocalDataManager, enumeration_definitions, schema_fingerprint


class Colour(Enum):
    Red = 0
    Green = 1
    Blue = 2


class Mood(Enum):
    Calm = 'calm'
    Tense = 'tense'


ENUMERATIONS = [Colour, Mood]

ITEM = {
    'name': 'item',
    'columns': [
        {'name': 'id', 'type': 'Uuid', 'kwargs': {'primary_key': True}},
        {'name': 'created', 'type': 'DateTime'},
        {'name': 'colour', 'enumeration': 'Colour'},
        {'name': 'duration', 'type': 'Interval'},
        {'name': 'flag', 'type': 'Boolean'},
        {'name': 'kind', 'type': 'String', 'type_args': [16]},
    ],
}
PAINTED_ITEM = {
    'name': 'painted_item',
    'inherits': 'item',
    'columns': [
        {'name': 'id', 'type': 'Uuid', 'foreign_key': 'item.id', 'kwargs': {'primary_key': True}},
        {'name': 'coats', 'type': 'Integer'},
    ],
}
EVENT = {
    'name': 'event',
    'columns': [
        {'name': 'id', 'type': 'Integer', 'identity': True, 'kwargs': {'primary_key': True}},
        {'name': 'item_id', 'type': 'Uuid', 'foreign_key': 'item.id'},
        {'name': 'mood', 'enumeration': 'Mood'},
        {'name': 'when', 'type': 'DateTime'},
    ],
    'indices': [{'name': 'ix_event_item', 'columns': ['item_id']}],
}
DEFINITIONS = [ITEM, PAINTED_ITEM, EVENT]


def application_schema(version: int = 2, definitions=DEFINITIONS, enumerations=ENUMERATIONS) -> ArchiveSchema:
    return ArchiveSchema('demo', version, schema_fingerprint(definitions, enumeration_definitions(enumerations)),
                         application_version='1.2.3')


@pytest.fixture
def url(tmp_path):
    return f'sqlite:///{tmp_path / "archive.sqlite"}'


@pytest.fixture
def make_manager(url):
    """A writable LocalDataManager on the test database with the demo schema declared, enumerations registered and
    the demo tables added; `ready` runs init_archive."""
    def factory(ready=True, version=2, definitions=DEFINITIONS, database_url=None):
        dm = LocalDataManager(None, {'url': database_url or url})
        dm.declare_schema(application_schema(version, definitions))
        dm.register_enumerations(ENUMERATIONS)
        dm.add_tables(list(definitions))
        if ready:
            dm.init_archive()
        return dm
    return factory
