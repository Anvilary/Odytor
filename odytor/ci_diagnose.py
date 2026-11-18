"""CI failure diagnosis, log extraction, and annotation correlation."""

from __future__ import annotations

import io
import re
import stat
import zipfile
from pathlib import PurePosixPath
from typing import Any

from odytor.diagnostics import redact_text, safe_payload, safe_text
from odytor.errors import APIError, RevisionMismatchError, ValidationError
from odytor.github import GitHubClient, validate_repo, validate_sha

_FAILURE_LINE_RE = re.compile(
    r"FAILED\s|AssertionError|Traceback|fatal error|exception|\berror:|##\[error\]|"
    r"runner.*(?:lost|offline)|operation was canceled|"
    r"could not compile|build error|syntax error|compilation error|"
    r"eslint.*error|ruff.*failed|flake8|mypy: error|TS\d{4}:",
    re.I,
)

_INFRA_RE = re.compile(
    r"runner.*(?:lost|offline)|operation was canceled|startup_failure|"
    r"provisioning|hosted runner|agent lost contact",
    re.I,
)
_TYPE_RE = re.compile(
    r"mypy: error|Incompatible types|Type error|TS\d{4}:|pyright: error",
    re.I,
)
_LINT_RE = re.compile(
    r"ruff(?:\s+check)?\s+failed|eslint.*error|flake8.*:\s*E\d|pylint.*error|"
    r"Prettier found formatting errors",
    re.I,
)
_COMPILER_RE = re.compile(
    r"could not compile|build error:|syntax error:|fatal error:|undefined reference to",
    re.I,
)
_TEST_RE = re.compile(
    r"FAILED\s|AssertionError|Traceback|=== FAIL:|Test suite failed|failed \d+ of \d+ tests",
    re.I,
)

_FILE_LINE_RE = re.compile(
    r"([\w./-]+\.(?:py|js|ts|tsx|jsx|go|rs|c|cpp|h|java|yml|yaml)):(\d+)(?::(\d+))?"
)


def archive_logs(raw: bytes, *, max_total_bytes: int = 8388608) -> list[tuple[str, str]]:
    """Safely read bounded ZIP log archive in memory without extracting to filesystem."""
    output: list[tuple[str, str]] = []
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) > 1000:
                raise ValidationError("Log archive contains too many files (>1000).")
            for member in members:
                path = PurePosixPath(member.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in member.filename
                    or ":" in member.filename
                    or "\x00" in member.filename
                    or stat.S_ISLNK(member.external_attr >> 16)
                ):
                    raise ValidationError(f"Unsafe file path in log archive: '{member.filename}'.")
                if member.is_dir():
                    continue
                total += member.file_size
                if (
                    total > max_total_bytes
                    or member.file_size > max_total_bytes
                    or member.file_size / max(member.compress_size, 1) > 200
                ):
                    raise ValidationError("Log archive exceeds safe decompression limits.")
                with archive.open(member) as stream:
                    content = stream.read(min(member.file_size + 1, max_total_bytes + 1))
                if len(content) != member.file_size:
                    raise ValidationError("Log archive member size mismatch.")
                output.append((member.filename, content.decode("utf-8", "replace")))
    except (zipfile.BadZipFile, RuntimeError, EOFError) as error:
        raise ValidationError(f"Invalid log archive: {error}") from error
    return output


def classify_diagnostic(text: str) -> str:
    """Classify the diagnostic category based on matched line text."""
    if _INFRA_RE.search(text):
        return "infrastructure_failure"
    if _TYPE_RE.search(text):
        return "type_check_failure"
    if _LINT_RE.search(text):
        return "lint_failure"
    if _COMPILER_RE.search(text):
        return "compiler_failure"
    if _TEST_RE.search(text):
        return "test_failure"
    return "step_failure"

