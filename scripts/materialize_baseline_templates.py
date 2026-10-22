#!/usr/bin/env python3
"""Materialize a registered NveEffectKit baseline without overwriting project files."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from feature_contract import (
    contract_report,
    implementation_features,
    normalize_requested_features,
    unknown_features,
)


SKILL_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
COMMON_TEMPLATES = (
    "common/NveRenderBridge.java.template",
    "common/NveSessionOwner.java.template",
    "common/NveAssetInstaller.java.template",
    "common/NveLatestValueQueue.java.template",
)
PROFILE_TEMPLATES = {
    "camera2-oes-nv21": (
        "camera2/NveCameraOrientation.java.template",
        "camera2/NveCamera2PreviewConfig.java.template",
        "camera2/NveNv21Converter.java.template",
        "camera2/NveCamera2FramePairer.java.template",
        "camera2/NveCamera2RenderInputFactory.java.template",
        "camera2/NveCamera2TexturePresenter.java.template",
    ),
}
UI_PROFILE_TEMPLATES = {
    "compact-beauty": (
        "ui/NveCompactBeautyPanelView.java.template",
    ),
    "compact-makeup": (
        "ui/NveCompactMakeupPanelView.java.template",
        "features/NveMakeupResourceLoader.java.template",
    ),
    "compact-filter": (
        "ui/NveCompactFilterPanelView.java.template",
    ),
    "compact-face-prop": (
        "ui/NveCompactFacePropPanelView.java.template",
    ),
}
UI_PROFILE_FEATURE_MATCHES = {
    "compact-beauty": ("beauty-",),
    "compact-makeup": ("makeup-",),
    "compact-filter": ("filter-packaged",),
    "compact-face-prop": ("face-prop",),
}
UI_PROFILE_MODULE_NAMES = {
    "compact-beauty": "美颜",
    "compact-makeup": "美妆",
    "compact-filter": "滤镜",
    "compact-face-prop": "道具",
}
MAKEUP_TEMPLATES = (
    "features/NveMakeupCoordinator.java.template",
    "features/NveMakeupSelectionState.java.template",
    "features/NveMakeupAssetRepository.java.template",
)
MAKEUP_TEST_TEMPLATES = (
    "tests/NveMakeupSelectionStateTest.java.template",
    "tests/NveMakeupAssetRepositoryTest.java.template",
)
COMMON_TEST_TEMPLATES = (
    "tests/NveLatestValueQueueTest.java.template",
)
PROFILE_TEST_TEMPLATES = {
    "camera2-oes-nv21": (
        "tests/NveCamera2FramePairerTest.java.template",
    ),
}
UI_COMMON_TEMPLATES = (
    "ui/NveCompactEffectEntryBar.java.template",
)
FEATURE_TEST_TEMPLATES = {
    "beauty": ("tests/NveBeautyCoordinatorTest.java.template",),
    "makeup": MAKEUP_TEST_TEMPLATES,
    "filter-packaged": ("tests/NveFilterCoordinatorTest.java.template",),
    "face-prop": ("tests/NveFacePropCoordinatorTest.java.template",),
}
ALL_TEST_TEMPLATES = frozenset(
    template
    for templates in (
        *FEATURE_TEST_TEMPLATES.values(),
        COMMON_TEST_TEMPLATES,
        *PROFILE_TEST_TEMPLATES.values(),
    )
    for template in templates
)


LAYERS = ("common", "controllers", "all")
PROFILES = tuple(PROFILE_TEMPLATES)
UI_PROFILES = tuple(UI_PROFILE_TEMPLATES)
SKIP_DEPENDENCY_DIRS = {
    ".git",
    ".gradle",
    ".idea",
    "build",
    "out",
    "node_modules",
}


def normalize_ui_profiles(
    ui_profiles: str | Sequence[str] | None,
) -> tuple[str, ...]:
    if ui_profiles is None:
        return ()
    values = (ui_profiles,) if isinstance(ui_profiles, str) else tuple(ui_profiles)
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return tuple(result)


def load_asset_map(path: Path | None) -> dict[str, object]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("--asset-map 顶层必须是 JSON object")
    return payload


def load_makeup_catalog_report(
    path: Path,
    catalog: dict[str, object],
    implementation: Sequence[str],
) -> dict[str, object]:
    if not path.is_file():
        raise ValueError(f"美妆目录检查报告不存在: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("--makeup-catalog-report 必须是 inspect_makeup_catalog.py 的 JSON 输出")
    if payload.get("errors"):
        raise ValueError("美妆目录检查仍有 errors，禁止落地模板")
    if payload.get("warnings"):
        raise ValueError("美妆目录检查仍有 warnings；先确认并修正目标侧元数据")
    if payload.get("selection_requests"):
        raise ValueError("美妆目录范围尚未明确；按 selection_requests 显式选择元数据")

    report_catalog = payload.get("catalog")
    if not isinstance(report_catalog, dict):
        raise ValueError("美妆目录检查报告缺少 catalog")
    available_types = {
        str(item.get("type", "")).lower()
        for item in report_catalog.get("single_categories", [])
        if isinstance(item, dict)
    }
    selected_single = {
        feature.removeprefix("makeup-single-")
        for feature in implementation
        if feature.startswith("makeup-single-")
    }
    enum_by_id = {
        str(item["id"]): str(item["enum"]).lower()
        for item in catalog.get("makeup", {}).get("single", [])
        if isinstance(item, dict) and item.get("id") and item.get("enum")
    }
    missing_single = sorted(
        feature
        for feature in selected_single
        if enum_by_id.get(feature, feature).lower() not in available_types
    )
    if missing_single:
        raise ValueError(
            "美妆目录检查报告缺少所选单妆分类: " + ", ".join(missing_single)
        )
    if "makeup-compose" in implementation and not payload.get("compose_item_count"):
        raise ValueError("美妆目录检查报告没有可用妆容")
    return payload


def template_runtime_dependencies(
    catalog: dict[str, object], selected_templates: Sequence[str]
) -> list[dict[str, str]]:
    selected = set(selected_templates)
    result: list[dict[str, str]] = []
    for item in catalog.get("makeup", {}).get("template_runtime_dependencies", []):
        if not isinstance(item, dict) or item.get("template") not in selected:
            continue
        if not item.get("coordinate") or not item.get("required_class"):
            raise ValueError("基线 catalog 的模板运行时依赖声明不完整")
        result.append({key: str(value) for key, value in item.items()})
    return result


def gradle_dependency_locations(
    project_root: Path, dependency: dict[str, str]
) -> list[str]:
    group, artifact, *_ = dependency["coordinate"].split(":")
    locations: list[str] = []
    for current, directories, filenames in os.walk(
        project_root, topdown=True, followlinks=False
    ):
        directories[:] = [
            name for name in directories if name not in SKIP_DEPENDENCY_DIRS
        ]
        for filename in filenames:
            if filename not in {
                "build.gradle",
                "build.gradle.kts",
                "libs.versions.toml",
            }:
                continue
            path = Path(current) / filename
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            compact = re.sub(r"\s+", "", text).lower()
            coordinate_key = f"{group}:{artifact}".lower()
            catalog_key = f'group="{group}",name="{artifact}"'.lower()
            if coordinate_key in compact or catalog_key in compact:
                locations.append(
                    str(path.relative_to(project_root)).replace("\\", "/")
                )
    return sorted(locations)


def shared_component_issues(
    source_dir: Path, implementation: Sequence[str]
) -> list[str]:
    if not any(feature.startswith("makeup-") for feature in implementation):
        return []
    installer = source_dir / "NveAssetInstaller.java"
    if not installer.is_file():
        return []
    text = installer.read_text(encoding="utf-8", errors="ignore")
    required = {"ASSET_PACKAGE_TYPE_MAKEUP"}
    if "makeup-compose" in implementation:
        required.update(
            {
                "ASSET_PACKAGE_TYPE_FACE_MESH",
                "ASSET_PACKAGE_TYPE_WARP",
                "ASSET_PACKAGE_TYPE_VIDEOFX",
            }
        )
    missing = sorted(token for token in required if token not in text)
    if not missing:
        return []
    return [
        "已有 NveAssetInstaller 未显式支持本次美妆素材类型: "
        + ", ".join(missing)
    ]


def unresolved_scopes(
    features: Sequence[str],
    asset_map: dict[str, object] | None = None,
) -> list[str]:
    report = contract_report(features)
    mapping = asset_map or {}

    def has_mapping(feature: str) -> bool:
        value = mapping.get(feature)
        if not value:
            return False
        if feature == "filter-packaged" and isinstance(value, dict):
            return bool(value.get("packages")) and bool(value.get("display_items"))
        return True

    return [
        feature
        for feature in report["scope_selection_required"]
        if not (
            feature in report.get("required_asset_contracts", {})
            and has_mapping(feature)
        )
    ]


def select_templates(
    features: list[str],
    layer: str,
    profile: str | None = None,
    ui_profile: str | Sequence[str] | None = None,
) -> tuple[str, ...]:
    if layer not in LAYERS:
        raise ValueError(f"未知模板层: {layer}")
    if profile is not None and profile not in PROFILES:
        raise ValueError(f"未登记渲染 profile: {profile}")
    if profile is not None and layer == "controllers":
        raise ValueError("--profile 只能与 common/all 一起使用")
    ui_profiles = normalize_ui_profiles(ui_profile)
    for item in ui_profiles:
        if item not in UI_PROFILES:
            raise ValueError(f"未登记 UI profile: {item}")
    implementation = implementation_features(features)
    for item in ui_profiles:
        matches = UI_PROFILE_FEATURE_MATCHES[item]
        if not any(
            feature == match or feature.startswith(match)
            for feature in implementation
            for match in matches
        ):
            raise ValueError(
                f"{item} UI profile 只适用于{UI_PROFILE_MODULE_NAMES[item]}功能"
            )
    selected = list(COMMON_TEMPLATES) if layer in {"common", "all"} else []
    if profile is not None and layer in {"common", "all"}:
        selected.extend(PROFILE_TEMPLATES[profile])
    if layer in {"controllers", "all"}:
        if any(feature.startswith("beauty-") for feature in implementation):
            selected.append("features/NveBeautyCoordinator.java.template")
        if any(feature.startswith("makeup-") for feature in implementation):
            selected.extend(MAKEUP_TEMPLATES)
        if "filter-packaged" in implementation:
            selected.append("features/NveFilterCoordinator.java.template")
        if "face-prop" in implementation:
            selected.append("features/NveFacePropCoordinator.java.template")
    if ui_profiles:
        selected.extend(UI_COMMON_TEMPLATES)
    for item in ui_profiles:
        selected.extend(UI_PROFILE_TEMPLATES[item])
    if not selected:
        raise ValueError("当前功能和 layer 没有登记可落地模板")
    return tuple(selected)


def target_name(template: Path) -> str:
    suffix = ".java.template"
    if not template.name.endswith(suffix):
        raise ValueError(f"不支持的模板名称: {template.name}")
    return template.name[: -len(".template")]


def resolve_inside_project(project_root: Path, relative_source_dir: str) -> Path:
    source_dir = Path(relative_source_dir)
    if source_dir.is_absolute():
        raise ValueError("--source-dir 必须是项目根目录下的相对路径")
    resolved = (project_root / source_dir).resolve()
    if not resolved.is_relative_to(project_root):
        raise ValueError("--source-dir 不得越过项目根目录")
    return resolved


def resolve_input_path(project_root: Path, input_path: Path) -> Path:
    """Resolve relative CLI inputs from the target project, not the caller's cwd."""
    expanded = input_path.expanduser()
    return expanded.resolve() if expanded.is_absolute() else (project_root / expanded).resolve()


