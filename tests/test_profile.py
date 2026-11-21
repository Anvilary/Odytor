"""Selector, requirement and policy validation for the maintainer-defined pytest profile."""
import pytest

from odytor.profile import (ProfileError, build_argv, load_policies, selector_matches, validate_requirements,
                              validate_selector, validate_selectors)

DIRS = ('tests', 'backend/tests')


@pytest.mark.parametrize('selector', [
    'tests', 'tests/', 'tests/test_a.py', 'tests/sub/test_a.py::test_x', 'tests/test_a.py::TestX::test_m',
    'tests/test_a.py::test_x[a-b 1]', 'backend/tests/test_b.py::test_y[1-2,3]'])
def test_valid_selectors(selector):
    assert validate_selector(selector, DIRS) == selector


@pytest.mark.parametrize('selector', [
    '', '/etc/passwd', '../tests/test_a.py', 'tests/../x.py', 'tests/./a.py', '-x', '--help', 'tests/a.py::', 'tests/a.py::::x',
    'tests/a.py::x\n', 'tests/a.py::x\x00', 'tests/a.py::x\\y', 'tests/a.py\n', 'src/a.py', 'testsx/a.py', 'tests//a.py',
    'tests/a.py::t' + 'x' * 600, '~/tests/a.py', 'C:/tests/a.py', 'tests/.hidden.py', 'tests::x', 'tests/a.py;rm', 'tests/$(id).py',
    'tests/a b.py', "tests/a.py::t'`id`", 5, None, ['tests']])
def test_invalid_selectors(selector):
    # Shell metacharacters are only unsafe in a shell; some pass as node IDs but never reach one.
    if selector == "tests/a.py::t'`id`":
        assert validate_selector(selector, DIRS) == selector
        return
    with pytest.raises(ProfileError):
        validate_selector(selector, DIRS)


def test_selector_collection_rules():
    assert validate_selectors(None, DIRS) == DIRS
    assert validate_selectors(['tests/a.py', 'tests/a.py'], DIRS) == ('tests/a.py',)
    for bad in ([], 'tests', ['tests'] * 0, ['tests/a.py'] * 1 + [f'tests/t{i}.py' for i in range(25)], {'a': 1}):
        with pytest.raises(ProfileError):
            validate_selectors(bad, DIRS)


def test_argv_is_fixed_and_selectors_follow_double_dash():
    argv = build_argv(('tests/a.py::t',))
    assert argv[:3] == ['python', '-m', 'pytest'] and argv[-2:] == ['--', 'tests/a.py::t']
    assert '-o' in argv and 'addopts=' in argv


@pytest.mark.parametrize('selector,node,expected', [
    ('tests', 'tests/a.py::t', True), ('tests/a.py', 'tests/a.py::t', True), ('tests/a.py::t', 'tests/a.py::t[1]', True),
    ('tests/a.py::T', 'tests/a.py::T::m', True), ('tests/a.py::t', 'tests/a.py::tx', False), ('tests', 'testsx/a.py::t', False),
    ('tests/a.py', 'tests/ab.py::t', False)])
def test_selector_matching(selector, node, expected):
    assert selector_matches(selector, node) is expected


def test_valid_requirements_are_normalized():
    text = "# approved\npytest==8.3.5\nrequests[socks]==2.32.0 ; python_version >= '3.9'\nattrs==23.1.0\n"
    out = validate_requirements(text)
    assert out.endswith('\n') and 'pytest==8.3.5' in out
    h = '--hash=sha256:' + 'a' * 64
    hashed = f"pytest==8.3.5 {h}\nattrs==23.1.0 \\\n    {h}\nrequests==2.32.0 ; python_version >= '3.9' {h}\n"
    assert validate_requirements(hashed).count('--hash') == 3


def test_hash_pinned_requirements_must_hash_every_package():
    h = '--hash=sha256:' + 'a' * 64
    for text in (f"pytest==8.3.5 {h}\nattrs==23.1.0\n", f"attrs==23.1.0\npytest==8.3.5 {h}\n", f"{h}\npytest==8.3.5\n"):
        with pytest.raises(ProfileError):
            validate_requirements(text)


@pytest.mark.parametrize('line', [
    'pytest>=8', 'git+https://evil/x.git', 'https://evil/x.whl', '-r other.txt', '-e .', '--index-url https://evil/',
    '--extra-index-url https://evil/', './local', 'pkg @ https://evil/x.whl', 'pkg==1.0 ; $(id)', 'pkg==1 --hash=md5:abc',
    '--find-links /tmp', '--trusted-host x', 'pkg===1.0', 'pkg==1.0 && id'])
def test_requirements_reject_unsafe_lines(line):
    with pytest.raises(ProfileError):
        validate_requirements(f'pytest==8.3.5\n{line}\n')


def test_requirements_must_pin_pytest_and_stay_bounded():
    with pytest.raises(ProfileError):
        validate_requirements('attrs==23.1.0\n')
    with pytest.raises(ProfileError):
        validate_requirements('pytest==8.3.5\n' + 'a==1\n' * 600)


