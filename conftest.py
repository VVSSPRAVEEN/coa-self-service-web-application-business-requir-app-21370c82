"""Repository-root pytest configuration for the Ventura COA platform.

This module is the single place where suite-wide command line options and
markers are declared. ``--localstack`` and ``--api-base-url`` are registered
through pytest's own supported ``pytest_addoption`` hook, so a run such as
``python -m pytest --localstack`` is accepted by pytest's own argument
validation instead of aborting with "unrecognized arguments".

Only the root ``conftest.py`` may call ``pytest_addoption``. A second
``pytest_addoption`` in any other conftest (for example ``tests/conftest.py``)
makes pytest emit a usage error and exit before collection, which is exactly
the class of failure recorded against the suite, so the option and marker
declarations below are deliberately kept in this one file.

Import roots are also established here: the Dockerfiles and docker-compose.yml
export ``PYTHONPATH=/workspace:/workspace/src:/workspace/tests`` for container
runs, and this module mirrors those three roots for local runs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pytest import Config, FixtureRequest, Parser

_REPO_ROOT = Path(__file__).resolve().parent

#: Import roots, in priority order, mirroring the container PYTHONPATH.
_IMPORT_ROOTS = (
    str(_REPO_ROOT),
    str(_REPO_ROOT / "src"),
    str(_REPO_ROOT / "tests"),
)

_MARKERS = (
    "integration: tests that require the Compose support services",
    "e2e: Playwright browser journeys",
    "slow: long running tests",
)


def _ensure_import_roots() -> None:
    """Put the repository roots on ``sys.path`` (idempotent)."""
    for entry in reversed(_IMPORT_ROOTS):
        if entry not in sys.path:
            sys.path.insert(0, entry)


_ensure_import_roots()


def pytest_addoption(parser: Parser) -> None:
    """Declare the options this suite understands, through pytest's own API.

    Registered here and nowhere else, so option parsing is single-sourced and
    the ``localstack`` / ``api_base_url`` fixtures below can always read them
    with ``getoption``.
    """
    group = parser.getgroup("ventura", "Ventura COA platform test options")
    group.addoption(
        "--localstack",
        action="store_true",
        default=False,
        dest="localstack",
        help="Run integration suites against the LocalStack/RDS Compose services.",
    )
    group.addoption(
        "--api-base-url",
        action="store",
        default="http://127.0.0.1:8000",
        dest="api_base_url",
        help="Base URL of the running COA API used by the end-to-end suites.",
    )


def pytest_configure(config: Config) -> None:
    """Register the suite markers on the running pytest config."""
    for marker in _MARKERS:
        config.addinivalue_line("markers", marker)


@pytest.fixture(scope="session")
def localstack(request: FixtureRequest) -> bool:
    """True when the run was started with ``--localstack``."""
    return bool(request.config.getoption("--localstack"))


@pytest.fixture(scope="session")
def api_base_url(request: FixtureRequest) -> str:
    """Base URL of the API under test, read through the declared option."""
    return str(request.config.getoption("--api_base_url"))
