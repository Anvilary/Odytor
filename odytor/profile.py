"""Maintainer-defined test profile, shared by Odytor and the runner (standard library only)."""
from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROFILE_NAME = "python-pytest"
PROFILE_VERSION = "1"
NODE_PROFILE = "node-test"
BROWSER_PROFILE = "browser-playwright"

PYTHON_LINT_PROFILE = "python-lint"
PYTHON_TYPECHECK_PROFILE = "python-typecheck"
PYTHON_BUILD_PROFILE = "python-build"
NODE_LINT_PROFILE = "node-lint"
NODE_TYPECHECK_PROFILE = "node-typecheck"
NODE_BUILD_PROFILE = "node-build"
CMAKE_BUILD_PROFILE = "cmake-build"
MAKE_BUILD_PROFILE = "make-build"
C_TEST_PROFILE = "c-test"


@dataclass(frozen=True)
class ProfileSpec:
    """What a profile is, fixed by the trusted code and never by a repository file.

    `tools` are what the pinned runner image must provide for the profile to be installed on a runner.
    Every profile runs with no network, a read-only root, no capabilities and an unprivileged user; the
    only network any profile ever gets is the dependency-preparation container of `needs_env` profiles.
    """
    name: str
    version: str
    kind: str                 # pytest | node_test | browser | linter | typechecker | builder | c_test
    runtime: str              # python | node | browser | c
    summary: str
    tools: tuple[str, ...]
    needs_env: bool = False
    produces_artifacts: bool = False
    commands: tuple[str, ...] = ()
    run_network: str = "none"


REGISTRY: dict[str, ProfileSpec] = {
    PROFILE_NAME: ProfileSpec(
        PROFILE_NAME, PROFILE_VERSION, "pytest", "python", "pytest on an exact tree with maintainer-approved pinned requirements",
        ("python",), needs_env=True, commands=("pytest",)),
    PYTHON_LINT_PROFILE: ProfileSpec(
        PYTHON_LINT_PROFILE, "1", "linter", "python", "Python code analysis and linting (ruff check or flake8)",
        ("python",), commands=("ruff", "lint")),
    PYTHON_TYPECHECK_PROFILE: ProfileSpec(
        PYTHON_TYPECHECK_PROFILE, "1", "typechecker", "python", "Python static type checking (mypy)",
        ("python",), commands=("mypy", "typecheck")),
    PYTHON_BUILD_PROFILE: ProfileSpec(
        PYTHON_BUILD_PROFILE, "1", "builder", "python", "Python package build verification (python -m build)",
        ("python",), commands=("build",)),
    NODE_PROFILE: ProfileSpec(
        NODE_PROFILE, "1", "node_test", "node", "node --test (TAP) on repository test files, no dependency installation",
        ("node",), commands=("node --test",)),
    NODE_LINT_PROFILE: ProfileSpec(
        NODE_LINT_PROFILE, "1", "linter", "node", "JavaScript/TypeScript code analysis (eslint)",
        ("node",), commands=("eslint", "lint")),
    NODE_TYPECHECK_PROFILE: ProfileSpec(
        NODE_TYPECHECK_PROFILE, "1", "typechecker", "node", "TypeScript static type checking (tsc --noEmit)",
        ("node",), commands=("tsc", "typecheck")),
    NODE_BUILD_PROFILE: ProfileSpec(
        NODE_BUILD_PROFILE, "1", "builder", "node", "Node/JavaScript build verification (npm run build)",
        ("node",), commands=("build",)),
    CMAKE_BUILD_PROFILE: ProfileSpec(
        CMAKE_BUILD_PROFILE, "1", "builder", "c", "C/C++ CMake configuration and build",
        ("cmake", "make", "compiler"), commands=("cmake", "make")),
    MAKE_BUILD_PROFILE: ProfileSpec(
        MAKE_BUILD_PROFILE, "1", "builder", "c", "C/C++ Make build",
        ("make", "compiler"), commands=("make",)),
    C_TEST_PROFILE: ProfileSpec(
        C_TEST_PROFILE, "1", "c_test", "c", "C/C++ test execution (ctest or make test)",
        ("make", "compiler"), commands=("ctest", "test")),
    BROWSER_PROFILE: ProfileSpec(
        BROWSER_PROFILE, "1", "browser", "browser", "Chromium against an application served inside the sandbox; scripted steps, assertions, screenshots, traces",
        ("python", "playwright", "chromium"), produces_artifacts=True,
        commands=("goto", "click", "fill", "type", "press", "select", "check", "uncheck", "hover", "wait_for", "wait_ms", "screenshot",
                  "snapshot", "assert_visible", "assert_hidden", "assert_text", "assert_count", "assert_requests", "wait_requests")),
}
PROFILES = {name: spec.version for name, spec in REGISTRY.items()}

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


