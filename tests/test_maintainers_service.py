from __future__ import annotations

import dupcanon.maintainers_service as maintainers_service
from dupcanon.logging_config import get_logger


def test_run_maintainers_returns_sorted_logins(monkeypatch) -> None:
    class FakeGitHubClient:
        def fetch_maintainers(self, *, repo):
            return {"zeta", "Alpha", "bravo"}

    monkeypatch.setattr(maintainers_service, "GitHubClient", FakeGitHubClient)

    maintainers = maintainers_service.run_maintainers(
        repo_value="org/repo",
        logger=get_logger("test"),
    )

    assert maintainers == ["Alpha", "bravo", "zeta"]
