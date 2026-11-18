"""Maintainer-defined test profile, shared by Git Plus and the runner (standard library only)."""
from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

PROFILE_NAME = "python-pytest"
PROFILE_VERSION = "1"
PROFILES = {PROFILE_NAME: PROFILE_VERSION}

MAX_SELECTORS = 20
MAX_SELECTOR_BYTES = 512
MAX_REQUIREMENT_LINES = 500
MAX_REQUIREMENT_BYTES = 65536

WORK_DIR = "/work"
ENV_DIR = "/opt/env"
INPUT_DIR = "/in"

_PATH_COMPONENT = r"[A-Za-z0-9_][A-Za-z0-9_.+@-]*"
_SELECTOR_PATH = re.compile(rf"{_PATH_COMPONENT}(?:/{_PATH_COMPONENT})*")
# Printable ASCII only, so parametrized IDs work but control characters never reach argv.
_NODE_ID = re.compile(r"[\x20-\x5b\x5d-\x7e]+")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_JOB_ID = re.compile(r"test-[0-9a-f]{32}")


class ProfileError(ValueError):
    """A request violates the maintainer-defined profile."""


def is_job_id(value: object) -> bool:
    return isinstance(value, str) and _JOB_ID.fullmatch(value) is not None


def is_repo(value: object) -> bool:
    return (isinstance(value, str) and _REPO.fullmatch(value) is not None
            and not any(part in {".", ".."} for part in value.split("/")))


def validate_selector(selector: object, test_dirs: tuple[str, ...]) -> str:
    """Accept only repository-relative pytest paths and node IDs below the test roots."""
    if not isinstance(selector, str) or not selector:
        raise ProfileError("Selectors must be non-empty strings.")
    if len(selector.encode("utf-8", errors="replace")) > MAX_SELECTOR_BYTES:
        raise ProfileError("Selector exceeds the length limit.")
    path, sep, node = selector.partition("::")
    if not sep and path.endswith("/"):
        path = path[:-1]
    if _SELECTOR_PATH.fullmatch(path) is None or any(part in {".", ".."} for part in path.split("/")):
        raise ProfileError("Selectors must be repository-relative paths without traversal or options.")
    if sep and (not path.endswith(".py") or _NODE_ID.fullmatch(node) is None
                or any(not segment for segment in node.split("::"))):
        raise ProfileError("Invalid pytest node identifier.")
    if not any(path == root or path.startswith(root + "/") for root in test_dirs):
        raise ProfileError("Selector is outside the configured test directories.")
    return selector


def validate_selectors(selectors: object, test_dirs: tuple[str, ...]) -> tuple[str, ...]:
    if selectors is None:
        return tuple(test_dirs)
    if not isinstance(selectors, (list, tuple)) or not selectors:
        raise ProfileError("selectors must be a non-empty list or omitted.")
    if len(selectors) > MAX_SELECTORS:
        raise ProfileError(f"At most {MAX_SELECTORS} selectors are allowed.")
    cleaned = tuple(dict.fromkeys(validate_selector(item, test_dirs) for item in selectors))
    return cleaned


def selector_matches(selector: str, nodeid: str) -> bool:
    """Whether a collected pytest node ID is covered by a validated selector."""
    if "::" not in selector and not selector.endswith(".py"):
        return nodeid.startswith(selector.rstrip("/") + "/")
    return nodeid == selector or nodeid.startswith((selector + "::", selector + "["))


def build_argv(selectors: tuple[str, ...]) -> list[str]:
    """The only pytest invocation the profile permits; callers contribute selectors alone."""
    return ["python", "-m", "pytest", "-p", "no:cacheprovider", "-p", "pytest_report",
            "-o", "addopts=", f"--rootdir={WORK_DIR}", "--color=no", "--tb=short", "-q",
            "--", *selectors]


_REQUIREMENT = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?(?:\[[A-Za-z0-9._,-]+\])?==[A-Za-z0-9][A-Za-z0-9._+!*-]*")
_MARKER = re.compile(r"[A-Za-z0-9._<>=!~'\" ()-]+")
_HASHES = re.compile(r"(?:\s*--hash=sha256:[0-9a-f]{64})*")