def test_policy_file(tmp_path):
    (tmp_path / 'req.txt').write_text('pytest==8.3.5\n')
    (tmp_path / 'p.toml').write_text('[repos."Owner/Repo"]\nrequirements = "req.txt"\ntest_dirs = ["tests/", "t2"]\n')
    policy = load_policies(tmp_path / 'p.toml')['owner/repo']
    assert policy.test_dirs == ('tests', 't2') and len(policy.requirements_sha256) == 64
    (tmp_path / 'bad.toml').write_text('[repos."o/r"]\nrequirements = "../req.txt"\n')
    with pytest.raises(ProfileError):
        load_policies(tmp_path / 'bad.toml')
    (tmp_path / 'bad2.toml').write_text('[repos."o/r"]\nrequirements = "req.txt"\nprofile = "shell"\n')
    with pytest.raises(ProfileError):
        load_policies(tmp_path / 'bad2.toml')


def test_the_shipped_odysseus_policy_is_valid_hash_complete_and_pins_pytest():
    from pathlib import Path
    from odytor.profile import load_policies
    policy = load_policies(Path(__file__).resolve().parents[1] / 'deploy' / 'policies' / 'policy.toml')['odysseus-dev/odysseus']
    lines = [line for line in policy.requirements.splitlines() if line]
    assert len(lines) > 50 and all('==' in line and '--hash=sha256:' in line for line in lines)
    assert policy.test_dirs == ('tests',) and len(policy.requirements_sha256) == 64


def test_node_argv_expands_directories_to_globs_and_keeps_files_literal():
    from odytor.profile import build_node_argv, validate_node_selectors
    argv = build_node_argv(('tests/unit/a.test.mjs', 'tests/web'))
    assert argv[:3] == ['node', '--test', '--test-reporter=tap'] and argv[3] == 'tests/unit/a.test.mjs'
    assert argv[4:] == ['tests/web/**/*.test.mjs', 'tests/web/**/*.test.js', 'tests/web/**/*.test.cjs']
    for selector in ('tests/*', 'tests/{a,b}', 'tests/a?.mjs', 'tests/[x]', '-e', '--eval', 'tests/a b'):
        try:
            validate_node_selectors([selector], ('tests',))
        except Exception:
            continue
        raise AssertionError(f'selector {selector!r} must be rejected so it can never become a glob or an option')


def test_multilanguage_profile_argv_generation():
    from odytor.profile import (
        build_profile_argv, validate_path_selectors,
        PYTHON_LINT_PROFILE, PYTHON_TYPECHECK_PROFILE, PYTHON_BUILD_PROFILE,
        NODE_LINT_PROFILE, NODE_TYPECHECK_PROFILE, NODE_BUILD_PROFILE,
        CMAKE_BUILD_PROFILE, MAKE_BUILD_PROFILE, C_TEST_PROFILE,
    )
    assert build_profile_argv(PYTHON_LINT_PROFILE, ('src/foo.py',)) == ['python3', '-m', 'ruff', 'check', 'src/foo.py']
    assert build_profile_argv(PYTHON_TYPECHECK_PROFILE, ()) == ['python3', '-m', 'mypy', '.']
    assert build_profile_argv(PYTHON_BUILD_PROFILE, ()) == ['python3', '-m', 'build', '--no-isolation']
    assert build_profile_argv(NODE_LINT_PROFILE, ()) == ['npx', '--no-install', 'eslint', '.']
    assert build_profile_argv(NODE_TYPECHECK_PROFILE, ()) == ['npx', '--no-install', 'tsc', '--noEmit']
    assert build_profile_argv(NODE_BUILD_PROFILE, ()) == ['npm', 'run', 'build']
    assert build_profile_argv(CMAKE_BUILD_PROFILE, ()) == ['sh', '-c', 'cmake -B build -S . && cmake --build build']
    assert build_profile_argv(MAKE_BUILD_PROFILE, ()) == ['make']
    assert 'ctest' in build_profile_argv(C_TEST_PROFILE, ())[2]

    assert validate_path_selectors(['src/a.py', 'src/b.py']) == ('src/a.py', 'src/b.py')
    with pytest.raises(Exception):
        validate_path_selectors(['../etc/passwd'])


def test_template_inheritance_in_load_policies():
    from pathlib import Path
    from odytor.profile import load_policies
    policies = load_policies(Path(__file__).resolve().parents[1] / 'deploy' / 'policies' / 'policy.toml')
    smoke_policy = policies['alteixeira20/odytor-smoke-test']
    assert 'python-pytest' in smoke_policy.profiles
    assert 'python-lint' in smoke_policy.profiles
    assert 'node-test' in smoke_policy.profiles
    assert 'cmake-build' in smoke_policy.profiles
    assert 'make-build' in smoke_policy.profiles


def test_load_policies_publish_exemptions():
    from pathlib import Path
    from odytor.profile import load_policies
    policies = load_policies(Path(__file__).resolve().parents[1] / 'deploy' / 'policies' / 'policy.toml')
    ody_policy = policies['odysseus-dev/odysseus']
    assert 'publish' in ody_policy.extra
    exemptions = ody_policy.extra['publish']['exempt_paths']
    assert "frontend/src/features/notes/application/noteSession.js" in exemptions
    assert "static/js/features/notes/application/noteSession.js" in exemptions


