"""Tests for quick_fix patch validator and security boundaries."""

import hashlib
import io
import textwrap
import pytest
from odytor.quick_fix import (
    PatchSummary,
    _validate_path,
    validate_patch,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_CHANGED_LINES,
    DEFAULT_MAX_BYTES,
)
from odytor.errors import SecurityError, ValidationError


# ---------------------------------------------------------------------------
# Path validation tests
# ---------------------------------------------------------------------------

def test_validate_path_valid() -> None:
    assert _validate_path("src/foo.py") == "src/foo.py"
    assert _validate_path("lib/utils/helpers.go") == "lib/utils/helpers.go"


def test_validate_path_rejects_traversal() -> None:
    with pytest.raises(SecurityError, match="traversal"):
        _validate_path("../../etc/passwd")
    with pytest.raises(SecurityError, match="traversal"):
        _validate_path("src/../../../etc/passwd")


def test_validate_path_rejects_absolute() -> None:
    with pytest.raises(SecurityError):
        _validate_path("/etc/passwd")


def test_validate_path_rejects_git_metadata() -> None:
    with pytest.raises(SecurityError, match="git metadata"):
        _validate_path(".git/config")
    with pytest.raises(SecurityError, match="github"):
        _validate_path(".github/workflows/ci.yml")


def test_validate_path_rejects_deploy_dirs() -> None:
    with pytest.raises(SecurityError, match="Restricted directory"):
        _validate_path("deploy/docker-compose.yml")
    with pytest.raises(SecurityError, match="Restricted directory"):
        _validate_path("scripts/release.sh")


def test_validate_path_rejects_sensitive_keywords() -> None:
    with pytest.raises(SecurityError, match="sensitive keyword"):
        _validate_path("src/auth/middleware.py")
    with pytest.raises(SecurityError, match="sensitive keyword"):
        _validate_path("config/oauth_settings.py")


def test_validate_path_rejects_key_extensions() -> None:
    with pytest.raises(SecurityError, match="Cryptographic"):
        _validate_path("certs/server.pem")
    with pytest.raises(SecurityError, match="Cryptographic"):
        _validate_path("keys/id.key")


def test_validate_path_rejects_script_extensions() -> None:
    with pytest.raises(SecurityError, match="Executable"):
        _validate_path("tools/build.sh")


def test_validate_path_rejects_lockfiles() -> None:
    with pytest.raises(SecurityError, match="Dependency"):
        _validate_path("requirements.txt")
    with pytest.raises(SecurityError, match="Dependency"):
        _validate_path("yarn.lock")


def test_validate_path_rejects_dockerfile() -> None:
    with pytest.raises(SecurityError, match="Container"):
        _validate_path("Dockerfile")
    with pytest.raises(SecurityError, match="Container"):
        _validate_path("app.dockerfile")


# ---------------------------------------------------------------------------
# Patch validation tests
# ---------------------------------------------------------------------------

SIMPLE_PATCH = textwrap.dedent("""\
    diff --git a/src/foo.py b/src/foo.py
    index abc123..def456 100644
    --- a/src/foo.py
    +++ b/src/foo.py
    @@ -1,3 +1,3 @@
     line 1
    -old line 2
    +new line 2
     line 3
""")


def test_validate_patch_valid() -> None:
    summary = validate_patch(SIMPLE_PATCH)
    assert summary.files == ("src/foo.py",)
    assert summary.added_lines == 1
    assert summary.removed_lines == 1
    assert summary.sha256 == hashlib.sha256(SIMPLE_PATCH.encode()).hexdigest()


def test_validate_patch_empty() -> None:
    with pytest.raises(ValidationError, match="empty"):
        validate_patch("   ")


def test_validate_patch_too_large() -> None:
    big_patch = "diff --git a/x b/x\n" + "+" * (DEFAULT_MAX_BYTES + 1)
    with pytest.raises(ValidationError, match="size.*exceeds"):
        validate_patch(big_patch)


