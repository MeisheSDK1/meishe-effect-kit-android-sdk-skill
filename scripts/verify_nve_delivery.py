#!/usr/bin/env python3
"""Run reproducible static/build/package/device gates for NveEffectKit delivery."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import zipfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from feature_contract import (
    FEATURE_GROUPS,
    contract_report,
    implementation_features,
    normalize_requested_features,
    unknown_features,
)
from inspect_android_project import (
    inspect_project,
    iter_files,
    load_asset_map,
    merged_manifest_application_id,
)
from validate_nve_integration import (
    PROFILES,
    detected_analysis_files,
    source_documents,
    validate,
)


CRASH_PATTERNS = (
    "FATAL EXCEPTION",
    "ANR in ",
    "Fatal signal",
    "tombstone written",
    "native crash",
    "UnsatisfiedLinkError",
)
MODULE_RE = re.compile(r"^:?[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)*$")
VARIANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
FEATURE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:access[_-]?token|auth|authorization|api[_-]?key|key|license|password|secret|"
    r"sig|signature|x-amz-signature|x-goog-signature)=)([^&\s\"']+)"
)
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:access[_-]?token|authorization|cookie|api[_-]?key|license[_-]?key|password|"
    r"proxy-authorization|secret|set-cookie|token)\b\s*[:=]\s*)(?:bearer\s+)?([^\s,;]+)"
)
URL_USERINFO_RE = re.compile(r"(?i)(\bhttps?://)[^/\s:@]+:[^/\s@]+@")


def redact_text(value: str, cwd: Path | None = None, device_serial: str | None = None) -> str:
    replacements: list[tuple[str, str]] = []
    for path, replacement in ((cwd, "<project>"), (Path.home(), "<user-home>")):
        if path is None:
            continue
        replacements.extend(((str(path), replacement), (path.as_posix(), replacement)))
    for source, replacement in replacements:
        value = value.replace(source, replacement)
    value = URL_USERINFO_RE.sub(r"\1<redacted>@", value)
    value = SENSITIVE_QUERY_RE.sub(r"\1<redacted>", value)
    value = SENSITIVE_ASSIGNMENT_RE.sub(r"\1<redacted>", value)
    if device_serial and len(device_serial) >= 4:
        value = value.replace(device_serial, "<device>")
    return value


def sanitize_report(value: Any, root: Path, device_serial: str | None) -> Any:
    if isinstance(value, dict):
        return {key: sanitize_report(item, root, device_serial) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_report(item, root, device_serial) for item in value]
    if isinstance(value, str):
        return redact_text(value, root, device_serial)
    return value


def run(command: list[str], cwd: Path, timeout: int = 600, tail_chars: int = 12000) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        output = (completed.stdout + "\n" + completed.stderr).strip()
        output = redact_text(output, cwd)
        recorded_command = [redact_text(item, cwd) for item in command]
        if "-s" in recorded_command:
            serial_index = recorded_command.index("-s") + 1
            if serial_index < len(recorded_command):
                recorded_command[serial_index] = "<device>"
        return {
            "command": recorded_command,
            "exit_code": completed.returncode,
            "duration_seconds": round(time.monotonic() - started, 2),
            "output_tail": output[-tail_chars:],
        }
    except (OSError, subprocess.TimeoutExpired) as error:
        recorded_command = [redact_text(item, cwd) for item in command]
        if "-s" in recorded_command:
            serial_index = recorded_command.index("-s") + 1
            if serial_index < len(recorded_command):
                recorded_command[serial_index] = "<device>"
        error_text = redact_text(str(error), cwd)
        return {
            "command": recorded_command,
            "exit_code": 127,
            "duration_seconds": round(time.monotonic() - started, 2),
            "output_tail": error_text,
        }


def gradle_wrapper(root: Path) -> list[str] | None:
    bat = root / "gradlew.bat"
    unix = root / "gradlew"
    preferred = bat if os.name == "nt" else unix
    if preferred.is_file():
        return [str(preferred)]
    return None


def task(module: str, name: str) -> str:
    normalized = module if module.startswith(":") else f":{module}"
    return f"{normalized}:{name}"


def capitalized_variant(variant: str) -> str:
    return variant[:1].upper() + variant[1:]


def locate_apks(root: Path, module: str, variant: str) -> list[Path]:
    module_path = root.joinpath(*module.strip(":").split(":"))
    output = module_path / "build" / "outputs" / "apk" / variant
    return sorted(output.glob("*.apk")) if output.exists() else []


def validate_cli_values(module: str, variant: str, features: list[str]) -> list[str]:
    errors: list[str] = []
    if not MODULE_RE.fullmatch(module):
        errors.append("module 格式非法")
    elif any(segment in {"", ".", ".."} for segment in module.strip(":").split(":")):
        errors.append("module 不能包含空段或路径跳转")
    if not VARIANT_RE.fullmatch(variant):
        errors.append("variant 格式非法")
    if not features:
        errors.append("features 不能为空")
    invalid_features = [feature for feature in features if not FEATURE_RE.fullmatch(feature)]
    if invalid_features:
        errors.append("feature 必须使用 kebab-case：" + ", ".join(invalid_features))
    unknown = unknown_features(features)
    if unknown:
        errors.append("未知 feature：" + ", ".join(unknown))
    return errors


def expected_apk_asset_entries(
    inventory: dict[str, Any], module: str
) -> list[str]:
    module_prefix = module.strip(":").replace(":", "/")
    pattern = re.compile(
        rf"^{re.escape(module_prefix)}/src/[^/]+/assets/(.+)$",
        re.IGNORECASE,
    )
    expected: set[str] = set()
    for resolution in inventory.get("prerequisite_gate", {}).get(
        "resolved_asset_map", {}
    ).values():
        for item in resolution.get("items", []):
            asset = item.get("asset")
            if not isinstance(asset, str):
                continue
            match = pattern.match(asset.replace("\\", "/"))
            if match:
                expected.add("assets/" + match.group(1))
    return sorted(expected)


def apk_report(
    apk: Path,
    root: Path,
    expected_assets: list[str] | None = None,
) -> dict[str, Any]:
    with zipfile.ZipFile(apk) as archive:
        names = archive.namelist()
    expected_assets = sorted(set(expected_assets or []))
    name_set = set(names)
    abis = sorted({match.group(1) for name in names if (match := re.match(r"lib/([^/]+)/.+\.so$", name))})
    streaming_entries = [name for name in names if "nvandroidstreamingsdk" in name.lower()]
    native_core = [name for name in names if name.lower().endswith("libnvstreamingsdkcore.so")]
    digest = hashlib.sha256()
    with apk.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": apk.relative_to(root).as_posix(),
        "sha256": digest.hexdigest(),
        "size_bytes": apk.stat().st_size,
        "abis": abis,
        "streaming_sdk_entries": streaming_entries,
        "native_core_entries": native_core,
        "expected_asset_entries": expected_assets,
        "missing_asset_entries": [
            name for name in expected_assets if name not in name_set
        ],
    }


def connected_devices(adb: str, root: Path) -> tuple[list[str], dict[str, Any]]:
    result = run([adb, "devices"], root, 30)
    devices = []
    if result["exit_code"] == 0:
        for line in result["output_tail"].splitlines():
            columns = line.split()
            if len(columns) == 2 and columns[1] == "device":
                devices.append(columns[0])
        for serial in devices:
            result["output_tail"] = result["output_tail"].replace(serial, "<device>")
    return devices, result


def install_apk(
    adb: str, serial: str, apk: Path, root: Path
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Install normally, retrying only the explicit Android testOnly rejection."""
    steps: list[dict[str, Any]] = []
    result = run(
        [adb, "-s", serial, "install", "-r", str(apk)], root, 180
    )
    steps.append({"name": "adb-install", **result})
    if (
        result["exit_code"] != 0
        and "INSTALL_FAILED_TEST_ONLY" in result["output_tail"]
    ):
        result = run(
            [adb, "-s", serial, "install", "-r", "-t", str(apk)],
            root,
            180,
        )
        steps.append({"name": "adb-install-test-only", **result})
    return steps, result


