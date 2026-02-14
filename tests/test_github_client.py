from __future__ import annotations

from datetime import UTC, datetime

import dupcanon.github_client as github_client
from dupcanon.github_client import (
    GitHubClient,
    _extract_labels,
    _parse_datetime,
    _parse_http_status,
    _should_retry,
)
from dupcanon.models import ItemType, RepoRef, StateFilter


def test_parse_http_status_extracts_code() -> None:
    assert _parse_http_status("gh: HTTP 502 Bad Gateway") == 502


def test_parse_http_status_returns_none_when_absent() -> None:
    assert _parse_http_status("some other error") is None


def test_should_retry_rules() -> None:
    assert _should_retry(None)
    assert _should_retry(429)
    assert _should_retry(500)
    assert _should_retry(503)
    assert not _should_retry(400)


def test_extract_labels_handles_mixed_formats() -> None:
    labels = _extract_labels([{"name": "bug"}, "help wanted", {"name": ""}, 123])

    assert labels == ["bug", "help wanted"]


def test_parse_datetime_none() -> None:
    assert _parse_datetime(None) is None


def test_parse_datetime_utc() -> None:
    parsed = _parse_datetime("2026-02-13T10:00:00Z")

    assert parsed is not None
    assert parsed.tzinfo == UTC
    assert parsed.year == 2026


def test_gh_api_paginated_collect_batches_and_flushes(monkeypatch) -> None:
    class _LineStream:
        def __init__(self, lines: list[str]) -> None:
            self.lines = lines

        def __iter__(self) -> _LineStream:
            return self

        def __next__(self) -> str:
            if not self.lines:
                raise StopIteration
            return self.lines.pop(0)

    class _ErrStream:
        def read(self) -> str:
            return ""

    class _Proc:
        def __init__(self, line_count: int) -> None:
            self.stdout = _LineStream([f'{{"id":{i}}}\n' for i in range(1, line_count + 1)])
            self.stderr = _ErrStream()

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(
        github_client.subprocess,
        "Popen",
        lambda *args, **kwargs: _Proc(205),
    )

    batches: list[int] = []
    client = GitHubClient(max_attempts=1)
    rows = client._gh_api_paginated_collect(
        "repos/org/repo/issues",
        params={"state": "all"},
        row_mapper=lambda row: row,
        on_batch_count=batches.append,
    )

    assert len(rows) == 205
    assert batches == [100, 100, 5]


