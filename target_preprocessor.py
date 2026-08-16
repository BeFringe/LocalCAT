"""Pure target-only literal preprocessing previews."""

from __future__ import annotations

from editor_contracts import (
    EditorProject,
    LiteralReplaceRule,
    PreprocessChange,
    PreprocessPreview,
)


class PreprocessValidationError(ValueError):
    """A validation rejection raised before a preprocessing preview is created."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def preview_preprocessing(
    project: EditorProject,
    project_session_id: str,
    revision: int,
    rules: tuple[LiteralReplaceRule, ...],
) -> PreprocessPreview:
    """Preview ordered literal replacements without mutating the project."""

    for rule in rules:
        if not rule.find:
            raise PreprocessValidationError(
                "EMPTY_FIND",
                "literal rule find value must not be empty",
            )

    enabled_rules = tuple(rule for rule in rules if rule.enabled)
    if not enabled_rules:
        raise PreprocessValidationError(
            "NO_ENABLED_RULES",
            "at least one literal rule must be enabled",
        )

    changes: list[PreprocessChange] = []
    for segment_index, segment in enumerate(project.segments):
        updated_target = segment.target
        for rule in enabled_rules:
            updated_target = updated_target.replace(rule.find, rule.replacement)
        if updated_target == segment.target:
            continue
        changes.append(
            PreprocessChange(
                segment_id=segment.id,
                segment_index=segment_index,
                before_target=segment.target,
                after_target=updated_target,
                before_confirmed=segment.confirmed,
                after_confirmed=False,
            )
        )

    if not changes:
        raise PreprocessValidationError(
            "NO_CHANGES",
            "enabled literal rules produced no target changes",
        )

    return PreprocessPreview(
        project_session_id=project_session_id,
        base_revision=revision,
        changes=tuple(changes),
    )