def resolve_launcher(adb: str, serial: str, app_id: str, root: Path) -> tuple[str | None, dict[str, Any]]:
    result = run(
        [adb, "-s", serial, "shell", "cmd", "package", "resolve-activity", "--brief", app_id],
        root,
        30,
    )
    component = None
    if result["exit_code"] == 0:
        for line in reversed(result["output_tail"].splitlines()):
            candidate = line.strip()
            if re.fullmatch(r"[A-Za-z0-9_.]+/[A-Za-z0-9_.$]+", candidate):
                component = candidate
                break
    return component, result


def resolve_process_ids(
    adb: str, serial: str, app_id: str, root: Path
) -> tuple[set[str], list[dict[str, Any]]]:
    """Resolve the main or a manifest-declared ``android:process`` process."""
    steps: list[dict[str, Any]] = []
    pidof = run([adb, "-s", serial, "shell", "pidof", app_id], root, 30)
    steps.append({"name": "pidof", **pidof})
    process_ids = {token for token in pidof["output_tail"].split() if token.isdigit()}
    if process_ids:
        return process_ids, steps

    process_list = run([adb, "-s", serial, "shell", "ps", "-A"], root, 30, 100000)
    steps.append({"name": "process-list-fallback", **process_list})
    if process_list["exit_code"] != 0:
        return set(), steps
    for line in process_list["output_tail"].splitlines():
        columns = line.split()
        if len(columns) < 2:
            continue
        process_name = columns[-1]
        if process_name != app_id and not process_name.startswith(app_id + ":"):
            continue
        # Android ps formats vary, but PID is the first numeric column after USER.
        for column in columns[1:-1]:
            if column.isdigit():
                process_ids.add(column)
                break
    return process_ids, steps


