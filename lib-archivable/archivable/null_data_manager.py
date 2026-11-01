import logging
from collections.abc import Mapping, Sequence

from archivable.data_manager import DataManager

_log = logging.getLogger(__name__)


class NullDataManager(DataManager):
    """
    A data manager that accepts every record and keeps none. For hosts that do not archive: tests, dry runs, and
    configuration checks.

    Table definitions are still parsed into SQLAlchemy tables, so a definition that would fail against a real archive
    (an unknown column type or enumeration, a redefinition that does not match) fails here as well.
    """

    Config = DataManager.Config

    def __init__(self, parent, config=None, **kwargs):
        super(NullDataManager, self).__init__(parent, config=config, **kwargs)
        self.archive_ready = True  # nothing is ever queued
        self.discarded = 0  # records accepted so far, for diagnostics

    def init_archive(self):
        self.archive_ready = True

    def archive_data(self, records: Mapping | Sequence = None):
        if records is None:
            return
        self.discarded += 1 if isinstance(records, Mapping) else len(records)
