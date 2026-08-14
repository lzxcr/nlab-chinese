"""nLab-to-Chinese translation pipeline.

Public API:
    scan_and_sync_catalog()          reconcile the nLab source tree with the state db
    audit_and_populate_dictionary()  interactive manual dictionary review
    generate_tree_and_extract_pending()  render category tree + dump pending list
    run_translation()                translate pending entries into Quartz pages
    StateStore                       SQLite state database
"""

from .catalog import (
    audit_and_populate_dictionary,
    generate_tree_and_extract_pending,
    scan_and_sync_catalog,
)
from .processor import run_translation
from .state import StateStore

__all__ = [
    "StateStore",
    "scan_and_sync_catalog",
    "audit_and_populate_dictionary",
    "generate_tree_and_extract_pending",
    "run_translation",
]