def test_validate_patch_too_many_files() -> None:
    patch = ""
    for i in range(DEFAULT_MAX_FILES + 1):
        patch += textwrap.dedent(f"""\
            diff --git a/src/f{i}.py b/src/f{i}.py
            index abc..def 100644
            --- a/src/f{i}.py
            +++ b/src/f{i}.py
            @@ -1 +1 @@
            -old
            +new
        """)
    with pytest.raises(ValidationError, match="exceeding limit"):
        validate_patch(patch)


def test_validate_patch_rejects_binary() -> None:
    patch = "diff --git a/x b/x\nBinary files a/x and b/x differ\n"
    with pytest.raises(ValidationError, match="Binary"):
        validate_patch(patch)


def test_validate_patch_rejects_new_file() -> None:
    patch = textwrap.dedent("""\
        diff --git a/new.py b/new.py
        new file mode 100644
        index 000000..abc123
        --- /dev/null
        +++ b/new.py
        @@ -0,0 +1 @@
        +hello
    """)
    with pytest.raises(ValidationError, match="creation"):
        validate_patch(patch)


def test_validate_patch_rejects_sensitive_path() -> None:
    patch = textwrap.dedent("""\
        diff --git a/src/auth/login.py b/src/auth/login.py
        index abc..def 100644
        --- a/src/auth/login.py
        +++ b/src/auth/login.py
        @@ -1 +1 @@
        -old
        +new
    """)
    with pytest.raises(SecurityError, match="sensitive keyword"):
        validate_patch(patch)


def test_validate_patch_too_many_lines() -> None:
    plus_lines = "\n".join(f"+line {i}" for i in range(DEFAULT_MAX_CHANGED_LINES + 1))
    minus_lines = "\n".join(f"-old {i}" for i in range(DEFAULT_MAX_CHANGED_LINES + 1))
    hunk_len = DEFAULT_MAX_CHANGED_LINES + 1
    patch = textwrap.dedent(f"""\
        diff --git a/src/big.py b/src/big.py
        index abc..def 100644
        --- a/src/big.py
        +++ b/src/big.py
        @@ -1,{hunk_len} +1,{hunk_len} @@
    """) + minus_lines + "\n" + plus_lines + "\n"
    with pytest.raises(ValidationError, match="exceeding limit"):
        validate_patch(patch)

from unittest.mock import Mock
from odytor.quick_fix import apply_quick_fix
from odytor.errors import APIError, GitOperationError, RevisionMismatchError
from test_git_workspace import client_for, run, repository, local_fetch


@pytest.mark.parametrize('patch', [
    SIMPLE_PATCH.replace('+++ b/src/foo.py', '+++ b/other.py'),
    SIMPLE_PATCH.replace('a/src/foo.py b/src/foo.py', 'a/other.py b/src/foo.py'),
    SIMPLE_PATCH.replace('@@ -1,3 +1,3 @@', '@@ -1,4 +1,3 @@'),
    SIMPLE_PATCH + 'garbage\n',
    SIMPLE_PATCH.replace('index abc123..def456 100644', 'old mode 100644\nnew mode 100755'),
    SIMPLE_PATCH.replace('src/foo.py', '../foo.py'),
    SIMPLE_PATCH.replace('src/foo.py', '"src/foo.py"'),
])
def test_rejects_malformed_patch(patch):
    with pytest.raises((ValidationError, SecurityError)):
        validate_patch(patch)


@pytest.mark.parametrize('key', ['max_files', 'max_changed_lines', 'max_bytes'])
def test_invalid_limits(key):
    with pytest.raises(ValidationError):
        validate_patch(SIMPLE_PATCH, **{key: 0})


def test_counts_header_like_content():
    patch = 'diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n---old\n+++new\n'
    result = validate_patch(patch)
    assert result.added_lines == result.removed_lines == 1


