"""Prints the SonarQube Cloud quality gate and open issues to the log and the GitHub job summary.

Runs after the scan with the short-lived token the sign-in step exported (SONAR_TOKEN), so the findings are
readable in GitHub without opening SonarQube Cloud. On the free plan for private projects, SonarQube Cloud
returns data for the main branch only; for other branches this says so and stops.
"""
import os
import sys

import httpx

API = "https://sonarcloud.io/api"
PROJECT = os.environ["SONAR_PROJECT_KEY"]
BRANCH = os.environ.get("SONAR_BRANCH", "main")


def get(path: str, **params: str) -> dict:
    response = httpx.get(f"{API}/{path}", params=params, timeout=30,
                         headers={"Authorization": f"Bearer {os.environ['SONAR_TOKEN']}"})
    response.raise_for_status()
    return response.json()


def main() -> None:
    lines = [f"### SonarQube Cloud: {PROJECT} ({BRANCH})", ""]
    try:
        gate = get("qualitygates/project_status", projectKey=PROJECT, branch=BRANCH)["projectStatus"]
        issues = get("issues/search", componentKeys=PROJECT, branch=BRANCH, inNewCodePeriod="true",
                     issueStatuses="OPEN,CONFIRMED", ps="100", s="SEVERITY")["issues"]
    except Exception as error:  # noqa: BLE001 - a report must never fail the pipeline
        print(f"Couldn't read results from SonarQube Cloud: {error}")
        return
    lines.append(f"**Quality gate: {gate['status']}**")
    for condition in gate.get("conditions", []):
        if condition["status"] != "OK":
            lines.append(f"- {condition['metricKey']}: {condition.get('actualValue', '?')} "
                         f"(needs {condition['comparator']} {condition['errorThreshold']})")
    lines += ["", f"**New issues: {len(issues)}**", "", "| Type | Severity | Rule | Where | Message |", "|---|---|---|---|---|"]
    for issue in issues:
        where = f"{issue['component'].split(':', 1)[-1]}:{issue.get('line', '')}"
        message = issue["message"].replace("|", "\\|")
        lines.append(f"| {issue.get('type', '')} | {issue.get('severity', '')} | {issue['rule']} | `{where}` | {message} |")
    text = "\n".join(lines)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


if __name__ == "__main__":
    sys.exit(main())