_NODE_FILE = re.compile(r".+\.(?:mjs|cjs|js)")


def validate_node_selector(selector: object, test_dirs: tuple[str, ...]) -> str:
    """A repository-relative test file or directory below the test roots; node has no node-id syntax here."""
    if not isinstance(selector, str) or not selector:
        raise ProfileError("Selectors must be non-empty strings.")
    if len(selector.encode("utf-8", errors="replace")) > MAX_SELECTOR_BYTES:
        raise ProfileError("Selector exceeds the length limit.")
    path = selector[:-1] if selector.endswith("/") else selector
    if _SELECTOR_PATH.fullmatch(path) is None or any(part in {".", ".."} for part in path.split("/")):
        raise ProfileError("Selectors must be repository-relative paths without traversal or options.")
    if "." in path.rsplit("/", 1)[-1] and not _NODE_FILE.fullmatch(path):
        raise ProfileError("node-test selectors are .mjs, .cjs or .js files, or directories.")
    if not any(path == root or path.startswith(root + "/") for root in test_dirs):
        raise ProfileError("Selector is outside the configured test directories.")
    return selector


def validate_node_selectors(selectors: object, test_dirs: tuple[str, ...]) -> tuple[str, ...]:
    if selectors is None:
        return tuple(test_dirs)
    if not isinstance(selectors, (list, tuple)) or not selectors:
        raise ProfileError("selectors must be a non-empty list or omitted.")
    if len(selectors) > MAX_SELECTORS:
        raise ProfileError(f"At most {MAX_SELECTORS} selectors are allowed.")
    return tuple(dict.fromkeys(validate_node_selector(item, test_dirs) for item in selectors))


def build_node_argv(selectors: tuple[str, ...]) -> list[str]:
    """The only node invocation the profile permits.

    Node 22 treats a positional argument as a module or glob, never as a directory, so a directory selector becomes
    the three conventional test-file globs below it. Validated selectors contain no glob metacharacters and cannot
    start with '-', so a selector can neither inject a pattern nor an option.
    """
    patterns: list[str] = []
    for selector in selectors:
        path = selector.rstrip("/")
        if _NODE_FILE.fullmatch(path):
            patterns.append(path)
        else:
            patterns += [f"{path}/**/*.test.mjs", f"{path}/**/*.test.js", f"{path}/**/*.test.cjs"]
    return ["node", "--test", "--test-reporter=tap", *patterns]


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


def validate_path_selectors(selectors: object) -> tuple[str, ...]:
    if selectors is None:
        return ()
    if not isinstance(selectors, (list, tuple)):
        raise ProfileError("selectors must be a list of paths or omitted.")
    if len(selectors) > MAX_SELECTORS:
        raise ProfileError(f"At most {MAX_SELECTORS} selectors are allowed.")
    cleaned = []
    for s in selectors:
        if not isinstance(s, str) or not s.strip():
            raise ProfileError("Selectors must be non-empty strings.")
        p = s.strip().rstrip("/")
        if _SELECTOR_PATH.fullmatch(p) is None or any(part in {".", ".."} for part in p.split("/")):
            raise ProfileError(f"Invalid selector path: {s!r}")
        cleaned.append(p)
    return tuple(dict.fromkeys(cleaned))


def build_profile_argv(profile: str, selectors: tuple[str, ...]) -> list[str]:
    """Build the command argv for a given profile and validated selectors."""
    if profile == PROFILE_NAME:
        return build_argv(selectors)
    if profile == NODE_PROFILE:
        return build_node_argv(selectors)
    if profile == PYTHON_LINT_PROFILE:
        return ["python3", "-m", "ruff", "check", *(selectors if selectors else (".",))]
    if profile == PYTHON_TYPECHECK_PROFILE:
        return ["python3", "-m", "mypy", *(selectors if selectors else (".",))]
    if profile == PYTHON_BUILD_PROFILE:
        return ["python3", "-m", "build", "--no-isolation"]
    if profile == NODE_LINT_PROFILE:
        return ["npx", "--no-install", "eslint", *(selectors if selectors else (".",))]
    if profile == NODE_TYPECHECK_PROFILE:
        return ["npx", "--no-install", "tsc", "--noEmit"]
    if profile == NODE_BUILD_PROFILE:
        return ["npm", "run", "build"]
    if profile == CMAKE_BUILD_PROFILE:
        return ["sh", "-c", "cmake -B build -S . && cmake --build build"]
    if profile == MAKE_BUILD_PROFILE:
        return ["make"]
    if profile == C_TEST_PROFILE:
        return ["sh", "-c", "if [ -d build ]; then ctest --test-dir build --output-on-failure; else make test; fi"]
    raise ProfileError(f"Unknown execution profile: {profile}")


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
    extra: dict = field(default_factory=dict)  # operator-defined per-profile settings: {"node-test": {...}, "browser-playwright": {...}}

    @property
    def profiles(self) -> tuple[str, ...]:
        """Every profile the maintainer enabled for this repository; nothing else may run."""
        return (self.profile, *[n for n in self.extra if n != self.profile])