def logcat_since_epoch(result: dict[str, Any], epoch_seconds: int) -> dict[str, Any]:
    filtered: list[str] = []
    for line in result["output_tail"].splitlines():
        match = re.match(r"\s*(\d+)(?:\.\d+)?\s", line)
        if match and int(match.group(1)) >= epoch_seconds:
            filtered.append(line)
    return {**result, "output_tail": "\n".join(filtered)}


def relevant_logcat(result: dict[str, Any], app_id: str, process_ids: set[str] | None = None) -> dict[str, Any]:
    identity_pattern = re.compile(rf"(?i){re.escape(app_id)}|nveeffectkit|nvstreaming")
    failure_pattern = re.compile(
        r"(?i)FATAL EXCEPTION|ANR in |Fatal signal|tombstone written|native crash|UnsatisfiedLinkError"
    )
    process_ids = process_ids or set()
    filtered: list[str] = []
    for line in result["output_tail"].splitlines():
        pid_match = re.match(r"\s*\d+(?:\.\d+)?\s+\d+\s+(\d+)\s+", line)
        belongs_to_process = bool(pid_match and pid_match.group(1) in process_ids)
        if identity_pattern.search(line) or (belongs_to_process and failure_pattern.search(line)):
            filtered.append(line)
    return {
        **result,
        "output_tail": "\n".join(filtered),
    }


