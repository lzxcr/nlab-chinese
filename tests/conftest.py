import os
import tempfile

import pytest

# Isolate every pipeline default path before any nlab_pipeline import:
# state db, glossaries, content dir, logs all live in a throwaway directory.
_TMP = tempfile.mkdtemp(prefix="nlab-pipeline-test-")
os.environ.setdefault("NLAB_DIR", os.path.join(_TMP, "nlab"))
os.environ.setdefault("QUARTZ_CONTENT_DIR", os.path.join(_TMP, "content"))
os.environ.setdefault("STATE_FILE", os.path.join(_TMP, "state", "state.db"))
os.environ.setdefault("GLOSSARY_FILE", os.path.join(_TMP, "glossary.json"))
os.environ.setdefault("DICTIONARY_FILE", os.path.join(_TMP, "dictionary.json"))
os.environ.setdefault("EXTERNAL_GLOSSARY_FILE", os.path.join(_TMP, "external_glossary.json"))
os.environ.setdefault("NON_TRANSLATABLE_FILE", os.path.join(_TMP, "non_translatable.json"))
os.environ.setdefault("PENDING_REVIEW_FILE", os.path.join(_TMP, "pending.json"))
os.environ.setdefault("LOG_DIR", os.path.join(_TMP, "logs"))


@pytest.fixture()
def store(tmp_path):
    from nlab_pipeline.state import StateStore

    db = tmp_path / "state.db"
    s = StateStore(db)
    s.init_schema()
    yield s
    s.close()
