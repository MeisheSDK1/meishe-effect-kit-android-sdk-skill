#!/usr/bin/env python3
"""Read-only inventory for Android projects that may integrate NveEffectKit."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from feature_contract import BASELINE_AAR_SHA256, contract_report, unknown_features


SKIP_DIRS = {
    ".git",
    ".gradle",
    ".gradle-local",
    ".idea",
    ".kotlin",
    "build",
    "out",
    "node_modules",
    "target",
}
SAFE_TEXT_SUFFIXES = {".gradle", ".kts", ".java", ".kt", ".toml", ".xml"}
SENSITIVE_NAMES = {
    ".env",
    "gradle.properties",
    "key.properties",
    "local.properties",
    "secrets.properties",
}
MAX_TEXT_BYTES = 2 * 1024 * 1024
ASSET_SUFFIXES = {
    ".arscene",
    ".facemesh",
    ".makeup",
    ".videofx",
    ".warp",
}
MODEL_SUFFIXES = {".dat", ".model"}
RESOURCE_FILE_SUFFIXES = ASSET_SUFFIXES | MODEL_SUFFIXES | {".aar", ".jar", ".lic"}
NO_SEPARATE_LICENSE_BASES = {
    "user-confirmed-no-separate-license",
    "delivery-confirmed-no-separate-license",
}


def iter_files(root: Path) -> Iterable[Path]:
    resolved_root = root.resolve()
    for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        directories[:] = [name for name in directories if name not in SKIP_DIRS]
        for filename in filenames:
            path = current_path / filename
            try:
                if not path.resolve(strict=True).is_relative_to(resolved_root):
                    continue
            except OSError:
                continue
            yield path


def iter_authorized_resource_files(paths: Iterable[Path]) -> Iterable[Path]:
    """Yield only resource candidates from explicitly supplied files or directories."""
    for supplied_path in paths:
        if supplied_path.is_file():
            if supplied_path.suffix.lower() in RESOURCE_FILE_SUFFIXES:
                yield supplied_path
            continue
        for path in iter_files(supplied_path):
            if path.suffix.lower() in RESOURCE_FILE_SUFFIXES:
                yield path


def safe_read(path: Path) -> str:
    if path.name.lower() in SENSITIVE_NAMES:
        return ""
    if path.suffix.lower() not in SAFE_TEXT_SUFFIXES:
        return ""
    try:
        if path.stat().st_size > MAX_TEXT_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def rel(path: Path, root: Path) -> str:
    try:
        value = path.relative_to(root).as_posix()
        return value or "."
    except ValueError:
        return path.name


def scoped_resource_path(path: Path, project_root: Path, resource_paths: list[Path]) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        pass
    for index, supplied_path in enumerate(resource_paths, start=1):
        label = f"provided-resource-{index}"
        if supplied_path.is_file() and path == supplied_path:
            return f"{label}/{path.name}"
        if supplied_path.is_dir():
            try:
                relative = path.relative_to(supplied_path).as_posix()
                return f"{label}/{relative}"
            except ValueError:
                continue
    return path.name


def unique(values: Iterable[str]) -> list[str]:
    return sorted({value for value in values if value})


def parse_settings(settings_text: str) -> list[str]:
    modules: set[str] = set()
    pattern = r"(?ms)^\s*include\s*(?:\((.*?)\)|([^\r\n]+))"
    for match in re.finditer(pattern, settings_text):
        declaration = match.group(1) if match.group(1) is not None else match.group(2)
        for module in re.findall(r"['\"](:[^'\"]+)['\"]", declaration or ""):
            modules.add(module)
    return sorted(modules)


def version_catalog_plugin_aliases(root: Path) -> dict[str, str]:
    """Return conventional libs.plugins accessors without loading Gradle or TOML libraries."""
    aliases: dict[str, str] = {}
    catalog_dir = root / "gradle"
    if not catalog_dir.is_dir():
        return aliases
    for path in sorted(catalog_dir.glob("*.versions.toml")):
        text = safe_read(path)
        section = re.search(r"(?ms)^\s*\[plugins\]\s*$\s*(.*?)(?=^\s*\[|\Z)", text)
        if not section:
            continue
        for match in re.finditer(
            r"(?m)^\s*([A-Za-z0-9_.-]+)\s*=\s*\{[^\r\n}]*\bid\s*=\s*['\"]([^'\"]+)['\"]",
            section.group(1),
        ):
            accessor = re.sub(r"[-_.]+", ".", match.group(1))
            aliases[accessor] = match.group(2)
    return aliases


def android_plugin(text: str, aliases: dict[str, str]) -> str:
    plugin_ids = {
        "com.android.application": "application",
        "com.android.library": "library",
        "com.android.dynamic-feature": "dynamic-feature",
    }
    for plugin_id, kind in plugin_ids.items():
        if plugin_id in text:
            return kind
    for accessor in re.findall(r"\balias\s*\(\s*libs\.plugins\.([A-Za-z0-9_.]+)\s*\)", text):
        plugin_id = aliases.get(accessor)
        if plugin_id in plugin_ids:
            return plugin_ids[plugin_id]
    return "unknown"


def abi_filters(text: str) -> list[str]:
    values: list[str] = []
    for match in re.finditer(
        r"\babiFilters?\s*(?:\+=|=)?\s*(?:(?:listOf|setOf)\s*)?\((.*?)\)",
        text,
        re.DOTALL,
    ):
        values.extend(re.findall(r"[\"']([^\"']+)[\"']", match.group(1)))
    for match in re.finditer(r"(?m)\babiFilters?\s+(?![+=])([^\r\n}]+)", text):
        values.extend(re.findall(r"[\"']([^\"']+)[\"']", match.group(1)))
    return unique(values)


def scalar(text: str, key: str) -> str | None:
    patterns = [
        rf"\b{re.escape(key)}\s*(?:=\s*)?[\"']([^\"']+)[\"']",
        rf"\b{re.escape(key)}\s*(?:=\s*)?([0-9]+)",
        rf"\b{re.escape(key)}\s+(rootProject\.[^\s,}}]+)",
        rf"\b{re.escape(key)}\s*=\s*([^\s,}}]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def root_gradle_values(root: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in sorted(root.glob("*.gradle*")):
        text = safe_read(path)
        for key, value in re.findall(r"\b([A-Za-z][A-Za-z0-9_]*)\s*:\s*[\"']?([0-9.]+)[\"']?", text):
            values[key] = value
        for key, value in re.findall(r"\b([A-Za-z][A-Za-z0-9_]*)\s*=\s*[\"']?([0-9.]+)[\"']?", text):
            values.setdefault(key, value)
    return values


def resolve_scalar(value: str | None, values: dict[str, str]) -> str | None:
    if value is None:
        return None
    key = value.rsplit(".", 1)[-1]
    return values.get(key, value)


def variant_suffix(text: str, variant: str | None) -> str | None:
    if not variant:
        return None
    match = re.search(rf"(?s)\b{re.escape(variant)}\s*\{{([^{{}}]*)\}}", text)
    if not match:
        return None
    return scalar(match.group(1), "applicationIdSuffix")


def merged_manifest_application_id(module_dir: Path, variant: str | None) -> str | None:
    if not variant:
        return None
    candidates = (
        module_dir / "build" / "intermediates" / "merged_manifest" / variant,
        module_dir / "build" / "intermediates" / "merged_manifests" / variant,
        module_dir / "build" / "intermediates" / "packaged_manifests" / variant,
    )
    for directory in candidates:
        if not directory.exists():
            continue
        for manifest in directory.rglob("AndroidManifest.xml"):
            try:
                text = manifest.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            match = re.search(r"<manifest\b[^>]*\bpackage=[\"']([^\"']+)", text)
            if match:
                return match.group(1)
    return None


def detect_module(
    module: str,
    module_dir: Path,
    root: Path,
    root_values: dict[str, str],
    variant: str | None,
    plugin_aliases: dict[str, str],
) -> dict[str, Any]:
    gradle = module_dir / "build.gradle"
    gradle_kts = module_dir / "build.gradle.kts"
    build_file = gradle_kts if gradle_kts.exists() else gradle
    text = safe_read(build_file) if build_file.exists() else ""
    plugin = android_plugin(text, plugin_aliases)

    application_id = scalar(text, "applicationId")
    selected_suffix = variant_suffix(text, variant)
    merged_application_id = merged_manifest_application_id(module_dir, variant)
    computed_application_id = (
        f"{application_id}{selected_suffix}"
        if application_id and selected_suffix
        else application_id
    )
    resolved_application_id = merged_application_id or computed_application_id
    application_id_suffixes = unique(
        re.findall(r"\bapplicationIdSuffix\s*(?:=\s*)?[\"']([^\"']+)[\"']", text)
    )
    application_id_suffix_present = re.search(r"\bapplicationIdSuffix\b", text) is not None
    if application_id_suffix_present:
        application_id_resolution = "variant-dependent"
    elif application_id:
        application_id_resolution = "base-only"
    else:
        application_id_resolution = "unknown"

    source_root = module_dir / "src"
    java_count = 0
    kotlin_count = 0
    if source_root.exists():
        for source in iter_files(source_root):
            java_count += source.suffix.lower() == ".java"
            kotlin_count += source.suffix.lower() == ".kt"

    return {
        "name": module,
        "path": rel(module_dir, root),
        "build_file": rel(build_file, root) if build_file.exists() else None,
        "dsl": "kotlin" if build_file.suffix == ".kts" else "groovy" if build_file.exists() else "unknown",
        "plugin": plugin,
        "namespace": scalar(text, "namespace"),
        "application_id": application_id,
        "selected_variant": variant,
        "resolved_application_id": resolved_application_id,
        "variant_resolution": (
            "resolved-from-merged-manifest"
            if merged_application_id
            else "resolved-base-and-build-type-suffix"
            if selected_suffix
            else "base-only-no-matching-suffix"
            if variant and application_id
            else "not-requested"
        ),
        "application_id_suffixes": application_id_suffixes,
        "application_id_resolution": application_id_resolution,
        "compile_sdk": resolve_scalar(
            scalar(text, "compileSdk") or scalar(text, "compileSdkVersion"), root_values
        ),
        "min_sdk": resolve_scalar(
            scalar(text, "minSdk") or scalar(text, "minSdkVersion"), root_values
        ),
        "target_sdk": resolve_scalar(
            scalar(text, "targetSdk") or scalar(text, "targetSdkVersion"), root_values
        ),
        "abi_filters": abi_filters(text),
        "languages": {"java": java_count, "kotlin": kotlin_count},
    }


def _jar_contains_nve_effect_kit(data: bytes) -> bool:
    if len(data) > 64 * 1024 * 1024:
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as classes:
            class_name = "com/meishe/nveffectkit/NveEffectKit.class"
            if class_name not in classes.namelist():
                return False
            info = classes.getinfo(class_name)
            if info.file_size < 4 or info.file_size > 16 * 1024 * 1024:
                return False
            with classes.open(info) as stream:
                return stream.read(4) == b"\xca\xfe\xba\xbe"
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile):
        return False


def inspect_aar(path: Path, root: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": rel(path, root),
        "embedded_streaming_sdk": False,
        "embedded_jars": [],
        "abis": [],
        "valid_aar": False,
        "contains_nve_effect_kit": False,
        "sha256": None,
        "baseline_match": False,
    }
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        result["sha256"] = digest.hexdigest()
        result["baseline_match"] = result["sha256"] == BASELINE_AAR_SHA256
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            lowered = [name.lower() for name in names]
            result["valid_aar"] = "AndroidManifest.xml" in names and "classes.jar" in names
            result["embedded_streaming_sdk"] = any(
                name.endswith("/nvandroidstreamingsdk.jar") or name == "nvandroidstreamingsdk.jar"
                for name in lowered
            )
            result["embedded_jars"] = sorted(
                name for name in names if name.lower().endswith(".jar")
            )
            for jar_name in result["embedded_jars"]:
                try:
                    if archive.getinfo(jar_name).file_size > 64 * 1024 * 1024:
                        continue
                    if _jar_contains_nve_effect_kit(archive.read(jar_name)):
                        result["contains_nve_effect_kit"] = True
                        break
                except (KeyError, OSError):
                    continue
            abis = []
            for name in names:
                match = re.match(r"(?:jni/)?([^/]+)/[^/]+\.so$", name)
                if match and match.group(1) in {
                    "arm64-v8a",
                    "armeabi-v7a",
                    "x86",
                    "x86_64",
                }:
                    abis.append(match.group(1))
            result["abis"] = unique(abis)
    except (OSError, RuntimeError, zipfile.BadZipFile):
        result["read_error"] = True
    return result


def is_nve_effect_kit_aar(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if "AndroidManifest.xml" not in names or "classes.jar" not in names:
                return False
            for jar_name in (name for name in names if name.lower().endswith(".jar")):
                if archive.getinfo(jar_name).file_size > 64 * 1024 * 1024:
                    continue
                if _jar_contains_nve_effect_kit(archive.read(jar_name)):
                    return True
            return False
    except (OSError, RuntimeError, zipfile.BadZipFile, KeyError):
        return False


def analyze_sources(files: list[Path], root: Path) -> dict[str, Any]:
    markers: dict[str, tuple[str, ...]] = {
        "CameraX": ("androidx.camera", "ImageAnalysis", "PreviewView"),
        "Camera2": ("android.hardware.camera2", "CameraDevice", "CameraCaptureSession"),
        "Camera1": ("android.hardware.Camera", "Camera.PreviewCallback"),
        "GLSurfaceView": ("GLSurfaceView", "onDrawFrame("),
        "OpenGL/EGL": ("GLES20", "GLES30", "EGLContext", "GL_TEXTURE_EXTERNAL_OES"),
    }
    render_markers: dict[str, tuple[str, ...]] = {
        "OES texture": ("NV_OES_TEXTURE", "GL_TEXTURE_EXTERNAL_OES", "SurfaceTexture"),
        "2D texture": ("NV_COMMON_TEXTURE",),
        "NV21 buffer": ("NV21", "VIDEO_FRAME_PIXEL_FROMAT_NV21"),
        "YUV_420_888 buffer": ("YUV_420_888",),
        "RGBA buffer": ("RGBA", "VIDEO_FRAME_PIXEL_FROMAT_RGBA"),
        "TEXTURE_TEXTURE": ("NveRenderMode.TEXTURE_TEXTURE",),
        "BUFFER_TEXTURE": ("NveRenderMode.BUFFER_TEXTURE",),
        "BUFFER_BUFFER": ("NveRenderMode.BUFFER_BUFFER",),
    }
    tech_hits: dict[str, set[str]] = {key: set() for key in markers}
    render_hits: dict[str, set[str]] = {key: set() for key in render_markers}
    integration_points: list[dict[str, str]] = []
    nve_files: set[str] = set()
    model_type_clues: dict[str, set[str]] = {}
    responsibility_hits: dict[str, set[str]] = {
        "camera-callback-thread": set(),
        "gl-render-thread": set(),
        "yuv-stride-conversion": set(),
        "timestamp-pairing": set(),
        "direction-and-mirror": set(),
        "bounded-latest-frame": set(),
    }
    ui_controls: set[str] = set()
    test_files: set[str] = set()

    for path in files:
        text = safe_read(path)
        if not text:
            continue
        relative = rel(path, root)
        if "/test/" in f"/{relative}" or "/androidTest/" in f"/{relative}":
            test_files.add(relative)
        for control_id in re.findall(r"\bR\.id\.([A-Za-z][A-Za-z0-9_]*)", text):
            ui_controls.add(control_id)
        if "View.generateViewId(" in text:
            ui_controls.add("dynamic-generated-id:not-stable-for-automation")
        for description in re.findall(r"setContentDescription\s*\(\s*[\"']([^\"']+)", text):
            ui_controls.add(f"contentDescription:{description}")
        if re.search(r"\b(?:Nve[A-Z][A-Za-z0-9_]*|NvsEffectSdkContext|NvsVideoEffect)\b|com\.meishe\.nveffectkit", text):
            nve_files.add(relative)
        for model_type in re.findall(r"\bNveDetectionModelType\.([A-Z][A-Z0-9_]*)", text):
            model_type_clues.setdefault(model_type, set()).add(relative)
        for name, tokens in markers.items():
            if any(token in text for token in tokens):
                tech_hits[name].add(relative)
        for name, tokens in render_markers.items():
            if any(token in text for token in tokens):
                render_hits[name].add(relative)
        reasons = []
        if "renderEffect(" in text:
            reasons.append("NveEffectKit renderEffect")
            has_output_texture = "getTexture(" in text
            has_output_consumer = any(
                token in text
                for token in (
                    "drawOes(",
                    "draw2D(",
                    "drawToCurrentSurface(",
                    "onFinalTexture(",
                )
            )
            if (
                has_output_texture
                and has_output_consumer
                and "recycleOutput(" in text
            ):
                reasons.append("NVE final output consumption")
        if "onDrawFrame(" in text:
            reasons.append("GL draw callback")
        if "analyze(" in text and "ImageProxy" in text:
            reasons.append("CameraX analyzer")
        if "onImageAvailable(" in text:
            reasons.append("ImageReader callback")
        if "onFrameAvailable(" in text:
            reasons.append("SurfaceTexture callback")
        if reasons:
            integration_points.append({"path": relative, "reason": ", ".join(reasons)})
        responsibility_tokens = {
            "camera-callback-thread": ("HandlerThread", "onImageAvailable(", "ImageAnalysis.Analyzer"),
            "gl-render-thread": ("onDrawFrame(", "queueEvent(", "EGLContext"),
            "yuv-stride-conversion": ("rowStride", "pixelStride"),
            "timestamp-pairing": ("timestampNs", "getTimestamp("),
            "direction-and-mirror": ("SENSOR_ORIENTATION", "setDisplayRotation", "setMirror", "verticalMirror"),
            "bounded-latest-frame": ("acquireLatestImage", "ArrayBlockingQueue", "removeFirst", "pollLast"),
        }
        for name, tokens in responsibility_tokens.items():
            if any(token in text for token in tokens):
                responsibility_hits[name].add(relative)

    return {
        "camera_and_graphics": {
            key: sorted(value) for key, value in tech_hits.items() if value
        },
        "frame_and_render_modes": {
            key: sorted(value) for key, value in render_hits.items() if value
        },
        "nve_source_files": sorted(nve_files),
        "model_type_clues": {
            key: sorted(value) for key, value in sorted(model_type_clues.items())
        },
        "candidate_integration_points": sorted(
            integration_points, key=lambda item: (item["path"], item["reason"])
        )[:30],
        "responsibility_map": {
            key: sorted(value) for key, value in responsibility_hits.items() if value
        },
        "ui_control_entries": sorted(ui_controls),
        "test_capabilities": {
            "source_files": sorted(test_files),
            "has_unit_tests": any("/test/" in f"/{path}" for path in test_files),
            "has_android_tests": any("/androidTest/" in f"/{path}" for path in test_files),
        },
    }


def inspect_project(
    root: Path,
    variant: str | None = None,
    features: Iterable[str] = (),
    files: Iterable[Path] | None = None,
    resource_paths: Iterable[Path] = (),
    asset_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    files = list(files) if files is not None else list(iter_files(root))
    resource_paths = [path.expanduser().resolve() for path in resource_paths]
    provided_resource_files = list(iter_authorized_resource_files(resource_paths))
    resource_candidate_files = list(dict.fromkeys([*files, *provided_resource_files]))
    project_file_set = {path.resolve() for path in files}

    def is_project_file(path: Path) -> bool:
        return path.resolve() in project_file_set

    def candidate_path(path: Path) -> str:
        return scoped_resource_path(path.resolve(), root.resolve(), resource_paths)

    def candidate_status(candidates: list[Path]) -> str:
        if any(is_project_file(path) for path in candidates):
            return "project-local-candidates-unverified"
        if candidates:
            return "user-provided-path-candidates-unverified"
        return "not-found"

    plugin_aliases = version_catalog_plugin_aliases(root)
    settings_files = [
        path for path in (root / "settings.gradle", root / "settings.gradle.kts") if path.exists()
    ]
    declared_modules: list[str] = []
    for settings in settings_files:
        declared_modules.extend(parse_settings(safe_read(settings)))
    declared_modules = unique(declared_modules)

    module_entries: list[tuple[str, Path]] = []
    for module in declared_modules:
        module_path = root.joinpath(*module.lstrip(":").split(":"))
        if (module_path / "build.gradle").exists() or (module_path / "build.gradle.kts").exists():
            module_entries.append((module, module_path))
    if not module_entries:
        for build_file in sorted(
            path for path in files if path.name in {"build.gradle", "build.gradle.kts"}
        ):
            text = safe_read(build_file)
            if android_plugin(text, plugin_aliases) != "unknown":
                path = build_file.parent
                name = ":" + rel(path, root).replace("/", ":")
                module_entries.append((name, path))

    root_values = root_gradle_values(root)
    modules = [
        detect_module(name, path, root, root_values, variant, plugin_aliases)
        for name, path in module_entries
    ]
    all_aar_files = [
        path for path in resource_candidate_files if path.suffix.lower() == ".aar"
    ]
    aar_files = [path for path in all_aar_files if is_nve_effect_kit_aar(path)]
    invalid_named_aar_candidates = [
        path
        for path in all_aar_files
        if re.search(r"nve?effectkit", path.name.lower()) and path not in aar_files
    ]
    standalone_streaming = [
        candidate_path(path)
        for path in resource_candidate_files
        if path.suffix.lower() == ".jar" and "nvandroidstreamingsdk" in path.name.lower()
    ]
    licenses = [path for path in resource_candidate_files if path.suffix.lower() == ".lic"]
    models = [
        path for path in resource_candidate_files if path.suffix.lower() in MODEL_SUFFIXES
    ]
    effect_assets = [
        path for path in resource_candidate_files if path.suffix.lower() in ASSET_SUFFIXES
    ]
    asset_map = dict(asset_map or {})
    source_files = [path for path in files if path.suffix.lower() in {".java", ".kt"}]
    source_analysis = analyze_sources(source_files, root)
    nve_dependency_declarations = []
    for path in files:
        if path.suffix.lower() not in {".gradle", ".kts", ".toml"}:
            continue
        text = safe_read(path)
        for line in text.splitlines():
            lowered = line.lower()
            is_gradle_dependency = any(
                token in lowered
                for token in ("implementation", "api(", "compileonly", "runtimeonly")
            )
            is_catalog_dependency = path.suffix.lower() == ".toml" and any(
                token in lowered for token in ("module", "group", "name")
            )
            if (is_gradle_dependency or is_catalog_dependency) and re.search(r"nve.?effect.?kit|nveffectkit", lowered):
                nve_dependency_declarations.append({"path": rel(path, root), "declaration": line.strip()[:300]})

    language_counts = Counter()
    for module in modules:
        language_counts.update(module["languages"])

    resource_source_status = {
        "license_files": candidate_status(licenses),
        "models": candidate_status(models),
        "effect_assets": candidate_status(effect_assets),
    }

    feature_contract = contract_report(features)
    requires_nve_prerequisites = feature_contract.get(
        "requires_nve_prerequisites", True
    )
    inspected_aars = [
        {**inspect_aar(path, root), "path": candidate_path(path)} for path in aar_files
    ]
    available_resource_paths = {
        candidate_path(path).replace("\\", "/").lower()
        for path in resource_candidate_files
    }
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    recording_requested = "recording" in feature_contract.get(
        "implementation_features", ()
    )
    has_nve_final_output = any(
        "NVE final output consumption" in item.get("reason", "")
        for item in source_analysis.get("candidate_integration_points", ())
        if isinstance(item, dict)
    )
    if recording_requested and not has_nve_final_output:
        finding = {
            "code": "NVE_FINAL_OUTPUT_SOURCE_MISSING",
            "message": (
                "未发现现有 NveEffectKit renderEffect 最终输出消费点；"
                "recording 只录制 NVE 最终输出，不覆盖通用相机录制。"
            ),
        }
        if requires_nve_prerequisites:
            warnings.append(
                {
                    **finding,
                    "code": "RECORDING_WAITS_FOR_NVE_FINAL_OUTPUT",
                }
            )
        else:
            blockers.append(finding)
    valid_asset_map_features = (
        set(feature_contract.get("candidate_baseline_asset_files", {}))
        | set(feature_contract.get("required_asset_contracts", {}))
    )
    for feature in sorted(set(asset_map) - valid_asset_map_features):
        warnings.append(
            {
                "code": "UNUSED_ASSET_MAP_ENTRY",
                "feature": feature,
                "message": f"素材映射项不属于当前功能范围：{feature}",
            }
        )
    unresolved_scope = [
        feature
        for feature in feature_contract.get("scope_selection_required", [])
        if not (
            feature in feature_contract.get("required_asset_contracts", {})
            and feature in asset_map
        )
    ]
    if unresolved_scope:
        blockers.append(
            {
                "code": "FEATURE_SCOPE_UNRESOLVED",
                "features": unresolved_scope,
                "message": (
                    "功能范围仍是分类/套件，不能默认按登记 catalog 全量接入；"
                    "必须先让用户选择完整范围、单一分类或具体调节项；"
                    "资源目录和“按需取用”不代表功能选择。收到答复后再重新执行门禁。"
                ),
                "selection_requests": feature_contract.get(
                    "selection_requests", []
                ),
            }
        )
    if requires_nve_prerequisites and not inspected_aars:
        blockers.append(
            {
                "code": "NVE_AAR_MISSING",
                "message": "未找到可验证的 NveEffectKit AAR。",
            }
        )
    elif (
        requires_nve_prerequisites
        and len({item.get("sha256") for item in inspected_aars}) > 1
    ):
        blockers.append(
            {
                "code": "MULTIPLE_NVE_AARS",
                "message": "检测到多个内容不同的 NveEffectKit AAR，不能确定唯一运行时基线。",
            }
        )
    elif requires_nve_prerequisites and len(inspected_aars) > 1:
        warnings.append(
            {
                "code": "DUPLICATE_IDENTICAL_AAR_CANDIDATES",
                "message": "发现同一 AAR 的多个相同副本；仅保留目标模块实际依赖的一份。",
            }
        )
    baseline_match = bool(inspected_aars) and all(
        item.get("baseline_match") is True for item in inspected_aars
    )
    scoped_effect_assets = {
        candidate_path(path).replace("\\", "/"): path for path in effect_assets
    }
    scoped_license_files = {
        candidate_path(path).replace("\\", "/"): path for path in licenses
    }
    resolved_asset_map: dict[str, dict[str, Any]] = {}

    def scoped_matches(
        declared: str, candidates: dict[str, Path]
    ) -> list[str]:
        normalized = declared.replace("\\", "/").strip().lower()
        return [
            scoped
            for scoped in candidates
            if scoped.lower() == normalized
            or scoped.lower().endswith("/" + normalized)
        ]

    def resolve_declared_asset(feature: str, declared: str, expected: str) -> None:
        matches = scoped_matches(declared, scoped_effect_assets)
        if not matches:
            blockers.append(
                {
                    "code": "DECLARED_ASSET_NOT_FOUND",
                    "feature": feature,
                    "declared": declared,
                    "message": f"{feature} 声明的素材不在已授权资源范围内。",
                }
            )
            return
        if len(matches) > 1:
            blockers.append(
                {
                    "code": "DECLARED_ASSET_AMBIGUOUS",
                    "feature": feature,
                    "declared": declared,
                    "message": f"{feature} 的素材路径匹配多个文件，请使用唯一相对路径。",
                }
            )
            return
        resolved = matches[0]
        expected_suffix = Path(expected).suffix.lower()
        actual_suffix = Path(resolved).suffix.lower()
        if actual_suffix != expected_suffix:
            blockers.append(
                {
                    "code": "DECLARED_ASSET_TYPE_MISMATCH",
                    "feature": feature,
                    "declared": declared,
                    "expected_suffix": expected_suffix,
                    "message": (
                        f"{feature} 素材类型错误：需要 {expected_suffix}，"
                        f"实际为 {actual_suffix or 'unknown'}。"
                    ),
                }
            )
            return
        resolved_asset_map[feature] = {
            "path": resolved,
            "source": "declared-map",
        }

    if requires_nve_prerequisites and baseline_match:
        for model_type, required_path in feature_contract[
            "baseline_model_files"
        ].items():
            normalized = required_path.replace("\\", "/").lower()
            if not any(path.endswith(normalized) for path in available_resource_paths):
                blockers.append(
                    {
                        "code": "REQUIRED_BASELINE_MODEL_MISSING",
                        "model_type": model_type,
                        "required": required_path,
                        "message": f"{model_type} 所需基线模型缺失。",
                    }
                )
    for feature, reference_filename in (
        feature_contract["required_baseline_asset_files"].items()
        if requires_nve_prerequisites
        else ()
    ):
        declared = asset_map.get(feature)
        if declared:
            if not isinstance(declared, str):
                blockers.append(
                    {
                        "code": "ASSET_MAP_VALUE_INVALID",
                        "feature": feature,
                        "message": f"{feature} 只接受单个素材路径字符串。",
                    }
                )
            else:
                resolve_declared_asset(feature, declared, reference_filename)
            continue
        normalized = reference_filename.lower()
        official_match = next(
            (
                path
                for path in scoped_effect_assets
                if path.lower().endswith("/" + normalized)
                or path.lower() == normalized
            ),
            None,
        )
        if baseline_match and official_match:
            resolved_asset_map[feature] = {
                "path": official_match,
                "source": "registered-baseline-reference",
            }
            continue
        blockers.append(
            {
                "code": "REQUIRED_ASSET_MAPPING_MISSING",
                "feature": feature,
                "reference": reference_filename,
                "message": (
                    f"{feature} 未找到可确认的素材映射；可提供自制/更新素材，"
                    "但必须显式声明该调节项对应的已授权资源路径。"
                ),
            }
        )

    for feature, requirement in feature_contract.get(
        "required_asset_contracts", {}
    ).items():
        declared_value = asset_map.get(feature)
        if declared_value is None:
            blockers.append(
                {
                    "code": "REQUIRED_ASSET_MAPPING_MISSING",
                    "feature": feature,
                    "expected_suffixes": requirement["extensions"],
                    "message": (
                        f"{feature} 需要显式素材映射；不能用工程中任意同扩展名素材"
                        "或其他模块组成包代替。"
                    ),
                }
            )
            continue

        two_layer_mapping = (
            requirement.get("mapping_model") == "packages-display-items"
            and isinstance(declared_value, dict)
            and (
                "packages" in declared_value
                or "display_items" in declared_value
            )
        )
        display_entries: list[Any] = []
        raw_entries: list[Any]
        if two_layer_mapping:
            packages_value = declared_value.get("packages")
            display_value = declared_value.get("display_items")
            if not isinstance(packages_value, list) or not packages_value:
                blockers.append(
                    {
                        "code": "ASSET_PACKAGE_MAP_EMPTY",
                        "feature": feature,
                        "message": f"{feature} 的 packages 必须是非空数组。",
                    }
                )
                continue
            if not isinstance(display_value, list) or not display_value:
                blockers.append(
                    {
                        "code": "FILTER_DISPLAY_ITEMS_EMPTY",
                        "feature": feature,
                        "message": (
                            f"{feature} 的 display_items 必须是非空数组。"
                        ),
                    }
                )
                continue
            raw_entries = packages_value
            display_entries = display_value
        elif isinstance(declared_value, list):
            raw_entries = declared_value
        else:
            raw_entries = [declared_value]
        if not raw_entries:
            blockers.append(
                {
                    "code": "ASSET_MAP_EMPTY",
                    "feature": feature,
                    "message": f"{feature} 的素材映射不能为空。",
                }
            )
            continue
        if not requirement.get("multiple") and len(raw_entries) != 1:
            blockers.append(
                {
                    "code": "ASSET_MAP_MULTIPLE_NOT_ALLOWED",
                    "feature": feature,
                    "message": f"{feature} 只允许映射一个素材。",
                }
            )
            continue

        resolved_items: list[dict[str, Any]] = []
        seen_assets: set[str] = set()
        resolved_packages_by_key: dict[str, dict[str, Any]] = {}
        seen_package_keys: set[str] = set()
        for index, raw_entry in enumerate(raw_entries):
            entry = {"asset": raw_entry} if isinstance(raw_entry, str) else raw_entry
            if not isinstance(entry, dict) or not isinstance(
                entry.get("asset"), str
            ):
                blockers.append(
                    {
                        "code": "ASSET_MAP_VALUE_INVALID",
                        "feature": feature,
                        "index": index,
                        "message": (
                            f"{feature} 第 {index + 1} 项必须是素材路径字符串，"
                            "或至少含 asset 字符串的对象。"
                        ),
                    }
                )
                continue

            package_key: str | None = None
            if two_layer_mapping:
                raw_key = entry.get("key")
                if not isinstance(raw_key, str) or not raw_key.strip():
                    blockers.append(
                        {
                            "code": "ASSET_PACKAGE_KEY_MISSING",
                            "feature": feature,
                            "index": index,
                            "message": (
                                f"{feature} 第 {index + 1} 个 package "
                                "缺少非空 key。"
                            ),
                        }
                    )
                    continue
                package_key = raw_key.strip()
                if package_key in seen_package_keys:
                    blockers.append(
                        {
                            "code": "DUPLICATE_ASSET_PACKAGE_KEY",
                            "feature": feature,
                            "index": index,
                            "key": package_key,
                            "message": (
                                f"{feature} 重复声明 package key："
                                f"{package_key}。"
                            ),
                        }
                    )
                    continue
                seen_package_keys.add(package_key)

            declared_asset = entry["asset"]
            asset_matches = scoped_matches(
                declared_asset, scoped_effect_assets
            )
            if not asset_matches:
                blockers.append(
                    {
                        "code": "DECLARED_ASSET_NOT_FOUND",
                        "feature": feature,
                        "index": index,
                        "declared": declared_asset,
                        "message": (
                            f"{feature} 第 {index + 1} 项素材不在已授权资源范围内。"
                        ),
                    }
                )
                continue
            if len(asset_matches) > 1:
                blockers.append(
                    {
                        "code": "DECLARED_ASSET_AMBIGUOUS",
                        "feature": feature,
                        "index": index,
                        "declared": declared_asset,
                        "message": (
                            f"{feature} 第 {index + 1} 项素材匹配多个文件，"
                            "请使用唯一相对路径。"
                        ),
                    }
                )
                continue
            resolved_asset = asset_matches[0]
            actual_suffix = Path(resolved_asset).suffix.lower()
            if actual_suffix not in requirement["extensions"]:
                blockers.append(
                    {
                        "code": "DECLARED_ASSET_TYPE_MISMATCH",
                        "feature": feature,
                        "index": index,
                        "declared": declared_asset,
                        "expected_suffixes": requirement["extensions"],
                        "message": (
                            f"{feature} 第 {index + 1} 项素材类型错误："
                            f"需要 {','.join(requirement['extensions'])}，"
                            f"实际为 {actual_suffix or 'unknown'}。"
                        ),
                    }
                )
                continue
            normalized_asset = resolved_asset.lower()
            if normalized_asset in seen_assets:
                blockers.append(
                    {
                        "code": "DUPLICATE_ASSET_MAPPING",
                        "feature": feature,
                        "index": index,
                        "declared": declared_asset,
                        "message": (
                            f"{feature} 多个展示项映射到同一素材：{resolved_asset}。"
                        ),
                    }
                )
                continue
            seen_assets.add(normalized_asset)

            resolved_license: str | None = None
            resolved_license_basis: str | None = None
            if requirement.get("license_policy") == "explicit":
                if "license" not in entry:
                    blockers.append(
                        {
                            "code": "ASSET_LICENSE_STATUS_UNRESOLVED",
                            "feature": feature,
                            "index": index,
                            "asset": resolved_asset,
                            "message": (
                                f"{feature} 第 {index + 1} 项未声明素材 lic；"
                                "提供 lic 路径，或在正式交付确认无需独立 lic 后显式写 null。"
                            ),
                        }
                    )
                    continue
                declared_license = entry.get("license")
                if declared_license is not None:
                    if not isinstance(declared_license, str) or not declared_license.strip():
                        blockers.append(
                            {
                                "code": "ASSET_LICENSE_VALUE_INVALID",
                                "feature": feature,
                                "index": index,
                                "message": (
                                    "素材 license 必须是非空路径字符串或 null。"
                                ),
                            }
                        )
                        continue
                    license_matches = scoped_matches(
                        declared_license, scoped_license_files
                    )
                    if not license_matches:
                        blockers.append(
                            {
                                "code": "DECLARED_ASSET_LICENSE_NOT_FOUND",
                                "feature": feature,
                                "index": index,
                                "declared": declared_license,
                                "message": (
                                    f"{feature} 第 {index + 1} 项声明的素材 lic "
                                    "不在已授权资源范围内。"
                                ),
                            }
                        )
                        continue
                    if len(license_matches) > 1:
                        blockers.append(
                            {
                                "code": "DECLARED_ASSET_LICENSE_AMBIGUOUS",
                                "feature": feature,
                                "index": index,
                                "declared": declared_license,
                                "message": (
                                    f"{feature} 第 {index + 1} 项素材 lic "
                                    "匹配多个文件，请使用唯一相对路径。"
                                ),
                            }
                        )
                        continue
                    resolved_license = license_matches[0]
                    resolved_license_basis = (
                        entry.get("license_basis")
                        if isinstance(entry.get("license_basis"), str)
                        else "file-provided"
                    )
                else:
                    basis = entry.get("license_basis")
                    if two_layer_mapping and basis not in NO_SEPARATE_LICENSE_BASES:
                        blockers.append(
                            {
                                "code": "ASSET_LICENSE_BASIS_MISSING",
                                "feature": feature,
                                "index": index,
                                "asset": resolved_asset,
                                "message": (
                                    f"{feature} 第 {index + 1} 个 package "
                                    "声明 license:null 时必须记录有效的 "
                                    "license_basis。"
                                ),
                            }
                        )
                        continue
                    if not two_layer_mapping and basis is None:
                        warnings.append(
                            {
                                "code": "ASSET_LICENSE_BASIS_UNRECORDED",
                                "feature": feature,
                                "index": index,
                                "asset": resolved_asset,
                                "message": (
                                    f"{feature} 使用旧版 license:null；"
                                    "建议迁移到两层映射并记录确认来源。"
                                ),
                            }
                        )
                    resolved_license_basis = (
                        basis if isinstance(basis, str) else None
                    )

            resolved_item = {
                "asset": resolved_asset,
                "license": resolved_license,
            }
            if package_key is not None:
                resolved_item["key"] = package_key
            if resolved_license_basis is not None:
                resolved_item["license_basis"] = resolved_license_basis
            for key in ("id", "name"):
                if isinstance(entry.get(key), str) and entry[key].strip():
                    resolved_item[key] = entry[key].strip()
            resolved_items.append(resolved_item)
            if package_key is not None:
                resolved_packages_by_key[package_key] = resolved_item

        if resolved_items:
            resolution: dict[str, Any] = {
                "source": "declared-map",
                "items": resolved_items,
                "count": len(resolved_items),
            }
            if two_layer_mapping:
                resolved_display_items: list[dict[str, Any]] = []
                seen_display_ids: set[str] = set()
                for index, raw_display in enumerate(display_entries):
                    if not isinstance(raw_display, dict):
                        blockers.append(
                            {
                                "code": "FILTER_DISPLAY_ITEM_INVALID",
                                "feature": feature,
                                "index": index,
                                "message": (
                                    f"{feature} 第 {index + 1} 个 "
                                    "display item 必须是对象。"
                                ),
                            }
                        )
                        continue
                    display_id = raw_display.get("id")
                    display_name = raw_display.get("name")
                    display_package = raw_display.get("package")
                    if not all(
                        isinstance(value, str) and value.strip()
                        for value in (
                            display_id,
                            display_name,
                            display_package,
                        )
                    ):
                        blockers.append(
                            {
                                "code": "FILTER_DISPLAY_ITEM_INVALID",
                                "feature": feature,
                                "index": index,
                                "message": (
                                    f"{feature} 第 {index + 1} 个 "
                                    "display item 必须包含非空 id、name、package。"
                                ),
                            }
                        )
                        continue
                    display_id = display_id.strip()
                    display_name = display_name.strip()
                    display_package = display_package.strip()
                    if display_id in seen_display_ids:
                        blockers.append(
                            {
                                "code": "DUPLICATE_FILTER_DISPLAY_ID",
                                "feature": feature,
                                "index": index,
                                "id": display_id,
                                "message": (
                                    f"{feature} 重复声明展示 ID："
                                    f"{display_id}。"
                                ),
                            }
                        )
                        continue
                    seen_display_ids.add(display_id)
                    package = resolved_packages_by_key.get(display_package)
                    if package is None:
                        blockers.append(
                            {
                                "code": "FILTER_DISPLAY_PACKAGE_UNRESOLVED",
                                "feature": feature,
                                "index": index,
                                "package": display_package,
                                "message": (
                                    f"{feature} 展示项 {display_name} 引用的 "
                                    f"package 不存在或未通过门禁："
                                    f"{display_package}。"
                                ),
                            }
                        )
                        continue
                    resolved_display = {
                        "id": display_id,
                        "name": display_name,
                        "package": display_package,
                        "asset": package["asset"],
                        "license": package["license"],
                    }
                    if isinstance(raw_display.get("cover"), str):
                        resolved_display["cover"] = raw_display[
                            "cover"
                        ].strip()
                    fit_ratio = raw_display.get("fit_ratio")
                    if isinstance(fit_ratio, int) and not isinstance(
                        fit_ratio, bool
                    ):
                        resolved_display["fit_ratio"] = fit_ratio
                    resolved_display_items.append(resolved_display)
                resolution.update(
                    {
                        "mapping_model": "packages-display-items",
                        "packages": resolved_items,
                        "package_count": len(resolved_items),
                        "display_items": resolved_display_items,
                        "display_item_count": len(resolved_display_items),
                    }
                )
            resolved_asset_map[feature] = resolution

    mapped_asset_license_paths = {
        item["license"].replace("\\", "/").lower()
        for resolution in resolved_asset_map.values()
        for item in resolution.get("items", [])
        if item.get("license")
    }
    sdk_licenses = [
        path
        for path in licenses
        if candidate_path(path).replace("\\", "/").lower()
        not in mapped_asset_license_paths
    ]
    asset_licenses = [
        path
        for path in licenses
        if candidate_path(path).replace("\\", "/").lower()
        in mapped_asset_license_paths
    ]
    if requires_nve_prerequisites and not sdk_licenses:
        blockers.append(
            {
                "code": "LICENSE_FILE_MISSING",
                "message": (
                    "未找到独立的 SDK .lic；已映射的滤镜/素材 lic "
                    "不能替代 SDK 初始化授权。"
                ),
            }
        )
    elif requires_nve_prerequisites:
        warnings.append(
            {
                "code": "LICENSE_BINDING_UNVERIFIED",
                "message": (
                    "已找到 SDK lic 候选，但仍须验证其与最终 applicationId 的绑定。"
                ),
            }
        )

    available_asset_features: dict[str, str] = {}
    unavailable_asset_features: dict[str, str] = {}
    if requires_nve_prerequisites and baseline_match:
        for feature, filename in feature_contract.get(
            "candidate_baseline_asset_files", {}
        ).items():
            normalized = filename.lower()
            target = (
                available_asset_features
                if any(
                    path.endswith("/" + normalized) or path == normalized
                    for path in available_resource_paths
                )
                else unavailable_asset_features
            )
            target[feature] = filename
    elif (
        requires_nve_prerequisites
        and inspected_aars
        and feature_contract["models"]
    ):
        warnings.append(
            {
                "code": "BASELINE_RESOURCE_CONTRACT_UNAVAILABLE",
                "message": "AAR 与注册基线不匹配，不能套用基线模型/素材文件名；必须先取得该 AAR 的官方配套清单。",
            }
        )
    for feature, resolution in resolved_asset_map.items():
        if "items" in resolution:
            if resolution.get("mapping_model") == "packages-display-items":
                available_asset_features[feature] = (
                    f"{resolution['package_count']} mapped package(s), "
                    f"{resolution['display_item_count']} display item(s)"
                )
            else:
                available_asset_features[feature] = (
                    f"{resolution['count']} mapped item(s)"
                )
        else:
            available_asset_features[feature] = resolution["path"]
        unavailable_asset_features.pop(feature, None)

    return {
        "schema_version": 9,
        "project": root.name,
        "variant": variant,
        "scan_scope": {
            "project_root": root.name,
            "provided_resource_paths": [
                {
                    "label": f"provided-resource-{index}",
                    "kind": "file" if path.is_file() else "directory",
                }
                for index, path in enumerate(resource_paths, start=1)
            ],
            "implicit_parent_or_workspace_scan": False,
        },
        "feature_contract": feature_contract,
        "prerequisite_gate": {
            "status": "blocked" if blockers else "pass-with-runtime-verification",
            "baseline_match": baseline_match,
            "blockers": blockers,
            "warnings": warnings,
            "available_asset_features": available_asset_features,
            "unavailable_optional_asset_features": unavailable_asset_features,
            "resolved_asset_map": resolved_asset_map,
        },
        "settings_files": [rel(path, root) for path in settings_files],
        "modules": modules,
        "languages": dict(sorted(language_counts.items())),
        "sdk_artifacts": {
            "nve_effect_kit_aars": inspected_aars,
            "invalid_named_aar_candidates": [
                {**inspect_aar(path, root), "path": candidate_path(path)}
                for path in invalid_named_aar_candidates
            ],
            "standalone_streaming_sdk_jars": sorted(standalone_streaming),
            "dependency_declarations": nve_dependency_declarations,
        },
        "resources": {
            "license_file_count": len(sdk_licenses),
            "asset_license_file_count": len(asset_licenses),
            "model_file_count": len(models),
            "license_files": sorted(candidate_path(path) for path in sdk_licenses),
            "asset_license_files": sorted(
                candidate_path(path) for path in asset_licenses
            ),
            "model_files": sorted(candidate_path(path) for path in models),
            "effect_asset_files": sorted(candidate_path(path) for path in effect_assets),
            "effect_asset_counts": dict(
                sorted(Counter(path.suffix.lower() for path in effect_assets).items())
            ),
            "source_status": resource_source_status,
        },
        **source_analysis,
    }


def render_text(report: dict[str, Any]) -> str:
    lines = [f"Project: {report['project']}"]
    scope = report["scan_scope"]
    lines.append(
        "Scan scope: project only"
        + (
            f", provided resource paths={len(scope['provided_resource_paths'])}"
            if scope["provided_resource_paths"]
            else ", no provided external resource paths"
        )
    )
    lines.append(f"Settings: {', '.join(report['settings_files']) or 'not found'}")
    lines.append("Modules:")
    for module in report["modules"]:
        languages = ", ".join(
            f"{name}={count}" for name, count in module["languages"].items() if count
        ) or "no sources"
        lines.append(
            "  - {name} ({plugin}, {dsl}, minSdk={min_sdk}, ABI={abis}, {languages})".format(
                name=module["name"],
                plugin=module["plugin"],
                dsl=module["dsl"],
                min_sdk=module["min_sdk"] or "unknown",
                abis=",".join(module["abi_filters"]) or "unrestricted",
                languages=languages,
            )
        )
        app_id = module["application_id"] or "unknown"
        suffixes = ",".join(module["application_id_suffixes"]) or "none"
        lines.append(
            f"    applicationId={app_id}, suffixes={suffixes}, "
            f"resolution={module['application_id_resolution']}"
        )
        if report.get("variant"):
            lines.append(
                f"    variant={report['variant']}, resolvedApplicationId="
                f"{module['resolved_application_id'] or 'unknown'}, "
                f"resolution={module['variant_resolution']}"
            )
    artifacts = report["sdk_artifacts"]
    lines.append(f"NveEffectKit AARs: {len(artifacts['nve_effect_kit_aars'])}")
    for aar in artifacts["nve_effect_kit_aars"]:
        lines.append(
            f"  - {aar['path']} (embedded Streaming SDK={aar['embedded_streaming_sdk']}, "
            f"ABI={','.join(aar['abis']) or 'unknown'}, SHA-256={aar['sha256']}, "
            f"baseline match={aar['baseline_match']})"
        )
    invalid_candidates = artifacts.get("invalid_named_aar_candidates", [])
    lines.append(f"Invalid named NveEffectKit AAR candidates: {len(invalid_candidates)}")
    for candidate in invalid_candidates:
        lines.append(
            f"  - {candidate['path']} (valid AAR={candidate.get('valid_aar', False)}, "
            f"contains NveEffectKit={candidate.get('contains_nve_effect_kit', False)})"
        )
    lines.append(
        "Standalone NvAndroidStreamingSdk JARs: "
        + (", ".join(artifacts["standalone_streaming_sdk_jars"]) or "none")
    )
    lines.append(f"Nve dependency declarations: {len(artifacts['dependency_declarations'])}")
    resources = report["resources"]
    lines.append(
        f"Resources: SDK lic={resources['license_file_count']}, "
        f"asset lic={resources.get('asset_license_file_count', 0)}, "
        f"models={resources['model_file_count']}, "
        f"effect assets={sum(resources['effect_asset_counts'].values())}"
    )
    lines.append(
        "Resource source status: "
        + ", ".join(f"{key}={value}" for key, value in resources["source_status"].items())
    )
    contract = report["feature_contract"]
    lines.append(
        "Required model order: "
        + (" -> ".join(contract["model_init_order"]) or "none")
    )
    for model_type, path in contract["baseline_model_files"].items():
        lines.append(f"  - model {model_type}: {path}")
    for feature, path in contract["required_baseline_asset_files"].items():
        lines.append(f"  - required asset {feature}: {path}")
    gate = report["prerequisite_gate"]
    lines.append(f"Prerequisite gate: {gate['status']}")
    for item in gate["blockers"]:
        lines.append(f"  - BLOCKER {item['code']}: {item['message']}")
    for item in gate["warnings"]:
        lines.append(f"  - VERIFY {item['code']}: {item['message']}")
    if gate["available_asset_features"]:
        lines.append(
            "  - Available optional asset items: "
            + ", ".join(gate["available_asset_features"])
        )
    for feature, resolution in gate["resolved_asset_map"].items():
        if "items" in resolution:
            if resolution.get("mapping_model") == "packages-display-items":
                lines.append(
                    f"  - resolved assets {feature}: "
                    f"{resolution['package_count']} package(s), "
                    f"{resolution['display_item_count']} display item(s) "
                    f"({resolution['source']})"
                )
            else:
                lines.append(
                    f"  - resolved assets {feature}: "
                    f"{resolution['count']} item(s) "
                    f"({resolution['source']})"
                )
            for item in resolution["items"]:
                label = (
                    item.get("key")
                    or item.get("name")
                    or item.get("id")
                    or item["asset"]
                )
                license_status = (
                    item["license"] if item["license"] is not None
                    else item.get(
                        "license_basis", "no-separate-license-confirmed"
                    )
                )
                lines.append(
                    f"      {label}: {item['asset']}, lic={license_status}"
                )
            for item in resolution.get("display_items", []):
                lines.append(
                    f"      display {item['name']} -> {item['package']}"
                )
        else:
            lines.append(
                f"  - resolved asset {feature}: {resolution['path']} "
                f"({resolution['source']})"
            )
    model_types = ", ".join(report["model_type_clues"].keys()) or "none"
    lines.append(f"Model type clues: {model_types}")
    lines.append(
        "Camera/graphics: " + (", ".join(report["camera_and_graphics"].keys()) or "not detected")
    )
    lines.append(
        "Frame/render clues: " + (", ".join(report["frame_and_render_modes"].keys()) or "not detected")
    )
    lines.append("Candidate integration points:")
    if report["candidate_integration_points"]:
        for item in report["candidate_integration_points"]:
            lines.append(f"  - {item['path']}: {item['reason']}")
    else:
        lines.append("  - none detected")
    lines.append(
        "Responsibilities: " + (", ".join(report["responsibility_map"].keys()) or "not detected")
    )
    lines.append(
        "UI automation entries: "
        + (", ".join(report["ui_control_entries"][:20]) or "not detected")
    )
    tests = report["test_capabilities"]
    lines.append(
        f"Tests: unit={tests['has_unit_tests']}, android={tests['has_android_tests']}, "
        f"files={len(tests['source_files'])}"
    )
    return "\n".join(lines)


def valid_asset_map_value(value: Any) -> bool:
    def valid_entry(entry: Any) -> bool:
        if isinstance(entry, str):
            return bool(entry.strip())
        if not isinstance(entry, dict) or not isinstance(entry.get("asset"), str):
            return False
        if not entry["asset"].strip():
            return False
        if "license" in entry and entry["license"] is not None:
            if not isinstance(entry["license"], str) or not entry["license"].strip():
                return False
        return all(
            key not in entry
            or entry[key] is None
            or isinstance(entry[key], str)
            for key in ("id", "name", "key", "license_basis")
        )

    def valid_display_item(entry: Any) -> bool:
        if not isinstance(entry, dict):
            return False
        if not all(
            isinstance(entry.get(key), str) and entry[key].strip()
            for key in ("id", "name", "package")
        ):
            return False
        if "cover" in entry and (
            not isinstance(entry["cover"], str)
            or not entry["cover"].strip()
        ):
            return False
        if "fit_ratio" in entry and (
            not isinstance(entry["fit_ratio"], int)
            or isinstance(entry["fit_ratio"], bool)
        ):
            return False
        return True

    if isinstance(value, dict) and (
        "packages" in value or "display_items" in value
    ):
        packages = value.get("packages")
        display_items = value.get("display_items")
        return (
            isinstance(packages, list)
            and bool(packages)
            and all(
                valid_entry(entry)
                and isinstance(entry, dict)
                and isinstance(entry.get("key"), str)
                and bool(entry["key"].strip())
                for entry in packages
            )
            and isinstance(display_items, list)
            and bool(display_items)
            and all(valid_display_item(entry) for entry in display_items)
        )
    if isinstance(value, list):
        return bool(value) and all(valid_entry(entry) for entry in value)
    return valid_entry(value)


def load_asset_map(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"Invalid asset map: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid asset map JSON: {error}") from error
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and valid_asset_map_value(value)
        for key, value in payload.items()
    ):
        raise ValueError(
            "Asset map must map string feature IDs to a non-empty path, "
            "an {asset, license, id?, name?} object, or a non-empty list "
            "of those entries; packaged filters may instead use "
            "{packages:[{key,asset,license,license_basis?}], "
            "display_items:[{id,name,package,cover?,fit_ratio?}]}."
        )
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only inventory of an Android project for NveEffectKit integration."
    )
    parser.add_argument("project_root", help="Android project root")
    parser.add_argument("--variant", default=None, help="variant/build type to resolve (for example debug)")
    parser.add_argument("--features", default="", help="comma-separated feature IDs or aliases")
    parser.add_argument(
        "--resource-path",
        action="append",
        default=[],
        help="user-authorized resource file or directory; repeat for multiple explicit paths",
    )
    parser.add_argument(
        "--asset-map",
        default=None,
        help=(
            "path to a JSON file mapping exact feature IDs to one asset path, "
            "a legacy list of {asset, license, id?, name?}, or packaged-filter "
            "{packages, display_items} entries within the project or an "
            "authorized --resource-path"
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--gate",
        action="store_true",
        help="fail before implementation when required AAR/license/models/assets are missing",
    )
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
    features = [item.strip() for item in args.features.split(",") if item.strip()]
    unknown = unknown_features(features)
    if unknown:
        print("Unknown feature(s): " + ", ".join(unknown), file=sys.stderr)
        return 2
    resource_paths: list[Path] = []
    for raw_path in args.resource_path:
        resource_path = Path(raw_path).expanduser().resolve()
        if not resource_path.exists():
            print(f"Invalid authorized resource path: {raw_path}", file=sys.stderr)
            return 2
        resource_paths.append(resource_path)
    asset_map: dict[str, Any] = {}
    if args.asset_map:
        asset_map_path = Path(args.asset_map).expanduser().resolve()
        try:
            asset_map = load_asset_map(asset_map_path)
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2
    report = inspect_project(
        root,
        args.variant,
        features,
        resource_paths=resource_paths,
        asset_map=asset_map,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_text(report))
    if args.gate and report["prerequisite_gate"]["blockers"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