def device_smoke_gate(adb: str, serial: str, app_id: str, root: Path) -> tuple[list[dict[str, Any]], list[str], dict[str, str]]:
    steps: list[dict[str, Any]] = []
    blockers: list[str] = []
    process_ids: set[str] = set()
    marker = run([adb, "-s", serial, "shell", "date", "+%s"], root, 30)
    steps.append({"name": "device-log-marker", **marker})
    try:
        marker_epoch = int(marker["output_tail"].strip())
    except (TypeError, ValueError):
        marker_epoch = 0
        blockers.append("无法取得设备日志起始时间")

    component, resolved = resolve_launcher(adb, serial, app_id, root)
    steps.append({"name": "resolve-launcher", **resolved})
    if not component:
        return steps, ["无法解析可启动 Activity"], {}

    for index in range(3):
        force_stop = run([adb, "-s", serial, "shell", "am", "force-stop", app_id], root, 30)
        steps.append({"name": f"force-stop-{index + 1}", **force_stop})
        launch = run([adb, "-s", serial, "shell", "am", "start", "-W", "-n", component], root, 60)
        steps.append({"name": f"cold-launch-{index + 1}", **launch})
        if launch["exit_code"] != 0 or "Status: ok" not in launch["output_tail"]:
            blockers.append(f"第 {index + 1} 次冷启动失败")
            continue
        # Cover asynchronous model/resource preparation that may crash after Activity launch.
        time.sleep(3)
        launch_process_ids, process_steps = resolve_process_ids(adb, serial, app_id, root)
        for process_step in process_steps:
            steps.append({**process_step, "name": f"process-{index + 1}-{process_step['name']}"})
        process_ids.update(launch_process_ids)
        activity = run([adb, "-s", serial, "shell", "dumpsys", "activity", "activities"], root, 60, 50000)
        steps.append({"name": f"foreground-{index + 1}", **activity})
        resumed_lines = [
            line
            for line in activity["output_tail"].splitlines()
            if any(token in line for token in ("mResumedActivity", "topResumedActivity", "ResumedActivity"))
        ]
        if not launch_process_ids:
            blockers.append(f"第 {index + 1} 次冷启动后进程不存在")
        if activity["exit_code"] != 0 or not any(app_id in line for line in resumed_lines):
            blockers.append(f"第 {index + 1} 次冷启动后应用不在前台")
        home = run([adb, "-s", serial, "shell", "input", "keyevent", "3"], root, 30)
        steps.append({"name": f"home-{index + 1}", **home})

    if not blockers:
        resume = run([adb, "-s", serial, "shell", "am", "start", "-W", "-n", component], root, 60)
        steps.append({"name": "resume-after-home", **resume})
        if resume["exit_code"] != 0 or "Status: ok" not in resume["output_tail"]:
            blockers.append("后台恢复失败")
        else:
            time.sleep(3)

    logcat = run(
        [adb, "-s", serial, "logcat", "-d", "-v", "epoch"],
        root,
        60,
        200000,
    )
    if marker_epoch:
        logcat = logcat_since_epoch(logcat, marker_epoch)
    logcat = relevant_logcat(logcat, app_id, process_ids)
    steps.append({"name": "logcat-since-start", **logcat})
    crash_buffer = run(
        [adb, "-s", serial, "logcat", "-b", "crash", "-d", "-v", "epoch"],
        root,
        60,
        200000,
    )
    if marker_epoch:
        crash_buffer = logcat_since_epoch(crash_buffer, marker_epoch)
    crash_buffer = relevant_logcat(crash_buffer, app_id, process_ids)
    steps.append({"name": "logcat-crash-buffer", **crash_buffer})
    if logcat["exit_code"] != 0 or crash_buffer["exit_code"] != 0:
        blockers.append("设备日志采集失败")
    crash_text = logcat["output_tail"] + "\n" + crash_buffer["output_tail"]
    crashes = [pattern for pattern in CRASH_PATTERNS if pattern in crash_text]
    if crashes:
        blockers.append("发现崩溃/ANR 日志：" + ", ".join(crashes))
    nve_error_lines = [
        line
        for line in logcat["output_tail"].splitlines()
        if re.search(r"(?i)(nveeffectkit|nvstreaming).*(?:\sE\s|error|invalid)", line)
    ]
    if len(nve_error_lines) >= 10:
        blockers.append(f"发现持续 Nve 渲染错误：{len(nve_error_lines)} 条")

    device = {
        "manufacturer": run([adb, "-s", serial, "shell", "getprop", "ro.product.manufacturer"], root, 30)["output_tail"].strip(),
        "model": run([adb, "-s", serial, "shell", "getprop", "ro.product.model"], root, 30)["output_tail"].strip(),
        "android": run([adb, "-s", serial, "shell", "getprop", "ro.build.version.release"], root, 30)["output_tail"].strip(),
    }
    return steps, blockers, device


