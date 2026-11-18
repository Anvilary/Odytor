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
