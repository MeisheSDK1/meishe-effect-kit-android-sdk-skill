#!/usr/bin/env python3
"""Canonical NveEffectKit feature routing backed by the registered catalog."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = (
    SKILL_ROOT
    / "assets"
    / "baseline-3.16.1"
    / "catalog"
    / "feature-catalog.json"
)
CATALOG = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
HOST_CAPABILITIES_PATH = SKILL_ROOT / "assets" / "host-capabilities.json"
HOST_CAPABILITIES = json.loads(
    HOST_CAPABILITIES_PATH.read_text(encoding="utf-8")
)

BEAUTY_SUITE = "beauty-suite"
MAKEUP_SUITE = "makeup-suite"
FILTER_PACKAGED = "filter-packaged"
BASELINE_AAR_SHA256 = CATALOG["aar_sha256"]
EXPLICIT_FULL_SCOPE_ALIASES = {
    "beauty-full": BEAUTY_SUITE,
    "full-beauty": BEAUTY_SUITE,
    "完整美颜": BEAUTY_SUITE,
    "beauty-skin-full": "beauty-skin",
    "beauty-shape-full": "beauty-shape",
    "beauty-micro-shape-full": "beauty-micro-shape",
    "makeup-full": MAKEUP_SUITE,
    "full-makeup": MAKEUP_SUITE,
    "完整美妆": MAKEUP_SUITE,
    "makeup-single-full": "makeup-single",
    "filter-full": FILTER_PACKAGED,
    "full-filter": FILTER_PACKAGED,
    "完整滤镜": FILTER_PACKAGED,
}

BEAUTY_SKIN_LEAVES = tuple(
    f"beauty-skin-{item['id']}" for item in CATALOG["beauty"]["skin"]
)
BEAUTY_SHAPE_LEAVES = tuple(
    f"beauty-shape-{item['id']}" for item in CATALOG["beauty"]["shape"]
)
BEAUTY_MICRO_SHAPE_LEAVES = tuple(
    f"beauty-micro-shape-{item['id']}"
    for item in CATALOG["beauty"]["micro_shape"]
)
MAKEUP_SINGLE_LEAVES = tuple(
    f"makeup-single-{item['id']}" for item in CATALOG["makeup"]["single"]
)

CONTRACT = CATALOG["feature_contract"]
FEATURE_ALIASES: dict[str, str] = {
    **CONTRACT["aliases"],
    **HOST_CAPABILITIES["aliases"],
}
FEATURE_GROUPS: dict[str, dict[str, Any]] = copy.deepcopy(
    CONTRACT["capabilities"]
)
FEATURE_GROUPS.update(
    copy.deepcopy(HOST_CAPABILITIES["capabilities"])
)


def _add_leaf(
    feature: str,
    parent: str,
    proof_tokens: Iterable[str],
    extra_models: Iterable[str] = (),
) -> None:
    spec = copy.deepcopy(FEATURE_GROUPS[parent])
    spec["parent"] = parent
    spec["proof_tokens"] = tuple(proof_tokens)
    if extra_models:
        spec["models"] = tuple(dict.fromkeys([*spec.get("models", ()), *extra_models]))
    FEATURE_GROUPS[feature] = spec


for item in CATALOG["beauty"]["skin"]:
    tokens = [item.get("type"), f"{item['setter']}("]
    if item.get("availability") == "high-cpu-only":
        tokens.extend(("getDeviceCpuLevel(", "DEVICE_CPU_POWER_LEVEL_HIGH"))
    _add_leaf(
        f"beauty-skin-{item['id']}",
        "beauty-skin",
        (token for token in tokens if token),
    )

for item in CATALOG["beauty"]["shape"]:
    _add_leaf(
        f"beauty-shape-{item['id']}",
        "beauty-shape",
        ("setDegreeByType(", item["enum"]),
    )

for item in CATALOG["beauty"]["micro_shape"]:
    _add_leaf(
        f"beauty-micro-shape-{item['id']}",
        "beauty-micro-shape",
        ("setDegreeByType(", item["enum"]),
    )

for item in CATALOG["makeup"]["single"]:
    _add_leaf(
        f"makeup-single-{item['id']}",
        "makeup-single",
        ("addSingleMakeUp(", item["enum"]),
        item.get("conditional_models", ()),
    )


IMPLEMENTATION_EXPANSIONS = {
    BEAUTY_SUITE: (
        *BEAUTY_SKIN_LEAVES,
        *BEAUTY_SHAPE_LEAVES,
        *BEAUTY_MICRO_SHAPE_LEAVES,
    ),
    "beauty-skin": BEAUTY_SKIN_LEAVES,
    "beauty-shape": BEAUTY_SHAPE_LEAVES,
    "beauty-micro-shape": BEAUTY_MICRO_SHAPE_LEAVES,
    MAKEUP_SUITE: (*MAKEUP_SINGLE_LEAVES, "makeup-compose"),
    "makeup-single": MAKEUP_SINGLE_LEAVES,
}

SCOPE_SELECTION_REQUESTS: dict[str, dict[str, Any]] = {
    BEAUTY_SUITE: {
        "prompt": "请选择美颜接入范围。",
        "options": [
            {
                "id": "beauty-full",
                "label": "完整美颜",
                "description": "接入美肤 12 项、美型 11 项和微整形 21 项。",
            },
            {
                "id": "beauty-skin-full",
                "label": "完整美肤",
                "description": "只接入磨皮、美白、去油光、红润、清晰度和锐度等美肤项。",
            },
            {
                "id": "beauty-shape-full",
                "label": "完整美型",
                "description": "只接入脸型、眼鼻、嘴巴等美型项。",
            },
            {
                "id": "beauty-micro-shape-full",
                "label": "完整微整形",
                "description": "只接入头型、轮廓、眼鼻眉等微整形项。",
            },
            {
                "id": "custom-leaf-features",
                "label": "自定义具体项",
                "description": "由用户明确列出需要的调节项。",
            },
        ],
    },
    "beauty-skin": {
        "prompt": "请选择美肤接入范围。",
        "options": [
            {
                "id": "beauty-skin-full",
                "label": "完整美肤",
                "description": "接入登记目录中的 12 个美肤项。",
            },
            {
                "id": "custom-leaf-features",
                "label": "指定美肤项",
                "description": "由用户明确列出需要的美肤调节项。",
            },
        ],
    },
    "beauty-shape": {
        "prompt": "请选择美型接入范围。",
        "options": [
            {
                "id": "beauty-shape-full",
                "label": "完整美型",
                "description": "接入登记目录中的 11 个美型项。",
            },
            {
                "id": "custom-leaf-features",
                "label": "指定美型项",
                "description": "由用户明确列出需要的美型调节项。",
            },
        ],
    },
    "beauty-micro-shape": {
        "prompt": "请选择微整形接入范围。",
        "options": [
            {
                "id": "beauty-micro-shape-full",
                "label": "完整微整形",
                "description": "接入登记目录中的 21 个微整形项。",
            },
            {
                "id": "custom-leaf-features",
                "label": "指定微整形项",
                "description": "由用户明确列出需要的微整形调节项。",
            },
        ],
    },
    MAKEUP_SUITE: {
        "prompt": "请选择美妆接入范围。",
        "options": [
            {
                "id": "makeup-full",
                "label": "完整美妆",
                "description": "接入妆容和九类单妆。",
            },
            {
                "id": "makeup-single-full",
                "label": "全部九类单妆",
                "description": "接入口红、眉毛、眼影等九类单妆。",
            },
            {
                "id": "makeup-compose",
                "label": "仅妆容",
                "description": "只接入组合妆容。",
            },
            {
                "id": "custom-leaf-features",
                "label": "自定义具体项",
                "description": "由用户明确列出需要的单妆分类或妆容。",
            },
        ],
    },
    "makeup-single": {
        "prompt": "请选择单妆接入范围。",
        "options": [
            {
                "id": "makeup-single-full",
                "label": "全部九类单妆",
                "description": "接入登记目录中的九类单妆。",
            },
            {
                "id": "custom-leaf-features",
                "label": "指定单妆分类",
                "description": "由用户明确列出需要的单妆分类。",
            },
        ],
    },
    FILTER_PACKAGED: {
        "prompt": "请选择滤镜素材范围。",
        "options": [
            {
                "id": "filter-full",
                "label": "全部已授权滤镜",
                "description": "接入用户明确授权目录中通过素材与 lic 门禁的全部滤镜。",
            },
            {
                "id": "custom-filter-items",
                "label": "指定滤镜",
                "description": "由用户明确选择具体滤镜项，再生成显式素材映射。",
            },
        ],
    },
}


def canonical_feature(feature: str) -> str:
    return FEATURE_ALIASES.get(feature, feature)


def normalize_requested_features(features: Iterable[str]) -> list[str]:
    result: list[str] = []
    for feature in features:
        canonical = canonical_feature(feature.strip())
        if canonical and canonical not in result:
            result.append(canonical)
    return result


def unknown_features(features: Iterable[str]) -> list[str]:
    return [
        item
        for item in normalize_requested_features(features)
        if item not in FEATURE_GROUPS
    ]


def _expand(
    features: Iterable[str], expansions: dict[str, Iterable[str]]
) -> list[str]:
    result: list[str] = []
    for feature in normalize_requested_features(features):
        for candidate in expansions.get(feature, (feature,)):
            if candidate not in result:
                result.append(candidate)
    return result


def implementation_features(features: Iterable[str]) -> list[str]:
    """Expand product requests to the item-level implementation contract."""
    return _expand(features, IMPLEMENTATION_EXPANSIONS)


def expand_features(features: Iterable[str]) -> list[str]:
    """Expand product bundles to controller-level capabilities."""
    bundles = {
        feature: spec["bundle"]
        for feature, spec in FEATURE_GROUPS.items()
        if spec.get("bundle")
    }
    return _expand(features, bundles)


def beauty_controller_groups(features: Iterable[str]) -> set[str]:
    groups: set[str] = set()
    for feature in expand_features(features):
        if feature == "beauty-skin" or feature.startswith("beauty-skin-"):
            groups.add("skin")
        elif feature == "beauty-shape" or feature.startswith("beauty-shape-"):
            groups.add("shape")
        elif feature == "beauty-micro-shape" or feature.startswith(
            "beauty-micro-shape-"
        ):
            groups.add("micro-shape")
    return groups


def contract_report(features: Iterable[str]) -> dict[str, Any]:
    raw_features = [feature.strip() for feature in features if feature.strip()]
    requested = normalize_requested_features(raw_features)
    explicit_full_targets = {
        target
        for feature in raw_features
        for target in (EXPLICIT_FULL_SCOPE_ALIASES.get(feature),)
        if target
    }
    scope_selection_required = [
        feature
        for feature in requested
        if (
            feature in IMPLEMENTATION_EXPANSIONS
            or feature in SCOPE_SELECTION_REQUESTS
        )
        and feature not in explicit_full_targets
    ]
    selection_requests = [
        {
            "feature": feature,
            **copy.deepcopy(SCOPE_SELECTION_REQUESTS[feature]),
        }
        for feature in scope_selection_required
        if feature in SCOPE_SELECTION_REQUESTS
    ]
    unknown = [item for item in requested if item not in FEATURE_GROUPS]
    effective = expand_features(requested)
    implementation = implementation_features(requested)
    effective_specs = [
        FEATURE_GROUPS[item] for item in effective if item in FEATURE_GROUPS
    ]
    implementation_specs = [
        FEATURE_GROUPS[item]
        for item in implementation
        if item in FEATURE_GROUPS
    ]
    host_capabilities = [
        item
        for item in implementation
        if FEATURE_GROUPS.get(item, {}).get("host_capability") is True
    ]
    nve_capabilities = [
        item for item in implementation if item not in host_capabilities
    ]
    # An omitted feature list preserves the historical full NVE inspection.
    # Only an explicit, non-empty host-only request receives the exemption.
    requires_nve_prerequisites = bool(nve_capabilities) or not implementation
    intensity_features = [
        item
        for item in implementation
        if FEATURE_GROUPS.get(item, {}).get("intensity")
    ]
    required_model_set = {
        model
        for spec in implementation_specs
        for model in spec.get("models", ())
    }
    model_init_order = [
        model
        for model in CATALOG.get("model_contract", {}).get(
            "initialization_order", ()
        )
        if model in required_model_set
    ]
    baseline_model_files = {
        model: path
        for model, path in CATALOG.get("model_contract", {})
        .get("baseline_files", {})
        .items()
        if model in required_model_set
    }
    beauty_asset_items: dict[str, dict[str, Any]] = {}
    for group_name, prefix in (
        ("shape", "beauty-shape"),
        ("micro_shape", "beauty-micro-shape"),
    ):
        for item in CATALOG["beauty"][group_name]:
            beauty_asset_items[f"{prefix}-{item['id']}"] = item
    baseline_asset_files = {
        feature: beauty_asset_items[feature]["asset"]
        for feature in implementation
        if feature in beauty_asset_items and beauty_asset_items[feature].get("asset")
    }
    directly_requested = set(requested)
    full_scope_implementation = {
        feature
        for target in explicit_full_targets
        for feature in implementation_features([target])
    }
    required_baseline_asset_files = {
        feature: filename
        for feature, filename in baseline_asset_files.items()
        if feature in directly_requested or feature in full_scope_implementation
    }
    built_in_features = [
        feature
        for feature in implementation
        if feature in beauty_asset_items
        and beauty_asset_items[feature].get("built_in") is True
    ]
    required_asset_contracts = {
        feature: {
            "extensions": sorted(
                set(FEATURE_GROUPS[feature].get("assets", ()))
            ),
            "multiple": bool(
                FEATURE_GROUPS[feature].get("asset_mapping_multiple")
            ),
            "license_policy": FEATURE_GROUPS[feature].get(
                "asset_license_policy", "none"
            ),
            "mapping_model": FEATURE_GROUPS[feature].get(
                "asset_mapping_model", "flat"
            ),
        }
        for feature in implementation
        if FEATURE_GROUPS.get(feature, {}).get("asset_mapping_required")
    }
    return {
        "schema_version": 6,
        "requested_features": requested,
        "effective_features": effective,
        "implementation_features": implementation,
        "unknown_features": unknown,
        "references": sorted({spec["reference"] for spec in effective_specs}),
        "controllers": sorted(
            {spec["controller"] for spec in effective_specs if spec.get("controller")}
        ),
        "models": model_init_order
        + sorted(required_model_set - set(model_init_order)),
        "model_init_order": model_init_order,
        "baseline_model_files": baseline_model_files,
        "baseline_asset_files": baseline_asset_files,
        "candidate_baseline_asset_files": baseline_asset_files,
        "required_baseline_asset_files": required_baseline_asset_files,
        "required_asset_contracts": required_asset_contracts,
        "asset_identity_policy": CATALOG.get("asset_contract", {}),
        "scope_selection_required": scope_selection_required,
        "selection_requests": selection_requests,
        "scope_policy": (
            "explicit-full"
            if explicit_full_targets
            else "exact-items"
            if not scope_selection_required
            else "selection-required"
        ),
        "built_in_features": built_in_features,
        "asset_types": sorted(
            {
                asset
                for spec in implementation_specs
                for asset in spec.get("assets", ())
            }
        ),
        "resource_semantics": "candidate-types-confirm-against-target-aar-and-metadata",
        "ui_modules": sorted(
            {spec["ui_module"] for spec in effective_specs if spec.get("ui_module")}
        ),
        "ui_behaviors": sorted(
            {
                spec["ui_behavior"]
                for spec in effective_specs
                if spec.get("ui_behavior")
            }
        ),
        "host_capabilities": host_capabilities,
        "nve_capabilities": nve_capabilities,
        "requires_nve_prerequisites": requires_nve_prerequisites,
        "prerequisite_mode": (
            "mixed"
            if host_capabilities and nve_capabilities
            else "nve"
            if nve_capabilities or not implementation
            else "host-only"
        ),
        "default_ui": {
            "entry": (
                "bottom-center-record-action"
                if host_capabilities and not nve_capabilities
                else "bottom-toggle-per-module-with-center-record-action"
                if host_capabilities
                else "bottom-toggle-per-module"
            ),
            "panel": (
                "none"
                if host_capabilities and not nve_capabilities
                else "single-open-collapsible"
            ),
        },
        "proof_requirements": {
            item: list(FEATURE_GROUPS[item].get("proof_tokens", ()))
            for item in implementation
            if FEATURE_GROUPS.get(item, {}).get("proof_tokens")
        },
        "intensity_evidence_features": intensity_features,
        "manual_evidence": {
            "per_feature_checks": [
                "visible",
                "switch_remove_restore",
                "failure_fallback",
            ]
            if nve_capabilities
            else [],
            "intensity_features": intensity_features,
            "no_intensity_required_features": [
                item for item in nve_capabilities if item not in intensity_features
            ],
            "common_checks": (
                [
                    "lifecycle",
                    "gl_context_recreated",
                    "output_texture_types",
                    "output_texture_dimensions",
                    "single_recycle",
                    "no_black_frames",
                    "no_flash",
                    "no_geometry_jump",
                    "raw_effect_visual_contract",
                    "performance",
                    "logcat_clean",
                ]
                if nve_capabilities
                else []
            ),
            "camera_pipeline_only_checks": (
                [
                    "front_back_camera",
                    "four_display_rotations",
                    "single_mirror",
                    "no_raw_effect_alternation",
                ]
                if nve_capabilities
                else []
            ),
            "recording_checks": (
                HOST_CAPABILITIES["manual_evidence"]["recording_checks"]
                if "recording" in implementation
                else []
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve canonical NveEffectKit feature scope."
    )
    parser.add_argument("--features", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    raw = [item.strip() for item in args.features.split(",") if item.strip()]
    if not raw:
        print("features 不能为空", file=sys.stderr)
        return 2
    report = contract_report(raw)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        for key in (
            "requested_features",
            "effective_features",
            "implementation_features",
            "references",
            "controllers",
            "models",
            "asset_types",
            "ui_modules",
            "intensity_evidence_features",
            "scope_selection_required",
            "unknown_features",
        ):
            print(f"{key}: {', '.join(report[key]) or 'none'}")
        print(
            "selection_requests: "
            + json.dumps(
                report["selection_requests"],
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        print(f"scope_policy: {report['scope_policy']}")
        print(
            "required_baseline_asset_files: "
            + json.dumps(
                report["required_baseline_asset_files"],
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        print(
            "proof_requirements: "
            + json.dumps(
                report["proof_requirements"], ensure_ascii=False, sort_keys=True
            )
        )
        print(
            "manual_evidence: "
            + json.dumps(
                report["manual_evidence"], ensure_ascii=False, sort_keys=True
            )
        )
    return 2 if report["unknown_features"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
