"""Archivable: identity, the record, the duplicate warning, and the swallowed-and-logged archive failure."""
import logging
from datetime import datetime, timezone

from uuid6 import UUID

from archivable import Archivable, NullDataManager


class Thing(Archivable):
    def __init__(self, data_manager, **kwargs):
        super().__init__(**kwargs)
        self._dm = data_manager
        self.value = 1

    @property
    def data_manager(self):
        return self._dm

    @property
    def table_name(self):
        return 'thing'

    def get_record(self):
        return {'id': self.id, 'value': self.value}

    @classmethod
    def get_archive_definitions(cls):
        return [{'name': 'thing', 'columns': [{'name': 'id', 'type': 'Uuid', 'kwargs': {'primary_key': True}},
                                              {'name': 'value', 'type': 'Integer'}]}]


def test_identity_is_new_by_default_or_adopted():
    dm = NullDataManager(None)
    thing = Thing(dm)
    assert isinstance(thing.id, UUID) and thing.id.version == 7 and thing.real_time_created.tzinfo is not None
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    adopted = Thing(dm, id=thing.id, real_time_created=when)
    assert adopted.id == thing.id and adopted.real_time_created == when


def test_archive_hands_the_record_to_the_data_manager_and_marks_archived(caplog):
    dm = NullDataManager(None)
    thing = Thing(dm)
    assert not thing.archived
    thing.archive()
    assert thing.archived and dm.discarded == 1
    with caplog.at_level(logging.WARNING, logger='archivable.archivable'):
        thing.archive()
    assert 'Duplicate archive: thing' in caplog.text
    thing.value = 2
    records = [{'table_name': 'other', 'data': {}}]
    thing.archive(records)
    assert records[-1] == {'table_name': 'thing', 'data': {'id': thing.id, 'value': 2}}
    assert dm.discarded == 4


def test_a_failing_data_manager_is_logged_not_raised(caplog):
    class Broken(NullDataManager):
        def archive_data(self, records=None):
            raise RuntimeError('database on fire')
    thing = Thing(Broken(None))
    with caplog.at_level(logging.ERROR, logger='archivable.archivable'):
        thing.archive()
    assert 'Failed to archive thing' in caplog.text and thing.archived
