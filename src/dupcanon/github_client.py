from __future__ import annotations

import json
import random
import re
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from json import JSONDecodeError
from typing import Any, TypeVar
from urllib.parse import urlencode

from dupcanon.models import ItemPayload, ItemType, RepoMetadata, RepoRef, StateFilter

_HTTP_STATUS_RE = re.compile(r"HTTP\s+(?P<code>\d{3})")
_T = TypeVar("_T")


class GitHubApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubNotFoundError(GitHubApiError):
    pass


def _parse_http_status(stderr: str) -> int | None:
    match = _HTTP_STATUS_RE.search(stderr)
    if match is None:
        return None
    return int(match.group("code"))


def _should_retry(status_code: int | None) -> bool:
    if status_code is None:
        return True
    if status_code == 429:
        return True
    return 500 <= status_code <= 599


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _extract_labels(raw: Any) -> list[str]:
    labels: list[str] = []
    if not isinstance(raw, list):
        return labels

    for label in raw:
        if isinstance(label, dict):
            name = label.get("name")
            if isinstance(name, str) and name:
                labels.append(name)
        elif isinstance(label, str) and label:
            labels.append(label)

    return labels


class GitHubClient:
    def __init__(self, *, max_attempts: int = 5) -> None:
        self.max_attempts = max_attempts

    def _gh_api(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        query = urlencode({k: v for k, v in (params or {}).items() if v is not None})
        api_path = f"{path}?{query}" if query else path

        cmd = [
            "gh",
            "api",
            api_path,
            "--method",
            "GET",
            "-H",
            "Accept: application/vnd.github+json",
        ]

        last_error: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            proc = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
            )

            if proc.returncode == 0:
                return json.loads(proc.stdout)

            status_code = _parse_http_status(proc.stderr)
            message = proc.stderr.strip() or proc.stdout.strip() or "unknown gh api error"

            if status_code == 404:
                raise GitHubNotFoundError(message, status_code=status_code)

            error = GitHubApiError(message, status_code=status_code)
            last_error = error

            if attempt >= self.max_attempts or not _should_retry(status_code):
                raise error

            delay = min(30.0, float(2 ** (attempt - 1))) + random.uniform(0.0, 0.25)
            time.sleep(delay)

        if last_error is not None:
            raise last_error
        raise GitHubApiError("unreachable gh api retry state")

    def _gh_api_paginated_collect(
        self,
        path: str,
        *,
        params: dict[str, Any] | None,
        row_mapper: Callable[[dict[str, Any]], _T | None],
        jq_expression: str = ".[]",
        on_batch_count: Callable[[int], None] | None = None,
    ) -> list[_T]:
        query = urlencode({k: v for k, v in (params or {}).items() if v is not None})
        api_path = f"{path}?{query}" if query else path

        cmd = [
            "gh",
            "api",
            api_path,
            "--method",
            "GET",
            "-H",
            "Accept: application/vnd.github+json",
            "--paginate",
            "--jq",
            jq_expression,
        ]

        last_error: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            mapped_rows: list[_T] = []
            pending = 0

            assert proc.stdout is not None
            for line in proc.stdout:
                raw = line.strip()
                if not raw:
                    continue

                try:
                    value = json.loads(raw)
                except JSONDecodeError as exc:
                    msg = "failed to decode paginated gh api object stream"
                    raise GitHubApiError(msg) from exc

                if not isinstance(value, dict):
                    continue

                mapped = row_mapper(value)
                if mapped is None:
                    continue

                mapped_rows.append(mapped)
                pending += 1
                if on_batch_count is not None and pending >= 100:
                    on_batch_count(pending)
                    pending = 0

            stderr = ""
            if proc.stderr is not None:
                stderr = proc.stderr.read()

            return_code = proc.wait()
            if return_code == 0:
                if on_batch_count is not None and pending > 0:
                    on_batch_count(pending)
                return mapped_rows

            status_code = _parse_http_status(stderr)
            message = stderr.strip() or "unknown gh api error"

            if status_code == 404:
                raise GitHubNotFoundError(message, status_code=status_code)

            error = GitHubApiError(message, status_code=status_code)
            last_error = error

            if attempt >= self.max_attempts or not _should_retry(status_code):
                raise error

            delay = min(30.0, float(2 ** (attempt - 1))) + random.uniform(0.0, 0.25)
            time.sleep(delay)

        if last_error is not None:
            raise last_error
        raise GitHubApiError("unreachable gh api retry state")

    def _gh_graphql_paginated_collect(
        self,
        *,
        query: str,
        variables: dict[str, str],
        jq_expression: str,
        row_mapper: Callable[[dict[str, Any]], _T | None],
        on_batch_count: Callable[[int], None] | None = None,
    ) -> list[_T]:
        cmd = [
            "gh",
            "api",
            "graphql",
            "--paginate",
            "-f",
            f"query={query}",
            "--jq",
            jq_expression,
        ]
        for key in sorted(variables):
            cmd.extend(["-F", f"{key}={variables[key]}"])

        last_error: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            mapped_rows: list[_T] = []
            pending = 0

            assert proc.stdout is not None
            for line in proc.stdout:
                raw = line.strip()
                if not raw:
                    continue

                try:
                    value = json.loads(raw)
                except JSONDecodeError as exc:
                    msg = "failed to decode paginated gh graphql object stream"
                    raise GitHubApiError(msg) from exc

                if not isinstance(value, dict):
                    continue

                mapped = row_mapper(value)
                if mapped is None:
                    continue

                mapped_rows.append(mapped)
                pending += 1
                if on_batch_count is not None and pending >= 100:
                    on_batch_count(pending)
                    pending = 0

            stderr = ""
            if proc.stderr is not None:
                stderr = proc.stderr.read()

            return_code = proc.wait()
            if return_code == 0:
                if on_batch_count is not None and pending > 0:
                    on_batch_count(pending)
                return mapped_rows

            status_code = _parse_http_status(stderr)
            message = stderr.strip() or "unknown gh graphql error"

            if status_code == 404:
                raise GitHubNotFoundError(message, status_code=status_code)

            error = GitHubApiError(message, status_code=status_code)
            last_error = error

            if attempt >= self.max_attempts or not _should_retry(status_code):
                raise error

            delay = min(30.0, float(2 ** (attempt - 1))) + random.uniform(0.0, 0.25)
            time.sleep(delay)

        if last_error is not None:
            raise last_error
        raise GitHubApiError("unreachable gh graphql retry state")

    def fetch_repo_metadata(self, repo: RepoRef) -> RepoMetadata:
        data = self._gh_api(f"repos/{repo.full_name()}")
        owner = data.get("owner") or {}
        return RepoMetadata(
            github_repo_id=int(data["id"]),
            org=str(owner.get("login") or repo.org),
            name=str(data.get("name") or repo.name),
        )

    def _since_created_qualifier(self, since: datetime) -> str:
        return since.astimezone(UTC).strftime("%Y-%m-%d")

    def _state_qualifier(self, state: StateFilter) -> str | None:
        if state == StateFilter.OPEN:
            return "is:open"
        if state == StateFilter.CLOSED:
            return "is:closed"
        return None

    def _issue_states_literal(self, state: StateFilter) -> str:
        if state == StateFilter.OPEN:
            return "[OPEN]"
        if state == StateFilter.CLOSED:
            return "[CLOSED]"
        return "[OPEN,CLOSED]"

    def _pr_states_literal(self, state: StateFilter) -> str:
        if state == StateFilter.OPEN:
            return "[OPEN]"
        if state == StateFilter.CLOSED:
            return "[CLOSED,MERGED]"
        return "[OPEN,CLOSED,MERGED]"

    def fetch_issues(
        self,
        *,
        repo: RepoRef,
        state: StateFilter,
        since: datetime | None,
        on_page_count: Callable[[int], None] | None = None,
    ) -> list[ItemPayload]:
        if since is not None:
            qualifiers = [
                f"repo:{repo.full_name()}",
                "is:issue",
                f"created:>={self._since_created_qualifier(since)}",
            ]
            state_qualifier = self._state_qualifier(state)
            if state_qualifier is not None:
                qualifiers.append(state_qualifier)

            return self._gh_api_paginated_collect(
                "search/issues",
                params={
                    "q": " ".join(qualifiers),
                    "per_page": 100,
                    "sort": "created",
                    "order": "asc",
                },
                row_mapper=self._to_issue_payload,
                jq_expression=".items[]",
                on_batch_count=on_page_count,
            )

        states_literal = self._issue_states_literal(state)
        query = f"""
query($owner:String!,$name:String!,$endCursor:String) {{
  repository(owner:$owner,name:$name) {{
    issues(
      first:100,
      after:$endCursor,
      states:{states_literal},
      orderBy:{{field:CREATED_AT,direction:ASC}}
    ) {{
      nodes {{
        number
        url
        title
        body
        state
        createdAt
        updatedAt
        closedAt
        author {{ login }}
        assignees(first:50) {{ nodes {{ login }} }}
        labels(first:100) {{ nodes {{ name }} }}
        comments {{ totalCount }}
      }}
      pageInfo {{ hasNextPage endCursor }}
    }}
  }}
}}
"""

        return self._gh_graphql_paginated_collect(
            query=query,
            variables={"owner": repo.org, "name": repo.name},
            jq_expression=".data.repository.issues.nodes[]",
            row_mapper=self._to_issue_payload_from_graphql,
            on_batch_count=on_page_count,
        )

    def fetch_pulls(
        self,
        *,
        repo: RepoRef,
        state: StateFilter,
        since: datetime | None,
        on_page_count: Callable[[int], None] | None = None,
    ) -> list[ItemPayload]:
        if since is not None:
            qualifiers = [
                f"repo:{repo.full_name()}",
                "is:pr",
                f"created:>={self._since_created_qualifier(since)}",
            ]
            state_qualifier = self._state_qualifier(state)
            if state_qualifier is not None:
                qualifiers.append(state_qualifier)

            return self._gh_api_paginated_collect(
                "search/issues",
                params={
                    "q": " ".join(qualifiers),
                    "per_page": 100,
                    "sort": "created",
                    "order": "asc",
                },
                row_mapper=self._to_pr_payload,
                jq_expression=".items[]",
                on_batch_count=on_page_count,
            )

        states_literal = self._pr_states_literal(state)
        query = f"""
query($owner:String!,$name:String!,$endCursor:String) {{
  repository(owner:$owner,name:$name) {{
    pullRequests(
      first:100,
      after:$endCursor,
      states:{states_literal},
      orderBy:{{field:CREATED_AT,direction:ASC}}
    ) {{
      nodes {{
        number
        url
        title
        body
        state
        createdAt
        updatedAt
        closedAt
        mergedAt
        author {{ login }}
        assignees(first:50) {{ nodes {{ login }} }}
        labels(first:100) {{ nodes {{ name }} }}
        comments {{ totalCount }}
        reviewThreads {{ totalCount }}
      }}
      pageInfo {{ hasNextPage endCursor }}
    }}
  }}
}}
"""

        return self._gh_graphql_paginated_collect(
            query=query,
            variables={"owner": repo.org, "name": repo.name},
            jq_expression=".data.repository.pullRequests.nodes[]",
            row_mapper=self._to_pr_payload_from_graphql,
            on_batch_count=on_page_count,
        )

    def fetch_item(self, *, repo: RepoRef, item_type: ItemType, number: int) -> ItemPayload:
        issue_like = self._gh_api(f"repos/{repo.full_name()}/issues/{number}")
        if item_type == ItemType.ISSUE:
            if "pull_request" in issue_like:
                msg = f"#{number} is a pull request, not an issue"
                raise GitHubNotFoundError(msg, status_code=404)
            return self._to_issue_payload(issue_like)

        pr = self._gh_api(f"repos/{repo.full_name()}/pulls/{number}")
        return self._to_pr_payload(pr, issue_like=issue_like)

    def fetch_maintainers(self, *, repo: RepoRef) -> set[str]:
        def to_maintainer_login(row: dict[str, Any]) -> str | None:
            login = row.get("login")
            if not isinstance(login, str) or not login:
                return None

            permissions = row.get("permissions")
            if not isinstance(permissions, dict):
                return None

            if any(bool(permissions.get(key)) for key in ("admin", "maintain", "push")):
                return login
            return None

        maintainers = self._gh_api_paginated_collect(
            f"repos/{repo.full_name()}/collaborators",
            params={"affiliation": "all", "per_page": 100},
            row_mapper=to_maintainer_login,
            jq_expression=".[]",
        )

        return set(maintainers)

    def _to_issue_payload_from_graphql(self, row: dict[str, Any]) -> ItemPayload:
        assignees = [
            str(node.get("login"))
            for node in (row.get("assignees") or {}).get("nodes", [])
            if isinstance(node, dict) and node.get("login")
        ]
        labels = [
            str(node.get("name"))
            for node in (row.get("labels") or {}).get("nodes", [])
            if isinstance(node, dict) and node.get("name")
        ]
        author = row.get("author") or {}

        raw_state = str(row.get("state") or "OPEN")
        state = StateFilter.OPEN if raw_state == "OPEN" else StateFilter.CLOSED

        return ItemPayload(
            type=ItemType.ISSUE,
            number=int(row["number"]),
            url=str(row.get("url") or ""),
            title=str(row.get("title") or ""),
            body=row.get("body"),
            state=state,
            author_login=author.get("login"),
            assignees=assignees,
            labels=labels,
            comment_count=int((row.get("comments") or {}).get("totalCount") or 0),
            review_comment_count=0,
            created_at_gh=_parse_datetime(row.get("createdAt")),
            updated_at_gh=_parse_datetime(row.get("updatedAt")),
            closed_at_gh=_parse_datetime(row.get("closedAt")),
        )

    def _to_pr_payload_from_graphql(self, row: dict[str, Any]) -> ItemPayload:
        assignees = [
            str(node.get("login"))
            for node in (row.get("assignees") or {}).get("nodes", [])
            if isinstance(node, dict) and node.get("login")
        ]
        labels = [
            str(node.get("name"))
            for node in (row.get("labels") or {}).get("nodes", [])
            if isinstance(node, dict) and node.get("name")
        ]
        author = row.get("author") or {}

        pr_state = str(row.get("state") or "OPEN")
        state = StateFilter.OPEN if pr_state == "OPEN" else StateFilter.CLOSED

        return ItemPayload(
            type=ItemType.PR,
            number=int(row["number"]),
            url=str(row.get("url") or ""),
            title=str(row.get("title") or ""),
            body=row.get("body"),
            state=state,
            author_login=author.get("login"),
            assignees=assignees,
            labels=labels,
            comment_count=int((row.get("comments") or {}).get("totalCount") or 0),
            review_comment_count=int((row.get("reviewThreads") or {}).get("totalCount") or 0),
            created_at_gh=_parse_datetime(row.get("createdAt")),
            updated_at_gh=_parse_datetime(row.get("updatedAt")),
            closed_at_gh=_parse_datetime(row.get("closedAt")),
        )

    def _to_issue_payload(self, row: dict[str, Any]) -> ItemPayload:
        assignees = [str(a.get("login")) for a in row.get("assignees", []) if isinstance(a, dict)]
        user = row.get("user") or {}

        return ItemPayload(
            type=ItemType.ISSUE,
            number=int(row["number"]),
            url=str(row.get("html_url") or ""),
            title=str(row.get("title") or ""),
            body=row.get("body"),
            state=StateFilter(str(row.get("state") or "open")),
            author_login=user.get("login"),
            assignees=assignees,
            labels=_extract_labels(row.get("labels")),
            comment_count=int(row.get("comments") or 0),
            review_comment_count=0,
            created_at_gh=_parse_datetime(row.get("created_at")),
            updated_at_gh=_parse_datetime(row.get("updated_at")),
            closed_at_gh=_parse_datetime(row.get("closed_at")),
        )

    def _to_pr_payload(
        self, row: dict[str, Any], *, issue_like: dict[str, Any] | None = None
    ) -> ItemPayload:
        assignees = [str(a.get("login")) for a in row.get("assignees", []) if isinstance(a, dict)]
        user = row.get("user") or {}

        issue_data = issue_like or {}
        labels_source = issue_data.get("labels") if issue_data else row.get("labels")

        return ItemPayload(
            type=ItemType.PR,
            number=int(row["number"]),
            url=str(row.get("html_url") or ""),
            title=str(row.get("title") or ""),
            body=row.get("body"),
            state=StateFilter(str(row.get("state") or "open")),
            author_login=user.get("login"),
            assignees=assignees,
            labels=_extract_labels(labels_source),
            comment_count=int(row.get("comments") or issue_data.get("comments") or 0),
            review_comment_count=int(row.get("review_comments") or 0),
            created_at_gh=_parse_datetime(row.get("created_at")),
            updated_at_gh=_parse_datetime(row.get("updated_at")),
            closed_at_gh=_parse_datetime(row.get("closed_at")),
        )