def test_quick_fix_atomic_commit(local_fetch):
    repo, sha = local_fetch
    client = client_for(sha)
    def mutation(query, variables):
        import base64
        value = variables['input']
        assert value['expectedHeadOid'] == sha
        assert value['branch']['repositoryNameWithOwner'] == 'owner/repo'
        assert base64.b64decode(value['fileChanges']['additions'][0]['contents']) == b'line 1\nnew line 2\nline 3\n'
        # Build the same tree separately, without changing the fixture branch.
        blob = run(repo, 'hash-object', '-w', '--stdin', input=b'line 1\nnew line 2\nline 3\n').decode().strip()
        run(repo, 'update-index', '--cacheinfo', f'100644,{blob},src/foo.py')
        tree = run(repo, 'write-tree').decode().strip()
        return {'createCommitOnBranch': {'commit': {'oid': 'b'*40, 'url': 'https://github.com/commit',
            'tree': {'oid': tree}, 'parents': {'totalCount': 1, 'nodes': [{'oid': sha}]}}}}
    client.graphql.side_effect = mutation
    result = apply_quick_fix(client, 'owner/repo', 1, sha, SIMPLE_PATCH, 'Fix line')
    assert result['new_sha'] == 'b'*40
    assert result['validation']['tests_run'] is False
    assert run(repo, 'rev-parse', 'HEAD').decode().strip() == sha
    assert (repo / 'src/foo.py').read_text().startswith('line 1\nold')
    client.graphql.assert_called_once()


def test_partial_multifile_patch_never_mutates(local_fetch):
    _, sha = local_fetch
    client = client_for(sha)
    patch = SIMPLE_PATCH + 'diff --git a/missing.py b/missing.py\n--- a/missing.py\n+++ b/missing.py\n@@ -1 +1 @@\n-old\n+new\n'
    with pytest.raises(SecurityError):
        apply_quick_fix(client, 'owner/repo', 1, sha, patch, 'Fix')
    client.graphql.assert_not_called()


def test_changed_head_before_commit(local_fetch):
    _, sha = local_fetch
    client = client_for(sha)
    initial = client.get_pr.return_value
    moved = dict(initial, head=dict(initial['head'], sha='b'*40))
    client.get_pr.side_effect = [initial, moved]
    with pytest.raises(RevisionMismatchError):
        apply_quick_fix(client, 'owner/repo', 1, sha, SIMPLE_PATCH, 'Fix')
    client.graphql.assert_not_called()


def test_lost_mutation_response(local_fetch):
    _, sha = local_fetch
    client = client_for(sha)
    client.graphql.side_effect = APIError('connection lost')
    with pytest.raises(APIError) as err:
        apply_quick_fix(client, 'owner/repo', 1, sha, SIMPLE_PATCH, 'Fix')
    assert err.value.diagnostic['outcome'] == 'unknown'
    client.graphql.assert_called_once()


@pytest.mark.parametrize('mode', ['100755', '120000', '160000'])
def test_actual_unsafe_mode_never_mutates(local_fetch, mode):
    repo, sha = local_fetch
    blob = run(repo, 'rev-parse', 'HEAD:src/foo.py').decode().strip()
    if mode == '160000':
        blob = sha
    run(repo, 'update-index', '--cacheinfo', f'{mode},{blob},src/foo.py')
    run(repo, 'commit', '-qm', 'unsafe mode')
    sha = run(repo, 'rev-parse', 'HEAD').decode().strip()
    client = client_for(sha)
    with pytest.raises(SecurityError):
        apply_quick_fix(client, 'owner/repo', 1, sha, SIMPLE_PATCH, 'Fix')
    client.graphql.assert_not_called()


# ---------------------------------------------------------------------------
# Repository-scoped path keyword exemption regression tests
# ---------------------------------------------------------------------------

