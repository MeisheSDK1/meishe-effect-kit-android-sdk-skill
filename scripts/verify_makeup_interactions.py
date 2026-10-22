#!/usr/bin/env python3
"""Verify the compose/single makeup selection contract on an adb device."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass


BOUNDS = re.compile(r"^\[(\d+),(\d+)]\[(\d+),(\d+)]$")
REMOTE_DUMP = "/sdcard/nve-makeup-ui.xml"


@dataclass(frozen=True)
class UiNode:
    description: str
    selected: bool
    bounds: tuple[int, int, int, int]

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.bounds
        return ((left + right) // 2, (top + bottom) // 2)


def parse_nodes(xml_text: str) -> dict[str, UiNode]:
    root = ET.fromstring(xml_text)
    result: dict[str, UiNode] = {}
    for element in root.iter("node"):
        description = element.attrib.get("content-desc", "")
        match = BOUNDS.fullmatch(element.attrib.get("bounds", ""))
        if not description or match is None:
            continue
        result[description] = UiNode(
            description=description,
            selected=element.attrib.get("selected") == "true",
            bounds=tuple(int(value) for value in match.groups()),
        )
    return result


class Adb:
    def __init__(self, executable: str, serial: str | None) -> None:
        self.prefix = [executable]
        if serial:
            self.prefix.extend(["-s", serial])

    def run(self, *args: str) -> str:
        completed = subprocess.run(
            [*self.prefix, *args],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        return completed.stdout

    def nodes(self) -> dict[str, UiNode]:
        self.run("shell", "uiautomator", "dump", REMOTE_DUMP)
        return parse_nodes(self.run("exec-out", "cat", REMOTE_DUMP))

    def tap(self, description: str) -> None:
        node = require_node(self.nodes(), description)
        x, y = node.center
        self.run("shell", "input", "tap", str(x), str(y))
        time.sleep(0.35)


def require_node(nodes: dict[str, UiNode], description: str) -> UiNode:
    node = nodes.get(description)
    if node is None:
        available = ", ".join(sorted(nodes))
        raise AssertionError(
            f"missing visible node {description!r}; visible descriptions: {available}"
        )
    return node


def require_selected(adb: Adb, description: str) -> None:
    node = require_node(adb.nodes(), description)
    if not node.selected:
        raise AssertionError(f"{description!r} is visible but not selected")


def verify(args: argparse.Namespace) -> dict[str, object]:
    adb = Adb(args.adb, args.serial)
    compose_item = f"nve-makeup-item-{args.compose_item_id}"
    single_item = f"nve-makeup-item-{args.single_item_id}"
    single_tab = f"nve-makeup-tab-{args.single_category}"

    adb.run("shell", "am", "force-stop", args.package)
    adb.run(
        "shell",
        "monkey",
        "-p",
        args.package,
        "-c",
        "android.intent.category.LAUNCHER",
        "1",
    )
    time.sleep(args.launch_wait)
    entry = require_node(adb.nodes(), "nve-makeup-entry")
    if not entry.selected:
        adb.tap("nve-makeup-entry")
    adb.tap("nve-makeup-tab-compose")
    adb.tap(compose_item)
    require_selected(adb, compose_item)

    adb.tap(single_tab)
    nodes = adb.nodes()
    synthetic = f"nve-makeup-item-compose-component-{args.single_category}"
    if synthetic in nodes:
        raise AssertionError(
            f"single category exposes forbidden synthetic compose item {synthetic!r}"
        )
    adb.tap(single_item)
    require_selected(adb, single_item)

    adb.tap("nve-makeup-tab-compose")
    require_selected(adb, compose_item)
    adb.tap("nve-makeup-none-compose")

    adb.tap(single_tab)
    require_selected(adb, single_item)
    return {
        "schema_version": 1,
        "status": "DEVICE_VERIFIED",
        "package": args.package,
        "compose_item_id": args.compose_item_id,
        "single_category": args.single_category,
        "single_item_id": args.single_item_id,
        "assertions": [
            "compose-selected",
            "no-synthetic-compose-item-in-single-category",
            "compose-selection-survives-single-override",
            "clearing-compose-preserves-explicit-single",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="在 adb 设备上验证组合妆与单妆叠加、选中态和分类清除语义。"
    )
    parser.add_argument("--package", required=True, help="待测 applicationId")
    parser.add_argument("--compose-item-id", required=True, help="一个真实组合妆条目 ID")
    parser.add_argument("--single-category", default="lip", help="单妆分类 ID")
    parser.add_argument("--single-item-id", required=True, help="一个真实单妆条目 ID")
    parser.add_argument("--serial", help="adb device serial；单设备时可省略")
    parser.add_argument("--adb", default="adb", help="adb 可执行文件")
    parser.add_argument("--launch-wait", type=float, default=1.5)
    args = parser.parse_args(argv)
    try:
        report = verify(args)
    except (
        AssertionError,
        ET.ParseError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "FAILED",
                    "reason": str(exc),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
