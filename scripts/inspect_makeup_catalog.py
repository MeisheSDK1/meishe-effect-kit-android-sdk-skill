#!/usr/bin/env python3
"""Inspect delivered makeup metadata without assuming vendor directory names."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


MAKEUP_TYPES = {
    "Lip",
    "Eyebrow",
    "Eyeshadow",
    "Eyelash",
    "Eyeliner",
    "Blusher",
    "Shadow",
    "Brighten",
    "Eyeball",
}
PACKAGE_SUFFIXES = {".makeup", ".facemesh", ".warp", ".videofx"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$"
)


def issue(code: str, message: str, **context: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **context}


def load_json(path: Path) -> tuple[Any | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError) as error:
        return None, str(error)


def relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def safe_parts(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parts = PurePosixPath(value.replace("\\", "/").strip("/")).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    return parts


def resolve_inside(
    root: Path,
    value: str | Path,
    *,
    base: Path | None = None,
) -> Path:
    supplied = Path(value).expanduser()
    candidate = supplied if supplied.is_absolute() else (base or root) / supplied
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"path escapes authorized resource root: {value}")
    return resolved


def resolve_declared(
    root: Path,
    value: Any,
    declared_path_prefix: tuple[str, ...],
) -> tuple[Path | None, str | None]:
    parts = safe_parts(value)
    if parts is None:
        return None, "path is empty or contains an unsafe segment"
    if declared_path_prefix:
        if parts[: len(declared_path_prefix)] != declared_path_prefix:
            return None, "path does not start with the configured metadata prefix"
        parts = parts[len(declared_path_prefix):]
    candidate = root.joinpath(*parts).resolve()
    if not candidate.is_relative_to(root):
        return None, "path escapes the authorized resource root"
    return candidate, None


def package_uuid(path: Path) -> str | None:
    value = path.name.split(".", 1)[0]
    return value if UUID_RE.fullmatch(value) else None


def image_candidates(directory: Path, root: Path) -> list[str]:
    return sorted(
        relative(path, root)
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    )


def inspect_declared_cover(
    *,
    root: Path,
    directory: Path,
    declared: Any,
    declared_path_prefix: tuple[str, ...] | None,
    owner: str,
    warnings: list[dict[str, Any]],
) -> str | None:
    if not isinstance(declared, str) or not declared.strip():
        return None
    if declared_path_prefix is None:
        parts = safe_parts(declared)
        if parts is None or len(parts) != 1:
            cover_path, error = None, "cover must be a filename"
        else:
            cover_path, error = (directory / parts[0]).resolve(), None
    else:
        cover_path, error = resolve_declared(
            root, declared, declared_path_prefix
        )
    if error or cover_path is None or not cover_path.is_relative_to(root):
        warnings.append(
            issue(
                "MAKEUP_COVER_PATH_INVALID",
                "封面路径不安全或不在已授权资源根内。",
                owner=owner,
                declared_cover=declared,
                reason=error,
            )
        )
        return None
    if not cover_path.is_file():
        candidates = image_candidates(directory, root)
        warnings.append(
            issue(
                "MAKEUP_DECLARED_COVER_MISSING",
                "元数据声明的封面不存在；候选图片只能经用户确认后写回，不能自动猜测。",
                owner=owner,
                declared_cover=declared,
                cover_candidates=candidates,
                requires_confirmation=bool(candidates),
            )
        )
        return None
    if cover_path.suffix.lower() not in IMAGE_SUFFIXES:
        warnings.append(
            issue(
                "MAKEUP_COVER_TYPE_UNSUPPORTED",
                "封面扩展名不属于登记的图片类型。",
                owner=owner,
                declared_cover=declared,
            )
        )
        return None
    return relative(cover_path, root)


def looks_like_single_index(payload: Any) -> bool:
    return (
        isinstance(payload, list)
        and bool(payload)
        and all(
            isinstance(item, dict)
            and isinstance(item.get("displayName"), str)
            and isinstance(item.get("jsonPath"), str)
            for item in payload
        )
    )


def looks_like_compose_metadata(payload: Any) -> bool:
    if not isinstance(payload, dict) or not isinstance(payload.get("uuid"), str):
        return False
    content = payload.get("effectContent")
    return isinstance(content, dict) and any(
        isinstance(content.get(key), list)
        for key in ("makeup", "beauty", "shape", "microShape", "filter")
    )


def discover_json(
    root: Path,
    base: Path,
    predicate: Any,
) -> list[Path]:
    result = []
    for path in sorted(base.rglob("*.json")):
        try:
            if not path.resolve().is_relative_to(root):
                continue
        except OSError:
            continue
        payload, error = load_json(path)
        if error is None and predicate(payload):
            result.append(path.resolve())
    return result


def iter_param_package_references(entries: Any) -> Iterable[str]:
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        params = entry.get("params")
        if not isinstance(params, list):
            continue
        for param in params:
            if not isinstance(param, dict):
                continue
            key = param.get("key")
            value = param.get("value")
            if (
                isinstance(key, str)
                and "Package Id" in key
                and isinstance(value, str)
                and UUID_RE.fullmatch(value)
            ):
                yield value


def compose_references(metadata: dict[str, Any]) -> set[str]:
    content = metadata.get("effectContent")
    if not isinstance(content, dict):
        return set()
    result = set()
    for group in ("makeup", "shape", "microShape"):
        result.update(iter_param_package_references(content.get(group)))
    filters = content.get("filter")
    if isinstance(filters, list):
        for item in filters:
            if not isinstance(item, dict):
                continue
            value = item.get("uuid")
            if isinstance(value, str) and UUID_RE.fullmatch(value):
                result.add(value)
    return result


def inspect_single_catalog(
    root: Path,
    index_path: Path,
    declared_path_prefix: tuple[str, ...],
    errors: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    categories, read_error = load_json(index_path)
    if read_error or not looks_like_single_index(categories):
        errors.append(
            issue(
                "MAKEUP_CATEGORY_INDEX_INVALID",
                "单妆分类索引必须是包含 displayName/jsonPath 的 JSON 数组。",
                path=relative(index_path, root),
                reason=read_error,
            )
        )
        return []

    normalized: list[dict[str, Any]] = []
    seen_types: set[str] = set()
    for category_index, category in enumerate(categories):
        makeup_type = category.get("displayName")
        if makeup_type not in MAKEUP_TYPES:
            errors.append(
                issue(
                    "MAKEUP_CATEGORY_TYPE_UNSUPPORTED",
                    "displayName 不是登记的单妆类型。",
                    index=category_index,
                    value=makeup_type,
                )
            )
            continue
        if makeup_type in seen_types:
            errors.append(
                issue(
                    "MAKEUP_CATEGORY_DUPLICATE",
                    "同一单妆类型被重复声明。",
                    makeup_type=makeup_type,
                )
            )
            continue
        seen_types.add(makeup_type)
        catalog_path, path_error = resolve_declared(
            root, category.get("jsonPath"), declared_path_prefix
        )
        if path_error or catalog_path is None or not catalog_path.is_file():
            errors.append(
                issue(
                    "MAKEUP_CATEGORY_CATALOG_MISSING",
                    "单妆分类引用的列表文件不存在或路径非法。",
                    makeup_type=makeup_type,
                    declared_path=category.get("jsonPath"),
                    reason=path_error,
                )
            )
            continue
        items, item_error = load_json(catalog_path)
        if item_error or not isinstance(items, list):
            errors.append(
                issue(
                    "MAKEUP_CATEGORY_CATALOG_INVALID",
                    "单妆分类列表必须是 JSON 数组。",
                    makeup_type=makeup_type,
                    reason=item_error,
                )
            )
            continue

        normalized_items = []
        for item_index, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append(
                    issue(
                        "MAKEUP_SINGLE_ITEM_INVALID",
                        "单妆展示项必须是对象。",
                        makeup_type=makeup_type,
                        index=item_index,
                    )
                )
                continue
            owner = f"{makeup_type}[{item_index}]"
            info_path, info_error = resolve_declared(
                root, item.get("infoPath"), declared_path_prefix
            )
            parent_path, parent_error = resolve_declared(
                root, item.get("parentPath"), declared_path_prefix
            )
            if (
                info_error
                or parent_error
                or info_path is None
                or parent_path is None
                or not info_path.is_file()
                or not parent_path.is_dir()
                or info_path.parent != parent_path
            ):
                errors.append(
                    issue(
                        "MAKEUP_SINGLE_PATH_INVALID",
                        "单妆 infoPath/parentPath 不存在、不一致或越出授权目录。",
                        owner=owner,
                        info_path=item.get("infoPath"),
                        parent_path=item.get("parentPath"),
                    )
                )
                continue
            metadata, metadata_error = load_json(info_path)
            if metadata_error or not isinstance(metadata, dict):
                errors.append(
                    issue(
                        "MAKEUP_SINGLE_INFO_INVALID",
                        "单妆详情元数据无法解析。",
                        owner=owner,
                        reason=metadata_error,
                    )
                )
                continue
            content = metadata.get("effectContent")
            args = content.get("makeupArgs") if isinstance(content, dict) else None
            if (
                not isinstance(args, list)
                or len(args) != 1
                or not isinstance(args[0], dict)
            ):
                errors.append(
                    issue(
                        "MAKEUP_SINGLE_ARGS_INVALID",
                        "单妆详情必须且只能包含一个 makeupArgs 项。",
                        owner=owner,
                    )
                )
                continue
            argument = args[0]
            item_uuid = argument.get("uuid")
            packages = sorted(parent_path.glob("*.makeup"))
            if len(packages) != 1:
                errors.append(
                    issue(
                        "MAKEUP_SINGLE_PACKAGE_COUNT_INVALID",
                        "每个单妆展示目录必须恰好包含一个 .makeup 包。",
                        owner=owner,
                        count=len(packages),
                    )
                )
                continue
            if not isinstance(item_uuid, str) or not UUID_RE.fullmatch(item_uuid):
                errors.append(
                    issue(
                        "MAKEUP_SINGLE_UUID_INVALID",
                        "makeupArgs.uuid 缺失或格式非法。",
                        owner=owner,
                    )
                )
                continue
            if package_uuid(packages[0]) != item_uuid:
                errors.append(
                    issue(
                        "MAKEUP_SINGLE_PACKAGE_UUID_MISMATCH",
                        "单妆元数据 UUID 与 .makeup 原始文件名不匹配。",
                        owner=owner,
                        metadata_uuid=item_uuid,
                        package=relative(packages[0], root),
                    )
                )
            if (
                argument.get("type") != makeup_type
                or metadata.get("makeupId") != makeup_type
            ):
                errors.append(
                    issue(
                        "MAKEUP_SINGLE_TYPE_MISMATCH",
                        "分类、makeupArgs.type 与 makeupId 必须一致。",
                        owner=owner,
                        category=makeup_type,
                        argument_type=argument.get("type"),
                        makeup_id=metadata.get("makeupId"),
                    )
                )
            cover = inspect_declared_cover(
                root=root,
                directory=parent_path,
                declared=item.get("cover"),
                declared_path_prefix=declared_path_prefix,
                owner=owner,
                warnings=warnings,
            )
            normalized_items.append(
                {
                    "id": item_uuid,
                    "name": item.get("name"),
                    "package": relative(packages[0], root),
                    "cover": cover,
                }
            )
        normalized.append({"type": makeup_type, "items": normalized_items})
    return normalized


def inspect_compose_catalog(
    root: Path,
    metadata_paths: list[Path],
    errors: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized = []
    for metadata_path in metadata_paths:
        directory = metadata_path.parent
        metadata, read_error = load_json(metadata_path)
        if read_error or not looks_like_compose_metadata(metadata):
            errors.append(
                issue(
                    "MAKEUP_COMPOSE_INFO_INVALID",
                    "妆容元数据无法解析或不符合妆容字段结构。",
                    path=relative(metadata_path, root),
                    reason=read_error,
                )
            )
            continue
        compose_id = metadata.get("uuid")
        owner = str(compose_id or relative(metadata_path, root))
        if not UUID_RE.fullmatch(compose_id):
            errors.append(
                issue(
                    "MAKEUP_COMPOSE_UUID_INVALID",
                    "妆容 uuid 缺失或格式非法。",
                    path=relative(metadata_path, root),
                )
            )

        packages = {
            uuid: path
            for path in directory.rglob("*")
            if path.is_file()
            and path.suffix.lower() in PACKAGE_SUFFIXES
            and (uuid := package_uuid(path)) is not None
        }
        references = compose_references(metadata)
        for missing in sorted(references - packages.keys()):
            errors.append(
                issue(
                    "MAKEUP_COMPOSE_PACKAGE_MISSING",
                    "妆容元数据引用的组成包不存在。",
                    compose_id=compose_id,
                    package_uuid=missing,
                )
            )
        for extra in sorted(packages.keys() - references):
            warnings.append(
                issue(
                    "MAKEUP_COMPOSE_PACKAGE_UNREFERENCED",
                    "妆容元数据所在目录中的素材包未被引用。",
                    compose_id=compose_id,
                    package=relative(packages[extra], root),
                )
            )
        cover = inspect_declared_cover(
            root=root,
            directory=directory,
            declared=metadata.get("cover"),
            declared_path_prefix=None,
            owner=owner,
            warnings=warnings,
        )
        normalized.append(
            {
                "id": compose_id,
                "name": metadata.get("name"),
                "metadata": relative(metadata_path, root),
                "cover": cover,
                "packages": [
                    relative(packages[uuid], root)
                    for uuid in sorted(references & packages.keys())
                ],
            }
        )
    return normalized


def select_single_index(
    root: Path,
    supplied: Path | None,
    errors: list[dict[str, Any]],
    selection_requests: list[dict[str, Any]],
) -> Path | None:
    if supplied is not None:
        return supplied
    candidates = discover_json(root, root, looks_like_single_index)
    if len(candidates) == 1:
        return candidates[0]
    code = (
        "MAKEUP_CATEGORY_INDEX_MISSING"
        if not candidates
        else "MAKEUP_CATEGORY_INDEX_AMBIGUOUS"
    )
    errors.append(
        issue(
            code,
            "无法唯一识别单妆分类索引；请显式传入 --single-index。",
            candidates=[relative(path, root) for path in candidates],
        )
    )
    if candidates:
        selection_requests.append(
            {
                "kind": "single-index",
                "argument": "--single-index",
                "candidates": [relative(path, root) for path in candidates],
            }
        )
    return None


def select_compose_metadata(
    root: Path,
    discovery_root: Path,
    supplied: list[Path],
    errors: list[dict[str, Any]],
    selection_requests: list[dict[str, Any]],
) -> list[Path]:
    candidates = supplied or discover_json(
        root, discovery_root, looks_like_compose_metadata
    )
    by_id: dict[str, list[Path]] = {}
    for path in candidates:
        payload, _ = load_json(path)
        compose_id = payload.get("uuid") if isinstance(payload, dict) else None
        by_id.setdefault(str(compose_id), []).append(path)
    selected = []
    for compose_id, paths in sorted(by_id.items()):
        if len(paths) == 1:
            selected.append(paths[0])
            continue
        values = [relative(path, root) for path in paths]
        errors.append(
            issue(
                "MAKEUP_COMPOSE_METADATA_AMBIGUOUS",
                "同一妆容存在多份候选元数据；请显式传入要使用的 --compose-metadata。",
                compose_id=compose_id,
                candidates=values,
            )
        )
        selection_requests.append(
            {
                "kind": "compose-metadata",
                "argument": "--compose-metadata",
                "compose_id": compose_id,
                "candidates": values,
            }
        )
    return selected


def inspect_makeup_catalog(
    resource_root: Path,
    *,
    scope: str = "all",
    single_index: Path | None = None,
    compose_root: Path | None = None,
    compose_metadata: list[Path] | None = None,
    declared_path_prefix: str = "",
) -> dict[str, Any]:
    root = resource_root.resolve()
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    selection_requests: list[dict[str, Any]] = []
    prefix = safe_parts(declared_path_prefix) if declared_path_prefix else ()
    if prefix is None:
        raise ValueError("declared path prefix contains an unsafe segment")

    singles: list[dict[str, Any]] = []
    selected_single_index: Path | None = None
    if scope in {"all", "single"}:
        selected_single_index = select_single_index(
            root, single_index, errors, selection_requests
        )
        if selected_single_index is not None:
            singles = inspect_single_catalog(
                root, selected_single_index, prefix, errors, warnings
            )

    composes: list[dict[str, Any]] = []
    if scope in {"all", "compose"}:
        metadata_paths = select_compose_metadata(
            root,
            compose_root or root,
            compose_metadata or [],
            errors,
            selection_requests,
        )
        composes = inspect_compose_catalog(
            root, metadata_paths, errors, warnings
        )

    return {
        "schema_version": 1,
        "resource_root": root.name,
        "scope": scope,
        "single_category_count": len(singles),
        "single_item_count": sum(len(item["items"]) for item in singles),
        "compose_item_count": len(composes),
        "errors": errors,
        "warnings": warnings,
        "selection_requests": selection_requests,
        "catalog": {
            "single_index": (
                relative(selected_single_index, root)
                if selected_single_index is not None
                else None
            ),
            "single_categories": singles,
            "compose_items": composes,
        },
    }


def render_text(report: dict[str, Any]) -> str:
    lines = [
        f"Makeup resources: {report['resource_root']}",
        (
            f"Single categories={report['single_category_count']}, "
            f"single items={report['single_item_count']}, "
            f"compose items={report['compose_item_count']}"
        ),
    ]
    for label, key in (("ERROR", "errors"), ("WARNING", "warnings")):
        for item in report[key]:
            lines.append(f"{label} {item['code']}: {item['message']}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect makeup metadata below an authorized resource root. "
            "Directory names are not part of the contract."
        )
    )
    parser.add_argument("resource_root", help="authorized resource root")
    parser.add_argument(
        "--scope",
        choices=("all", "single", "compose"),
        default="all",
        help="selected makeup capability",
    )
    parser.add_argument(
        "--single-index",
        help="single-makeup category index, relative to resource root",
    )
    parser.add_argument(
        "--compose-root",
        help="limit schema discovery to this directory below resource root",
    )
    parser.add_argument(
        "--compose-metadata",
        action="append",
        default=[],
        help="explicit compose metadata path; repeat for multiple looks",
    )
    parser.add_argument(
        "--declared-path-prefix",
        default="",
        help="exact metadata path prefix to strip before resolving below resource root",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.resource_root).expanduser().resolve()
    if not root.is_dir():
        print(f"Invalid resource root: {args.resource_root}", file=sys.stderr)
        return 2
    try:
        single_index = (
            resolve_inside(root, args.single_index)
            if args.single_index
            else None
        )
        compose_root = (
            resolve_inside(root, args.compose_root)
            if args.compose_root
            else None
        )
        compose_metadata = [
            resolve_inside(root, value)
            for value in args.compose_metadata
        ]
        report = inspect_makeup_catalog(
            root,
            scope=args.scope,
            single_index=single_index,
            compose_root=compose_root,
            compose_metadata=compose_metadata,
            declared_path_prefix=args.declared_path_prefix,
        )
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_text(report))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