def build_plan(
    project_root: Path,
    source_dir: Path,
    package_name: str,
    baseline: str,
    features: list[str],
    layer: str,
    profile: str | None = None,
    ui_profile: str | Sequence[str] | None = None,
    test_source_dir: Path | None = None,
) -> list[dict[str, str]]:
    baseline_root = SKILL_ROOT / "assets" / f"baseline-{baseline}"
    catalog_path = baseline_root / "catalog" / "feature-catalog.json"
    if not catalog_path.is_file():
        raise ValueError(f"未登记基线: {baseline}")
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    if catalog.get("sdk_version") != baseline:
        raise ValueError(f"基线 catalog 版本不一致: {baseline}")

    result: list[dict[str, str]] = []
    selected_templates = list(
        select_templates(features, layer, profile, ui_profile)
    )
    implementation = implementation_features(features)
    if test_source_dir is not None:
        if layer in {"common", "all"}:
            selected_templates.extend(COMMON_TEST_TEMPLATES)
        if profile is not None and layer in {"common", "all"}:
            selected_templates.extend(PROFILE_TEST_TEMPLATES.get(profile, ()))
        if any(feature.startswith("beauty-") for feature in implementation):
            selected_templates.extend(FEATURE_TEST_TEMPLATES["beauty"])
        if any(feature.startswith("makeup-") for feature in implementation):
            selected_templates.extend(FEATURE_TEST_TEMPLATES["makeup"])
        for feature in ("filter-packaged", "face-prop"):
            if feature in implementation:
                selected_templates.extend(FEATURE_TEST_TEMPLATES[feature])

    for relative in selected_templates:
        template = baseline_root / relative
        if not template.is_file():
            raise ValueError(f"基线模板缺失: {relative}")
        target_root = (
            test_source_dir
            if relative in ALL_TEST_TEMPLATES
            else source_dir
        )
        target = target_root / target_name(template)
        rendered = template.read_text(encoding="utf-8").replace(
            "__TARGET_PACKAGE__", package_name
        )
        if "__TARGET_PACKAGE__" in rendered:
            raise ValueError(f"模板包名占位符未完全替换: {relative}")
        result.append(
            {
                "template": str(template.relative_to(SKILL_ROOT)).replace("\\", "/"),
                "target": str(target.relative_to(project_root)).replace("\\", "/"),
                "content": rendered,
            }
        )
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_baseline_aar(aar_path: Path, baseline: str) -> str:
    baseline_root = SKILL_ROOT / "assets" / f"baseline-{baseline}"
    catalog_path = baseline_root / "catalog" / "feature-catalog.json"
    if not catalog_path.is_file():
        raise ValueError(f"未登记基线: {baseline}")
    if not aar_path.is_file() or aar_path.suffix.lower() != ".aar":
        raise ValueError(f"AAR 不存在或扩展名错误: {aar_path}")
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    actual = sha256_file(aar_path)
    expected = catalog.get("aar_sha256")
    if actual != expected:
        raise ValueError(
            f"AAR SHA-256 未命中基线 {baseline}: expected={expected}, actual={actual}"
        )
    return actual