def test_fetch_issues_with_since_uses_server_side_created_filter(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_collect(self, path, *, params, row_mapper, jq_expression=".[]", on_batch_count=None):
        captured["path"] = path
        captured["params"] = params
        captured["jq_expression"] = jq_expression
        return []

    monkeypatch.setattr(GitHubClient, "_gh_api_paginated_collect", fake_collect)

    client = GitHubClient(max_attempts=1)
    client.fetch_issues(
        repo=RepoRef.parse("org/repo"),
        state=StateFilter.ALL,
        since=datetime(2026, 2, 13, tzinfo=UTC),
    )

    assert captured["path"] == "search/issues"
    params = captured["params"]
    assert isinstance(params, dict)
    assert "is:issue" in str(params["q"])
    assert "created:>=2026-02-13" in str(params["q"])
    assert captured["jq_expression"] == ".items[]"


def test_fetch_issues_without_since_uses_graphql_server_side_type_state(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_graphql_collect(
        self,
        *,
        query,
        variables,
        jq_expression,
        row_mapper,
        on_batch_count=None,
    ):
        captured["query"] = query
        captured["variables"] = variables
        captured["jq_expression"] = jq_expression
        return []

    monkeypatch.setattr(GitHubClient, "_gh_graphql_paginated_collect", fake_graphql_collect)

    client = GitHubClient(max_attempts=1)
    client.fetch_issues(
        repo=RepoRef.parse("org/repo"),
        state=StateFilter.OPEN,
        since=None,
    )

    assert "issues(" in str(captured["query"])
    assert "states:[OPEN]" in str(captured["query"])
    assert captured["variables"] == {"owner": "org", "name": "repo"}
    assert captured["jq_expression"] == ".data.repository.issues.nodes[]"


def test_fetch_pulls_without_since_uses_graphql_server_side_type_state(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_graphql_collect(
        self,
        *,
        query,
        variables,
        jq_expression,
        row_mapper,
        on_batch_count=None,
    ):
        captured["query"] = query
        captured["variables"] = variables
        captured["jq_expression"] = jq_expression
        return []

    monkeypatch.setattr(GitHubClient, "_gh_graphql_paginated_collect", fake_graphql_collect)

    client = GitHubClient(max_attempts=1)
    client.fetch_pulls(
        repo=RepoRef.parse("org/repo"),
        state=StateFilter.CLOSED,
        since=None,
    )

    assert "pullRequests(" in str(captured["query"])
    assert "states:[CLOSED,MERGED]" in str(captured["query"])
    assert captured["variables"] == {"owner": "org", "name": "repo"}
    assert captured["jq_expression"] == ".data.repository.pullRequests.nodes[]"


def test_fetch_pulls_with_since_uses_server_side_created_filter(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_collect(self, path, *, params, row_mapper, jq_expression=".[]", on_batch_count=None):
        captured["path"] = path
        captured["params"] = params
        captured["jq_expression"] = jq_expression
        return []

    monkeypatch.setattr(GitHubClient, "_gh_api_paginated_collect", fake_collect)

    client = GitHubClient(max_attempts=1)
    client.fetch_pulls(
        repo=RepoRef.parse("org/repo"),
        state=StateFilter.OPEN,
        since=datetime(2026, 2, 13, tzinfo=UTC),
    )

    assert captured["path"] == "search/issues"
    params = captured["params"]
    assert isinstance(params, dict)
    assert "is:pr" in str(params["q"])
    assert "is:open" in str(params["q"])
    assert "created:>=2026-02-13" in str(params["q"])
    assert captured["jq_expression"] == ".items[]"


def test_fetch_maintainers_filters_by_permissions(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_collect(self, path, *, params, row_mapper, jq_expression=".[]", on_batch_count=None):
        captured["path"] = path
        captured["params"] = params
        captured["jq_expression"] = jq_expression

        rows = [
            {"login": "alice", "permissions": {"admin": True}},
            {"login": "bob", "permissions": {"maintain": True}},
            {"login": "carol", "permissions": {"push": True}},
            {"login": "dave", "permissions": {"triage": True}},
            {"login": "eve", "permissions": {"pull": True}},
            {"login": "", "permissions": {"admin": True}},
        ]
        return [login for row in rows if (login := row_mapper(row)) is not None]

    monkeypatch.setattr(GitHubClient, "_gh_api_paginated_collect", fake_collect)

    client = GitHubClient(max_attempts=1)
    maintainers = client.fetch_maintainers(repo=RepoRef.parse("org/repo"))

    assert maintainers == {"alice", "bob", "carol"}
    assert captured["path"] == "repos/org/repo/collaborators"
    params = captured["params"]
    assert isinstance(params, dict)
    assert params["affiliation"] == "all"
    assert params["per_page"] == 100
    assert captured["jq_expression"] == ".[]"


def test_close_item_as_duplicate_uses_issue_command(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Proc:
        returncode = 0
        stdout = "closed"
        stderr = ""

    def fake_run(cmd, *, check, capture_output, text):
        captured["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(github_client.subprocess, "run", fake_run)

    client = GitHubClient(max_attempts=1)
    result = client.close_item_as_duplicate(
        repo=RepoRef.parse("org/repo"),
        item_type=ItemType.ISSUE,
        number=42,
        canonical_number=7,
    )

    assert result["status"] == "closed"
    assert result["item_type"] == "issue"
    assert result["number"] == 42

    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert cmd[:4] == ["gh", "issue", "close", "42"]
    assert "--repo" in cmd
    assert "org/repo" in cmd
    assert "--comment" in cmd
    assert "#7" in cmd[-1]
