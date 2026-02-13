from __future__ import annotations

import dupcanon.database as database_module
from dupcanon.database import Database, _vector_literal


def test_database_connect_disables_prepare_for_pooler(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_connect(conninfo: str, **kwargs: object) -> object:
        captured["conninfo"] = conninfo
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(database_module, "connect", fake_connect)

    db = Database("postgresql://example/db")
    _ = db._connect()

    assert captured["conninfo"] == "postgresql://example/db"
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs.get("prepare_threshold") is None


def test_vector_literal_serialization() -> None:
    literal = _vector_literal([0.1, 0.2, 0.3])

    assert literal == "[0.1,0.2,0.3]"
