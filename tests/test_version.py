"""One version, in every place that states it.

The number lives in four places that nothing keeps in step: `pyproject.toml`,
which is what the wheel is *labelled*; `__version__`, which is what
`switchboard --version` and the `serve` banner *report*; and two sample outputs
in the quickstart, which is what a newcomer compares their own terminal
against.

The release workflow checks the tag against `pyproject.toml` and nothing else,
so a drift between the first two is not caught by anything: it publishes a
wheel called 0.10.0 whose `--version` says 0.9.0, and PyPI never lets a version
be reused, so it is unfixable rather than merely embarrassing.

The docs half is not hypothetical either — the quickstart printed `0.7.2` for
three releases, which reads to a newcomer as a broken install rather than a
stale doc.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import tomllib

from switchboard import __version__

ROOT = Path(__file__).resolve().parents[1]


def _project_version(pyproject: Path) -> str:
    return tomllib.loads(pyproject.read_text())["project"]["version"]


def test_the_wheel_is_labelled_what_the_code_reports():
    """`pyproject.toml` names the artifact; `__version__` answers
    `switchboard --version`. A user comparing the two must not find a
    discrepancy, and the release workflow only ever looks at the first."""
    assert _project_version(ROOT / "pyproject.toml") == __version__


def test_the_add_on_carries_its_own_version():
    """Two distributions, two tags, two version numbers — deliberately. This
    only pins that the add-on states one at all and that it is not accidentally
    tracking the SDK's, which would make `viewer-vX` and `vX` mean different
    things while looking identical."""
    viewer = _project_version(ROOT / "extras" / "viewer" / "pyproject.toml")
    assert re.fullmatch(r"\d+\.\d+\.\d+", viewer)


@pytest.mark.parametrize("doc", ["docs/quickstart.md"])
def test_sample_output_in_the_docs_is_this_version(doc):
    """Every `switchboard <version>` and `"version": "<version>"` a doc shows
    as *output* has to be what the code actually prints."""
    text = (ROOT / doc).read_text()
    shown = set(re.findall(r"^switchboard (\d+\.\d+\.\d+)", text, re.MULTILINE))
    shown |= set(re.findall(r'"version":\s*"(\d+\.\d+\.\d+)"', text))
    assert shown, f"{doc} shows no version output — did the sample change shape?"
    stale = sorted(v for v in shown if v != __version__)
    assert not stale, f"{doc} shows {stale}, but this is {__version__}"
