"""The base of every object that writes rows of itself into the archive."""
import abc
import logging

from datetime import datetime, timezone
from uuid6 import uuid7, UUID

_log = logging.getLogger(__name__)


class Archivable(abc.ABC):
    """
    An object with an identity (a uuid7 ``id``), a creation time and a row in one archive table (``table_name``).
    ``archive()`` hands its record ({"table_name", "data"}) to its ``data_manager``; a subclass says what the row holds
    in ``get_record`` and declares the tables it writes in ``get_archive_definitions``.
    """

    def __init__(self, id: UUID | None = None, real_time_created: datetime | None = None):
        # An existing identity may be adopted (an object rebuilt from a changed configuration keeps its run); by
        # default every archivable is new.
        self.__id: UUID = id if id is not None else uuid7()
        self.__real_time_created: datetime = (real_time_created if real_time_created is not None
                                              else datetime.now(timezone.utc).astimezone())
        self.__last_archived_record: dict | None = None

    @property
    def id(self) -> UUID:
        return self.__id

    @property
    def real_time_created(self) -> datetime:
        return self.__real_time_created

    @property
    def archived(self) -> bool:
        """True once archive() has been called at least once (objects that must precede their children archive
        themselves in __init__)."""
        return self.__last_archived_record is not None

    @property
    @abc.abstractmethod
    def data_manager(self):
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def table_name(self):
        raise NotImplementedError

    def archive_record(self) -> dict:
        """This object's own archive record ({'table_name', 'data'}), marking it as archived. Used by archive() and by
        owners that write a dependent's row in the same unit of work as their own."""
        base_record = {
            'table_name': self.table_name,
            'data': self.get_record()
        }
        # Warn if this exact record (same id, same data) was already archived.
        if self.__last_archived_record is not None and base_record == self.__last_archived_record:
            _log.warning(f'Duplicate archive: {self.table_name} (id={self.id}) archived with identical data.')
        self.__last_archived_record = base_record
        return base_record

    def archive(self, records: list[dict] = None):
        records = records if records else []
        records.append(self.archive_record())

        try:
            self.data_manager.archive_data(records)
        except Exception:
            _log.exception(f'Failed to archive {self.table_name} (id={self.id}).')

    @abc.abstractmethod
    def get_record(self) -> dict:
        return {
            "id": self.id
        }

    @classmethod
    @abc.abstractmethod
    def get_archive_definitions(cls) -> list[dict]:
        return []
