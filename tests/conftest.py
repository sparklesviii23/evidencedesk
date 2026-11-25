import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def home(monkeypatch):
    d = tempfile.mkdtemp(prefix="edtest")
    monkeypatch.setenv("EVIDENCEDESK_HOME", d)
    monkeypatch.setenv("EVIDENCEDESK_NO_KEYRING", "1")
    from evidencedesk import credentials, service
    credentials._cache.clear()
    from evidencedesk.db import DB
    service.set_db(DB(Path(d) / "t.db"))
    yield Path(d)
    service.set_db(None)


def rec(source, sid, **kw):
    from evidencedesk.adapters.base import empty_record
    r = empty_record(source, sid)
    r.update(kw)
    return r
