#!/usr/bin/env python3
"""Materialize host-owned NVE final-output recording templates without an AAR gate."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = SKILL_ROOT / "assets" / "host-recording"
PACKAGE_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
)
SOURCE_TEMPLATES = (
    "NveFinalTextureFrameSink.java.template",
    "NveVideoRecordingConfig.java.template",
    "RecordingOutputTarget.java.template",
    "NveVideoRecorder.java.template",
)
TEST_TEMPLATES = ("tests/NveVideoRecordingConfigTest.java.template",)


def resolve_inside_project(project_root: Path, relative_dir: str) -> Path:
    candidate = Path(relative_dir)
    if candidate.is_absolute():
        raise ValueError("输出目录必须是项目根目录下的相对路径")
    resolved = (project_root / candidate).resolve()
    if not resolved.is_relative_to(project_root):
        raise ValueError("输出目录不得越过项目根目录")
    return resolved


def target_name(template: Path) -> str:
    if not template.name.endswith(".java.template"):
        raise ValueError(f"不支持的模板名称: {template.name}")
    return template.name[: -len(".template")]


def build_plan(
    project_root: Path,
    source_dir: Path,
    package_name: str,
    test_source_dir: Path | None = None,
) -> list[dict[str, str]]:
    selected = list(SOURCE_TEMPLATES)
    if test_source_dir is not None:
        selected.extend(TEST_TEMPLATES)
    plan: list[dict[str, str]] = []
    for relative in selected:
        template = TEMPLATE_ROOT / relative
        if not template.is_file():
            raise ValueError(f"录制模板缺失: {relative}")
        target_root = (
            test_source_dir if relative in TEST_TEMPLATES else source_dir
        )
        assert target_root is not None
        rendered = template.read_text(encoding="utf-8").replace(
            "__TARGET_PACKAGE__", package_name
        )
        if "__TARGET_PACKAGE__" in rendered:
            raise ValueError(f"模板包名占位符未完全替换: {relative}")
        target = target_root / target_name(template)
        plan.append(
            {
                "template": str(template.relative_to(SKILL_ROOT)).replace(
                    "\\", "/"
                ),
                "target": str(target.relative_to(project_root)).replace(
                    "\\", "/"
                ),
                "content": rendered,
            }
        )
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "落地只消费 NveEffectKit 最终输出纹理的宿主录制模板；"
            "不创建特效管线，也不要求 AAR hash。"
        )
    )
    parser.add_argument("project_root")
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--package", required=True, dest="package_name")
    parser.add_argument("--test-source-dir")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    project_root = Path(args.project_root).resolve()
    if not project_root.is_dir():
        print(f"项目根目录不存在: {project_root}", file=sys.stderr)
        return 2
    if not PACKAGE_PATTERN.fullmatch(args.package_name):
        print(f"非法 Java 包名: {args.package_name}", file=sys.stderr)
        return 2

    try:
        source_dir = resolve_inside_project(project_root, args.source_dir)
        test_source_dir = (
            resolve_inside_project(project_root, args.test_source_dir)
            if args.test_source_dir
            else None
        )
        plan = build_plan(
            project_root,
            source_dir,
            args.package_name,
            test_source_dir,
        )
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2

    existing = [
        item["target"]
        for item in plan
        if (project_root / item["target"]).exists()
    ]
    if existing:
        print("拒绝覆盖已有文件: " + ", ".join(existing), file=sys.stderr)
        return 3

    if not args.dry_run:
        source_dir.mkdir(parents=True, exist_ok=True)
        if test_source_dir is not None:
            test_source_dir.mkdir(parents=True, exist_ok=True)
        for item in plan:
            target = project_root / item["target"]
            target.write_text(item["content"], encoding="utf-8", newline="\n")

    report = {
        "schema_version": 1,
        "status": "DRY_RUN" if args.dry_run else "MATERIALIZED",
        "scope": "nve-final-output-only",
        "aar_hash_required": False,
        "created_files": [item["target"] for item in plan],
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"status: {report['status']}")
        for path in report["created_files"]:
            print(f"file: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
