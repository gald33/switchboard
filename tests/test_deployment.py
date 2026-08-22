"""The deployment's two halves, checked against each other.

Reading the managed hub from a browser needs two things that live in
different files and are deployed by different workflows: a page, published to
an origin (`.github/workflows/pages.yml`), and a hub that allows that origin
(`docker-compose.yml`). Neither half fails loudly when it disagrees with the
other — the page simply cannot read, and the browser will not say why — so
the disagreement has to be caught here instead.

The page also carries two constants copied from `config.py`. Copies are the
right call (a static page cannot import Python) and they are exactly the kind
of thing that goes stale the day the original moves, which this repo has
already been bitten by more than once.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from switchboard.config import (
    MANAGED_HUB_TOKEN,
    MANAGED_HUB_URL,
    PUBLISHED_PAGE_URL,
    ServerConfig,
)

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (ROOT / "docker-compose.yml").read_text()
PAGES = (ROOT / ".github" / "workflows" / "pages.yml").read_text()
PAGE = (ROOT / "extras" / "viewer" / "switchboard_viewer" / "web"
        / "index.html").read_text()
WEB_README = (ROOT / "extras" / "viewer" / "README.md").read_text()


def _js_const(name: str) -> str:
    found = re.search(rf'^const {name} = "([^"]*)";', PAGE, re.M)
    assert found, f"{name} is gone from extras/viewer/switchboard_viewer/web/index.html"
    return found.group(1)


def _compose_cors_default() -> str:
    found = re.search(
        r"SWITCHBOARD_CORS_ORIGINS:\s*\$\{SWITCHBOARD_CORS_ORIGINS-([^}]*)\}", COMPOSE)
    assert found, "docker-compose.yml no longer sets SWITCHBOARD_CORS_ORIGINS"
    return found.group(1)


def test_the_page_dials_the_hub_the_library_dials():
    """A page that starts on a different hub than every other client sends a
    reader to an empty room and tells them nothing is happening there."""
    assert _js_const("MANAGED_HUB") == MANAGED_HUB_URL


def test_the_page_carries_the_published_token_verbatim():
    """The token is published on purpose, and prefilled so nobody has to go
    and find it. A stale copy is a 401 that looks like an outage."""
    assert _js_const("MANAGED_TOKEN") == MANAGED_HUB_TOKEN


def test_the_managed_hub_allows_the_origin_the_page_is_published_to():
    """The half that cannot be tested by making a request: a browser refuses
    a cross-origin read before it is sent, so the only place this is checkable
    is between the two files that have to agree."""
    origin = _compose_cors_default()
    assert origin, "the compose default no longer allows any browser origin"
    # Pages serves a project site from the owner's github.io origin. The
    # workflow names the URL in its own header comment; if that ever changes,
    # this is the line that fails.
    assert origin in PAGES, (
        f"docker-compose.yml allows {origin}, which pages.yml never mentions")


def test_the_compose_default_reaches_the_server_as_an_allowed_origin(monkeypatch):
    """That the string is in the file proves nothing on its own — it has to
    survive `ServerConfig.from_env` to become a CORS header."""
    origin = _compose_cors_default()
    monkeypatch.setenv("SWITCHBOARD_CORS_ORIGINS", origin)
    assert ServerConfig.from_env().cors_origins == (origin,)


def test_an_operator_can_still_turn_browser_reads_off(monkeypatch):
    """`${VAR-default}` rather than `${VAR:-default}`, so an explicitly empty
    value means empty and not "give me the default back"."""
    assert "${SWITCHBOARD_CORS_ORIGINS-" in COMPOSE
    monkeypatch.setenv("SWITCHBOARD_CORS_ORIGINS", "")
    assert ServerConfig.from_env().cors_origins == ()


def test_the_page_is_published_from_the_repo_without_a_build_step():
    """What is served has to be diffable against the commit it claims to come
    from — that is the only thing a reader can check about a hosted page."""
    assert "path: extras/viewer/switchboard_viewer/web" in PAGES
    # No `run:` anywhere in the workflow is the strict form of "no build step":
    # the job is checkout, configure, upload, deploy, and nothing may run
    # between the commit and what is served.
    assert "run:" not in PAGES


def test_the_workflow_asks_for_the_permissions_a_deploy_needs_and_no_more():
    for granted in ("contents: read", "pages: write", "id-token: write"):
        assert granted in PAGES


def test_the_manual_step_stays_written_down_where_the_error_will_send_you():
    """`enablement: true` was tried and cannot work — creating a Pages site
    needs repo admin, which GITHUB_TOKEN never has. So the switch is manual,
    and the failure when it is unset is a bare `Not Found` that names nothing.
    The workflow is the only place a reader will land from that error, so the
    remedy has to be legible there."""
    assert "enablement" not in PAGES.replace("# ", "", 1).split("jobs:")[1], (
        "the enablement option is back in the job; it fails with "
        "'Resource not accessible by integration'")
    header = PAGES.split("jobs:")[0]
    assert "Settings -> Pages" in header
    assert "Source: GitHub Actions" in header


def test_the_hosted_origin_is_written_down_where_a_reader_will_look():
    assert _compose_cors_default() in WEB_README


def test_the_page_invites_link_to_is_the_page_that_gets_published():
    """`switchboard invite --link` with no argument sends a key to this URL,
    and the reason that is safe to default is the workflow above: the
    directory is uploaded verbatim, so what ran in the recipient's browser is
    diffable against the commit. A constant pointing anywhere else keeps the
    convenience and loses the only thing that justified it."""
    origin = urlsplit(PUBLISHED_PAGE_URL)
    assert origin.scheme == "https"
    assert PUBLISHED_PAGE_URL in PAGES, (
        f"invites link to {PUBLISHED_PAGE_URL}, which pages.yml never mentions")
    assert f"{origin.scheme}://{origin.netloc}" == _compose_cors_default(), (
        "the page invites link to is on an origin the managed hub does not "
        "allow, so it will open and then read nothing")
