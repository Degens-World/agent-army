from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_army.goal_profile import GoalProfile


@dataclass(frozen=True, slots=True)
class ValidationResult:
    approved: bool
    summary: str
    issues: list[str]
    suggested_fixes: list[str]


class CodingValidator:
    def validate(
        self,
        *,
        goal: str,
        profile: GoalProfile,
        phase: str,
        workspace_path: Path | None,
    ) -> ValidationResult:
        if workspace_path is None or not workspace_path.exists():
            return ValidationResult(
                approved=False,
                summary="No workspace was materialized for the coding task.",
                issues=["Workspace directory is missing."],
                suggested_fixes=["Write the task output into a workspace folder before review."],
            )

        if phase == "contract":
            return self._validate_contract(workspace_path)
        if phase == "verification":
            return self._validate_verification(workspace_path)
        return self._validate_runnable(goal=goal, profile=profile, workspace_path=workspace_path)

    @staticmethod
    def _validate_contract(workspace_path: Path) -> ValidationResult:
        report_path = workspace_path / "report.md"
        if not report_path.exists():
            return ValidationResult(
                approved=False,
                summary="The implementation contract is missing its report file.",
                issues=["Expected `report.md` in the task workspace."],
                suggested_fixes=["Write the contract as markdown in `report.md`."],
            )
        content = report_path.read_text(encoding="utf-8")
        issues: list[str] = []
        if "<html" in content.lower() or "<!doctype" in content.lower():
            issues.append("The contract output contains implementation code instead of a specification.")
        required_terms = ["rule", "state", "win", "ui"]
        for term in required_terms:
            if term not in content.lower():
                issues.append(f"The contract does not clearly cover `{term}`.")
        suggested_fixes = [f"Add explicit contract coverage for: {issue.split('`')[1]}" for issue in issues if "`" in issue]
        if any("implementation code" in issue for issue in issues):
            suggested_fixes.append("Replace implementation code with a written contract.")
        return ValidationResult(
            approved=not issues,
            summary="Contract workspace validation passed." if not issues else "Contract workspace validation found issues.",
            issues=issues,
            suggested_fixes=suggested_fixes,
        )

    @staticmethod
    def _validate_verification(workspace_path: Path) -> ValidationResult:
        report_path = workspace_path / "report.md"
        if not report_path.exists():
            return ValidationResult(
                approved=False,
                summary="The verification phase is missing its report file.",
                issues=["Expected `report.md` in the task workspace."],
                suggested_fixes=["Write the verification audit as markdown in `report.md`."],
            )
        content = report_path.read_text(encoding="utf-8")
        issues: list[str] = []
        if "fix" not in content.lower():
            issues.append("The verification report does not include concrete fix guidance.")
        if "-" not in content and "*" not in content:
            issues.append("The verification report is not structured as a concrete issue list.")
        return ValidationResult(
            approved=not issues,
            summary="Verification workspace validation passed." if not issues else "Verification workspace validation found issues.",
            issues=issues,
            suggested_fixes=["List each issue with a concrete fix."] if issues else [],
        )

    @staticmethod
    def _validate_runnable(*, goal: str, profile: GoalProfile, workspace_path: Path) -> ValidationResult:
        issues: list[str] = []
        if profile.artifact_format == "single_html":
            index_path = workspace_path / "index.html"
            if not index_path.exists():
                issues.append("Expected `index.html` in the workspace.")
            else:
                content = index_path.read_text(encoding="utf-8")
                lowered = content.lower()
                for needle in ("<!doctype html", "<html", "<script"):
                    if needle not in lowered:
                        issues.append(f"`index.html` is missing required HTML structure: {needle}")
                goal_text = goal.lower()
                if "checkers" in goal_text:
                    for token in ("board", "king", "capture"):
                        if token not in lowered:
                            issues.append(f"`index.html` does not appear to cover `{token}` logic.")
        else:
            manifest = workspace_path / "artifact_manifest.json"
            if not manifest.exists():
                issues.append("Expected `artifact_manifest.json` in the workspace.")
        return ValidationResult(
            approved=not issues,
            summary="Runnable workspace validation passed." if not issues else "Runnable workspace validation found issues.",
            issues=issues,
            suggested_fixes=["Update the workspace files to satisfy the required structure and behavior hints."] if issues else [],
        )