def extract_failure_excerpts(
    content: str,
    *,
    max_excerpts: int = 50,
    context_lines: int = 2,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Extract bounded failure lines with surrounding context and file/line coordinates."""
    if max_excerpts <= 0 or context_lines < 0:
        raise ValidationError("Excerpt limit must be positive and context lines nonnegative.")
    cleaned = redact_text(content)
    lines = cleaned.splitlines()

    matched_indices = [idx for idx, line in enumerate(lines) if _FAILURE_LINE_RE.search(line)]
    if not matched_indices:
        return [], []

    # Preserve failure anchors before spending the remaining budget on context.
    selected_indices: set[int] = set(matched_indices[:max_excerpts])
    for idx in matched_indices[:max_excerpts]:
        for context in range(max(0, idx - context_lines), min(len(lines), idx + context_lines + 1)):
            if len(selected_indices) >= max_excerpts:
                break
            selected_indices.add(context)
    sorted_indices = sorted(selected_indices)

    excerpts: list[dict[str, Any]] = []
    categories: set[str] = set()

    for idx in sorted_indices:
        line_text = safe_text(lines[idx], 1024)
        cat = classify_diagnostic(line_text)
        if _FAILURE_LINE_RE.search(line_text):
            categories.add(cat)

        file_match = _FILE_LINE_RE.search(line_text)
        file_path = file_match.group(1) if file_match else None
        line_num = int(file_match.group(2)) if file_match else None
        col_num = int(file_match.group(3)) if file_match and file_match.group(3) else None

        excerpts.append(
            {
                "log_line": idx + 1,
                "text": line_text,
                "category": cat if _FAILURE_LINE_RE.search(line_text) else "context",
                "file": file_path,
                "line": line_num,
                "column": col_num,
            }
        )

    return excerpts, sorted(categories)


def diagnose_ci(
    client: GitHubClient,
    repo: str,
    number: int,
    *,
    expected_head: str | None = None,
    run_id: int | None = None,
    job_id: int | None = None,
    max_excerpts_per_job: int = 30,
    include_annotations: bool = True,
    include_code_scanning: bool = True,
) -> dict[str, Any]:
    """Diagnose CI failures across Actions runs, jobs, steps, logs, and check annotations."""
    validate_repo(repo)
    if number < 1:
        raise ValidationError(f"Pull request number must be positive (got {number}).")

    if expected_head:
        expected_head = validate_sha(expected_head, "expected_head")
    if not 1 <= max_excerpts_per_job <= 100:
        raise ValidationError("max_excerpts_per_job must be between 1 and 100.")
    if run_id is not None and run_id < 1:
        raise ValidationError("run_id must be positive.")
    if job_id is not None and job_id < 1:
        raise ValidationError("job_id must be positive.")

    pr = client.get_pr(repo, number)
    head_sha = pr.get("head", {}).get("sha", "")
    if expected_head and head_sha.casefold() != expected_head.casefold():
        raise RevisionMismatchError(expected=expected_head, actual=head_sha)

    runs_data = client.get_workflow_runs(repo, head_sha=head_sha)
    runs = runs_data.get("workflow_runs", [])
    evidence_complete = len(runs) < 30 and runs_data.get("total_count", len(runs)) <= len(runs)
    matching_runs = [r for r in runs if str(r.get("head_sha", "")).casefold() == head_sha.casefold()]
    if len(matching_runs) != len(runs):
        evidence_complete = False
    runs = matching_runs

    if run_id is not None:
        selected_runs = [r for r in runs if r.get("id") == run_id]
        if not selected_runs:
            evidence_complete = False
        runs = selected_runs

    diagnosed_jobs: list[dict[str, Any]] = []
    overall_categories: set[str] = set()
    logs_available = True
    selected_job_found = False
    jobs_limited = False
    code_scanning_available = False

    for run in runs:
        r_id = run.get("id")
        r_conclusion = run.get("conclusion")
        r_name = run.get("name")

        jobs_data = client.get_run_jobs(repo, r_id)
        jobs = jobs_data.get("jobs", [])
        if len(jobs) >= 100 or jobs_data.get("total_count", len(jobs)) > len(jobs):
            evidence_complete = False

        if job_id is not None:
            selected_jobs = [j for j in jobs if j.get("id") == job_id]
            if not selected_jobs:
                evidence_complete = False
            else:
                selected_job_found = True
            jobs = selected_jobs

        for job in jobs:
            j_id = job.get("id")
            j_name = job.get("name")
            j_conclusion = job.get("conclusion")
            j_status = job.get("status")

            is_failed = j_conclusion in {"failure", "timed_out", "startup_failure", "cancelled"}
            if not is_failed:
                continue

            failed_steps: list[dict[str, Any]] = []
            for step in job.get("steps", []):
                if step.get("conclusion") in {"failure", "timed_out"}:
                    failed_steps.append(
                        {
                            "name": step.get("name"),
                            "number": step.get("number"),
                            "conclusion": step.get("conclusion"),
                        }
                    )

            if len(diagnosed_jobs) >= 20:
                evidence_complete = False
                jobs_limited = True
                break
            log_text = client.get_job_logs(repo, j_id)
            excerpts: list[dict[str, Any]] = []
            job_categories: list[str] = []

            if log_text:
                excerpts, job_categories = extract_failure_excerpts(
                    log_text,
                    max_excerpts=max_excerpts_per_job,
                )
                overall_categories.update(job_categories)
            else:
                logs_available = False

            annotations: list[dict[str, Any]] = []
            if include_annotations and job.get("check_run_url"):
                match = re.search(r"/check-runs/(\d+)", job["check_run_url"])
                if match:
                    try:
                        cr_id = int(match.group(1))
                        raw_ann = client.get_check_run_annotations(repo, cr_id)
                        if len(raw_ann) >= 100:
                            evidence_complete = False
                        for a in raw_ann:
                            annotations.append(
                                {
                                    "path": a.get("path"),
                                    "start_line": a.get("start_line"),
                                    "end_line": a.get("end_line"),
                                    "annotation_level": a.get("annotation_level"),
                                    "title": a.get("title"),
                                    "message": safe_text(a.get("message", ""), 512),
                                }
                            )
                    except APIError:
                        evidence_complete = False

            diagnosed_jobs.append(
                {
                    "run_id": r_id,
                    "workflow_name": r_name,
                    "job_id": j_id,
                    "job_name": j_name,
                    "conclusion": j_conclusion,
                    "status": j_status,
                    "failed_steps": failed_steps,
                    "failure_categories": job_categories,
                    "excerpts": excerpts,
                    "annotations": annotations,
                    "logs_found": bool(log_text),
                }
            )

    if job_id is not None and not selected_job_found:
        evidence_complete = False

    code_scanning_alerts: list[dict[str, Any]] = []
    code_scanning_error: dict[str, Any] | None = None
    if include_code_scanning:
        try:
            response = client.request(
                "GET",
                f"repos/{repo}/code-scanning/alerts",
                params={"per_page": 100, "ref": f"refs/pull/{number}/head"},
            )
            raw_alerts = response.json()
            code_scanning_available = True
        except APIError as error:
            raw_alerts = []
            evidence_complete = False
            code_scanning_error = {
                "status_code": error.status_code,
                "classification": "unavailable_or_unauthorized",
            }
        if len(raw_alerts) >= 100:
            evidence_complete = False
        for alert in raw_alerts:
            instance = alert.get("most_recent_instance", {})
            if str(instance.get("commit_sha", "")).casefold() != head_sha.casefold():
                evidence_complete = False
                continue
            code_scanning_alerts.append(
                {
                    "number": alert.get("number"),
                    "rule_id": alert.get("rule", {}).get("id"),
                    "rule_description": alert.get("rule", {}).get("description"),
                    "severity": alert.get("rule", {}).get("severity"),
                    "path": instance
                    .get("location", {})
                    .get("path"),
                    "start_line": instance
                    .get("location", {})
                    .get("start_line"),
                }
            )

    final_pr = client.get_pr(repo, number)
    final_head = final_pr.get("head", {}).get("sha", "")
    final_base = final_pr.get("base", {}).get("sha", "")
    snapshot_consistent = (
        final_head.casefold() == head_sha.casefold()
        and final_base.casefold() == pr.get("base", {}).get("sha", "").casefold()
    )
    result = {
        "repo": repo,
        "number": number,
        "head_sha": head_sha,
        "total_failed_jobs": len(diagnosed_jobs),
        "failure_categories": sorted(overall_categories),
        "failed_jobs": diagnosed_jobs,
        "code_scanning_alerts": code_scanning_alerts,
        "code_scanning_error": code_scanning_error,
        "evidence_complete": (
            evidence_complete
            and logs_available
            and snapshot_consistent
            and include_annotations
            and include_code_scanning
        ),
        "inconsistent_snapshot": not snapshot_consistent,
        "logs_available": logs_available,
        "jobs_limited": jobs_limited,
        "max_diagnosed_jobs": 20,
        "code_scanning_available": code_scanning_available,
    }

    return safe_payload(result)
