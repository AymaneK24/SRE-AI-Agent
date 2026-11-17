"""Prints the SonarQube Cloud quality gate and open issues to the log and the GitHub job summary.

Runs after the scan with the short-lived token the sign-in step exported (SONAR_TOKEN), so the findings are
readable in GitHub without opening SonarQube Cloud. On the free plan for private projects, SonarQube Cloud
returns data for the main branch only. A report must never fail the pipeline, so every error is printed, not raised.
"""
import os

import httpx

API = "https://sonarcloud.io/api"
PROJECT = os.environ["SONAR_PROJECT_KEY"]
BRANCH = os.environ.get("SONAR_BRANCH", "main")
HEADERS = {"Authorization": f"Bearer {os.environ['SONAR_TOKEN']}"}


def get(path: str, **params: str) -> dict:
    response = httpx.get(f"{API}/{path}", params=params, headers=HEADERS, timeout=30)
    if response.status_code >= 400:
        raise RuntimeError(f"{path} returned {response.status_code}: {response.text[:300]}")
    return response.json()


def gate_lines() -> list[str]:
    gate = get("qualitygates/project_status", projectKey=PROJECT, branch=BRANCH)["projectStatus"]
    lines = [f"**Quality gate: {gate['status']}**"]
    for condition in gate.get("conditions", []):
        if condition["status"] != "OK":
            lines.append(f"- {condition['metricKey']}: {condition.get('actualValue', '?')} "
                         f"(needs {condition['comparator']} {condition['errorThreshold']})")
    return lines


def new_issues() -> tuple[str, list[dict]]:
    """Open issues in new code. Parameter names differ between API versions, so a few forms are tried in turn."""
    base = {"componentKeys": PROJECT, "branch": BRANCH, "resolved": "false", "ps": "100"}
    attempts = [("new code", {"inNewCodePeriod": "true"}), ("new code", {"sinceLeakPeriod": "true"}), ("all open", {})]
    errors = []
    for scope, extra in attempts:
        try:
            return scope, get("issues/search", **base, **extra)["issues"]
        except RuntimeError as error:
            errors.append(str(error))
    raise RuntimeError(" | ".join(errors))


def issue_row(issue: dict) -> str:
    where = f"{issue['component'].split(':', 1)[-1]}:{issue.get('line', '')}"
    impacts = ", ".join(f"{i['softwareQuality'].lower()} {i['severity'].lower()}" for i in issue.get("impacts", []))
    kind = impacts or f"{issue.get('type', '')} {issue.get('severity', '')}".strip()
    message = issue["message"].replace("|", "\\|")
    return f"| {kind} | {issue['rule']} | `{where}` | {message} |"


def main() -> None:
    lines = [f"### SonarQube Cloud: {PROJECT} ({BRANCH})", ""]
    try:
        lines += gate_lines()
    except Exception as error:  # noqa: BLE001
        lines.append(f"Couldn't read the quality gate: {error}")
    try:
        scope, issues = new_issues()
        security_first = sorted(issues, key=lambda i: (i.get("type") != "VULNERABILITY",
                                                       not any(x["softwareQuality"] == "SECURITY" for x in i.get("impacts", []))))
        lines += ["", f"**Issues ({scope}): {len(issues)}**", "", "| Impact | Rule | Where | Message |", "|---|---|---|---|"]
        lines += [issue_row(issue) for issue in security_first]
    except Exception as error:  # noqa: BLE001
        lines.append(f"Couldn't read the issues: {error}")
    text = "\n".join(lines)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


if __name__ == "__main__":
    main()