def validate_requirements(text: object) -> str:
    """Pinned PyPI requirements only: no URLs, paths, options, editables or includes."""
    if not isinstance(text, str) or "\0" in text:
        raise ProfileError("Requirements must be text.")
    if len(text.encode("utf-8", errors="replace")) > MAX_REQUIREMENT_BYTES:
        raise ProfileError("Requirements file is too large.")
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    if len(lines) > MAX_REQUIREMENT_LINES:
        raise ProfileError("Requirements file has too many lines.")
    kept: list[str] = []
    pins: list[list] = []  # [text, has_hash]
    for line in lines:
        body = line.strip()
        if not body or body.startswith("#"):
            continue
        body = body.removesuffix("\\").rstrip()
        if body.startswith("--hash=sha256:"):
            if _HASHES.fullmatch(body) is None or not pins:
                raise ProfileError("Invalid hash line in requirements.")
            pins[-1][1] = True
        else:
            requirement, _, marker = body.partition(";")
            hashes = ""
            for part in ("requirement", "marker"):  # hashes may follow either the pin or the environment marker
                text_ = requirement if part == "requirement" else marker
                at = text_.find("--hash")
                if at >= 0:
                    hashes = text_[at:]
                    if part == "requirement":
                        requirement = text_[:at]
                    else:
                        marker = text_[:at]
            if (_REQUIREMENT.fullmatch(requirement.strip()) is None
                    or (marker and _MARKER.fullmatch(marker.strip()) is None)
                    or (hashes and _HASHES.fullmatch(hashes) is None)):
                raise ProfileError(f"Unsupported requirement line: {body[:80]!r}. Only name==version pins are allowed.")
            pins.append([body, bool(hashes)])
        kept.append(line.rstrip())
    if any(has for _, has in pins) and not all(has for _, has in pins):
        missing = next(text for text, has in pins if not has)
        raise ProfileError(f"Hash-pinned requirements must hash every package; missing for {missing[:60]!r}.")
    if not any(re.match(r"\s*pytest(?:\[[^\]]*\])?==", line, re.I) for line in kept):
        raise ProfileError("The approved requirements must pin pytest.")
    return "\n".join(kept) + "\n"


def requirements_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RepoPolicy:
    """Maintainer policy for one repository; everything else is rejected."""
    repo: str
    profile: str
    test_dirs: tuple[str, ...]
    requirements: str
    requirements_sha256: str


def load_policies(path: str | Path) -> dict[str, RepoPolicy]:
    """Parse the maintainer-controlled TOML policy file (keys are casefolded repositories)."""
    file = Path(path)
    with file.open("rb") as handle:
        data = tomllib.load(handle)
    policies: dict[str, RepoPolicy] = {}
    for repo, entry in (data.get("repos") or {}).items():
        if not is_repo(repo) or not isinstance(entry, dict):
            raise ProfileError(f"Invalid repository policy '{repo}'.")
        profile = entry.get("profile", PROFILE_NAME)
        if profile not in PROFILES:
            raise ProfileError(f"Unknown profile '{profile}' for {repo}.")
        dirs = entry.get("test_dirs", ["tests"])
        if not isinstance(dirs, list) or not dirs:
            raise ProfileError(f"test_dirs for {repo} must be a non-empty list.")
        roots = []
        for item in dirs:
            if not isinstance(item, str) or _SELECTOR_PATH.fullmatch(item.rstrip("/")) is None:
                raise ProfileError(f"Invalid test directory for {repo}.")
            roots.append(item.rstrip("/"))
        req_name = entry.get("requirements")
        if not isinstance(req_name, str) or not req_name:
            raise ProfileError(f"{repo} requires an approved 'requirements' file.")
        req_path = (file.parent / req_name).resolve()
        if file.parent.resolve() not in req_path.parents:
            raise ProfileError("Requirements files must live beside the policy file.")
        text = validate_requirements(req_path.read_text(encoding="utf-8"))
        policies[repo.casefold()] = RepoPolicy(repo, profile, tuple(roots), text, requirements_digest(text))
    return policies
