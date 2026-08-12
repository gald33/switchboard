"""One definition of the managed hub's token, and two workflows that read it.

`deploy.yml` starts the hub with `MANAGED_HUB_TOKEN`, and `ci.yml` announces
each run with it. Both pull the value out of `cli.py` with the same `sed`
rather than restating it, because a second copy is a copy that can go stale:
CI carried a private one in `secrets.SWITCHBOARD_TOKEN`, the hub's token moved,
and the announce 401'd for a day inside a green build (#96).

A `sed` against source is not a thing the type checker or the test suite would
otherwise notice breaking. Reformatting the constant — quotes, a type
annotation, a line break — leaves both workflows extracting an empty string,
and the failure mode is silence in both: a hub that admits anyone, and a CI
job that announces nothing. So the extraction is pinned here, by running the
same expression the workflows run against the same file they read.
"""

from __future__ import annotations

import re
from pathlib import Path

from switchboard.cli import MANAGED_HUB_TOKEN, MANAGED_HUB_URL

ROOT = Path(__file__).resolve().parent.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"
DEPLOY = ROOT / ".github" / "workflows" / "deploy.yml"

#: The Python spelling of `sed -n 's/^MANAGED_HUB_TOKEN = "\\([^"]*\\)".*/\\1/p'`,
#: which is what both workflows run.
EXTRACT = re.compile(r'^MANAGED_HUB_TOKEN = "([^"]*)"', re.MULTILINE)


def _extracted() -> list[str]:
    return EXTRACT.findall((ROOT / "src" / "switchboard" / "cli.py").read_text())


def test_the_workflows_sed_finds_the_token() -> None:
    assert _extracted() == [MANAGED_HUB_TOKEN]


def test_the_token_is_not_empty() -> None:
    # An empty match is the dangerous outcome, not a missing one: deploy.yml
    # guards on it, but a hub started with no token admits every caller.
    assert MANAGED_HUB_TOKEN


def test_both_workflows_read_the_token_rather_than_restating_it() -> None:
    for path in (CI, DEPLOY):
        text = path.read_text()
        assert "MANAGED_HUB_TOKEN" in text, f"{path.name} should read the constant"
        assert MANAGED_HUB_TOKEN not in text, (
            f"{path.name} restates the token instead of reading it from cli.py"
        )


def test_ci_does_not_carry_a_private_copy_of_the_token() -> None:
    # The regression itself: a secret here is a second value nobody compares
    # against the hub's, and it fails in a way no build reports. Matched as an
    # expression rather than a bare name so the comment explaining why the
    # secret went away does not count as the secret coming back.
    assert not re.search(r"\$\{\{\s*secrets\.SWITCHBOARD_TOKEN", CI.read_text())


def test_the_announce_reports_its_own_failure() -> None:
    text = CI.read_text()
    announce = text[text.index("register-switchboard:") :]
    assert "|| true" not in announce, (
        "a swallowed failure is how the announce broke unnoticed; warn instead"
    )
    assert "::warning" in announce
    # Still non-gating: nothing in the step may exit non-zero on a failed hub.
    assert "exit 1" not in announce


def test_ci_announces_to_the_hub_the_token_belongs_to() -> None:
    # The token is only meaningful paired with its hub; pointing this job at a
    # different URL would send the published token somewhere it means nothing.
    assert MANAGED_HUB_URL in CI.read_text()
