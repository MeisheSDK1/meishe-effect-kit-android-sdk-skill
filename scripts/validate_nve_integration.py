#!/usr/bin/env python3
"""Static validation for an NveEffectKit integration in an Android project."""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from feature_contract import (
    BEAUTY_SUITE,
    FEATURE_GROUPS,
    beauty_controller_groups,
    contract_report,
    expand_features,
    implementation_features,
    normalize_requested_features,
    unknown_features,
)
from inspect_android_project import (
    inspect_project,
    iter_files,
    load_asset_map,
    rel,
    safe_read,
)


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    message: str
    locations: tuple[str, ...] = ()


def dependency_declaration_locations(
    documents: dict[str, str], group: str, artifact: str
) -> tuple[str, ...]:
    coordinate = f"{group}:{artifact}".lower()
    catalog_pair = f'group="{group}",name="{artifact}"'.lower()
    result = []
    for path, text in documents.items():
        if not path.lower().endswith(
            ("build.gradle", "build.gradle.kts", "libs.versions.toml")
        ):
            continue
        compact = re.sub(r"\s+", "", text).lower()
        if coordinate in compact or catalog_pair in compact:
            result.append(path)
    return tuple(sorted(result))


def archive_contains_class(path: Path, class_entry: str) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            if class_entry in archive.namelist():
                return True
            if path.suffix.lower() != ".aar":
                return False
            for name in archive.namelist():
                if not name.lower().endswith(".jar"):
                    continue
                with zipfile.ZipFile(io.BytesIO(archive.read(name))) as nested:
                    if class_entry in nested.namelist():
                        return True
    except (OSError, KeyError, zipfile.BadZipFile):
        return False
    return False


def bundled_class_locations(
    root: Path, files: Iterable[Path], class_entry: str
) -> tuple[str, ...]:
    return tuple(
        sorted(
            rel(path, root)
            for path in files
            if path.suffix.lower() in {".aar", ".jar"}
            and archive_contains_class(path, class_entry)
        )
    )


def source_documents(
    root: Path, files: Iterable[Path] | None = None
) -> dict[str, str]:
    documents: dict[str, str] = {}
    for path in files if files is not None else iter_files(root):
        suffix = path.suffix.lower()
        if suffix not in {
            ".java", ".kt", ".gradle", ".kts", ".toml", ".xml", ".json"
        }:
            continue
        if suffix == ".json":
            relative = rel(path, root).lower()
            is_scoped_nve_metadata = (
                "/assets/" in f"/{relative}"
                and any(
                    token in relative
                    for token in (
                        "makeup",
                        "compose",
                        "effect",
                        "filter",
                        "sticker",
                        "prop",
                        "beauty",
                    )
                )
            )
            if not is_scoped_nve_metadata:
                continue
            try:
                text = (
                    path.read_text(encoding="utf-8", errors="ignore")
                    if path.stat().st_size <= 2 * 1024 * 1024
                    else ""
                )
            except OSError:
                text = ""
        else:
            text = safe_read(path)
        if text:
            documents[rel(path, root)] = text
    return documents


def code_only(text: str) -> str:
    """Blank comments and literals while preserving offsets and newlines."""
    result = list(text)
    index = 0
    state = "code"
    delimiter = ""
    while index < len(text):
        current = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""

        if state == "code":
            if current == "/" and following == "/":
                result[index] = result[index + 1] = " "
                index += 2
                state = "line-comment"
                continue
            if current == "/" and following == "*":
                result[index] = result[index + 1] = " "
                index += 2
                state = "block-comment"
                continue
            if text.startswith('"""', index):
                result[index : index + 3] = [" ", " ", " "]
                index += 3
                state = "literal"
                delimiter = '"""'
                continue
            if current in {'"', "'"}:
                result[index] = " "
                index += 1
                state = "literal"
                delimiter = current
                continue
            index += 1
            continue

        if state == "line-comment":
            if current == "\n":
                state = "code"
            else:
                result[index] = " "
            index += 1
            continue

        if state == "block-comment":
            if current == "*" and following == "/":
                result[index] = result[index + 1] = " "
                index += 2
                state = "code"
            else:
                if current != "\n":
                    result[index] = " "
                index += 1
            continue

        if delimiter == '"""' and text.startswith(delimiter, index):
            result[index : index + 3] = [" ", " ", " "]
            index += 3
            state = "code"
            delimiter = ""
            continue
        if delimiter != '"""' and current == "\\":
            result[index] = " "
            if index + 1 < len(text):
                if text[index + 1] != "\n":
                    result[index + 1] = " "
                index += 2
            else:
                index += 1
            continue
        if current == delimiter:
            result[index] = " "
            index += 1
            state = "code"
            delimiter = ""
            continue
        if current != "\n":
            result[index] = " "
        index += 1

    return "".join(result)


def locations_with(documents: dict[str, str], token: str) -> tuple[str, ...]:
    return tuple(sorted(path for path, text in documents.items() if token in text))


def any_pattern(documents: dict[str, str], pattern: str) -> tuple[str, ...]:
    regex = re.compile(pattern, re.MULTILINE)
    return tuple(sorted(path for path, text in documents.items() if regex.search(text)))