def existing_targets(plan: list[dict[str, str]], project_root: Path) -> list[str]:
    return [
        item["target"]
        for item in plan
        if (project_root / item["target"]).exists()
    ]


def partition_existing_targets(
    plan: list[dict[str, str]], project_root: Path
) -> tuple[list[str], list[str]]:
    reused: list[str] = []
    conflicts: list[str] = []
    for item in plan:
        target = project_root / item["target"]
        if not target.exists():
            continue
        try:
            identical = target.read_text(encoding="utf-8") == item["content"]
        except (OSError, UnicodeError):
            identical = False
        (reused if identical else conflicts).append(item["target"])
    return reused, conflicts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把已登记 NveEffectKit 基线模板安全落地到目标 Android 工程。"
    )
    parser.add_argument("project_root", help="用户指定的 Android 项目根目录")
    parser.add_argument(
        "--source-dir",
        required=True,
        help="项目根目录下用于放置生成 Java 文件的相对目录",
    )
    parser.add_argument("--package", required=True, dest="package_name")
    parser.add_argument("--features", required=True, help="逗号分隔的 feature ID/别名")
    parser.add_argument("--baseline", default="3.16.1")
    parser.add_argument(
        "--aar",
        required=True,
        type=Path,
        help="用户明确提供或目标工程内的 NveEffectKit AAR；相对路径以项目根目录为基准",
    )
    parser.add_argument(
        "--layer",
        required=True,
        choices=LAYERS,
        help="common=仅公共桥；controllers=仅功能控制器；all=首次接入两者",
    )
    parser.add_argument(
        "--profile",
        choices=PROFILES,
        help="common/all 同时落地已登记的渲染 profile 适配器",
    )
    parser.add_argument(
        "--ui-profile",
        choices=UI_PROFILES,
        action="append",
        dest="ui_profiles",
        help="落地已登记的可选 UI profile；已有产品 UI 时不要使用",
    )
    parser.add_argument(
        "--test-source-dir",
        help="可选：项目内 Java 单元测试源码目录；为最终所选 controller 落地登记回归测试",
    )
    parser.add_argument(
        "--asset-map",
        type=Path,
        help="已通过前置门禁的 feature-to-path JSON；相对路径以项目根目录为基准",
    )
    parser.add_argument(
        "--makeup-catalog-report",
        type=Path,
        help="inspect_makeup_catalog.py 的零错误/零警告 JSON 输出；美妆范围必需",
    )
    parser.add_argument("--dry-run", action="store_true", help="只输出计划，不写文件")
    parser.add_argument("--json", action="store_true", help="输出机器可读结果")
    args = parser.parse_args(argv)

    project_root = Path(args.project_root).resolve()
    if not project_root.is_dir():
        print(f"项目根目录不存在: {project_root}", file=sys.stderr)
        return 2
    if not PACKAGE_PATTERN.fullmatch(args.package_name):
        print(f"非法 Java 包名: {args.package_name}", file=sys.stderr)
        return 2

    features = [
        item.strip() for item in args.features.split(",") if item.strip()
    ]
    requested = normalize_requested_features(features)
    unknown = unknown_features(requested)
    if not requested:
        print("features 不能为空", file=sys.stderr)
        return 2
    if unknown:
        print(f"未知 feature: {', '.join(unknown)}", file=sys.stderr)
        return 2

    try:
        asset_map = load_asset_map(
            resolve_input_path(project_root, args.asset_map) if args.asset_map else None
        )
        unresolved = unresolved_scopes(features, asset_map)
        if unresolved:
            raise ValueError(
                "功能范围未解析，先按 feature_contract.py 的 selection_requests "
                "取得用户选择并重新执行门禁: "
                + ", ".join(unresolved)
            )
        aar_sha256 = verify_baseline_aar(
            resolve_input_path(project_root, args.aar), args.baseline
        )
        source_dir = resolve_inside_project(project_root, args.source_dir)
        test_source_dir = (
            resolve_inside_project(project_root, args.test_source_dir)
            if args.test_source_dir
            else None
        )
        baseline_root = SKILL_ROOT / "assets" / f"baseline-{args.baseline}"
        catalog = json.loads(
            (baseline_root / "catalog" / "feature-catalog.json").read_text(
                encoding="utf-8"
            )
        )
        implementation = implementation_features(requested)
        makeup_in_scope = any(
            feature.startswith("makeup-") for feature in implementation
        )
        makeup_catalog = None
        if makeup_in_scope:
            if args.makeup_catalog_report is None:
                raise ValueError(
                    "美妆模板落地必须传 --makeup-catalog-report；"
                    "先运行 inspect_makeup_catalog.py 并解决全部 errors/warnings/selection_requests"
                )
            makeup_catalog = load_makeup_catalog_report(
                resolve_input_path(project_root, args.makeup_catalog_report),
                catalog,
                implementation,
            )
        plan = build_plan(
            project_root,
            source_dir,
            args.package_name,
            args.baseline,
            requested,
            args.layer,
            args.profile,
            args.ui_profiles,
            test_source_dir,
        )
        selected_templates = select_templates(
            requested, args.layer, args.profile, args.ui_profiles
        )
        runtime_dependencies = template_runtime_dependencies(
            catalog, selected_templates
        )
        dependency_locations = {
            item["coordinate"]: gradle_dependency_locations(project_root, item)
            for item in runtime_dependencies
        }
        missing_dependencies = [
            item["coordinate"]
            for item in runtime_dependencies
            if not dependency_locations[item["coordinate"]]
        ]
        compatibility_issues = shared_component_issues(
            source_dir, implementation
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    reused, conflicts = partition_existing_targets(plan, project_root)
    if conflicts:
        print(
            "拒绝覆盖内容不同的已有文件: " + ", ".join(conflicts),
            file=sys.stderr,
        )
        return 3
    if not args.dry_run and missing_dependencies:
        print(
            "缺少模板运行时依赖，先修改目标模块 Gradle: "
            + ", ".join(missing_dependencies),
            file=sys.stderr,
        )
        return 4
    if not args.dry_run and compatibility_issues:
        print(
            "共享组件兼容性检查失败: " + "; ".join(compatibility_issues),
            file=sys.stderr,
        )
        return 4

    if not args.dry_run:
        source_dir.mkdir(parents=True, exist_ok=True)
        if test_source_dir is not None:
            test_source_dir.mkdir(parents=True, exist_ok=True)
        for item in plan:
            target = project_root / item["target"]
            if item["target"] in reused:
                continue
            target.write_text(item["content"], encoding="utf-8", newline="\n")

    report = {
        "schema_version": 5,
        "status": "DRY_RUN" if args.dry_run else "MATERIALIZED",
        "baseline": args.baseline,
        "aar_sha256": aar_sha256,
        "layer": args.layer,
        "profile": args.profile,
        "ui_profiles": normalize_ui_profiles(args.ui_profiles),
        "requested_features": requested,
        "implementation_features": implementation,
        "created_files": [
            item["target"] for item in plan if item["target"] not in reused
        ],
        "reused_files": reused,
        "required_gradle_dependencies": runtime_dependencies,
        "dependency_locations": dependency_locations,
        "missing_gradle_dependencies": missing_dependencies,
        "compatibility_issues": compatibility_issues,
        "makeup_catalog": (
            {
                "resource_root": makeup_catalog.get("resource_root"),
                "single_index": makeup_catalog.get("catalog", {}).get("single_index"),
                "single_category_count": makeup_catalog.get("single_category_count"),
                "single_item_count": makeup_catalog.get("single_item_count"),
                "compose_item_count": makeup_catalog.get("compose_item_count"),
                "compose_metadata": [
                    item.get("metadata")
                    for item in makeup_catalog.get("catalog", {}).get(
                        "compose_items", []
                    )
                ],
            }
            if makeup_catalog is not None
            else None
        ),
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"status: {report['status']}")
        print(f"baseline: {report['baseline']}")
        for path in report["created_files"]:
            print(f"file: {path}")
        for path in report["reused_files"]:
            print(f"reused: {path}")
        for dependency in report["required_gradle_dependencies"]:
            print(f"gradle-dependency: {dependency['coordinate']}")
        for issue in report["compatibility_issues"]:
            print(f"compatibility-issue: {issue}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
