"""nLab-to-Chinese translation pipeline with a lazily loaded public API.

Public API:
    scan_and_sync_catalog()          reconcile the nLab source tree with the state db
    audit_and_populate_dictionary()  interactive manual dictionary review
    generate_tree_and_extract_pending()  render category tree + dump pending list
    run_translation()                translate pending entries into Quartz pages
    StateStore                       SQLite state database
"""

__all__ = [
    "StateStore",
    "scan_and_sync_catalog",
    "audit_and_populate_dictionary",
    "generate_tree_and_extract_pending",
    "run_translation",
]


def __getattr__(name: str):
    # Importing the package itself must not create log directories or resolve
    # environment configuration.  This also lets the console wrapper turn an
    # invalid configuration into a concise exit-code-4 error.
    if name == "StateStore":
        from .state import StateStore

        return StateStore
    if name == "run_translation":
        from .processor import run_translation

        return run_translation
    if name in {
        "scan_and_sync_catalog",
        "audit_and_populate_dictionary",
        "generate_tree_and_extract_pending",
    }:
        from . import catalog

        return getattr(catalog, name)
    raise AttributeError(name)
