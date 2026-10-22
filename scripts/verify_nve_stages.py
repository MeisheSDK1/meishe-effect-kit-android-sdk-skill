#!/usr/bin/env python3
"""Fail-closed validation for ordered NveEffectKit runtime stage evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

from feature_contract import contract_report, implementation_features, unknown_features


STAGES = ("raw-preview", "no-effect", "single-effect", "scope", "restore")
REQUIRED = {
    "raw-preview": (
        "observed",
        "frames_present",
        "non_black",
        "viewport_correct",
        "direction_correct",
        "aspect_ratio_correct",
        "front_mirror_correct",
        "back_direction_correct",
    ),
    "no-effect": (
        "observed",
        "non_black",
        "raw_fallback",
        "output_texture_type_recorded",
        "output_dimensions_recorded",
        "gl_state_restored",
        "same_geometry_as_raw",
        "same_direction_as_raw",
        "same_mirror_as_raw",
        "no_flash",
        "no_raw_effect_alternation",
        "oes_and_2d_paths_verified",
    ),
    "single-effect": (
        "observed",
        "visible_change",
        "disable_restores_raw",
        "failure_fallback",
        "no_geometry_jump_on_enable",
        "no_flash_on_enable",
    ),
    "restore": (
        "background_foreground",
        "page_reentry",
        "gl_context_recreated",
        "controllers_reregistered",
        "state_restored",
        "frames_after_restore",
        "no_ar_scene_null",
        "no_null_pointer_exception",
        "no_fatal_or_anr",
        "logcat_clean",
    ),
}


def _true_fields(data: Any, fields: Iterable[str], prefix: str) -> list[str]:
    if not isinstance(data, dict):
        return [prefix]
    return [f"{prefix}.{field}" for field in fields if data.get(field) is not True]


def validate_stage_evidence(
    payload: dict[str, Any], through: str, features: Iterable[str]
) -> list[str]:
    if through not in STAGES:
        return [f"unknown stage: {through}"]
    requested = list(features)
    if not requested:
        return ["features"]
    unknown = unknown_features(requested)
    if unknown:
        return ["unknown features: " + ", ".join(unknown)]
    unresolved = contract_report(requested).get("scope_selection_required", [])
    if unresolved:
        return [
            "explicit feature selection required: " + ", ".join(unresolved)
        ]

    stages = payload.get("stages")
    if not isinstance(stages, dict):
        return ["stages"]

    missing: list[str] = []
    through_index = STAGES.index(through)
    for stage in STAGES[: through_index + 1]:
        data = stages.get(stage)
        if stage == "scope":
            if not isinstance(data, dict):
                missing.append("stages.scope")
                continue
            checks = data.get("feature_checks")
            if not isinstance(checks, dict):
                missing.append("stages.scope.feature_checks")
                continue
            for feature in implementation_features(requested):
                missing.extend(
                    _true_fields(
                        checks.get(feature),
                        ("visible", "switch_remove_restore", "failure_fallback"),
                        f"stages.scope.feature_checks.{feature}",
                    )
                )
            continue
        missing.extend(
            _true_fields(data, REQUIRED[stage], f"stages.{stage}")
        )
    return missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate ordered runtime stage evidence before expanding Nve features."
    )
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--through", required=True, choices=STAGES)
    parser.add_argument("--features", required=True, help="comma-separated feature IDs")
    args = parser.parse_args(argv)

    try:
        payload = json.loads(args.evidence.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"BLOCKED: cannot read evidence: {error}", file=sys.stderr)
        return 2

    features = [item.strip() for item in args.features.split(",") if item.strip()]
    missing = validate_stage_evidence(payload, args.through, features)
    if missing:
        print("BLOCKED")
        for item in missing:
            print(f"- {item}")
        return 1
    print(f"PASS: {args.through}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
