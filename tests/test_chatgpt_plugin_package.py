"""The ChatGPT plugin package, held to what OpenAI's portal checks.

The portal validates the ZIP on upload and again at final directory
submission, where some limits are tighter (a 30-character short description,
128-character starter prompts). An edit that breaks one would only surface as
an error code in the portal, days from the change that caused it, so the
documented rules are checked here instead:
https://developers.openai.com/plugins/deploy/submission-errors
"""

from __future__ import annotations

import importlib.util
import json
import re
import struct
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "extras" / "chatgpt-plugin"
PACKAGE = ROOT / "package"
MANIFEST = json.loads((PACKAGE / "plugin.json").read_text(encoding="utf-8"))
INTERFACE = MANIFEST["extensions"]["com.openai"]["interface"]
CATEGORIES = {"Productivity", "Creativity", "Developer Tools", "Business & Operations",
              "Data & Analytics", "Communication", "Education & Research", "Security",
              "Finance", "Healthcare", "Travel", "Entertainment", "Other"}


def _builder():
    spec = importlib.util.spec_from_file_location("build_package", ROOT / "build_package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _luminance(hex_color: str) -> float:
    def channel(c: int) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_the_manifest_identifies_the_plugin():
    assert MANIFEST["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", MANIFEST["name"])
    assert re.fullmatch(r"\d+\.\d+\.\d+", MANIFEST["version"])
    assert 0 < len(MANIFEST["description"]) <= 1024
    assert MANIFEST["homepage"].startswith("https://")


def test_the_developer_is_named_the_same_way_twice():
    # Otherwise the portal replaces both with the verified identity's name.
    assert MANIFEST["author"]["name"] == INTERFACE["developerName"]
    assert len(INTERFACE["developerName"]) <= 80


def test_the_listing_fits_the_final_directory_limits():
    assert 0 < len(INTERFACE["displayName"]) <= 30
    assert 0 < len(INTERFACE["shortDescription"]) <= 30
    assert "\n" not in INTERFACE["shortDescription"]
    assert 0 < len(INTERFACE["longDescription"]) <= 4000
    assert INTERFACE["category"] in CATEGORIES
    assert len(INTERFACE["capabilities"]) <= 20
    assert all(0 < len(c) <= 120 for c in INTERFACE["capabilities"])


@pytest.mark.parametrize("field", ["websiteURL", "supportURL", "privacyPolicyURL",
                                   "termsOfServiceURL"])
def test_every_listing_url_is_https(field):
    # All four are required for a With-MCP submission; `mailto:` is not accepted.
    url = INTERFACE[field]
    assert url.startswith("https://") and len(url) <= 1024


def test_starter_prompts_fit():
    prompts = INTERFACE["defaultPrompt"]
    assert 0 < len(prompts) <= 3
    assert all(0 < len(p) <= 128 and "\n" not in p for p in prompts)


def test_brand_colours_are_legible():
    assert re.fullmatch(r"#[0-9A-Fa-f]{6}", INTERFACE["brandColor"])
    assert _contrast(INTERFACE["brandColor"], "#FFFFFF") >= 2
    assert _contrast(INTERFACE["brandColorDark"], "#212121") >= 2


@pytest.mark.parametrize("field", ["logo", "composerIcon"])
def test_branding_images_are_square_pngs(field):
    path = INTERFACE[field]
    assert path.startswith("./assets/") and path.endswith(".png")
    data = (PACKAGE / path).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) <= 5 * 1024 * 1024
    width, height = struct.unpack(">II", data[16:24])
    assert width == height and 48 <= width <= 4096


def test_the_skill_is_where_portable_packages_look():
    skill = (PACKAGE / "skills" / "switchboard" / "SKILL.md").read_text(encoding="utf-8")
    front = skill.split("---")[1]
    assert re.search(r"^name: switchboard$", front, re.M)
    assert re.search(r"^description: .+", front, re.M)


def test_the_zip_has_the_plugin_at_its_root_and_no_mcp_config(tmp_path):
    out = _builder().build(tmp_path / "plugin.zip")
    with zipfile.ZipFile(out) as archive:
        names = archive.namelist()
    assert "plugin.json" in names and "skills/switchboard/SKILL.md" in names
    assert not {"mcp.json", ".mcp.json", ".app.json"} & {Path(n).name for n in names}
    assert all(not n.startswith("/") and ".." not in n.split("/") for n in names)


def test_the_zip_is_reproducible(tmp_path):
    builder = _builder()
    one = builder.build(tmp_path / "one.zip").read_bytes()
    two = builder.build(tmp_path / "two.zip").read_bytes()
    assert one == two


# --- chatgpt-app-submission.json, the portal's import file --------------------

SUBMISSION = ROOT / "chatgpt-app-submission.json"


def _submission_builder():
    spec = importlib.util.spec_from_file_location("build_submission",
                                                  ROOT / "build_submission.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_submission_file_is_what_the_generator_writes():
    # Regenerate with: python3 extras/chatgpt-plugin/build_submission.py
    assert SUBMISSION.read_text(encoding="utf-8") == _submission_builder().render()


def test_the_submission_file_validates_against_openais_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((ROOT / "chatgpt-app-submission.v1.schema.json").read_text())
    data = json.loads(SUBMISSION.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(data)
    assert data["$schema"] == schema["$id"]


def test_the_submission_declares_exactly_the_tools_the_bridge_serves():
    builder = _submission_builder()
    served = {t["name"]: t["annotations"] for t in builder.hosted_tools()}
    declared = json.loads(SUBMISSION.read_text(encoding="utf-8"))["tools"]
    assert set(declared) == set(served)
    for name, entry in declared.items():
        for hint, value in entry["annotations"].items():
            assert served[name][hint] is value, (name, hint)


def test_the_listing_in_the_submission_matches_the_package():
    info = json.loads(SUBMISSION.read_text(encoding="utf-8"))["app_info"]
    assert info["display_name"] == INTERFACE["displayName"]
    assert info["subtitle"] == INTERFACE["shortDescription"]
    assert info["description"] == INTERFACE["longDescription"]


def test_the_submission_has_exactly_the_test_cases_the_form_takes():
    # The schema says "at least"; the portal's form rejects anything but these.
    data = json.loads(SUBMISSION.read_text(encoding="utf-8"))
    assert len(data["test_cases"]) == 5
    assert len(data["negative_test_cases"]) == 3