def validate_manual_evidence(
    path: Path | None,
    features: list[str],
    apk_sha256: str | None,
    profile: str = "camera2-oes-nv21",
) -> tuple[bool, dict[str, Any] | None, list[str]]:
    feature_report = contract_report(features)
    unresolved = feature_report.get("scope_selection_required", [])
    if unresolved:
        return False, None, [
            "发布证据必须使用用户实际选择的具体调节项，不能使用分类/套件："
            + ", ".join(unresolved)
        ]
    features = implementation_features(features)
    nve_features = feature_report.get("nve_capabilities", [])
    recording_checks = feature_report.get("manual_evidence", {}).get(
        "recording_checks", []
    )
    if path is None:
        return False, None, ["未提供人工/自动化功能证据 JSON"]
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return False, None, [f"证据 JSON 无法读取：{error}"]
    missing: list[str] = []
    if not apk_sha256 or evidence.get("apk_sha256") != apk_sha256:
        missing.append("apk_sha256 与本次构建不一致")
    device_matrix = evidence.get("device_matrix")
    if not isinstance(device_matrix, list) or not device_matrix or not all(isinstance(item, str) and item.strip() for item in device_matrix):
        missing.append("device_matrix 必须是非空字符串数组")
    covered_value = evidence.get("features")
    covered = set(covered_value) if isinstance(covered_value, list) and all(isinstance(item, str) for item in covered_value) else set()
    missing.extend(f"feature:{name}" for name in features if name not in covered)
    intensity_features = [
        name for name in features if FEATURE_GROUPS.get(name, {}).get("intensity")
    ]
    effect_strengths = evidence.get("effect_strengths")
    if intensity_features and effect_strengths is not True and not (
        isinstance(effect_strengths, dict)
        and all(effect_strengths.get(name) is True for name in intensity_features)
    ):
        missing.append("effect_strengths 必须覆盖全部强度型能力")
    feature_checks = evidence.get("feature_checks")
    for name in nve_features:
        checks = feature_checks.get(name) if isinstance(feature_checks, dict) else None
        if not isinstance(checks, dict) or any(checks.get(key) is not True for key in ("visible", "switch_remove_restore", "failure_fallback")):
            missing.append(f"feature_checks:{name}")
    required_checks: list[str] = []
    if nve_features:
        required_checks.extend(
            [
                "failure_fallback",
                "gl_context_recreated",
                "output_texture_types_verified",
                "output_texture_dimensions_verified",
                "recycle_stress_verified",
                "no_black_frames",
                "no_flash",
                "no_geometry_jump",
                "raw_effect_visual_contract_verified",
                "logcat_clean",
            ]
        )
    if nve_features and profile in {"camera2-oes-nv21", "camerax-buffer"}:
        required_checks.extend(
            (
                "four_display_rotations",
                "front_back_camera",
                "no_raw_effect_alternation",
            )
        )
    for key in required_checks:
        if evidence.get(key) is not True:
            missing.append(f"{key}!=true")
    for key in recording_checks:
        if evidence.get(key) is not True:
            missing.append(f"{key}!=true")
    interactions = evidence.get("interaction_repetitions")
    if nve_features and (
        isinstance(interactions, bool)
        or not isinstance(interactions, int)
        or interactions < 10
    ):
        missing.append("interaction_repetitions<10")
    lifecycle = evidence.get("lifecycle_repetitions")
    if (nve_features or recording_checks) and (
        isinstance(lifecycle, bool)
        or not isinstance(lifecycle, int)
        or lifecycle < 10
    ):
        missing.append("lifecycle_repetitions<10")
    performance = evidence.get("performance")
    required_performance = ("fps", "frame_time_ms", "memory_mb", "temperature_note")
    if nve_features and (
        not isinstance(performance, dict)
        or any(key not in performance for key in required_performance)
    ):
        missing.append("performance 缺少实测 fps/frame_time_ms/memory_mb/temperature_note")
    elif nve_features and performance.get("customer_target_met") not in (True, None):
        missing.append("customer_target_met!=true")
    return not missing, evidence, missing