def detected_analysis_files(report: dict[str, Any]) -> set[str]:
    """Nve source plus detected frame/render adapter entry points."""
    detected = set(report.get("nve_source_files", ())) | {
        item["path"]
        for item in report.get("candidate_integration_points", ())
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    responsibilities = report.get("responsibility_map", {})
    for key in (
        "camera-callback-thread",
        "yuv-stride-conversion",
        "timestamp-pairing",
        "direction-and-mirror",
        "bounded-latest-frame",
    ):
        detected.update(
            path for path in responsibilities.get(key, ()) if isinstance(path, str)
        )
    return detected


PROFILES = ("camera2-oes-nv21", "camerax-buffer", "custom-gl")
UUID_LITERAL_RE = re.compile(
    r"[\"']([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})[\"']"
)
ZERO_UUID = "00000000-0000-0000-0000-000000000000"


def has_any(documents: dict[str, str], *tokens: str) -> bool:
    return any(any(token in text for token in tokens) for text in documents.values())


def add_recording_findings(
    findings: list[Finding],
    documents: dict[str, str],
    strict: bool,
) -> None:
    """Validate the host-owned recording path without requiring NVE artifacts."""
    severity = "ERROR" if strict else "WARNING"
    recording_markers = (
        "MediaRecorder",
        "eglPresentationTimeANDROID",
        "MediaStore.Video",
        "startRecording",
        "stopRecording",
        "NveFinalTextureFrameSink",
    )
    recording_documents = {
        path: text
        for path, text in documents.items()
        if any(token in text for token in recording_markers)
    }
    source_locations = tuple(sorted(recording_documents))
    checks = (
        (
            "RECORDING_SURFACE_VIDEO_SOURCE_MISSING",
            ("MediaRecorder.VideoSource.SURFACE",),
            "录制必须从共享 GL 编码 Surface 接收处理后纹理。",
        ),
        (
            "RECORDING_PRESENTATION_TIME_MISSING",
            ("eglPresentationTimeANDROID",),
            "编码 EGLSurface 必须逐帧提交单调递增的 presentation timestamp。",
        ),
        (
            "RECORDING_MEDIASTORE_OUTPUT_MISSING",
            ("MediaStore.Video.Media", "RELATIVE_PATH", "DIRECTORY_DCIM"),
            "视频必须直接写入 MediaStore 的 DCIM/Camera，而不是只留在应用私有目录。",
        ),
        (
            "RECORDING_ATOMIC_PUBLISH_MISSING",
            ("IS_PENDING", "getFileDescriptor"),
            "Android 10+ 必须通过 pending MediaStore 项和文件描述符原子发布。",
        ),
        (
            "RECORDING_STATE_MACHINE_MISSING",
            ("STARTING", "RECORDING", "STOPPING", "IDLE"),
            "录制入口必须由 IDLE/STARTING/RECORDING/STOPPING 状态机约束。",
        ),
        (
            "RECORDING_LIFECYCLE_STOP_MISSING",
            ("onPause", "stopRecording"),
            "进入后台时必须先停止并保存录制，再释放相机、GL 和 EffectKit。",
        ),
    )
    combined = "\n".join(recording_documents.values())
    for code, tokens, message in checks:
        if not all(token in combined for token in tokens):
            findings.append(Finding(severity, code, message, source_locations))

    has_timestamp_rebase = (
        "System.nanoTime()" in combined
        and re.search(
            r"\bfirst(?:Camera|Source)\w*Timestamp\w*\b",
            combined,
            re.IGNORECASE,
        )
        is not None
        and re.search(
            r"\b(?:source|camera)\w*Delta\w*\b|\b\w*Timestamp\w*\s*-\s*first(?:Camera|Source)",
            combined,
            re.IGNORECASE,
        )
        is not None
        and "eglPresentationTimeANDROID" in combined
    )
    if not has_timestamp_rebase:
        findings.append(
            Finding(
                severity,
                "RECORDING_TIMESTAMP_REBASE_MISSING",
                "相机时间戳必须以 MediaRecorder.start() 后的 System.nanoTime() 为锚点重基。",
                source_locations,
            )
        )

    has_dedicated_worker = any(
        token in combined
        for token in ("HandlerThread", "ExecutorService", "new Thread(", "Thread(")
    )
    has_bounded_nonblocking_queue = (
        (
            "ArrayBlockingQueue" in combined
            or re.search(r"(?:FRAME_SLOT_COUNT|SLOT_COUNT)\s*=\s*3\b", combined)
        )
        and any(token in combined for token in (".offer(", "drop", "Drop"))
    )
    if not has_dedicated_worker:
        findings.append(
            Finding(
                severity,
                "RECORDING_ENCODER_WORKER_MISSING",
                "编码 EGL、swap 和停止收尾必须在专用工作线程执行，不能阻塞预览 GL/UI 线程。",
                source_locations,
            )
        )
    if not has_bounded_nonblocking_queue:
        findings.append(
            Finding(
                severity,
                "RECORDING_BOUNDED_QUEUE_MISSING",
                "录制帧必须使用有界非阻塞队列或三个纹理槽；满载时丢弃新录制帧，不能阻塞预览。",
                source_locations,
            )
        )

    ui_tokens = (
        "GradientDrawable.OVAL",
        "录制视频",
        "停止录制",
        "录制完成",
    )
    missing_ui = [token for token in ui_tokens if token not in combined]
    if missing_ui:
        findings.append(
            Finding(
                "WARNING",
                "RECORDING_UI_CONTRACT_UNVERIFIED",
                "未能静态确认正圆入口、可访问描述和完成提示：" + ", ".join(missing_ui),
                source_locations,
            )
        )
    if "● 录制中" in combined:
        findings.append(
            Finding(
                "ERROR",
                "RECORDING_PERSISTENT_STATUS_FORBIDDEN",
                "录制中不得显示持久“● 录制中”文字；使用按钮状态和瞬时提示。",
                source_locations,
            )
        )
    second_render_locations = tuple(
        sorted(
            path
            for path, text in recording_documents.items()
            if "renderEffect(" in text
            and (
                "implements NveFinalTextureFrameSink" in text
                or re.search(
                    r"\bclass\s+[A-Za-z0-9_$]*(?:Recorder|RecordingSink)\b",
                    text,
                )
            )
        )
    )
    if second_render_locations:
        findings.append(
            Finding(
                severity,
                "RECORDING_SECOND_EFFECT_RENDER_FORBIDDEN",
                "录制模块只能消费现有 NVE 最终输出纹理，不得再次调用 renderEffect。",
                second_render_locations,
            )
        )
    findings.extend(
        (
            Finding(
                "RUNTIME_REQUIRED",
                "RUNTIME_RECORDING_FIRST_SWAP",
                "真机确认只有首次 encoder swap 成功后才进入 RECORDING 并提示开始。",
                source_locations,
            ),
            Finding(
                "RUNTIME_REQUIRED",
                "RUNTIME_RECORDING_STOP_ORDER",
                "真机确认停止顺序为停帧→排空队列→MediaRecorder.stop→释放编码 EGL；时长/文件上限按正常完成保存。",
                source_locations,
            ),
            Finding(
                "RUNTIME_REQUIRED",
                "RUNTIME_RECORDING_GALLERY",
                "真机确认视频位于系统相册 DCIM/Camera，照片/系统播放器可直接打开且音画同步。",
                source_locations,
            ),
            Finding(
                "RUNTIME_REQUIRED",
                "RUNTIME_RECORDING_BACKGROUND",
                "真机确认录制中切后台会保存有效文件，重复前后台不崩溃、不换进程且预览可恢复。",
                source_locations,
            ),
        )
    )


def nonzero_uuid_literal_locations(documents: dict[str, str]) -> tuple[str, ...]:
    """Find hard-coded real-looking UUIDs while allowing a defensive zero sentinel."""
    locations: list[str] = []
    for path, text in documents.items():
        if any(match.group(1).lower() != ZERO_UUID for match in UUID_LITERAL_RE.finditer(text)):
            locations.append(path)
    return tuple(sorted(locations))


def has_usable_package_id_guard(text: str, variable: str = "packageId") -> bool:
    """Return whether one install result rejects empty, malformed and zero UUID values."""
    name = re.escape(variable)
    empty_guard = bool(
        re.search(
            rf"(?:TextUtils\.isEmpty|isNullOrEmpty|isNullOrBlank)\s*\(\s*{name}\b|"
            rf"\b{name}\s*==\s*null|\b{name}\.isEmpty\s*\(\s*\)",
            text,
        )
    )
    format_guard = bool(
        re.search(
            rf"(?:UUID\.fromString|UUID\.fromStringOrNull|isValidUuid)\s*\(\s*{name}\b",
            text,
        )
    )
    zero_guard = (
        bool(re.search(rf"{re.escape(ZERO_UUID)}[^;\n]*\b{name}\b|\b{name}\b[^;\n]*{re.escape(ZERO_UUID)}", text, re.IGNORECASE))
        or (
            re.search(rf"\b{name}\b[^;\n]*getMostSignificantBits", text) is not None
            and re.search(rf"\b{name}\b[^;\n]*getLeastSignificantBits", text) is not None
            and re.search(r"(?:!=|==)\s*0L?\b", text) is not None
        )
        or (
            re.search(r"UUID\s*\(\s*0L?\s*,\s*0L?\s*\)", text) is not None
            and re.search(rf"\b{name}\b", text) is not None
        )
        or (
            re.search(rf"UUID\s+\w+\s*=\s*UUID\.fromString\s*\(\s*{name}\s*\)", text)
            is not None
            and "getMostSignificantBits" in text
            and "getLeastSignificantBits" in text
            and re.search(r"(?:!=|==)\s*0L?\b", text) is not None
        )
    )
    return empty_guard and format_guard and zero_guard


def makeup_package_name_mismatches(
    root: Path, files: Iterable[Path] | None = None
) -> tuple[str, ...]:
    """Cross-check bundled single-makeup metadata UUIDs with UUID/version package names."""
    project_files = list(files) if files is not None else list(iter_files(root))
    package_paths = [path for path in project_files if path.suffix.lower() == ".makeup"]
    if not package_paths:
        return ()

    package_names = {path.name.lower() for path in package_paths}
    mismatches: set[str] = set()
    for path in project_files:
        if path.suffix.lower() != ".json":
            continue
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "makeupArgs" not in text or "makeupId" not in text:
            continue
        try:
            metadata = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(metadata, dict) or not metadata.get("makeupId"):
            continue
        effect_content = metadata.get("effectContent")
        makeup_args = effect_content.get("makeupArgs") if isinstance(effect_content, dict) else None
        if not isinstance(makeup_args, list):
            continue
        uuids = {
            str(item.get("uuid", "")).lower()
            for item in makeup_args
            if isinstance(item, dict)
            and re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", str(item.get("uuid", "")))
        }
        for package_uuid in uuids:
            expected = re.compile(rf"^{re.escape(package_uuid)}\.\d+\.makeup$", re.IGNORECASE)
            if not any(expected.fullmatch(name) for name in package_names):
                mismatches.add(rel(path, root))
                mismatches.update(rel(package_path, root) for package_path in package_paths)
    return tuple(sorted(mismatches))


def ignored_standalone_calls(documents: dict[str, str], method: str) -> tuple[str, ...]:
    """Return files containing a bare call whose result is neither stored nor tested.

    The receiver may itself be a call (for example
    ``NveEffectKit.getInstance().init(...)``), which the previous token-only
    regular expression did not recognize.
    """
    call = re.compile(rf"\b{re.escape(method)}\s*\(")
    locations: set[str] = set()
    for path, text in documents.items():
        clean = re.sub(r"(?m)//.*$", "", text)
        for match in call.finditer(clean):
            next_brace = clean.find("{", match.end())
            next_semicolon = clean.find(";", match.end())
            declaration_prefix = clean[
                max(clean.rfind(";", 0, match.start()), clean.rfind("}", 0, match.start())) + 1
                : match.start()
            ]
            if (
                "." not in declaration_prefix
                and next_brace >= 0
                and (next_semicolon < 0 or next_brace < next_semicolon)
            ):
                continue
            statement_start = max(
                clean.rfind(";", 0, match.start()),
                clean.rfind("{", 0, match.start()),
                clean.rfind("}", 0, match.start()),
            ) + 1
            statement_end = clean.find(";", match.end())
            if statement_end < 0:
                continue
            prefix = clean[statement_start:match.start()].strip()
            # Assignments and calls used as a condition/return are observable.
            if re.search(r"(?<![=!<>])=(?!=)", prefix):
                continue
            if re.search(r"\b(?:return|if|while|for|assert|switch)\b", prefix):
                continue
            # Exclude abstract/interface method declarations such as
            # ``boolean init(Context context);`` while retaining receiver-less
            # invocations such as ``init(context);``.
            if "." not in prefix and re.fullmatch(
                r"(?:public|protected|private|static|final|abstract|native|synchronized|default|\s)*"
                r"[A-Za-z_$][A-Za-z0-9_$<>\[\]?., ]*\s+",
                prefix,
            ):
                continue
            locations.add(path)
            break
    return tuple(sorted(locations))


def braced_span_after(
    text: str, start: int, search_limit: int = 500
) -> tuple[int, int] | None:
    limit = min(len(text), start + search_limit)
    brace = text.find("{", start, limit)
    if brace < 0:
        return None
    semicolon = text.find(";", start, limit)
    if semicolon >= 0 and semicolon < brace:
        return None
    depth = 0
    for index in range(brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return brace + 1, index
    return None


def braced_region_after(text: str, start: int, search_limit: int = 500) -> str | None:
    span = braced_span_after(text, start, search_limit)
    return text[span[0] : span[1]] if span else None


METHOD_RE = re.compile(
    r"(?m)(?:\bfun\s+|(?:public|protected|private|static|final|synchronized|native|\s)+"
    r"[A-Za-z_$][A-Za-z0-9_$<>?\[\]., ]*\s+)"
    r"[A-Za-z_$][A-Za-z0-9_$]*\s*\([^;{}]*\)\s*(?:throws\s+[^{}]+)?\{"
)


def method_region_containing(text: str, position: int) -> tuple[str, int] | None:
    """Return the smallest detected Java/Kotlin method body containing position."""
    candidates: list[tuple[int, int]] = []
    for match in METHOD_RE.finditer(text, 0, position + 1):
        span = braced_span_after(text, match.end() - 1, 5)
        if span and span[0] <= position <= span[1]:
            candidates.append(span)
    if not candidates:
        return None
    start, end = max(candidates, key=lambda item: item[0])
    return text[start:end], position - start


INSTALL_RE = re.compile(r"\b(?:installFxPackage|installAssetPackage|upgradeAssetPackage)\s*\(")


def is_method_declaration(text: str, call_start: int) -> bool:
    line_start = text.rfind("\n", 0, call_start) + 1
    prefix = text[line_start:call_start]
    return "." not in prefix and re.fullmatch(
        r"\s*(?:public|protected|private|static|final|abstract|native|synchronized|default|\s)*"
        r"[A-Za-z_$][A-Za-z0-9_$<>\[\]?., ]*\s+",
        prefix,
    ) is not None


def unguarded_install_locations(
    documents: dict[str, str], asset_type_tokens: Iterable[str] = ()
) -> tuple[str, ...]:
    """Check each install result in its own method; never borrow guards from another file."""
    failures: set[str] = set()
    type_tokens = tuple(asset_type_tokens)
    for path, text in documents.items():
        for call in INSTALL_RE.finditer(text):
            if is_method_declaration(text, call.start()):
                continue
            region_info = method_region_containing(text, call.start())
            region, local_pos = region_info or (text, call.start())
            if type_tokens and not any(token in region for token in type_tokens):
                continue
            line_start = region.rfind("\n", 0, local_pos) + 1
            prefix = region[line_start:local_pos]
            assignment = re.search(r"\b([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*[^=]*$", prefix)
            variable = assignment.group(1) if assignment else None
            proof = region[local_pos:]
            guarded = bool(variable and has_usable_package_id_guard(proof, variable))
            helper_call = bool(
                variable
                and re.search(
                    rf"\bisUsablePackageId\s*\(\s*{re.escape(variable)}\s*\)", proof
                )
            )
            if helper_call:
                for helper in re.finditer(r"\bisUsablePackageId\s*\([^)]*\)\s*\{", text):
                    helper_region = braced_region_after(text, helper.end() - 1, 5) or ""
                    if has_usable_package_id_guard(helper_region, "packageId"):
                        guarded = True
                        break
            if not guarded:
                failures.add(path)
    return tuple(sorted(failures))


def unchecked_install_locations(documents: dict[str, str]) -> tuple[str, ...]:
    """Find install calls whose individual return value is not stored or observed."""
    failures: set[str] = set()
    for path, text in documents.items():
        for call in INSTALL_RE.finditer(text):
            if is_method_declaration(text, call.start()):
                continue
            start = max(text.rfind(";", 0, call.start()), text.rfind("{", 0, call.start())) + 1
            prefix = text[start : call.start()]
            if re.search(r"(?<![=!<>])=(?!=)", prefix):
                continue
            if re.search(r"\b(?:return|if|while|assert)\b", prefix):
                continue
            failures.add(path)
    return tuple(sorted(failures))


BACKGROUND_INSTALL_DISPATCH_RE = re.compile(
    r"\b(?:worker|background(?:Executor)?|assetExecutor|ioExecutor|executorService)"
    r"\s*\.\s*(?:execute|submit)\s*\("
    r"|\bnew\s+Thread\s*\("
    r"|\bExecutors\.new(?:SingleThread|FixedThread|CachedThread)Executor"
    r"\s*\([^)]*\)\s*\.\s*(?:execute|submit)\s*\("
    r"|\b(?:withContext|launch|async)\s*\(\s*Dispatchers\.IO\b"
    r"|\bdoInBackground\s*\(",
    re.IGNORECASE,
)


def background_install_locations(documents: dict[str, str]) -> tuple[str, ...]:
    """Find package installation nested in an explicitly background dispatch region."""
    failures: set[str] = set()
    for path, text in documents.items():
        for dispatch in BACKGROUND_INSTALL_DISPATCH_RE.finditer(text):
            span = braced_span_after(text, dispatch.start(), 1200)
            if span is None:
                continue
            region = text[span[0] : span[1]]
            if any(
                not is_method_declaration(region, call.start())
                for call in INSTALL_RE.finditer(region)
            ):
                failures.add(path)
                break
    return tuple(sorted(failures))


def handler_regions(text: str, marker: str) -> list[str]:
    regions: list[str] = []
    for match in re.finditer(marker, text):
        region = braced_region_after(text, match.end())
        if region is not None:
            regions.append(region)
    return regions


def calls_non_declaration_method(documents: dict[str, str], method: str) -> bool:
    pattern = re.compile(rf"\b{re.escape(method)}\s*\(")
    return any(
        not is_method_declaration(text, match.start())
        for text in documents.values()
        for match in pattern.finditer(text)
    )


def region_or_called_helper_contains(
    text: str, region: str, token_pattern: str
) -> bool:
    """Check a callback body and one same-file helper hop for a forbidden token."""
    token = re.compile(token_pattern)
    if token.search(region):
        return True
    helper_names = {
        match.group(1)
        for match in re.finditer(
            r"(?<![.@])\b([A-Za-z_$][A-Za-z0-9_$]*)\s*\(",
            region,
        )
        if match.group(1)
        not in {
            "if",
            "for",
            "while",
            "switch",
            "catch",
            "return",
            "new",
            "super",
            "this",
        }
    }
    for name in helper_names:
        declaration = re.compile(
            rf"\b{re.escape(name)}\s*\([^;{{}}]*\)\s*\{{"
        )
        for match in declaration.finditer(text):
            helper = braced_region_after(text, match.end() - 1, 5)
            if helper is not None and token.search(helper):
                return True
    return False


READY_GUARD_RE = re.compile(
    r"renderCoreReady|capabilityReady|\bisReady\s*\(|\bisAvailable\s*\(|"
    r"\bcanApply[A-Za-z0-9_]*\s*\(|\bRUNNING\b|applyDesiredStateIfReady|runWhenEffectReady"
)
SDK_MUTATION_RE = re.compile(
    r"\b(?:setBeauty|setShape|setMicroShape|setMakeup|setFaceProp|addFilter|removeFilter|"
    r"setEnable|setDegreeByType|setBlurType|setBlurIntensity|setWhiteningIntensity|"
    r"setReddeningIntensity|setSharpnessIntensity|setFilterIntensity|setIntensityByType|"
    r"addEffect|insertEffect|removeEffect)\s*\("
)
STATE_HANDOFF_RE = re.compile(
    r"\b(?:desired|state|effectState|pending)[A-Za-z0-9_.]*\s*(?:=|\.)|"
    r"\b(?:apply|update|change|dispatch|post|queue)[A-Za-z0-9_]*\s*\(|"
    r"\b(?:listener|callback|delegate)\.[A-Za-z_$][A-Za-z0-9_$]*\s*\(|"
    r"\b(?:controller|effectController|renderer)\.(?:set|update|apply|post|queue)[A-Za-z0-9_$]*\s*\("
)
UI_SELECTION_STATE_RE = re.compile(
    r"\b(?:selectedByCategory|selectedFeature|selectedItem|currentFeature|currentItem|"
    r"activeFeature|activeItem)\b"
)
UI_SELECTION_EFFECT_WRITE_RE = re.compile(
    r"\b(?:onValueChanged|setDegreeByType|setShape|setMicroShape|setBeauty|"
    r"applyFeatureValue|updateEffectValue|requestRender)\s*\("
)


def add_contract_findings(
    findings: list[Finding],
    root: Path,
    documents: dict[str, str],
    nve_documents: dict[str, str],
    profile: str,
    strict: bool,
    features: Iterable[str] = (),
    baseline_match: bool = True,
    project_files: Iterable[Path] | None = None,
) -> None:
    all_text = "\n".join(documents.values())
    nve_text = "\n".join(nve_documents.values())
    code_nve_documents = {
        path: code_only(text) for path, text in nve_documents.items()
    }
    code_nve_text = "\n".join(code_nve_documents.values())
    strict_severity = "ERROR" if strict else "WARNING"

    unchecked_init = ignored_standalone_calls(nve_documents, "init")
    if unchecked_init:
        findings.append(Finding(strict_severity, "INIT_RESULT_UNCHECKED", "init 返回值未被检查；授权失败时 UI/渲染必须降级。", unchecked_init))
    unchecked_models = ignored_standalone_calls(nve_documents, "initModel")
    if unchecked_models:
        findings.append(Finding(strict_severity, "MODEL_RESULT_UNCHECKED", "发现未检查返回值的 initModel；必须逐模型记录能力。", unchecked_models))

    for path, text in code_nve_documents.items():
        if baseline_match:
            for queued in re.finditer(
                r"queueEvent\s*\((?P<body>.{0,1200}?\binitialize\s*\(\s*\))",
                text,
                re.DOTALL,
            ):
                if "runOnUiThread" not in queued.group("body"):
                    findings.append(
                        Finding(
                            strict_severity,
                            "BASELINE_INITIALIZE_ON_GL_THREAD",
                            "登记基线要求在 GL 资源就绪后由 Android 主线程调用 initialize；"
                            "不得把 initialize 直接放入 GLSurfaceView.queueEvent。",
                            (path,),
                        )
                    )
                    break

        for cleanup in re.finditer(r"\bcleanUp\s*\(\s*\)", text):
            region_info = method_region_containing(text, cleanup.start())
            region = region_info[0] if region_info else text
            cleanup_pos = region.find(cleanup.group(0))
            releases = [
                pos
                for token in (
                    "glDrawer.release(",
                    "releaseHostGl",
                    "releaseGlResource",
                    "releaseShaders",
                    "releaseFbo",
                    "glDelete",
                )
                if (pos := region.find(token)) >= 0
            ]
            if baseline_match and any(pos > cleanup_pos for pos in releases):
                findings.append(
                    Finding(
                        strict_severity,
                        "BASELINE_GL_RELEASE_AFTER_CLEANUP",
                        "登记基线的 cleanUp 可能改变当前 EGL 状态；必须先释放宿主持有的 "
                        "shader/FBO/texture，再在同一有效 context 中调用 cleanUp。",
                        (path,),
                    )
                )
                break

        for render in re.finditer(r"\brenderEffect\s*\(", text):
            region_info = method_region_containing(text, render.start())
            region = region_info[0] if region_info else text
            if not any(
                token in region
                for token in ("restoreHostGlState", "setupFrameInfo", "glViewport")
            ):
                findings.append(
                    Finding(
                        strict_severity if baseline_match else "RUNTIME_REQUIRED",
                        "HOST_GL_STATE_RESTORE_MISSING",
                        "renderEffect 所在帧方法未发现宿主 GL 状态恢复边界；"
                        "至少显式恢复 viewport/FBO/program/texture 状态后再绘制输出。",
                        (path,),
                    )
                )
                break

        beauty_pos = text.find("setBeauty(")
        for token, label in (("setShape(", "Shape"), ("setMicroShape(", "MicroShape")):
            dependent_pos = text.find(token)
            if dependent_pos >= 0 and beauty_pos >= 0 and dependent_pos < beauty_pos:
                severity = strict_severity if baseline_match else "RUNTIME_REQUIRED"
                findings.append(Finding(severity, "AR_SCENE_REGISTRATION_ORDER", f"{label} 在 Beauty 建立 AR Scene 前注册；该顺序仅对已登记 AAR 基线可静态判定。", (path,)))
            elif dependent_pos >= 0 and beauty_pos < 0:
                findings.append(Finding("RUNTIME_REQUIRED", "RUNTIME_AR_SCENE_ORDER", f"{label} 与 Beauty 分散在不同文件，静态分析无法证明 AR Scene 注册顺序。", (path,)))

    for path, text in code_nve_documents.items():
        for method in METHOD_RE.finditer(text):
            span = braced_span_after(text, method.end() - 1, 5)
            if not span:
                continue
            region = text[span[0] : span[1]]
            for controller, register_token, enable_token in (
                ("Beauty", "setBeauty(", "beauty.setEnable("),
                ("Shape", "setShape(", "shape.setEnable("),
                ("MicroShape", "setMicroShape(", "microShape.setEnable("),
            ):
                register_pos = region.find(register_token)
                enable_pos = region.find(enable_token)
                if register_pos >= 0 and 0 <= enable_pos < register_pos:
                    findings.append(
                        Finding(
                            strict_severity if baseline_match else "RUNTIME_REQUIRED",
                            "CONTROLLER_ENABLE_BEFORE_REGISTRATION",
                            f"{controller} calls setEnable before EffectKit re-registration; "
                            "cleanUp or GLContext recreation may have invalidated its effect.",
                            (path,),
                        )
                    )
                    break

    has_ui_events = has_any(documents, "setOnClickListener", "OnSeekBarChangeListener", "Slider", "SeekBar")
    ui_handler_files: set[str] = set()
    for path, text in documents.items():
        click_regions = handler_regions(text, r"setOnClickListener\s*\(")
        slider_regions = handler_regions(text, r"\bonProgressChanged\s*\(")
        regions = click_regions + slider_regions
        if regions:
            ui_handler_files.add(path)
        for region in click_regions:
            if (UI_SELECTION_STATE_RE.search(region)
                    and UI_SELECTION_EFFECT_WRITE_RE.search(region)):
                findings.append(
                    Finding(
                        strict_severity,
                        "UI_SELECTION_REAPPLIES_EFFECT_VALUE",
                        "功能项选择 handler 同时修改 selection 并重写效果值/请求渲染；"
                        "选择只能刷新 UI，SDK 写入必须由真实滑块输入、重置或恢复触发。",
                        (path,),
                    )
                )
        for region in regions:
            mutation = SDK_MUTATION_RE.search(region)
            if not mutation:
                continue
            guard = READY_GUARD_RE.search(region)
            if not guard or guard.start() > mutation.start():
                findings.append(Finding(strict_severity, "UI_SDK_CALL_UNGUARDED", "UI handler 在就绪/能力保护前直接调用 SDK 控制器。", (path,)))
    unresolved_ui = tuple(
        sorted(
            path
            for path, text in documents.items()
            if any(token in text for token in ("setOnClickListener", "OnSeekBarChangeListener", "onProgressChanged"))
            and path not in ui_handler_files
        )
    )
    if unresolved_ui:
        findings.append(Finding("RUNTIME_REQUIRED", "RUNTIME_UI_HANDLER_GUARDS", "部分 UI handler 通过对象/方法引用绑定，静态分析无法逐入口证明 readiness 与能力保护。", unresolved_ui))
    has_automation_entry = has_any(documents, "android:id=", "R.id.", "setContentDescription(")
    if has_ui_events and not has_automation_entry:
        findings.append(Finding(strict_severity, "UI_AUTOMATION_ENTRY_MISSING", "交互控件未发现稳定 resource ID/contentDescription，无法执行可靠真机回归。"))

    effective_features = expand_features(features)
    intensity_in_scope = not effective_features or any(
        FEATURE_GROUPS.get(item, {}).get("intensity") for item in effective_features
    )
    has_slider = intensity_in_scope and has_any(
        documents, "OnSeekBarChangeListener", "onProgressChanged", "Slider"
    )
    slider_callbacks = (
        [
            (path, region)
            for path, text in documents.items()
            for region in handler_regions(text, r"\bonProgressChanged\s*\(")
        ]
        if intensity_in_scope
        else []
    )
    if has_slider and not slider_callbacks:
        findings.append(Finding("RUNTIME_REQUIRED", "RUNTIME_SLIDER_HANDLER", "发现滑块绑定但无法解析强度回调，必须真机确认参数传播。"))
    for path, region in slider_callbacks:
        if region_or_called_helper_contains(
            documents[path], region, r"\brequestRender\s*\("
        ):
            findings.append(
                Finding(
                    strict_severity,
                    "SLIDER_REQUEST_RENDER_FORBIDDEN",
                    "滑块回调直接或经一层 helper 调用 requestRender；"
                    "实时预览必须只由相机新帧驱动，参数更新仅合并最新 desired value。",
                    (path,),
                )
            )
        if SDK_MUTATION_RE.search(region):
            continue
        if STATE_HANDOFF_RE.search(region):
            findings.append(Finding("RUNTIME_REQUIRED", "RUNTIME_SLIDER_PROPAGATION", "滑块回调只发现状态/异步移交，静态分析无法证明最终调用当前能力的 SDK setter。", (path,)))
        else:
            findings.append(Finding(strict_severity, "SLIDER_CALLBACK_SDK_UPDATE_MISSING", "滑块回调未发现 SDK setter 或明确的 desired-state/线程移交。", (path,)))
    value_callback_render_files: set[str] = set()
    for path, text in documents.items():
        for callback in re.finditer(
            r"\bonValueChanged\s*\([^)]*\)\s*\{", text
        ):
            region = braced_region_after(text, callback.end() - 1, 5)
            if region is not None and region_or_called_helper_contains(
                text, region, r"\brequestRender\s*\("
            ):
                value_callback_render_files.add(path)
    if intensity_in_scope and value_callback_render_files:
        findings.append(
            Finding(
                strict_severity,
                "VALUE_CALLBACK_REQUEST_RENDER_FORBIDDEN",
                "效果值回调直接或经一层 helper 调用 requestRender；"
                "滑块值只能合并，渲染必须等待下一次相机帧。",
                tuple(sorted(value_callback_render_files)),
            )
        )

    if "AdvancedBuffingSkin1" in nve_text and "BuffingSkin" not in nve_text.replace("AdvancedBuffingSkin1", ""):
        findings.append(Finding(strict_severity, "DEFAULT_SMOOTHING_IS_ADVANCED", "仅发现 AdvancedBuffingSkin1；普通磨皮默认应映射 BuffingSkin。", locations_with(nve_documents, "AdvancedBuffingSkin1")))

    install_files = any_pattern(nve_documents, r"\b(?:installFxPackage|installAssetPackage|upgradeAssetPackage)\s*\(")
    background_installs = background_install_locations(nve_documents)
    if background_installs:
        findings.append(
            Finding(
                strict_severity if baseline_match else "RUNTIME_REQUIRED",
                "BASELINE_ASSET_INSTALL_ON_BACKGROUND_THREAD",
                "登记 3.16.1 的素材文件/元数据可在 worker 准备，但 getInstance 和素材包安装 API 必须提交到 Android 主线程；不得从 Executor worker、Thread 或 Dispatchers.IO 直接安装。",
                background_installs,
            )
        )
    ignored_installs = unchecked_install_locations(nve_documents)
    if ignored_installs:
        findings.append(Finding(strict_severity, "ASSET_INSTALL_RESULT_UNCHECKED", "素材安装返回值未保存/检查，不能可靠使用安装后的 package ID。", ignored_installs))
    hardcoded_package = nonzero_uuid_literal_locations(nve_documents)
    if install_files and hardcoded_package:
        findings.append(Finding(strict_severity, "HARDCODED_EFFECT_PACKAGE_ID", "安装素材同时存在硬编码 UUID；业务必须使用安装返回 package ID。", hardcoded_package))

    makeup_requested = not effective_features or any(
        item in {"makeup-single", "makeup-compose"}
        or FEATURE_GROUPS.get(item, {}).get("parent") == "makeup-single"
        for item in effective_features
    )
    makeup_install = makeup_requested and bool(install_files) and any(
        token in nve_text
        for token in ("ASSET_PACKAGE_TYPE_MAKEUP", "NveSingleMakeUp", "addSingleMakeUp", "setMakeup(")
    )
    unguarded_installs = unguarded_install_locations(
        nve_documents, ("ASSET_PACKAGE_TYPE_MAKEUP",)
    )
    if makeup_install and unguarded_installs:
        findings.append(
            Finding(
                strict_severity,
                "MAKEUP_PACKAGE_ID_GUARD_MISSING",
                "单妆安装未证明同时拒绝空值、非法 UUID 和全零 UUID 哨兵；不能据此设置 packageID 或开放 UI。",
                unguarded_installs,
            )
        )
    naming_mismatches = (
        makeup_package_name_mismatches(root, project_files) if makeup_requested else ()
    )
    if naming_mismatches:
        findings.append(
            Finding(
                strict_severity,
                "MAKEUP_PACKAGE_NAME_MISMATCH",
                "单妆元数据 UUID 未匹配到 `<UUID>.<version>.makeup` 原始文件名；禁止用业务别名重命名二进制包。",
                naming_mismatches,
            )
        )
    if makeup_install:
        findings.append(
            Finding(
                "RUNTIME_REQUIRED",
                "RUNTIME_SINGLE_MAKEUP_VISIBLE",
                "必须真机确认每个可用单妆点击后画面明显变化、切换不叠加、移除后恢复，并核对安装日志不是全零 package ID。",
                install_files,
            )
        )

    if ".setEnable(" in code_nve_text:
        enabled_types = {
            "beauty": ("beauty.setEnable(", "setBeauty("),
            "shape": ("shape.setEnable(", "setShape("),
            "micro-shape": ("microShape.setEnable(", "setMicroShape("),
            "makeup": ("makeup.setEnable(", "setMakeup("),
            "prop": ("faceProp.setEnable(", "setFaceProp("),
        }
        for name, (enable_token, register_token) in enabled_types.items():
            if enable_token in code_nve_text and register_token not in code_nve_text:
                findings.append(Finding(strict_severity, "ENABLE_WITHOUT_REREGISTRATION", f"{name} 只调用 setEnable，未发现 EffectKit 重新注册。"))

    if any(token in code_nve_text for token in ("setBeauty(", "setShape(", "setMicroShape(", "setMakeup(", "addFilter(")) and not re.search(r"(?i)(onResume|restore|reapply)", code_nve_text):
        findings.append(Finding(strict_severity, "CONTROLLER_RESTORE_MISSING", "已注册控制器但未发现 onResume/restore/reapply 恢复路径。"))

    render_files = locations_with(code_nve_documents, "renderEffect(")
    for path in render_files:
        text = code_nve_documents[path]
        for call in re.finditer(r"\brenderEffect\s*\(", text):
            region_info = method_region_containing(text, call.start())
            region, local_pos = region_info or (text, call.start())
            containing_try: tuple[int, int] | None = None
            for try_match in re.finditer(
                r"\btry\s*(?:\([^)]*\))?\s*\{", region[: local_pos + 1]
            ):
                span = braced_span_after(region, try_match.end() - 1, 5)
                if span and span[0] <= local_pos <= span[1]:
                    containing_try = span
            if not containing_try:
                findings.append(Finding(strict_severity, "RENDER_CALL_OUTSIDE_TRY", "renderEffect 调用未被当前方法内的 try 包围。", (path,)))
                findings.append(Finding(strict_severity, "RECYCLE_NOT_IN_FINALLY", "当前 renderEffect 没有可配对的 try/finally 回收区域。", (path,)))
                if not any(token in region for token in ("drawRaw", "drawOriginal", "drawOes", "fallback", "rawFrame")):
                    findings.append(Finding(strict_severity, "RAW_FRAME_FALLBACK_MISSING", "当前 renderEffect 所在方法未发现原始帧降级。", (path,)))
                continue
            suffix = region[containing_try[1] :]
            next_try = re.search(r"\btry\b", suffix)
            finally_match = re.search(r"\bfinally\s*\{", suffix)
            finally_region = (
                braced_region_after(suffix, finally_match.end() - 1, 5)
                if finally_match and (not next_try or finally_match.start() < next_try.start())
                else None
            )
            if not finally_region or "recycleOutput(" not in finally_region:
                findings.append(Finding(strict_severity, "RECYCLE_NOT_IN_FINALLY", "当前 renderEffect 输出未被对应 finally 回收。", (path,)))
            if not any(token in region for token in ("drawRaw", "drawOriginal", "drawOes", "fallback", "rawFrame")):
                findings.append(Finding(strict_severity, "RAW_FRAME_FALLBACK_MISSING", "当前 renderEffect 所在方法未发现原始帧降级。", (path,)))

    texture_pipeline = has_any(documents, "NV_OES_TEXTURE", "GL_TEXTURE_EXTERNAL_OES", "TEXTURE_TEXTURE")
    if render_files and texture_pipeline and not (
        "getTextureType(" in all_text
        and "NV_OES_TEXTURE" in all_text
        and has_any(documents, "drawOes", "drawExternal")
        and has_any(documents, "draw2D", "drawTexture2d", "drawTexture2D", "drawCommonTexture")
    ):
        findings.append(Finding(strict_severity, "OUTPUT_TEXTURE_TYPE_DISPATCH_MISSING", "纹理管线未按 NveRenderOutput 真实 textureType 分流 OES/2D，关闭效果可能黑屏。", render_files))

    custom_effect_files = any_pattern(nve_documents, r"\bcreateVideoEffect\s*\(")
    if custom_effect_files:
        if not has_any(nve_documents, "addEffect(", "insertEffect("):
            findings.append(Finding(strict_severity, "CUSTOM_EFFECT_NOT_REGISTERED", "创建自定义特效后未发现 addEffect/insertEffect。", custom_effect_files))
        if not has_any(nve_documents, "removeEffect("):
            findings.append(Finding(strict_severity, "CUSTOM_EFFECT_REMOVE_MISSING", "自定义特效未发现 removeEffect，切换/销毁可能叠加或泄漏。", custom_effect_files))
        for path in custom_effect_files:
            text = nve_documents[path]
            assignment = re.search(r"\b([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*[^;]*createVideoEffect\s*\(", text)
            variable = assignment.group(1) if assignment else None
            checked = bool(variable and re.search(rf"(?:{re.escape(variable)}\s*(?:==|!=)\s*null|Objects\.requireNonNull\s*\(\s*{re.escape(variable)})", text))
            if not checked:
                findings.append(Finding(strict_severity, "CUSTOM_EFFECT_NULL_UNCHECKED", "createVideoEffect 结果未发现针对该对象的空值检查。", (path,)))

    if profile == "camera2-oes-nv21":
        required = {
            "CAMERA2_MISSING": ("CameraDevice", "CameraCaptureSession"),
            "OES_INPUT_MISSING": ("NV_OES_TEXTURE", "GL_TEXTURE_EXTERNAL_OES"),
            "NV21_INPUT_MISSING": ("VIDEO_FRAME_PIXEL_FROMAT_NV21", "NV21"),
            "YUV_STRIDE_HANDLING_MISSING": ("rowStride", "pixelStride"),
        }
        for code, tokens in required.items():
            if not all(token in all_text for token in tokens):
                findings.append(Finding(strict_severity, code, "camera2-oes-nv21 profile 缺少契约证据：" + ", ".join(tokens)))
        if "acquireNextImage" in all_text and "acquireLatestImage" not in all_text:
            findings.append(Finding(strict_severity, "IMAGE_READER_BACKPRESSURE", "ImageReader 使用 acquireNextImage 且未见最新帧策略，可能积帧。"))
        surface_listener_files: set[str] = set()
        main_thread_listener_files: set[str] = set()
        for path, text in documents.items():
            main_handlers = {
                match.group(1)
                for match in re.finditer(
                    r"\b([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
                    r"(?:new\s+)?Handler\s*\(\s*Looper\.getMainLooper\s*\(\s*\)",
                    text,
                )
            }
            for listener in re.finditer(
                r"\bsetOnFrameAvailableListener\s*\(", text
            ):
                surface_listener_files.add(path)
                call = text[listener.start() : listener.start() + 600]
                semicolon = call.find(";")
                if semicolon >= 0:
                    call = call[:semicolon]
                second_arg_is_main = bool(
                    "Looper.getMainLooper()" in call
                    or re.search(
                        r",\s*(?:main|ui)[A-Za-z0-9_$]*Handler\b",
                        call,
                        re.IGNORECASE,
                    )
                    or any(
                        re.search(rf",\s*{re.escape(handler)}\b", call)
                        for handler in main_handlers
                    )
                )
                if second_arg_is_main:
                    main_thread_listener_files.add(path)
        if main_thread_listener_files:
            findings.append(
                Finding(
                    strict_severity,
                    "SURFACE_TEXTURE_CALLBACK_ON_MAIN_THREAD",
                    "SurfaceTexture 帧回调绑定到 UI main Handler；"
                    "滑块拖动会挤压相机帧节拍，必须改用专用 HandlerThread。",
                    tuple(sorted(main_thread_listener_files)),
                )
            )
        elif surface_listener_files and "HandlerThread" not in all_text:
            findings.append(
                Finding(
                    "RUNTIME_REQUIRED",
                    "RUNTIME_SURFACE_TEXTURE_CALLBACK_THREAD",
                    "发现 SurfaceTexture 帧回调，但静态分析未证明其 Handler 来自专用 "
                    "HandlerThread；必须记录回调线程名并确认不是 UI main。",
                    tuple(sorted(surface_listener_files)),
                )
            )
        for path, text in documents.items():
            for region in handler_regions(text, r"\bonImageAvailable\s*\("):
                if any(token in region for token in ("compressToJpeg", "JPEG", "BitmapFactory.decode")):
                    findings.append(Finding(strict_severity, "HEAVY_IMAGE_CALLBACK", "onImageAvailable 内出现 JPEG/Bitmap 工作；必须移出相机回调。", (path,)))
                if "rowStride" in region or "pixelStride" in region:
                    findings.append(Finding(strict_severity, "YUV_CONVERSION_ON_CAMERA_CALLBACK", "onImageAvailable 内直接执行 YUV stride 转换；应只取得最新 Image 并移交有界工作队列。", (path,)))
        bounded = has_any(documents, "ArrayBlockingQueue", "acquireLatestImage", "MAX_PENDING", "removeFirst(", "pollLast(")
        if not bounded:
            findings.append(Finding(strict_severity, "BOUNDED_FRAME_QUEUE_MISSING", "未发现有界最新帧/丢旧帧策略。"))
        if re.search(r"(?:timestamp|Timestamp)[A-Za-z0-9_]*\s*==\s*[A-Za-z0-9_.]*(?:timestamp|Timestamp)", all_text):
            findings.append(Finding(strict_severity, "STRICT_TIMESTAMP_EQUALITY", "纹理与 Buffer 使用严格时间戳相等，可能长期无检测帧。"))
        if re.search(r"setDisplayRotation\s*\(\s*(?:sensor|camera)Orientation\s*\)", all_text, re.IGNORECASE) or re.search(r"setCameraOrientation\s*\(\s*displayRotation\s*\)", all_text, re.IGNORECASE):
            findings.append(Finding(strict_severity, "SENSOR_DISPLAY_ROTATION_MIXED", "传感器方向与显示旋转被混用，会导致预览旋转 90° 或检测坐标错误。"))
        if "setCameraOrientation(" not in all_text:
            findings.append(Finding(strict_severity, "TEXTURE_SENSOR_ORIENTATION_MISSING", "OES 纹理未发现独立设置传感器方向；不得用显示旋转代替。"))
        if "setDisplayRotation(" not in all_text:
            findings.append(Finding(strict_severity, "BUFFER_DISPLAY_ROTATION_MISSING", "检测 Buffer 未发现独立设置显示旋转；不得用传感器方向代替。"))
        if "setMirror(" not in all_text:
            findings.append(Finding(strict_severity, "INPUT_MIRROR_CONTRACT_MISSING", "未发现输入镜像契约；前摄镜像必须在原始与效果路径中只应用一次。"))
        if not re.search(
            r"(?:display|oriented|rotated)[A-Za-z0-9_]*(?:Width|Height)|"
            r"(?:rotation|orientation)[^;\n]{0,120}(?:%\s*180|==\s*90|==\s*270)",
            all_text,
            re.IGNORECASE,
        ):
            findings.append(
                Finding(
                    strict_severity,
                    "TEXTURE_DIMENSION_SPACE_UNPROVEN",
                    "未发现 OES 纹理尺寸按显示方向交换宽高的证据；90°/270° 时直接使用相机平面尺寸会造成变形。",
                )
            )
        paired_input_found = False
        for text in documents.values():
            for call in re.finditer(r"\brenderEffect\s*\(", text):
                region = (method_region_containing(text, call.start()) or (text, 0))[0]
                if "NveTexture" in region and "NveImageBuffer" in region:
                    paired_input_found = True
                    break
            if paired_input_found:
                break
        if (
            "NveRenderInput" in all_text
            and ".setTexture(" in all_text
            and ".setImageBuffer(" in all_text
        ):
            paired_input_found = True
        if not paired_input_found:
            findings.append(
                Finding(
                    strict_severity,
                    "OES_NV21_PAIR_NOT_PROVEN",
                    "效果帧未证明在同一次 renderEffect 中成对提交 OES 纹理与 NV21 检测 Buffer。",
                )
            )
        if not re.search(r"(?:output|result)[A-Za-z0-9_]*\.getSize\s*\(|getSize\s*\(\s*\)", all_text):
            findings.append(
                Finding(
                    strict_severity,
                    "OUTPUT_TEXTURE_SIZE_IGNORED",
                    "未发现读取 SDK 输出纹理实际尺寸；沿用输入尺寸绘制可能造成拉伸、裁剪或黑屏。",
                )
            )
        if re.search(r"drawRaw\w*\s*\([^;]*(?:true|false)[^;]*\)", all_text) and re.search(r"drawEffect\w*\s*\([^;]*(?:true|false)[^;]*\)", all_text):
            raw = re.findall(r"drawRaw\w*\s*\([^;]*(true|false)[^;]*\)", all_text)
            effect = re.findall(r"drawEffect\w*\s*\([^;]*(true|false)[^;]*\)", all_text)
            if raw and effect and raw[-1] != effect[-1]:
                findings.append(Finding(strict_severity, "RAW_EFFECT_MIRROR_MISMATCH", "原始与效果路径使用不同镜像常量，开关效果会左右翻转。"))
        for path, text in documents.items():
            acquisitions = list(
                re.finditer(
                    r"\b([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*[^;\n]*(?:acquireLatestImage|acquireNextImage)\s*\(",
                    text,
                )
            )
            for acquisition in acquisitions:
                variable = acquisition.group(1)
                tail = text[acquisition.end(): acquisition.end() + 5000]
                try_resource = re.search(
                    rf"try\s*\([^)]*\b{re.escape(variable)}\b[^)]*(?:acquireLatestImage|acquireNextImage)",
                    text[max(0, acquisition.start() - 300): acquisition.end() + 300],
                    re.DOTALL,
                )
                ownership_transferred = re.search(
                    rf"\bofferLatestImage\s*\(\s*{re.escape(variable)}\b",
                    tail,
                )
                pairer_closes_owned_images = (
                    "void offerLatestImage(Image" in all_text
                    and re.search(
                        r"\b[A-Za-z_$][A-Za-z0-9_$]*\.image\.close\s*\(\s*\)",
                        all_text,
                    )
                    and "image.close()" in all_text
                )
                if (
                    not try_resource
                    and not re.search(
                        rf"\b{re.escape(variable)}\.close\s*\(\s*\)", tail
                    )
                    and not (
                        ownership_transferred and pairer_closes_owned_images
                    )
                ):
                    findings.append(Finding(strict_severity, "CAMERA_IMAGE_CLOSE_MISSING", f"取得 Camera2 Image `{variable}` 后未发现对应 close/try-with-resources。", (path,)))
    elif profile == "camerax-buffer":
        if not has_any(documents, "ImageAnalysis", "ImageProxy"):
            findings.append(Finding(strict_severity, "CAMERAX_ANALYZER_MISSING", "camerax-buffer profile 未发现 ImageAnalysis/ImageProxy。"))
        analyzer_found = False
        for path, text in documents.items():
            java_matches = list(re.finditer(r"\banalyze\s*\(\s*ImageProxy\s+([A-Za-z_$][A-Za-z0-9_$]*)", text))
            kotlin_matches = list(re.finditer(r"\banalyze\s*\(\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*:\s*ImageProxy", text))
            for match in java_matches + kotlin_matches:
                analyzer_found = True
                region = braced_region_after(text, match.end()) or ""
                parameter = match.group(1)
                if not re.search(rf"\b{re.escape(parameter)}\.close\s*\(\s*\)", region):
                    findings.append(Finding(strict_severity, "IMAGE_PROXY_CLOSE_MISSING", "CameraX analyzer 未在对应 ImageProxy 路径中关闭当前帧。", (path,)))
        if "ImageProxy" in all_text and not analyzer_found:
            findings.append(Finding("RUNTIME_REQUIRED", "RUNTIME_IMAGE_PROXY_CLOSE", "ImageProxy analyzer 通过间接类型/函数引用实现，静态分析无法证明逐帧 close。"))
        if not has_any(documents, "BUFFER_TEXTURE", "BUFFER_BUFFER"):
            findings.append(Finding(strict_severity, "BUFFER_RENDER_MODE_MISSING", "camerax-buffer profile 未发现 BUFFER_TEXTURE/BUFFER_BUFFER 渲染模式。"))
    elif profile == "custom-gl":
        if not has_any(documents, "EGLContext", "GLSurfaceView", "onDrawFrame("):
            findings.append(Finding(strict_severity, "CUSTOM_GL_CONTEXT_MISSING", "custom-gl profile 未发现明确 GLContext/渲染线程。"))

    runtime_checks = [
        ("RUNTIME_EFFECT_VISIBLE", "每项请求能力是否可见只能真机验证。"),
        ("RUNTIME_LIFECYCLE", "前后台和 GLContext 重建的恢复只能真机验证。"),
    ]
    if profile in {"camera2-oes-nv21", "camerax-buffer"}:
        runtime_checks.extend(
            [
                ("RUNTIME_DIRECTION_MATRIX", "传感器方向、显示旋转、四方向裁剪只能真机验证。"),
                ("RUNTIME_SINGLE_MIRROR", "前摄是否只镜像一次、开关前后是否一致只能真机验证。"),
            ]
        )
    if any(FEATURE_GROUPS.get(item, {}).get("intensity") for item in expand_features(features)):
        runtime_checks.append(("RUNTIME_EFFECT_STRENGTH", "强度型能力的 0/默认/明显档位只能真机验证。"))
    for code, message in runtime_checks:
        findings.append(Finding("RUNTIME_REQUIRED", code, message))


def add_feature_scope_findings(
    findings: list[Finding],
    nve_documents: dict[str, str],
    features: Iterable[str],
    strict: bool,
    baseline_match: bool = True,
    metadata_documents: dict[str, str] | None = None,
) -> None:
    requested = normalize_requested_features(features)
    unknown = unknown_features(requested)
    if unknown:
        findings.append(
            Finding(
                "ERROR",
                "UNKNOWN_FEATURE",
                "未知 feature 不得进入校验或证据流程：" + ", ".join(unknown),
            )
        )
        return
    effective = expand_features(requested)
    groups = beauty_controller_groups(requested)

    severity = "ERROR" if strict else "WARNING"
    version_sensitive_severity = severity if baseline_match else "RUNTIME_REQUIRED"
    metadata_text = "\n".join((metadata_documents or {}).values())
    catalog_missing: list[str] = []
    metadata_bound: list[str] = []
    implementation = implementation_features(requested)
    for feature in implementation:
        proof_tokens = FEATURE_GROUPS.get(feature, {}).get("proof_tokens", ())
        missing_tokens: list[str] = []
        if feature.startswith("makeup-single-") and len(proof_tokens) == 2:
            call_token, category_token = proof_tokens
            call_documents = [
                text for text in nve_documents.values() if call_token in text
            ]
            category_in_source = any(
                re.search(rf"\b{re.escape(category_token)}\b", text)
                for text in call_documents
            )
            category_in_metadata = (
                re.search(rf"\b{re.escape(category_token)}\b", metadata_text)
                is not None
            )
            if not call_documents:
                missing_tokens.append(call_token)
            if not category_in_source:
                if call_documents and category_in_metadata:
                    metadata_bound.append(feature)
                else:
                    missing_tokens.append(category_token)
        elif proof_tokens and not any(
            all(
                (
                    token in text
                    if "(" in token
                    else re.search(rf"\b{re.escape(token)}\b", text) is not None
                )
                for token in proof_tokens
            )
            for text in nve_documents.values()
        ):
            missing_tokens = list(proof_tokens)
        if missing_tokens:
            if feature not in requested:
                catalog_missing.append(feature)
                continue
            findings.append(
                Finding(
                    version_sensitive_severity,
                    "FEATURE_LEAF_IMPLEMENTATION_MISSING",
                    f"{feature} 缺少目标 AAR 基线要求的实现证据："
                    + ", ".join(missing_tokens),
                    tuple(sorted(nve_documents)),
                )
            )
    if metadata_bound:
        findings.append(
            Finding(
                "RUNTIME_REQUIRED",
                "RUNTIME_FEATURE_METADATA_BINDING",
                "以下动态目录项仅由素材元数据与通用注册入口证明，必须逐项验证 UI → 安装结果 → "
                "控制器 → 移除/恢复链路：" + ", ".join(metadata_bound),
                tuple(sorted((metadata_documents or {}).keys())),
            )
        )
    if catalog_missing:
        findings.append(
            Finding(
                version_sensitive_severity,
                "FEATURE_CATALOG_INCOMPLETE",
                "产品级功能未覆盖登记基线的完整实现目录（"
                f"缺少 {len(catalog_missing)} 项）："
                + ", ".join(catalog_missing),
                tuple(sorted(nve_documents)),
            )
        )
    registrations = {
        "skin": ("setBeauty(", "BEAUTY_SKIN_CONTROLLER_MISSING", "美肤范围未发现 NveBeauty/setBeauty 注册。"),
        "shape": ("setShape(", "BEAUTY_SHAPE_CONTROLLER_MISSING", "美型范围未发现 NveShape/setShape 注册。"),
        "micro-shape": ("setMicroShape(", "BEAUTY_MICRO_SHAPE_CONTROLLER_MISSING", "微整形范围未发现 NveMicroShape/setMicroShape 注册。"),
    }
    for group in sorted(groups):
        token, code, message = registrations[group]
        if not has_any(nve_documents, token):
            findings.append(Finding(severity, code, message, tuple(sorted(nve_documents))))

    if groups & {"shape", "micro-shape"} and not has_any(nve_documents, "setBeauty("):
        findings.append(
            Finding(
                severity,
                "BEAUTY_AR_SCENE_ANCHOR_MISSING",
                "美型/微整形缺少参数中性的 Beauty AR Scene 前置注册；该内部依赖不应暴露为额外美肤功能。",
                tuple(sorted(nve_documents)),
            )
        )

    if BEAUTY_SUITE in requested:
        findings.append(
            Finding(
                "RUNTIME_REQUIRED",
                "RUNTIME_BEAUTY_SUITE_SCOPE",
                "完整美颜必须逐项验证美肤、美型、微整形的独立入口、开关/重置、滑块生效和单项失败降级。",
            )
        )

    if groups:
        beauty_ui_text = "\n".join(nve_documents.values())
        has_beauty_item_ui = bool(
            re.search(
                r"(?i)(?:nve-beauty-feature-|Beauty(?:Panel|Item)View)",
                beauty_ui_text,
            )
        )
        has_beauty_cover_view = bool(
            re.search(
                r"(?is)(?:Bitmap|Drawable|AsyncImage|ImageBitmap).{0,120}cover",
                beauty_ui_text,
            )
            or (
                has_any(nve_documents, "ImageView")
                and bool(re.search(r"(?i)cover", beauty_ui_text))
            )
        )
        if has_beauty_item_ui and not has_beauty_cover_view:
            findings.append(
                Finding(
                    severity,
                    "BEAUTY_COVER_VIEW_MISSING",
                    "发现美颜功能列表，但未发现封面图片视图/绑定；"
                    "默认美颜条目必须展示封面和正式名称，不能退化为纯文字。",
                    tuple(sorted(nve_documents)),
                )
            )
        uses_compact_beauty = has_any(
            nve_documents, "NveCompactBeautyPanelView"
        )
        if (
            uses_compact_beauty
            and not calls_non_declaration_method(nve_documents, "setCovers")
        ):
            findings.append(
                Finding(
                    severity,
                    "BEAUTY_COVER_DATA_NOT_BOUND",
                    "已生成 NveCompactBeautyPanelView，但宿主未实际调用 setCovers(...)；"
                    "资源工作线程解码后必须按 `<category>:<item-id>` 发布封面。",
                    tuple(sorted(nve_documents)),
                )
            )

    checks: dict[str, tuple[tuple[str, ...], str, str]] = {
        "makeup-single": (("setMakeup(", "addSingleMakeUp("), "MAKEUP_SINGLE_REGISTRATION_MISSING", "单妆范围未发现 NveMakeup 注册和 addSingleMakeUp。"),
        "filter-packaged": (("addFilter(",), "FILTER_REGISTRATION_MISSING", "包滤镜范围未发现 addFilter。"),
        "face-prop": (("setFaceProp(",), "FACE_PROP_REGISTRATION_MISSING", "人脸道具范围未发现 setFaceProp。"),
        "custom-effect": (("createVideoEffect(", "addEffect("), "CUSTOM_EFFECT_REGISTRATION_MISSING", "自定义特效范围未发现创建和注册闭环。"),
        "custom-effect-background-segmentation": (("createVideoEffect(", "addEffect("), "BACKGROUND_SEGMENTATION_REGISTRATION_MISSING", "背景分割自定义特效未发现创建和注册闭环。"),
    }
    for feature in effective:
        parent = FEATURE_GROUPS.get(feature, {}).get("parent", feature)
        check = checks.get(parent)
        if check and not all(has_any(nve_documents, token) for token in check[0]):
            findings.append(Finding(severity, check[1], check[2], tuple(sorted(nve_documents))))

    if "makeup-compose" in effective and not (
        has_any(nve_documents, "setMakeup(")
        and has_any(nve_documents, "useComposeMakeup(", "setComposeMakeup(")
    ):
        findings.append(Finding(severity, "MAKEUP_COMPOSE_REGISTRATION_MISSING", "妆容范围未发现 NveMakeup 注册和经 AAR 确认的妆容应用 API。", tuple(sorted(nve_documents))))

    has_makeup_feature = any(
        FEATURE_GROUPS.get(feature, {}).get("parent", feature)
        in {"makeup-single", "makeup-compose"}
        for feature in effective
    )
    if has_makeup_feature:
        makeup_ui_text = "\n".join(nve_documents.values())
        has_makeup_item_ui = bool(
            re.search(
                r"(?i)(?:nve-makeup-item-|Makeup(?:Panel|Item)View)",
                makeup_ui_text,
            )
        )
        has_cover_binding = bool(
            re.search(r"(?i)(?:Bitmap|Drawable|AsyncImage|ImageBitmap).{0,80}cover", makeup_ui_text)
            or (
                has_any(nve_documents, "ImageView")
                and bool(re.search(r"(?i)cover", makeup_ui_text))
            )
        )
        if has_makeup_item_ui and not has_cover_binding:
            findings.append(
                Finding(
                    severity,
                    "MAKEUP_COVER_BINDING_MISSING",
                    "发现美妆素材列表，但未发现封面图片绑定；"
                    "单妆/妆容元数据已有封面时不得退化为纯文字条目。",
                    tuple(sorted(nve_documents)),
                )
            )
        uses_compact_makeup_contract = bool(
            has_any(nve_documents, "NveCompactMakeupPanelView")
            or (
                has_any(nve_documents, "nve-makeup-entry")
                and has_any(nve_documents, "nve-makeup-none-")
                and has_any(nve_documents, "nve-makeup-item-")
            )
        )
        has_none_icon_card = bool(
            re.search(
                r"(?i)(?:noneIconResource|makeupNoneIcon|"
                r"R\.drawable\.[A-Za-z0-9_]*(?:makeup|filter)[A-Za-z0-9_]*none|"
                r"Icons\.(?:Default|Outlined)\.(?:Block|DoNotDisturb))",
                makeup_ui_text,
            )
            or (
                has_any(nve_documents, "nve-makeup-none-")
                and bool(
                    re.search(
                        r"(?i)(?:setImageResource|setImageDrawable|painterResource|Icon\s*\()",
                        makeup_ui_text,
                    )
                )
            )
        )
        if uses_compact_makeup_contract and not has_none_icon_card:
            findings.append(
                Finding(
                    severity,
                    "MAKEUP_NONE_ICON_CARD_MISSING",
                    "发现美妆素材列表，但未发现图标式“无”卡片；"
                    "默认美妆 UI 应复用滤镜的圆圈斜杠语义，"
                    "并以描边/图标颜色表达选中态。",
                    tuple(sorted(nve_documents)),
                )
            )

    if "makeup-compose" in effective:
        compose_state_text = "\n".join(nve_documents.values())
        has_snapshot = bool(
            re.search(
                r"(?i)(?:snapshot|capture|save|store)[A-Za-z0-9_]*(?:compose|conflict|effect)state|"
                r"(?:compose|conflict|effect)state[A-Za-z0-9_]*(?:snapshot|saved)",
                compose_state_text,
            )
        )
        has_restore = bool(
            re.search(
                r"(?i)(?:restore|reapply)[A-Za-z0-9_]*(?:compose|conflict|effect)state",
                compose_state_text,
            )
        )
        if not (has_snapshot and has_restore):
            findings.append(
                Finding(
                    severity,
                    "MAKEUP_COMPOSE_STATE_TRANSACTION_MISSING",
                    "妆容可能覆盖独立美颜、美型、微整形和滤镜；"
                    "未发现应用前状态快照与移除后的冲突状态恢复。",
                    tuple(sorted(nve_documents)),
                )
            )

        has_single_makeup = any(
            FEATURE_GROUPS.get(feature, {}).get("parent", feature)
            == "makeup-single"
            for feature in effective
        )
        if has_single_makeup:
            has_provenance = bool(
                re.search(
                    r"(?i)(?:decomposedComposeTypes|composeOrigin|"
                    r"inheritedCompose|COMPOSE_COMPONENT_ID)",
                    compose_state_text,
                )
            )
            if not has_provenance:
                findings.append(
                    Finding(
                        severity,
                        "MAKEUP_COMPOSE_PROVENANCE_MISSING",
                        "同时接入妆容和单妆时，未发现拆分组成项来源跟踪；"
                        "无法证明分类“无”和妆容“无”不会互相误清。",
                        tuple(sorted(nve_documents)),
                    )
                )

            has_independent_ui_state = bool(
                re.search(
                    r"(?i)(?:NveMakeupSelectionState|composeDecomposed|"
                    r"selectedComposeId|composeSelectionState)",
                    compose_state_text,
                )
            )
            if not has_independent_ui_state:
                findings.append(
                    Finding(
                        severity,
                        "MAKEUP_COMPOSE_UI_STATE_SEPARATION_MISSING",
                        "未发现独立的妆容 UI 选择状态；SDK compose 对象拆分后"
                        "可能被错误显示为“无”。",
                        tuple(sorted(nve_documents)),
                    )
                )

            has_external_component_policy = bool(
                re.search(
                    r"(?i)(?:canDecomposeCompose|onComposeDecomposed|"
                    r"composeNonMakeup|nonMakeupComponentPolicy)",
                    compose_state_text,
                )
            )
            if not has_external_component_policy:
                findings.append(
                    Finding(
                        "WARNING",
                        "MAKEUP_COMPOSE_EXTERNAL_POLICY_UNVERIFIED",
                        "妆容可能包含美颜、美型、微整形或滤镜；未发现拆分时"
                        "针对非单妆组成项的显式保留/恢复策略。",
                        tuple(sorted(nve_documents)),
                    )
                )

            has_selection_snapshot = bool(
                re.search(
                    r"(?i)(?:snapshot|save)[A-Za-z0-9_]*"
                    r"(?:makeup|selection)state|"
                    r"(?:makeup|selection)state[A-Za-z0-9_]*(?:snapshot|saved)",
                    compose_state_text,
                )
            )
            if not has_selection_snapshot:
                findings.append(
                    Finding(
                        "WARNING",
                        "MAKEUP_SELECTION_LIFECYCLE_UNVERIFIED",
                        "未发现美妆选择状态快照；旋转、页面重建或 GLContext "
                        "恢复后 UI、desired state 与 applied state 可能不一致。",
                        tuple(sorted(nve_documents)),
                    )
                )

    removal_checks = {
        "makeup-single": ("removeSingleMakeUp(", "MAKEUP_SINGLE_REMOVE_MISSING"),
        "makeup-compose": ("removeComposeMake", "MAKEUP_COMPOSE_REMOVE_MISSING"),
        "filter-packaged": ("removeFilter(", "FILTER_REMOVE_MISSING"),
        "custom-effect": ("removeEffect(", "CUSTOM_EFFECT_REMOVE_MISSING"),
        "custom-effect-background-segmentation": ("removeEffect(", "CUSTOM_EFFECT_REMOVE_MISSING"),
    }
    for feature in effective:
        parent = FEATURE_GROUPS.get(feature, {}).get("parent", feature)
        removal = removal_checks.get(parent)
        if removal and not has_any(nve_documents, removal[0]):
            findings.append(Finding(severity, removal[1], f"{parent} 未发现移除/关闭路径。", tuple(sorted(nve_documents))))

    if "face-prop" in effective:
        explicit_clear = bool(
            any_pattern(
                nve_documents,
                r"(?i)\b[A-Za-z_$][A-Za-z0-9_$]*prop[A-Za-z0-9_$]*\s*\.\s*"
                r"setPropId\s*\(\s*[\"']\s*[\"']\s*\)",
            )
        )
        explicit_disable = bool(
            any_pattern(
                nve_documents,
                r"(?i)\b[A-Za-z_$][A-Za-z0-9_$]*prop[A-Za-z0-9_$]*\s*\.\s*"
                r"setEnable\s*\(\s*false\s*\)",
            )
        )
        variable_disable = False
        for text in nve_documents.values():
            for enable_call in re.finditer(
                r"(?i)\b([A-Za-z_$][A-Za-z0-9_$]*prop[A-Za-z0-9_$]*)\s*\.\s*"
                r"setEnable\s*\(\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*\)",
                text,
            ):
                region_info = method_region_containing(text, enable_call.start())
                region = region_info[0] if region_info else text
                prop, enabled = enable_call.groups()
                if (
                    re.search(rf"\b{re.escape(enabled)}\s*=\s*\w+\s*!=\s*null", region)
                    and re.search(
                        rf"\b{re.escape(prop)}\s*\.\s*setPropId\s*\([^;]*"
                        rf"==\s*null\s*\?\s*[\"']\s*[\"']\s*:",
                        region,
                    )
                ):
                    variable_disable = True
                    break
        if not variable_disable and not (explicit_clear and explicit_disable):
            findings.append(
                Finding(
                    severity,
                    "FACE_PROP_DISABLE_INCOMPLETE",
                    "face-prop 的“无”状态必须同时清空 propId、关闭控制器并更新 desired state；"
                    "只执行其中一步会导致恢复后重现或后续选择仍保持 disabled。",
                    tuple(sorted(nve_documents)),
                )
            )

    selected_asset_tokens: set[str] = set()
    if any(item in effective for item in ("makeup-single", "makeup-compose")):
        selected_asset_tokens.add("ASSET_PACKAGE_TYPE_MAKEUP")
    if "filter-packaged" in effective:
        selected_asset_tokens.add("ASSET_PACKAGE_TYPE_VIDEOFX")
    if "face-prop" in effective:
        selected_asset_tokens.add("ASSET_PACKAGE_TYPE_ARSCENE")
    if selected_asset_tokens:
        install_files = tuple(
            sorted(
                path
                for path, text in nve_documents.items()
                if any(
                    token in (method_region_containing(text, call.start()) or (text, 0))[0]
                    for call in INSTALL_RE.finditer(text)
                    if not is_method_declaration(text, call.start())
                    for token in selected_asset_tokens
                )
            )
        )
        if not install_files:
            findings.append(Finding(severity, "FEATURE_ASSET_INSTALL_MISSING", "所选素材型能力未发现素材安装调用。", tuple(sorted(nve_documents))))
        else:
            unguarded = unguarded_install_locations(nve_documents, selected_asset_tokens)
            if unguarded:
                findings.append(Finding(severity, "FEATURE_PACKAGE_ID_GUARD_MISSING", "每次素材安装都必须在同一处理区域拒绝空值、非法 UUID 和全零 UUID。", unguarded))

    if effective and not re.search(r"(?i)(onResume|restore|reapply)", "\n".join(nve_documents.values())):
        findings.append(Finding(severity, "FEATURE_RESTORE_MISSING", "所选能力未发现恢复路径。", tuple(sorted(nve_documents))))

    required_models = set(contract_report(requested)["models"])
    for path, text in nve_documents.items():
        variable_models: dict[str, str] = {}
        for match in re.finditer(
            r"\b(?:boolean|Boolean|val|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
            r"[^;]*?initModel\s*\([^;]*?NveDetectionModelType\.([A-Z][A-Z0-9_]*)[^;]*;",
            text,
            re.DOTALL,
        ):
            variable_models[match.group(1)] = match.group(2)
        for match in re.finditer(
            r"\b(?:boolean|Boolean|val|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
            r"[^;]*?initializePropModel\s*\([^;]*?PropModel\.([A-Z][A-Z0-9_]*)[^;]*;",
            text,
            re.DOTALL,
        ):
            variable_models[match.group(1)] = match.group(2)
        for ready in re.finditer(
            r"\b(?:sdkReady|renderCoreReady|capabilityReady)\s*=\s*([^;]+);", text
        ):
            expression = ready.group(1)
            extras = sorted(
                {
                    model
                    for variable, model in variable_models.items()
                    if re.search(rf"\b{re.escape(variable)}\b", expression)
                    and model not in required_models
                }
            )
            if extras:
                findings.append(
                    Finding(
                        "RUNTIME_REQUIRED",
                        "UNRELATED_MODEL_GATES_FEATURE",
                        "全局 readiness 依赖当前功能契约之外的模型，可能使无关模型失败阻断目标能力："
                        + ", ".join(extras),
                        (path,),
                    )
                )


def validate(
    root: Path,
    profile: str = "custom-gl",
    strict: bool = False,
    features: Iterable[str] | None = None,
    includes: Iterable[str] = (),
    inventory: dict[str, Any] | None = None,
    documents: dict[str, str] | None = None,
    project_files: Iterable[Path] | None = None,
) -> list[Finding]:
    requested_features = list(features or ())
    unknown = unknown_features(requested_features)
    if unknown:
        return [
            Finding(
                "ERROR",
                "UNKNOWN_FEATURE",
                "未知 feature 不得进入校验或证据流程：" + ", ".join(unknown),
            )
        ]
    cached_files = list(project_files) if project_files is not None else None
    if cached_files is None and (inventory is None or documents is None):
        cached_files = list(iter_files(root))
    report = inventory or inspect_project(root, features=requested_features, files=cached_files)
    documents = documents or source_documents(root, cached_files)
    findings: list[Finding] = []
    feature_report = report.get("feature_contract", {})
    recording_requested = "recording" in feature_report.get(
        "implementation_features", ()
    )
    requires_nve_prerequisites = feature_report.get(
        "requires_nve_prerequisites", True
    )
    normalized_requested = normalize_requested_features(requested_features)
    nve_requested_features = [
        feature
        for feature in normalized_requested
        if not FEATURE_GROUPS.get(feature, {}).get("host_capability")
    ]
    if recording_requested:
        add_recording_findings(findings, documents, strict)
    if not requires_nve_prerequisites:
        for blocker in report.get("prerequisite_gate", {}).get("blockers", []):
            if blocker.get("code") == "NVE_FINAL_OUTPUT_SOURCE_MISSING":
                findings.append(
                    Finding(
                        "ERROR",
                        blocker["code"],
                        blocker.get("message", "缺少 NVE 最终输出消费点。"),
                    )
                )
        if not any(item.severity == "ERROR" for item in findings):
            findings.append(
                Finding(
                    "INFO",
                    "HOST_CAPABILITY_STATIC_CHECK_PASSED",
                    "录制宿主能力未发现阻断级静态问题；未重复检查既有 NVE AAR、lic、模型和素材。",
                )
            )
        return sorted(
            findings, key=lambda item: (item.severity, item.code, item.locations)
        )
    for blocker in report.get("prerequisite_gate", {}).get("blockers", []):
        code = blocker.get("code", "")
        if code == "FEATURE_SCOPE_UNRESOLVED" or "ASSET" in code:
            findings.append(
                Finding(
                    "ERROR",
                    code,
                    blocker.get("message", "素材前置门禁未通过。"),
                )
            )
    artifacts = report["sdk_artifacts"]
    aars = artifacts["nve_effect_kit_aars"]
    invalid_aars = artifacts.get("invalid_named_aar_candidates", [])
    standalone = artifacts["standalone_streaming_sdk_jars"]
    dependency_declarations = artifacts.get("dependency_declarations", [])

    if invalid_aars:
        findings.append(
            Finding(
                "ERROR",
                "NVE_AAR_INVALID",
                "发现名称类似 NveEffectKit 但结构无效或不含 NveEffectKit.class 的 AAR。",
                tuple(item["path"] for item in invalid_aars),
            )
        )
    if not aars and not dependency_declarations:
        findings.append(
            Finding(
                "ERROR",
                "NVE_AAR_MISSING",
                "未发现本地 NveEffectKit AAR；若通过其他受支持方式提供依赖，请人工确认依赖解析结果。",
            )
        )
    elif not aars and dependency_declarations:
        findings.append(
            Finding(
                "WARNING",
                "DECLARED_AAR_UNINSPECTED",
                "发现 NveEffectKit 依赖声明但无本地 AAR；必须从 Gradle 解析产物核对 API、内嵌依赖和 ABI。",
                tuple(item["path"] for item in dependency_declarations),
            )
        )
    if len(aars) > 1:
        findings.append(
            Finding(
                "ERROR",
                "MULTIPLE_NVE_AARS",
                "发现多个 NveEffectKit AAR，可能导致重复类或版本漂移。",
                tuple(aar["path"] for aar in aars),
            )
        )
    embedded = tuple(aar["path"] for aar in aars if aar.get("embedded_streaming_sdk"))
    if embedded and standalone:
        findings.append(
            Finding(
                "ERROR",
                "DUPLICATE_STREAMING_SDK",
                "NveEffectKit AAR 已内嵌 NvAndroidStreamingSdk.jar，同时工程仍有独立 JAR。",
                tuple(sorted((*embedded, *standalone))),
            )
        )

    host_abis = {
        abi
        for module in report["modules"]
        if module["plugin"] == "application"
        for abi in module["abi_filters"]
    }
    aar_abis = {abi for aar in aars for abi in aar.get("abis", [])}
    unsupported = sorted(host_abis - aar_abis) if host_abis and aar_abis else []
    if unsupported:
        findings.append(
            Finding(
                "ERROR",
                "ABI_NOT_PACKAGED",
                "应用 ABI 过滤器包含 NveEffectKit AAR 未提供的架构：" + ", ".join(unsupported),
            )
        )

    nve_files = tuple(report["nve_source_files"])
    if not nve_files:
        findings.append(
            Finding("ERROR", "NVE_USAGE_MISSING", "未发现 NveEffectKit 源码调用，接入闭环尚未建立。")
        )
        return sorted(findings, key=lambda item: (item.severity, item.code, item.locations))

    include_paths: list[str] = []
    resolved_root = root.resolve()
    for value in includes:
        supplied = Path(value)
        candidate = (root / supplied).resolve()
        if supplied.is_absolute() or not candidate.is_relative_to(resolved_root) or not candidate.is_file():
            findings.append(Finding("ERROR", "INCLUDE_FILE_INVALID", f"--include 必须是工程内现有文件：{value}"))
            continue
        relative = rel(candidate, root)
        if relative not in documents:
            findings.append(Finding("ERROR", "INCLUDE_FILE_UNREADABLE", f"--include 文件不在安全文本范围：{value}"))
            continue
        include_paths.append(relative)
    metadata_files = {
        path
        for path, text in documents.items()
        if path.lower().endswith(".json")
        and any(
            token in text
            for token in (
                '"makeupId"',
                '"effectContent"',
                '"fxPackageId"',
                '"stringFilePath"',
            )
        )
    }
    explicit_nve_ui_files = {
        path
        for path, text in documents.items()
        if any(
            token in text
            for token in (
                "contentDescription:nve",
                'setContentDescription("nve',
                'testTag("nve',
                'testTag = "nve',
            )
        )
    }
    analyzed_files = tuple(
        sorted(
            detected_analysis_files(report)
            | set(include_paths)
            | explicit_nve_ui_files
        )
    )
    nve_documents = {path: documents[path] for path in analyzed_files if path in documents}
    metadata_documents = {
        path: documents[path] for path in metadata_files if path in documents
    }
    sdk_documents = {path: documents[path] for path in nve_files if path in documents}
    gson_required = locations_with(
        nve_documents, "ParseJsonFile.fromJson"
    )
    if gson_required:
        gson_declarations = dependency_declaration_locations(
            documents, "com.google.code.gson", "gson"
        )
        dependency_files = (
            cached_files if cached_files is not None else list(iter_files(root))
        )
        gson_bundled = bundled_class_locations(
            root, dependency_files, "com/google/gson/Gson.class"
        )
        if not gson_declarations and not gson_bundled:
            findings.append(
                Finding(
                    "ERROR" if strict else "WARNING",
                    "MAKEUP_GSON_RUNTIME_DEPENDENCY_MISSING",
                    "NveMakeupResourceLoader 调用 ParseJsonFile.fromJson，"
                    "登记 3.16.1 必须在目标模块 runtime classpath 提供 "
                    "com.google.gson.Gson；仅编译通过不能证明运行时依赖闭合。",
                    gson_required,
                )
            )
    unresolved_ui = tuple(
        sorted(
            path
            for path, text in documents.items()
            if path not in nve_documents
            and any(token in text for token in ("effectController.", "renderer.setEffect"))
        )
    )
    if unresolved_ui:
        findings.append(Finding("RUNTIME_REQUIRED", "RUNTIME_UNRESOLVED_UI_FILES", "发现可能相关但未显式纳入的 UI 文件；用 --include 纳入静态分析，或在运行时核实。", unresolved_ui))
    lifecycle = {
        "init": any_pattern(sdk_documents, r"\.init\s*\("),
        "init_model": locations_with(sdk_documents, "initModel("),
        "initialize": locations_with(sdk_documents, "initialize("),
        "render": locations_with(sdk_documents, "renderEffect("),
        "recycle": locations_with(sdk_documents, "recycleOutput("),
        "cleanup": locations_with(sdk_documents, "cleanUp("),
        "destroy": locations_with(sdk_documents, "destroy("),
    }
    for key, label in (
        ("init", "init"),
        ("initialize", "initialize"),
        ("render", "renderEffect"),
    ):
        if not lifecycle[key]:
            findings.append(
                Finding("ERROR", f"{key.upper()}_MISSING", f"未发现 {label} 调用。", nve_files)
            )

    required_model_order = report["feature_contract"].get("model_init_order", [])
    model_call_locations: dict[str, list[tuple[str, int]]] = {}
    for path, text in sdk_documents.items():
        for match in re.finditer(
            r"\binitModel\s*\([^;]*?\bNveDetectionModelType\.([A-Z][A-Z0-9_]*)",
            text,
            re.DOTALL,
        ):
            model_call_locations.setdefault(match.group(1), []).append(
                (path, match.start())
            )
    missing_model_calls = [
        model for model in required_model_order if model not in model_call_locations
    ]
    if missing_model_calls:
        findings.append(
            Finding(
                "ERROR",
                "REQUIRED_MODEL_INIT_MISSING",
                "当前功能缺少必需的 initModel 调用：" + ", ".join(missing_model_calls),
                nve_files,
            )
        )
    if len(required_model_order) > 1:
        order_proven = False
        for path, text in sdk_documents.items():
            positions = {
                model: min(
                    (
                        match.start()
                        for match in re.finditer(
                            rf"\binitModel\s*\([^;]*?\bNveDetectionModelType\.{re.escape(model)}\b",
                            text,
                            re.DOTALL,
                        )
                    ),
                    default=None,
                )
                for model in required_model_order
            }
            if all(position is not None for position in positions.values()):
                order_proven = True
                ordered_positions = [
                    positions[model] for model in required_model_order
                ]
                if ordered_positions != sorted(ordered_positions):
                    findings.append(
                        Finding(
                            "ERROR",
                            "REQUIRED_MODEL_INIT_ORDER_INVALID",
                            "必需模型初始化顺序不符合当前 AAR 基线："
                            + " -> ".join(required_model_order),
                            (path,),
                        )
                    )
        if (
            not missing_model_calls
            and not order_proven
            and all(model in model_call_locations for model in required_model_order)
        ):
            findings.append(
                Finding(
                    "RUNTIME_REQUIRED",
                    "REQUIRED_MODEL_INIT_ORDER_UNPROVEN",
                    "必需模型调用分散在不同文件/方法，静态分析无法证明顺序："
                    + " -> ".join(required_model_order),
                    nve_files,
                )
            )

    if lifecycle["render"] and not lifecycle["recycle"]:
        findings.append(
            Finding(
                "ERROR",
                "RENDER_OUTPUT_NOT_RECYCLED",
                "发现 renderEffect，但未发现 recycleOutput；每个非空输出都必须回收。",
                lifecycle["render"],
            )
        )
    if lifecycle["initialize"] and not lifecycle["cleanup"]:
        findings.append(
            Finding(
                "ERROR",
                "CLEANUP_MISSING",
                "发现 initialize，但未发现 cleanUp；GL 会话结束时可能泄漏资源。",
                lifecycle["initialize"],
            )
        )
    if lifecycle["init"] and not lifecycle["destroy"]:
        findings.append(
            Finding(
                "WARNING",
                "DESTROY_NOT_FOUND",
                "发现 SDK init，但未发现 destroy；若由进程级管理器统一销毁，请人工确认。",
                lifecycle["init"],
            )
        )

    for path in lifecycle["render"]:
        text = documents.get(path, "")
        if "recycleOutput(" not in text:
            findings.append(
                Finding(
                    "WARNING",
                    "RECYCLE_CROSS_FILE_OR_BRANCH",
                    "渲染文件内未看到 recycleOutput，请确认所有返回分支都回收输出。",
                    (path,),
                )
            )
        if "NveRenderError.NO_ERROR" not in text and "getErrorCode(" not in text:
            findings.append(
                Finding(
                    "WARNING",
                    "RENDER_ERROR_UNCHECKED",
                    "渲染调用所在文件未检查错误码或空输出。",
                    (path,),
                )
            )

    for path in lifecycle["cleanup"]:
        text = documents.get(path, "")
        if not any(token in text for token in ("queueEvent(", "GLThread", "HandlerThread", "onDrawFrame(")):
            findings.append(
                Finding(
                    "WARNING",
                    "GL_THREAD_NOT_EVIDENT",
                    "cleanUp 所在线程无法由静态分析确认；必须在持有有效 GLContext 的线程调用。",
                    (path,),
                )
            )

    if lifecycle["init_model"]:
        if report["resources"]["model_file_count"] == 0:
            findings.append(
                Finding(
                    "WARNING",
                    "MODEL_FILES_NOT_FOUND",
                    "存在 initModel 调用，但工程内未发现 .model/.dat；设备侧功能验证被阻塞。若模型由运行时提供，需确认其与当前 AAR 配套并逐项检查 initModel 结果。",
                    lifecycle["init_model"],
                )
            )
        else:
            findings.append(
                Finding(
                    "WARNING",
                    "MODEL_COMPATIBILITY_UNVERIFIED",
                    "工程内发现模型候选文件，但静态检查不能确认模型类型和版本与当前 AAR 配套；完成设备验证前不得视为已确认。",
                    lifecycle["init_model"],
                )
            )
    if lifecycle["init"]:
        if report["resources"]["license_file_count"] == 0:
            findings.append(
                Finding(
                    "WARNING",
                    "LICENSE_FILE_NOT_FOUND",
                    "存在 SDK init 调用，但工程内未发现 lic；SDK 初始化和设备侧效果验证被阻塞。若 lic 由运行时提供，需确认其绑定最终 applicationId。",
                    lifecycle["init"],
                )
            )
        else:
            findings.append(
                Finding(
                    "WARNING",
                    "LICENSE_BINDING_UNVERIFIED",
                    "工程内发现 lic 候选文件，但静态检查不能确认授权来源、有效期及其与最终 applicationId 的绑定；完成设备初始化验证前不得视为已确认。",
                    lifecycle["init"],
                )
            )

    package_usage = any_pattern(
        sdk_documents,
        r"\b(?:installFxPackage|installAssetPackage|upgradeAssetPackage)\s*\(",
    )
    effect_asset_count = sum(report["resources"]["effect_asset_counts"].values())
    if package_usage and effect_asset_count == 0:
        findings.append(
            Finding(
                "WARNING",
                "EFFECT_ASSET_FILES_NOT_FOUND",
                "发现素材包安装调用，但工程内未发现支持的效果素材；相关滤镜、美型、美妆或道具的设备验证被阻塞。若素材由运行时下载，需确认来源、包类型和必要素材 lic。",
                package_usage,
            )
        )

    setters = any_pattern(
        sdk_documents,
        r"\.(?:setBeauty|setShape|setMicroShape|setMakeup|setFaceProp|addFilter)\s*\(",
    )
    restore_markers = any_pattern(sdk_documents, r"(?:onResume|restore|reapply|initialize)\s*\(")
    if setters and not restore_markers:
        findings.append(
            Finding(
                "WARNING",
                "STATE_RESTORE_NOT_EVIDENT",
                "已注册效果控制器，但未发现恢复入口；GLContext/页面恢复后可能丢失效果状态。",
                setters,
            )
        )

    hardcoded_user_paths = any_pattern(documents, r"[A-Za-z]:\\Users\\|/Users/|/home/[^/\s]+/")
    if hardcoded_user_paths:
        findings.append(
            Finding(
                "ERROR",
                "HARDCODED_USER_PATH",
                "源码或构建脚本包含用户目录绝对路径，独立分发和客户环境不可用。",
                hardcoded_user_paths,
            )
        )

    baseline_match = bool(aars) and all(aar.get("baseline_match") for aar in aars)
    if aars and not baseline_match:
        findings.append(
            Finding(
                "RUNTIME_REQUIRED",
                "AAR_BASELINE_MISMATCH",
                "NveEffectKit AAR 不匹配首发基线；版本敏感 API/顺序规则降级为编译或人工核实，不猜测接口。",
                tuple(aar["path"] for aar in aars),
            )
        )
    if baseline_match:
        available_resource_paths = {
            path.replace("\\", "/").lower()
            for key in ("model_files", "effect_asset_files")
            for path in report.get("resources", {}).get(key, [])
        }
        for model_type, required_path in report["feature_contract"].get(
            "baseline_model_files", {}
        ).items():
            normalized = required_path.replace("\\", "/").lower()
            if not any(path.endswith(normalized) for path in available_resource_paths):
                findings.append(
                    Finding(
                        "ERROR",
                        "REQUIRED_BASELINE_MODEL_MISSING",
                        f"{model_type} 必须使用当前基线配套模型：{required_path}",
                    )
                )
        for feature, filename in report["feature_contract"].get(
            "required_baseline_asset_files", {}
        ).items():
            normalized = filename.lower()
            if not any(
                path == normalized or path.endswith("/" + normalized)
                for path in available_resource_paths
            ):
                findings.append(
                    Finding(
                        "RUNTIME_REQUIRED",
                        "RUNTIME_CUSTOM_ASSET_MAPPING",
                        (
                            f"{feature} 未使用登记基线文件名 {filename}；"
                            "允许自制或更新素材，但必须核实显式 feature→path 映射、"
                            "包类型、素材授权、安装返回 ID 和真机效果。"
                        ),
                    )
                )
    add_contract_findings(
        findings,
        root,
        nve_documents,
        sdk_documents,
        profile,
        strict,
        nve_requested_features,
        baseline_match,
        cached_files,
    )
    if nve_requested_features:
        add_feature_scope_findings(
            findings,
            nve_documents,
            nve_requested_features,
            strict,
            baseline_match,
            metadata_documents,
        )

    if not any(item.severity == "ERROR" for item in findings):
        resource_blocker_codes = {
            "MODEL_FILES_NOT_FOUND",
            "MODEL_COMPATIBILITY_UNVERIFIED",
            "LICENSE_FILE_NOT_FOUND",
            "LICENSE_BINDING_UNVERIFIED",
            "EFFECT_ASSET_FILES_NOT_FOUND",
        }
        if any(item.code in resource_blocker_codes for item in findings):
            findings.append(
                Finding(
                    "INFO",
                    "STATIC_CODE_CHECK_PASSED_WITH_RESOURCE_BLOCKERS",
                    "未发现代码级静态错误，但仍有资源或授权验证阻塞；不得据此宣称功能已完成验证。",
                )
            )
        else:
            findings.append(Finding("INFO", "STATIC_CHECK_PASSED", "未发现阻断级静态问题。"))
    order = {"ERROR": 0, "WARNING": 1, "RUNTIME_REQUIRED": 2, "INFO": 3}
    return sorted(findings, key=lambda item: (order[item.severity], item.code, item.locations))


def render_text(findings: Iterable[Finding]) -> str:
    lines = []
    for finding in findings:
        suffix = f" [{', '.join(finding.locations)}]" if finding.locations else ""
        lines.append(f"{finding.severity} {finding.code}: {finding.message}{suffix}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate an existing NveEffectKit Android integration.")
    parser.add_argument("project_root", help="Android project root")
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--strict", action="store_true", help="treat statically provable contract violations as errors")
    parser.add_argument("--features", help="comma-separated requested features; beauty/beauty-suite expands to the official three-part beauty module")
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
    parser.add_argument("--include", action="append", default=[], help="project-relative UI/source file to include in effect validation; repeatable")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.project_root).expanduser().resolve()
    if not root.is_dir():
        print(f"Invalid project root: {args.project_root}", file=sys.stderr)
        return 2
    if not any((root / name).exists() for name in ("settings.gradle", "settings.gradle.kts", "build.gradle", "build.gradle.kts")):
        print("No Gradle Android project markers found.", file=sys.stderr)
        return 2
    requested_features = [item.strip() for item in (args.features or "").split(",") if item.strip()]
    unknown = unknown_features(requested_features)
    if unknown:
        finding = Finding(
            "ERROR",
            "UNKNOWN_FEATURE",
            "未知 feature 不得进入校验或证据流程：" + ", ".join(unknown),
        )
        if args.json:
            print(
                json.dumps(
                    {
                        "schema_version": 4,
                        "project": root.name,
                        "profile": args.profile,
                        "strict": args.strict,
                        "requested_features": normalize_requested_features(requested_features),
                        "effective_features": expand_features(requested_features),
                        "feature_contract": contract_report(requested_features),
                        "analyzed_files": [],
                        "unresolved_ui_files": [],
                        "static_pass": False,
                        "runtime_required": [],
                        "findings": [asdict(finding)],
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(render_text([finding]))
        return 1
    resource_paths: list[Path] = []
    for raw_path in args.resource_path:
        resource_path = Path(raw_path).expanduser().resolve()
        if not resource_path.exists():
            print(f"Invalid authorized resource path: {raw_path}", file=sys.stderr)
            return 2
        resource_paths.append(resource_path)
    asset_map: dict[str, Any] = {}
    if args.asset_map:
        try:
            asset_map = load_asset_map(Path(args.asset_map).expanduser().resolve())
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2
    project_files = list(iter_files(root))
    inventory = inspect_project(
        root,
        features=requested_features,
        files=project_files,
        resource_paths=resource_paths,
        asset_map=asset_map,
    )
    documents = source_documents(root, project_files)
    findings = validate(
        root,
        args.profile,
        args.strict,
        requested_features,
        args.include,
        inventory,
        documents,
        project_files,
    )
    auto_ui_files = {
        path
        for path, text in documents.items()
        if any(
            token in text
            for token in (
                "contentDescription:nve",
                'setContentDescription("nve',
                'testTag("nve',
                'testTag = "nve',
            )
        )
    }
    analyzed_files = sorted(
        detected_analysis_files(inventory) | set(args.include) | auto_ui_files
    )
    unresolved_ui_files = sorted(
        path
        for path, text in documents.items()
        if path not in analyzed_files
        and any(token in text for token in ("effectController.", "renderer.setEffect"))
    )
    if args.json:
        print(
            json.dumps(
                {
                    "schema_version": 4,
                    "project": root.name,
                    "profile": args.profile,
                    "strict": args.strict,
                    "requested_features": normalize_requested_features(requested_features),
                    "effective_features": expand_features(requested_features),
                    "feature_contract": contract_report(requested_features),
                    "analyzed_files": analyzed_files,
                    "unresolved_ui_files": unresolved_ui_files,
                    "static_pass": not any(item.severity == "ERROR" for item in findings),
                    "runtime_required": [item.code for item in findings if item.severity == "RUNTIME_REQUIRED"],
                    "findings": [asdict(item) for item in findings],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(render_text(findings))
    return 1 if any(item.severity == "ERROR" for item in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