def test_validate_path_odysseus_notes_session_exemptions() -> None:
    p1 = "frontend/src/features/notes/application/noteSession.js"
    p2 = "static/js/features/notes/application/noteSession.js"
    p3 = "tests/notesDraftRecovery.test.mjs"

    # Exact paths accepted for odysseus-dev/odysseus
    assert _validate_path(p1, repo="odysseus-dev/odysseus") == p1
    assert _validate_path(p2, repo="odysseus-dev/odysseus") == p2
    assert _validate_path(p3, repo="odysseus-dev/odysseus") == p3

    # Case-insensitive repository matching
    assert _validate_path(p1, repo="Odysseus-Dev/Odysseus") == p1
    assert _validate_path(p2, repo="ODYSSEUS-DEV/ODYSSEUS") == p2


def test_validate_path_odysseus_notes_session_denied_in_other_repos() -> None:
    p1 = "frontend/src/features/notes/application/noteSession.js"
    p2 = "static/js/features/notes/application/noteSession.js"

    # Denied when repo is another repository
    for other_repo in ("owner/repo", "alteixeira20/odytor-smoke-test", "odysseus-dev/other", ""):
        with pytest.raises(SecurityError, match="sensitive keyword pattern"):
            _validate_path(p1, repo=other_repo)
        with pytest.raises(SecurityError, match="sensitive keyword pattern"):
            _validate_path(p2, repo=other_repo)

    # Denied when repo is omitted (None)
    with pytest.raises(SecurityError, match="sensitive keyword pattern"):
        _validate_path(p1)
    with pytest.raises(SecurityError, match="sensitive keyword pattern"):
        _validate_path(p2)


def test_validate_path_nearby_protected_paths_denied_in_odysseus() -> None:
    repo = "odysseus-dev/odysseus"

    # Nearby non-exempt session paths
    nearby_session_paths = (
        "frontend/src/features/notes/application/anotherSession.js",
        "frontend/src/features/notes/application/session.js",
        "frontend/src/features/notes/application/authSession.js",
        "frontend/src/features/auth/noteSession.js",
        "static/js/auth/noteSession.js",
        "frontend/src/features/notes/ui/editorSession.js",
    )
    for path in nearby_session_paths:
        with pytest.raises(SecurityError, match="sensitive keyword pattern"):
            _validate_path(path, repo=repo)

    # Other sensitive keywords in notes
    other_sensitive_paths = (
        "frontend/src/features/notes/application/noteAuth.js",
        "frontend/src/features/notes/application/secretHelper.js",
        "frontend/src/features/notes/domain/tokenValidator.js",
        "frontend/src/features/notes/application/notePassword.js",
    )
    for path in other_sensitive_paths:
        with pytest.raises(SecurityError, match="sensitive keyword pattern"):
            _validate_path(path, repo=repo)


def test_validate_path_exemption_preserves_structural_and_extension_checks() -> None:
    repo = "odysseus-dev/odysseus"

    # Directory traversal
    with pytest.raises(SecurityError, match="traversal"):
        _validate_path("frontend/src/features/notes/application/../application/noteSession.js", repo=repo)
    with pytest.raises(SecurityError, match="traversal"):
        _validate_path("../frontend/src/features/notes/application/noteSession.js", repo=repo)

    # Prohibited directories
    with pytest.raises(SecurityError, match="Restricted directory"):
        _validate_path("scripts/frontend/src/features/notes/application/noteSession.js", repo=repo)
    with pytest.raises(SecurityError, match="Restricted directory"):
        _validate_path("deploy/noteSession.js", repo=repo)
    with pytest.raises(SecurityError, match="github config"):
        _validate_path(".github/frontend/src/features/notes/application/noteSession.js", repo=repo)

    # Executable and cryptographic extensions on non-exempt paths
    with pytest.raises(SecurityError, match="Executable script/binary"):
        _validate_path("frontend/src/features/notes/application/runner.sh", repo=repo)
    with pytest.raises(SecurityError, match="Cryptographic key/cert"):
        _validate_path("frontend/src/features/notes/application/cert.pem", repo=repo)

    # Appending extensions to exempted path is not exempt and rejected by keyword check
    with pytest.raises(SecurityError, match="sensitive keyword pattern"):
        _validate_path("frontend/src/features/notes/application/noteSession.js.sh", repo=repo)
    with pytest.raises(SecurityError, match="sensitive keyword pattern"):
        _validate_path("frontend/src/features/notes/application/noteSession.js.key", repo=repo)

    # Sensitive identity / environment files
    with pytest.raises(SecurityError, match="Sensitive identity/environment"):
        _validate_path("frontend/src/features/notes/application/.env", repo=repo)