def load_policies(path: str | Path) -> dict[str, RepoPolicy]:
    """Parse the maintainer-controlled TOML policy file (keys are casefolded repositories)."""
    file = Path(path)
    with file.open("rb") as handle:
        data = tomllib.load(handle)
    templates = data.get("templates") or {}
    policies: dict[str, RepoPolicy] = {}
    for repo, raw_entry in (data.get("repos") or {}).items():
        if not is_repo(repo) or not isinstance(raw_entry, dict):
            raise ProfileError(f"Invalid repository policy '{repo}'.")
        entry = dict(raw_entry)
        template_name = entry.get("template")
        if template_name:
            if template_name not in templates or not isinstance(templates[template_name], dict):
                raise ProfileError(f"Unknown template '{template_name}' for {repo}.")
            entry = {**templates[template_name], **entry}
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
        if profile == PROFILE_NAME or req_name is not None:
            if not isinstance(req_name, str) or not req_name:
                raise ProfileError(f"{repo} requires an approved 'requirements' file.")
            req_path = (file.parent / req_name).resolve()
            if file.parent.resolve() not in req_path.parents:
                raise ProfileError("Requirements files must live beside the policy file.")
            text = validate_requirements(req_path.read_text(encoding="utf-8"))
        else:
            text = ""  # a profile without Python dependencies has nothing to approve
        extra: dict = {}
        for p in entry.get("profiles", []):
            if p in PROFILES and p != profile:
                extra.setdefault(p, {})
        for p_name in PROFILES:
            if p_name in entry and p_name not in {NODE_PROFILE, BROWSER_PROFILE} and p_name != profile:
                val = entry[p_name]
                extra[p_name] = val if isinstance(val, dict) else {}
        if NODE_PROFILE in entry:
            table = entry[NODE_PROFILE]
            node_dirs = table.get("test_dirs", dirs) if isinstance(table, dict) else None
            if not isinstance(node_dirs, list) or not node_dirs or any(
                    not isinstance(d, str) or _SELECTOR_PATH.fullmatch(d.rstrip("/")) is None for d in node_dirs):
                raise ProfileError(f"Invalid {NODE_PROFILE} settings for {repo}.")
            extra[NODE_PROFILE] = {"test_dirs": tuple(d.rstrip("/") for d in node_dirs)}
        if BROWSER_PROFILE in entry:
            from odytor.browser_spec import SpecError, validate_serve
            table = entry[BROWSER_PROFILE]
            try:
                if not isinstance(table, dict):
                    raise SpecError("the browser table must be an object.")
                app_profiles: dict[str, dict[str, Any]] = {}
                raw_profiles = table.get("profiles")
                if isinstance(raw_profiles, dict):
                    for prof_name, prof_cfg in raw_profiles.items():
                        if not isinstance(prof_name, str) or not prof_name:
                            raise SpecError("application launch profile names must be non-empty strings.")
                        app_profiles[prof_name] = validate_serve(prof_cfg)
                default_serve = validate_serve(table) if table.get("kind") else (
                    app_profiles.get("default") or (next(iter(app_profiles.values())) if app_profiles else None)
                )
                if default_serve is None:
                    raise SpecError("no default serve policy or application launch profile defined.")
                if "default" not in app_profiles:
                    app_profiles["default"] = default_serve
                extra[BROWSER_PROFILE] = {"serve": default_serve, "profiles": app_profiles}
            except SpecError as exc:
                raise ProfileError(f"Invalid {BROWSER_PROFILE} settings for {repo}: {exc}") from exc
        policies[repo.casefold()] = RepoPolicy(repo, profile, tuple(roots), text, requirements_digest(text), extra)
    return policies
