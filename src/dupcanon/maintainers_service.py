from __future__ import annotations

from dupcanon.github_client import GitHubClient
from dupcanon.logging_config import BoundLogger
from dupcanon.models import RepoRef


def run_maintainers(*, repo_value: str, logger: BoundLogger) -> list[str]:
    repo = RepoRef.parse(repo_value)
    logger = logger.bind(repo=repo.full_name(), stage="maintainers")
    logger.info("maintainers.start", status="started")

    gh = GitHubClient()
    maintainers = sorted(gh.fetch_maintainers(repo=repo), key=str.lower)

    logger.info("maintainers.complete", status="ok", count=len(maintainers))
    return maintainers