def test_validate_patch_with_odysseus_notes_session_paths() -> None:
    patch = textwrap.dedent("""\
        diff --git a/frontend/src/features/notes/application/noteSession.js b/frontend/src/features/notes/application/noteSession.js
        index 0000000..1111111 100644
        --- a/frontend/src/features/notes/application/noteSession.js
        +++ b/frontend/src/features/notes/application/noteSession.js
        @@ -1,2 +1,3 @@
         line 1
        +line 2
         line 3
        diff --git a/tests/notesDraftRecovery.test.mjs b/tests/notesDraftRecovery.test.mjs
        index 0000000..2222222 100644
        --- a/tests/notesDraftRecovery.test.mjs
        +++ b/tests/notesDraftRecovery.test.mjs
        @@ -1,2 +1,3 @@
         test 1
        +test 2
         test 3
    """)

    # Accepted for odysseus-dev/odysseus
    summary = validate_patch(patch, repo="odysseus-dev/odysseus")
    assert summary.files == (
        "frontend/src/features/notes/application/noteSession.js",
        "tests/notesDraftRecovery.test.mjs",
    )
    assert summary.added_lines == 2

    # Denied for other repo
    with pytest.raises(SecurityError, match="sensitive keyword pattern"):
        validate_patch(patch, repo="owner/repo")

    # Denied when repo is omitted
    with pytest.raises(SecurityError, match="sensitive keyword pattern"):
        validate_patch(patch)


def test_component_words_prevents_false_positives_on_common_words() -> None:
    from odytor.quick_fix import _component_words, register_path_keyword_exemptions

    # Substring checks previously matched 'auth' in 'author' or 'token' in 'tokenizer'
    assert "auth" not in _component_words("author.py")
    assert "token" not in _component_words("tokenizer.py")
    assert "auth" not in _component_words("authentic.js")
    assert "session" not in _component_words("concession.py")

    # Word-boundary checks catch compound forms
    assert "auth" in _component_words("auth_middleware.py")
    assert "auth" in _component_words("authMiddleware.js")
    assert "session" in _component_words("session_manager.py")
    assert "session" in _component_words("userSession.ts")

    # Path validation allows common non-sensitive source files
    assert _validate_path("src/author.py") == "src/author.py"
    assert _validate_path("src/tokenizer.py") == "src/tokenizer.py"
    assert _validate_path("src/authentic.js") == "src/authentic.js"

    # Compound forms remain blocked without exemptions
    with pytest.raises(SecurityError, match="sensitive keyword pattern"):
        _validate_path("src/auth_middleware.py")
    with pytest.raises(SecurityError, match="sensitive keyword pattern"):
        _validate_path("src/authMiddleware.js")
    with pytest.raises(SecurityError, match="sensitive keyword pattern"):
        _validate_path("src/session_manager.py")

    # Declarative registration allows new exemptions
    test_repo = "example-org/example-repo"
    register_path_keyword_exemptions(test_repo, ["src/session_manager.py"])
    assert _validate_path("src/session_manager.py", repo=test_repo) == "src/session_manager.py"
    # Other repos still reject it
    with pytest.raises(SecurityError, match="sensitive keyword pattern"):
        _validate_path("src/session_manager.py", repo="other-org/other-repo")