def test_playwright_spec_selector_validation():
    from odytor.profile import (
        ProfileError, validate_playwright_spec_selector, validate_playwright_spec_selectors,
    )
    allowed = ("tests/e2e/notes-qa/notes-qa-regressions.spec.mjs", "tests/e2e/notes-qa/notes-edge-cases.spec.mjs")
    allowed_dirs = ("tests/e2e/notes-qa",)

    # Valid full selector with literal title
    canon, parsed = validate_playwright_spec_selector(
        "tests/e2e/notes-qa/notes-qa-regressions.spec.mjs::typing during a checklist request survives the response and the next save",
        allowed_specs=allowed, allowed_dirs=allowed_dirs,
    )
    assert canon == "tests/e2e/notes-qa/notes-qa-regressions.spec.mjs::typing during a checklist request survives the response and the next save"
    assert parsed["spec_path"] == "tests/e2e/notes-qa/notes-qa-regressions.spec.mjs"
    assert parsed["test_title"] == "typing during a checklist request survives the response and the next save"

    # Valid selector without title
    canon2, parsed2 = validate_playwright_spec_selector(
        "tests/e2e/notes-qa/notes-qa-regressions.spec.mjs",
        allowed_specs=allowed, allowed_dirs=allowed_dirs,
    )
    assert canon2 == "tests/e2e/notes-qa/notes-qa-regressions.spec.mjs"
    assert parsed2["test_title"] is None

    # Rejection of traversal
    with pytest.raises(ProfileError, match="traversal"):
        validate_playwright_spec_selector("../evil.spec.mjs", allowed_specs=allowed, allowed_dirs=allowed_dirs)

    # Rejection of flags / options
    with pytest.raises(ProfileError, match="dash"):
        validate_playwright_spec_selector("-g", allowed_specs=allowed, allowed_dirs=allowed_dirs)
    with pytest.raises(ProfileError, match="dash"):
        validate_playwright_spec_selector("tests/e2e/notes-qa/notes-qa-regressions.spec.mjs::-g", allowed_specs=allowed, allowed_dirs=allowed_dirs)

    # Rejection of non-spec extensions
    with pytest.raises(ProfileError, match=r"\.spec\.mjs"):
        validate_playwright_spec_selector("tests/test_app.py", allowed_specs=allowed, allowed_dirs=allowed_dirs)

    # Rejection of shell metacharacters in title
    with pytest.raises(ProfileError, match="invalid characters"):
        validate_playwright_spec_selector("tests/e2e/notes-qa/notes-qa-regressions.spec.mjs::test; rm -rf /", allowed_specs=allowed, allowed_dirs=allowed_dirs)
    with pytest.raises(ProfileError, match="invalid characters"):
        validate_playwright_spec_selector("tests/e2e/notes-qa/notes-qa-regressions.spec.mjs::test | cat", allowed_specs=allowed, allowed_dirs=allowed_dirs)

    # Rejection of unapproved spec path
    with pytest.raises(ProfileError, match="maintainer-approved"):
        validate_playwright_spec_selector("tests/unapproved/evil.spec.mjs", allowed_specs=allowed, allowed_dirs=allowed_dirs)

    # validate_playwright_spec_selectors list handling
    canons, parsed_list = validate_playwright_spec_selectors(
        ["tests/e2e/notes-qa/notes-qa-regressions.spec.mjs::typing during a checklist request survives the response and the next save"],
        allowed_specs=allowed, allowed_dirs=allowed_dirs,
    )
    assert len(canons) == 1
    assert len(parsed_list) == 1

    # An omitted selector is unambiguous only for a single allowed default.
    canons_def, _ = validate_playwright_spec_selectors(None, allowed_specs=allowed[:1])
    assert canons_def == allowed[:1]
    another = "tests/e2e/other/notes-qa-regressions.spec.mjs"
    with pytest.raises(ProfileError, match="one selector"):
        validate_playwright_spec_selectors([allowed[0], another])
    with pytest.raises(ProfileError, match="explicitly"):
        validate_playwright_spec_selectors(None, allowed_specs=allowed)


def test_load_policies_playwright_spec():
    from pathlib import Path
    from odytor.profile import PLAYWRIGHT_SPEC_PROFILE, load_policies
    policies = load_policies(Path(__file__).resolve().parents[1] / 'deploy' / 'policies' / 'policy.toml')
    ody_policy = policies['odysseus-dev/odysseus']
    assert PLAYWRIGHT_SPEC_PROFILE in ody_policy.profiles
    assert PLAYWRIGHT_SPEC_PROFILE in ody_policy.extra
    cfg = ody_policy.extra[PLAYWRIGHT_SPEC_PROFILE]
    assert "tests/e2e/notes-qa/notes-qa-regressions.spec.mjs" in cfg["specs"]
    assert cfg["launcher"] == "tests/e2e/notes-qa/run_notes_qa.py"