def apk_contract_blockers(
    apk: dict[str, Any], requires_nve_prerequisites: bool
) -> list[str]:
    blockers: list[str] = []
    if requires_nve_prerequisites and not apk.get("native_core_entries"):
        blockers.append("APK 未包含 libNvStreamingSdkCore.so")
    missing_assets = apk.get("missing_asset_entries", [])
    if missing_assets:
        blockers.append("APK 缺少已映射素材：" + ", ".join(missing_assets))
    return blockers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NveEffectKit delivery gate")
    parser.add_argument("project_root")
    parser.add_argument("--module", required=True)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--features", required=True, help="comma-separated requested features")
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument(
        "--resource-path",
        action="append",
        default=[],
        help="user-authorized resource file or directory; repeatable",
    )
    parser.add_argument(
        "--asset-map",
        default=None,
        help="path to the same JSON asset map used by the prerequisite gate",
    )
    parser.add_argument("--include", action="append", default=[], help="project-relative UI/source file to include; repeatable")
    parser.add_argument("--device", default=None)
    parser.add_argument("--evidence", type=Path, default=None, help="optional manual evidence record; never auto-approves release")
    parser.add_argument(
        "--continue-on-failure",
        action="store_true",
        help="run later Gradle checks after an earlier failure; default is fail-fast",
    )
    args = parser.parse_args(argv)

    root = Path(args.project_root).expanduser().resolve()
    if not root.is_dir():
        print(json.dumps({"status": "BLOCKED", "report": None, "blockers": ["project root 不存在"]}, ensure_ascii=False, indent=2))
        return 2

    raw_features = [item.strip() for item in args.features.split(",") if item.strip()]
    requested_features = normalize_requested_features(raw_features)
    features = implementation_features(raw_features)
    resource_paths: list[Path] = []
    for raw_path in args.resource_path:
        resource_path = Path(raw_path).expanduser().resolve()
        if not resource_path.exists():
            print(json.dumps(
                {
                    "status": "BLOCKED",
                    "report": None,
                    "blockers": [f"授权资源路径不存在：{raw_path}"],
                },
                ensure_ascii=False,
                indent=2,
            ))
            return 2
        resource_paths.append(resource_path)
    asset_map: dict[str, Any] = {}
    if args.asset_map:
        try:
            asset_map = load_asset_map(Path(args.asset_map).expanduser().resolve())
        except ValueError as error:
            print(json.dumps(
                {
                    "status": "BLOCKED",
                    "report": None,
                    "blockers": [str(error)],
                },
                ensure_ascii=False,
                indent=2,
            ))
            return 2
    report_dir = root / "build" / "nve-verification"
    report_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "schema_version": 6,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project": root.name,
        "module": args.module,
        "variant": args.variant,
        "requested_features": requested_features,
        "features": features,
        "feature_contract": contract_report(raw_features),
        "profile": args.profile,
        "status": "BLOCKED",
        "steps": [],
        "blockers": validate_cli_values(args.module, args.variant, raw_features),
    }
    requires_nve_prerequisites = report["feature_contract"].get(
        "requires_nve_prerequisites", True
    )
    unresolved_scope = report["feature_contract"].get(
        "scope_selection_required", []
    )
    unresolved_scope = [
        feature
        for feature in unresolved_scope
        if not (
            feature in report["feature_contract"].get(
                "required_asset_contracts", {}
            )
            and feature in asset_map
        )
    ]
    if unresolved_scope:
        report["blockers"].append(
            "功能范围未解析为具体调节项："
            + ", ".join(unresolved_scope)
        )

    if not report["blockers"]:
        project_files = list(iter_files(root))
        inventory = inspect_project(
            root,
            args.variant,
            raw_features,
            project_files,
            resource_paths=resource_paths,
            asset_map=asset_map,
        )
        documents = source_documents(root, project_files)
        report["inventory"] = inventory
        findings = validate(
            root,
            args.profile,
            strict=True,
            features=raw_features,
            includes=args.include,
            inventory=inventory,
            documents=documents,
            project_files=project_files,
        )
        report["analyzed_files"] = sorted(
            detected_analysis_files(inventory) | set(args.include)
        )
        report["unresolved_ui_files"] = sorted(
            {
                location
                for item in findings
                if item.code == "RUNTIME_UNRESOLVED_UI_FILES"
                for location in item.locations
            }
        )
        report["static_findings"] = [asdict(item) for item in findings]
        static_errors = [item.code for item in findings if item.severity == "ERROR"]
        if static_errors:
            report["blockers"].append("严格静态校验失败：" + ", ".join(static_errors))

        wrapper = gradle_wrapper(root)
        if wrapper is None:
            report["blockers"].append("未发现 Gradle wrapper")
        elif not report["blockers"]:
            variant_cap = capitalized_variant(args.variant)
            build = run(
                wrapper
                + [
                    task(args.module, f"test{variant_cap}UnitTest"),
                    task(args.module, f"assemble{variant_cap}"),
                    "--no-daemon",
                ],
                root,
                1200,
            )
            report["steps"].append({"name": "unit-tests-and-assemble", **build})
            if build["exit_code"] != 0:
                report["blockers"].append("相关单元测试或 Gradle 构建失败")

            if not report["blockers"] or args.continue_on_failure:
                dependencies = run(
                    wrapper + [task(args.module, "dependencies"), "--configuration", f"{args.variant}RuntimeClasspath", "--no-daemon"],
                    root,
                    600,
                )
                report["steps"].append({"name": "dependencies", **dependencies})
                if dependencies["exit_code"] != 0:
                    report["blockers"].append("依赖树检查失败")

            if not report["blockers"]:
                module_dir = root.joinpath(*args.module.strip(":").split(":"))
                resolved_id = merged_manifest_application_id(module_dir, args.variant)
                if resolved_id:
                    for module in report["inventory"]["modules"]:
                        if module["name"].strip(":") == args.module.strip(":"):
                            module["resolved_application_id"] = resolved_id
                            module["variant_resolution"] = "resolved-from-merged-manifest"

        apks = locate_apks(root, args.module, args.variant)
        expected_assets = expected_apk_asset_entries(
            report.get("inventory", {}), args.module
        )
        report["apks"] = [
            apk_report(apk, root, expected_assets) for apk in apks
        ]
        if not apks and not report["blockers"]:
            report["blockers"].append("未找到构建 APK")
        if len(apks) > 1:
            report["blockers"].append("发现多个 APK；当前门禁不猜测 split/universal 产物，需明确提供单一可安装 APK")
        for apk in report["apks"]:
            report["blockers"].extend(
                apk_contract_blockers(apk, requires_nve_prerequisites)
            )

        if not report["blockers"]:
            report["status"] = "STATIC_READY"

        adb = shutil.which("adb")
        selected_device = args.device
        if selected_device and not adb:
            report["blockers"].append("已指定 --device，但未找到 adb")
        if report["status"] == "STATIC_READY" and selected_device and adb:
            devices, device_list = connected_devices(adb, root)
            report["steps"].append({"name": "adb-devices", **device_list})
            if selected_device not in devices:
                report["blockers"].append("指定设备不可用")

        if report["status"] == "STATIC_READY" and not report["blockers"] and selected_device and adb and len(apks) == 1:
            app_modules = [m for m in report["inventory"]["modules"] if m["plugin"] == "application" and m["name"].strip(":") == args.module.strip(":")]
            app_id = app_modules[0]["resolved_application_id"] if app_modules else None
            device_abi = run([adb, "-s", selected_device, "shell", "getprop", "ro.product.cpu.abi"], root, 30)
            report["steps"].append({"name": "device-abi", **device_abi})
            abi_value = device_abi["output_tail"].strip()
            device_abi_list = run([adb, "-s", selected_device, "shell", "getprop", "ro.product.cpu.abilist"], root, 30)
            report["steps"].append({"name": "device-abi-list", **device_abi_list})
            supported_device_abis = {
                abi.strip()
                for abi in device_abi_list["output_tail"].split(",")
                if abi.strip()
            } or ({abi_value} if abi_value else set())
            packaged_abis = set(report["apks"][0]["abis"])
            if device_abi["exit_code"] != 0 or (
                packaged_abis
                and not (packaged_abis & supported_device_abis)
            ):
                report["blockers"].append(
                    f"设备支持 ABI {sorted(supported_device_abis) or ['unknown']} 与 APK ABI {sorted(packaged_abis)} 无交集"
                )
            if not app_id:
                report["blockers"].append("无法确定最终 applicationId，不能自动启动")
            if not report["blockers"]:
                install_steps, install = install_apk(
                    adb, selected_device, apks[0], root
                )
                report["steps"].extend(install_steps)
                if install["exit_code"] != 0 or "Success" not in install["output_tail"]:
                    report["blockers"].append("ADB 安装失败")
                else:
                    smoke_steps, smoke_blockers, device = device_smoke_gate(adb, selected_device, app_id, root)
                    report["steps"].extend(smoke_steps)
                    report["blockers"].extend(smoke_blockers)
                    if not smoke_blockers:
                        device["abi"] = abi_value
                        report["device"] = device
                        report["status"] = "DEVICE_VERIFIED"

        expected_hash = report["apks"][0]["sha256"] if len(report.get("apks", [])) == 1 else None
        evidence_complete, evidence, missing = validate_manual_evidence(
            args.evidence, features, expected_hash, args.profile
        )
        report["release_evidence_supplied"] = evidence is not None
        report["release_evidence_missing"] = missing
        report["release_evidence_status"] = (
            "RECORDED_NOT_AUTO_APPROVED"
            if evidence_complete
            else "INCOMPLETE_OR_NOT_SUPPLIED"
        )

        if report["blockers"]:
            report["status"] = "BLOCKED"

    report = sanitize_report(report, root, args.device)
    report_path = report_dir / f"{args.module.strip(':').replace(':', '-')}-{args.variant}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(report_path), "blockers": report["blockers"]}, ensure_ascii=False, indent=2))
    return 1 if report["status"] == "BLOCKED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
