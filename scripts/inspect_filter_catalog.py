#!/usr/bin/env python3
"""Inspect delivered packaged-filter metadata and emit a two-layer asset map."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any


NO_LICENSE_POLICIES = {
    "user-confirmed-no-separate-license",
    "delivery-confirmed-no-separate-license",
}
PACKAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def issue(code: str, message: str, **context: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **context}


def mapped_path(prefix: str, filename: str) -> str:
    return (
        (PurePosixPath(prefix) / filename).as_posix()
        if prefix
        else filename
    )


def safe_leaf(value: Any, suffix: str) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value == Path(value).name
        and "/" not in value
        and "\\" not in value
        and ".." not in value
        and value.lower().endswith(suffix)
    )


def load_metadata(filter_root: Path) -> tuple[list[Any], list[dict[str, Any]]]:
    info_path = filter_root / "info.json"
    if not info_path.is_file():
        return [], [issue("FILTER_INFO_MISSING", "滤镜目录缺少 info.json。")]
    try:
        payload = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [], [
            issue("FILTER_INFO_INVALID", f"info.json 无法解析：{error}")
        ]
    info_list = payload.get("infoList") if isinstance(payload, dict) else None
    if not isinstance(info_list, list) or not info_list:
        return [], [
            issue(
                "FILTER_INFO_LIST_INVALID",
                "info.json 的 infoList 必须是非空数组。",
            )
        ]
    return info_list, []


def inspect_catalog(
    filter_root: Path,
    asset_prefix: str = "",
    license_policy: str = "require",
) -> dict[str, Any]:
    root = filter_root.resolve()
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    info_list, metadata_errors = load_metadata(root)
    errors.extend(metadata_errors)
    packages_by_key: dict[str, dict[str, Any]] = {}
    asset_to_key: dict[str, str] = {}
    display_items: list[dict[str, Any]] = []
    key_occurrences: Counter[str] = Counter()

    for index, raw_item in enumerate(info_list):
        if not isinstance(raw_item, dict):
            errors.append(
                issue(
                    "FILTER_ITEM_INVALID",
                    "滤镜展示项必须是对象。",
                    index=index,
                )
            )
            continue
        name = raw_item.get("name")
        asset_name = raw_item.get("fxFileName")
        package_key = raw_item.get("fxPackageId")
        if not isinstance(name, str) or not name.strip():
            errors.append(
                issue(
                    "FILTER_DISPLAY_NAME_MISSING",
                    "滤镜展示项缺少 name。",
                    index=index,
                )
            )
            continue
        if not safe_leaf(asset_name, ".videofx"):
            errors.append(
                issue(
                    "FILTER_ASSET_NAME_INVALID",
                    "fxFileName 必须是安全的 .videofx 文件名。",
                    index=index,
                    name=name,
                )
            )
            continue
        if (
            not isinstance(package_key, str)
            or not PACKAGE_ID_RE.fullmatch(package_key)
        ):
            errors.append(
                issue(
                    "FILTER_PACKAGE_KEY_INVALID",
                    "fxPackageId 缺失或格式非法。",
                    index=index,
                    name=name,
                )
            )
            continue
        if not (
            asset_name == f"{package_key}.videofx"
            or asset_name.startswith(f"{package_key}.")
        ):
            errors.append(
                issue(
                    "FILTER_PACKAGE_FILENAME_MISMATCH",
                    "fxPackageId 与 fxFileName 不匹配。",
                    index=index,
                    name=name,
                    package=package_key,
                    asset=asset_name,
                )
            )
            continue

        asset_path = root / asset_name
        if not asset_path.is_file():
            errors.append(
                issue(
                    "FILTER_ASSET_MISSING",
                    "元数据声明的滤镜包不存在。",
                    index=index,
                    name=name,
                    asset=asset_name,
                )
            )

        existing_package = packages_by_key.get(package_key)
        existing_key = asset_to_key.get(asset_name)
        if existing_package and existing_package["asset_filename"] != asset_name:
            errors.append(
                issue(
                    "FILTER_PACKAGE_KEY_CONFLICT",
                    "同一 fxPackageId 指向多个滤镜包。",
                    index=index,
                    package=package_key,
                )
            )
            continue
        if existing_key and existing_key != package_key:
            errors.append(
                issue(
                    "FILTER_ASSET_IDENTITY_CONFLICT",
                    "同一滤镜包被声明为多个 fxPackageId。",
                    index=index,
                    asset=asset_name,
                )
            )
            continue

        if not existing_package:
            license_name = raw_item.get("fxLicFileName", "")
            license_value: str | None
            license_basis: str
            if license_name:
                if not safe_leaf(license_name, ".lic"):
                    errors.append(
                        issue(
                            "FILTER_LICENSE_NAME_INVALID",
                            "fxLicFileName 必须是安全的 .lic 文件名。",
                            index=index,
                            package=package_key,
                        )
                    )
                    license_value = None
                    license_basis = "unresolved"
                elif (root / license_name).is_file():
                    license_value = mapped_path(asset_prefix, license_name)
                    license_basis = "file-provided"
                elif license_policy in NO_LICENSE_POLICIES:
                    license_value = None
                    license_basis = license_policy
                    warnings.append(
                        issue(
                            "FILTER_LICENSE_DECLARED_BUT_CONFIRMED_OPTIONAL",
                            "元数据声明的素材 lic 缺失，已按显式确认记录为无需独立 lic。",
                            package=package_key,
                            declared_license=license_name,
                            basis=license_policy,
                        )
                    )
                else:
                    errors.append(
                        issue(
                            "FILTER_LICENSE_MISSING",
                            "元数据声明的素材 lic 不存在；请提供文件或显式确认无需独立 lic。",
                            package=package_key,
                            declared_license=license_name,
                        )
                    )
                    license_value = None
                    license_basis = "unresolved"
            elif license_policy in NO_LICENSE_POLICIES:
                license_value = None
                license_basis = license_policy
            else:
                errors.append(
                    issue(
                        "FILTER_LICENSE_STATUS_UNRESOLVED",
                        "滤镜包未声明素材 lic 状态。",
                        package=package_key,
                    )
                )
                license_value = None
                license_basis = "unresolved"

            packages_by_key[package_key] = {
                "key": package_key,
                "asset": mapped_path(asset_prefix, asset_name),
                "asset_filename": asset_name,
                "license": license_value,
                "license_basis": license_basis,
            }
            asset_to_key[asset_name] = package_key

        key_occurrences[package_key] += 1
        display_id = (
            package_key.lower()
            + "-"
            + str(key_occurrences[package_key])
        )
        display_item: dict[str, Any] = {
            "id": display_id,
            "name": name.strip(),
            "package": package_key,
        }
        cover_name = raw_item.get("imageName")
        if cover_name:
            if not safe_leaf(cover_name, ".png"):
                warnings.append(
                    issue(
                        "FILTER_COVER_NAME_INVALID",
                        "imageName 不是安全的 PNG 文件名。",
                        index=index,
                        name=name,
                    )
                )
            else:
                display_item["cover"] = mapped_path(
                    asset_prefix, cover_name
                )
                if not (root / cover_name).is_file():
                    warnings.append(
                        issue(
                            "FILTER_COVER_MISSING",
                            "滤镜封面不存在；效果可接入但 UI 需降级。",
                            index=index,
                            name=name,
                            cover=cover_name,
                        )
                    )
        fit_ratio = raw_item.get("fitRatio")
        if isinstance(fit_ratio, int) and not isinstance(fit_ratio, bool):
            display_item["fit_ratio"] = fit_ratio
        display_items.append(display_item)

    declared_assets = {
        package["asset_filename"] for package in packages_by_key.values()
    }
    actual_assets = {path.name for path in root.glob("*.videofx")}
    for extra in sorted(actual_assets - declared_assets):
        warnings.append(
            issue(
                "FILTER_ASSET_UNREFERENCED",
                "目录中的 .videofx 未被 info.json 引用。",
                asset=extra,
            )
        )

    packages = []
    for package in packages_by_key.values():
        packages.append(
            {
                key: value
                for key, value in package.items()
                if key != "asset_filename"
            }
        )
    asset_map = {
        "filter-packaged": {
            "packages": packages,
            "display_items": display_items,
        }
    }
    return {
        "schema_version": 1,
        "filter_root": root.name,
        "display_item_count": len(display_items),
        "unique_package_count": len(packages),
        "shared_package_count": sum(
            1 for count in key_occurrences.values() if count > 1
        ),
        "errors": errors,
        "warnings": warnings,
        "asset_map": asset_map,
    }


def render_text(report: dict[str, Any]) -> str:
    lines = [
        f"Filter catalog: {report['filter_root']}",
        (
            f"Display items={report['display_item_count']}, "
            f"unique packages={report['unique_package_count']}, "
            f"shared packages={report['shared_package_count']}"
        ),
    ]
    for label, key in (("ERROR", "errors"), ("WARNING", "warnings")):
        for item in report[key]:
            lines.append(f"{label} {item['code']}: {item['message']}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect a packaged-filter directory and optionally write a "
            "packages/display_items asset map."
        )
    )
    parser.add_argument("filter_root", help="authorized filter directory")
    parser.add_argument(
        "--asset-prefix",
        default="",
        help="path prefix stored before each package/cover filename",
    )
    parser.add_argument(
        "--license-policy",
        choices=["require", *sorted(NO_LICENSE_POLICIES)],
        default="require",
        help="how to handle a declared or implicit missing material license",
    )
    parser.add_argument(
        "--asset-map-out",
        default=None,
        help="write the generated pure asset-map JSON after validation passes",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.filter_root).expanduser().resolve()
    if not root.is_dir():
        print(f"Invalid filter root: {args.filter_root}", file=sys.stderr)
        return 2
    prefix = args.asset_prefix.replace("\\", "/").strip("/")
    if any(part in {"", ".", ".."} for part in PurePosixPath(prefix).parts):
        print("Invalid asset prefix", file=sys.stderr)
        return 2
    report = inspect_catalog(root, prefix, args.license_policy)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_text(report))
    if args.asset_map_out and not report["errors"]:
        output = Path(args.asset_map_out).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                report["asset_map"],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
