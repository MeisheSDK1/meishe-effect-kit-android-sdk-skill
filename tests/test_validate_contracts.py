from __future__ import annotations

import io
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
import uuid
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
TEST_WORK_ROOT = Path(tempfile.gettempdir()) / "nve-effect-kit-skill-tests"
sys.path.insert(0, str(SCRIPT_DIR))

from inspect_android_project import inspect_project, iter_files  # noqa: E402
from inspect_filter_catalog import inspect_catalog  # noqa: E402
from inspect_makeup_catalog import inspect_makeup_catalog  # noqa: E402
from feature_contract import (  # noqa: E402
    FEATURE_ALIASES,
    FEATURE_GROUPS,
    beauty_controller_groups,
    contract_report,
    expand_features,
    implementation_features,
    normalize_requested_features,
)
from validate_nve_integration import code_only, source_documents, validate  # noqa: E402
from verify_nve_stages import REQUIRED, validate_stage_evidence  # noqa: E402
from verify_nve_delivery import (  # noqa: E402
    apk_contract_blockers,
    apk_report,
    device_smoke_gate,
    expected_apk_asset_entries,
    gradle_wrapper,
    install_apk,
    logcat_since_epoch,
    main as verify_main,
    redact_text,
    relevant_logcat,
    resolve_process_ids,
    sanitize_report,
    validate_manual_evidence,
    validate_cli_values,
)


GOOD_SOURCE = r"""
package sample;
class Integration {
  boolean renderCoreReady, capabilityReady, desiredEnabled;
  float desiredDegree;
  Object CameraDevice, CameraCaptureSession;
  void init() {
    boolean licensed = NveEffectKit.getInstance().init(context, license);
    boolean face = NveEffectKit.getInstance().initModel(
        context, NveDetectionModelType.FACE, faceModel);
    boolean faceCommon = NveEffectKit.getInstance().initModel(
        context, NveDetectionModelType.FACE_COMMON, faceCommonModel);
    boolean advancedBeauty = NveEffectKit.getInstance().initModel(
        context, NveDetectionModelType.ADVANCED_BEAUTY, advancedBeautyModel);
    runOnUiThread(() -> NveEffectKit.getInstance().initialize());
    NveEffectKit.getInstance().setBeauty(beauty);
    NveEffectKit.getInstance().setShape(shape);
    NveEffectKit.getInstance().setMicroShape(microShape);
    beauty.setBlurType(BuffingSkin);
    control.setId(View.generateViewId());
    control.setContentDescription("nve-toggle");
    control.setEnabled(renderCoreReady && capabilityReady);
    control.setOnClickListener(v -> {
      desiredEnabled = true;
      if (!renderCoreReady || !capabilityReady) return;
      beauty.setEnable(desiredEnabled);
    });
    seek.setOnSeekBarChangeListener(listener);
  }
  void onProgressChanged(SeekBar seekBar, int progress, boolean fromUser) {
    desiredDegree = progress / 100.0f;
    if (!renderCoreReady || !capabilityReady) return;
    shape.setDegreeByType(FACE_SIZE, desiredDegree);
    beauty.setBlurIntensity(desiredDegree);
  }
  void onDrawFrame() {
    int displayWidth = rotation % 180 == 0 ? cameraWidth : cameraHeight;
    int displayHeight = rotation % 180 == 0 ? cameraHeight : cameraWidth;
    NveTexture inputTexture = new NveTexture();
    inputTexture.setSize(displayWidth, displayHeight);
    inputTexture.setCameraOrientation(sensorOrientation);
    inputTexture.setMirror(frontCamera);
    NveImageBuffer detectionBuffer = new NveImageBuffer();
    detectionBuffer.setSize(cameraWidth, cameraHeight);
    detectionBuffer.setDisplayRotation(displayRotation);
    detectionBuffer.setMirror(frontCamera);
    NveRenderOutput output = null;
    try {
      output = NveEffectKit.getInstance().renderEffect(inputTexture, detectionBuffer);
      if (output == null || output.getErrorCode() != NO_ERROR) { drawRawOes(); return; }
      restoreHostGlState();
      Object outputSize = output.getTexture().getSize();
      if (output.getTexture().getTextureType() == NV_OES_TEXTURE) drawOes(); else draw2D();
    } finally {
      if (output != null) NveEffectKit.getInstance().recycleOutput(output);
    }
  }
  void frames() {
    Object image = acquireLatestImage();
    try {
      int a = rowStride + pixelStride + MAX_PENDING;
      long delta = Math.abs(textureTimestampNs - bufferTimestampNs);
      int b = VIDEO_FRAME_PIXEL_FROMAT_NV21 + NV21 + GL_TEXTURE_EXTERNAL_OES;
    } finally { image.close(); }
  }
  void pause() { queueEvent(() -> NveEffectKit.getInstance().cleanUp()); }
  void restore() { NveEffectKit.getInstance().setBeauty(beauty); }
  void finish() { NveEffectKit.getInstance().destroy(); }
  void restoreHostGlState() {} void drawRawOes() {} void drawOes() {} void draw2D() {}
}
"""

GOOD_RECORDING_SOURCE = r"""
package sample;
class RecordingController {
  enum State { IDLE, STARTING, RECORDING, STOPPING }
  static final int FRAME_SLOT_COUNT = 3;
  Object queue = new java.util.concurrent.ArrayBlockingQueue<>(FRAME_SLOT_COUNT);
  Object worker = new android.os.HandlerThread("recording-egl");
  long firstCameraTimestampNs;
  void configure() {
    int source = android.media.MediaRecorder.VideoSource.SURFACE;
    String path = android.provider.MediaStore.Video.Media.EXTERNAL_CONTENT_URI.toString();
    String relative = android.provider.MediaStore.MediaColumns.RELATIVE_PATH;
    String pending = android.provider.MediaStore.MediaColumns.IS_PENDING;
    Object fd = descriptor.getFileDescriptor();
    queue.offer(frame);
    android.opengl.EGLExt.eglPresentationTimeANDROID(display, surface,
        System.nanoTime() + frameTimestampNs - firstCameraTimestampNs);
    drawable.setShape(android.graphics.drawable.GradientDrawable.OVAL);
    button.setContentDescription("录制视频");
    button.setContentDescription("停止录制");
    toast("录制完成，已保存到手机相册");
    String album = android.os.Environment.DIRECTORY_DCIM + "/Camera";
  }
  void onPause() { stopRecording(); }
  void stopRecording() {}
}
"""

NVE_FINAL_OUTPUT_SOURCE = r"""
package sample;
class ExistingNvePipeline {
  void renderFrame() {
    Object output =
        NveEffectKit.getInstance().renderEffect(inputTexture, detectionBuffer);
    Object finalTexture = output.getTexture();
    draw2D(finalTexture);
    NveEffectKit.getInstance().recycleOutput(output);
  }
}
"""


def write_valid_nve_aar(path: Path) -> None:
    classes_buffer = io.BytesIO()
    with zipfile.ZipFile(classes_buffer, "w") as classes:
        classes.writestr("com/meishe/nveffectkit/NveEffectKit.class", b"\xca\xfe\xba\xbesynthetic")
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as aar:
        aar.writestr("AndroidManifest.xml", '<manifest package="synthetic.nve"/>')
        aar.writestr("classes.jar", classes_buffer.getvalue())
        aar.writestr("jni/arm64-v8a/libNvStreamingSdkCore.so", b"synthetic")


def make_project(root: Path, source: str = GOOD_SOURCE) -> None:
    (root / "settings.gradle").write_text("include ':app'\n", encoding="utf-8")
    app = root / "app"
    app.mkdir(parents=True, exist_ok=True)
    (app / "build.gradle").write_text(
        "plugins { id 'com.android.application' }\n"
        "android { compileSdk 35\n defaultConfig { applicationId 'sample.app'; minSdk 23; targetSdk 35 }\n"
        "buildTypes { debug { applicationIdSuffix '.debug' } } }\n",
        encoding="utf-8",
    )
    java = app / "src/main/java/sample/Integration.java"
    java.parent.mkdir(parents=True, exist_ok=True)
    java.write_text(source, encoding="utf-8")
    write_valid_nve_aar(app / "libs/NveEffectKit.aar")


def write_nve_final_output_source(root: Path) -> None:
    path = root / "app/src/main/java/sample/ExistingNvePipeline.java"
    path.write_text(NVE_FINAL_OUTPUT_SOURCE, encoding="utf-8")


class ContractValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_WORK_ROOT.mkdir(parents=True, exist_ok=True)
        self.root = TEST_WORK_ROOT / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        make_project(self.root)

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def codes(self, source: str, severity: str = "ERROR", profile: str = "camera2-oes-nv21") -> set[str]:
        make_project(self.root, source)
        return {item.code for item in validate(self.root, profile, strict=True) if item.severity == severity}

    def test_good_contract_has_no_static_errors(self) -> None:
        self.assertEqual(set(), self.codes(GOOD_SOURCE))

    def test_beauty_alias_expands_to_official_three_part_suite(self) -> None:
        self.assertEqual(["beauty-suite"], normalize_requested_features(["beauty"]))
        self.assertEqual(["beauty-suite"], normalize_requested_features(["美颜"]))
        self.assertEqual(
            ["beauty-skin", "beauty-shape", "beauty-micro-shape"],
            expand_features(["beauty"]),
        )
        self.assertEqual(
            ["beauty-skin"],
            contract_report(["美肤"])["scope_selection_required"],
        )
        self.assertEqual(
            [],
            contract_report(["beauty-skin-full"])["scope_selection_required"],
        )

    def test_recording_is_exact_host_capability_without_nve_prerequisites(self) -> None:
        report = contract_report(["录制"])
        self.assertEqual(["recording"], report["requested_features"])
        self.assertEqual(["recording"], report["implementation_features"])
        self.assertEqual([], report["scope_selection_required"])
        self.assertEqual(["references/recording.md"], report["references"])
        self.assertEqual([], report["models"])
        self.assertEqual([], report["asset_types"])
        self.assertEqual(["recording"], report["host_capabilities"])
        self.assertEqual([], report["nve_capabilities"])
        self.assertFalse(report["requires_nve_prerequisites"])
        self.assertEqual("host-only", report["prerequisite_mode"])
        self.assertEqual(
            "bottom-center-action-no-panel", report["ui_behaviors"][0]
        )

    def test_recording_existing_output_is_not_blocked_by_nve_resources(
        self,
    ) -> None:
        write_nve_final_output_source(self.root)
        (self.root / "app/libs/NveEffectKit.aar").unlink()
        report = inspect_project(self.root, features=["recording"])
        codes = {
            item["code"] for item in report["prerequisite_gate"]["blockers"]
        }
        self.assertEqual("pass-with-runtime-verification", report["prerequisite_gate"]["status"])
        self.assertNotIn("NVE_AAR_MISSING", codes)
        self.assertNotIn("LICENSE_FILE_MISSING", codes)
        self.assertEqual("host-only", report["feature_contract"]["prerequisite_mode"])

    def test_recording_without_existing_nve_final_output_is_blocked(
        self,
    ) -> None:
        make_project(self.root, GOOD_RECORDING_SOURCE)
        (self.root / "app/libs/NveEffectKit.aar").unlink()
        report = inspect_project(self.root, features=["recording"])
        codes = {
            item["code"] for item in report["prerequisite_gate"]["blockers"]
        }
        self.assertEqual("blocked", report["prerequisite_gate"]["status"])
        self.assertIn("NVE_FINAL_OUTPUT_SOURCE_MISSING", codes)

    def test_recording_rejects_bare_render_call_without_output_consumption(
        self,
    ) -> None:
        make_project(
            self.root,
            "class PartialPipeline { void frame() { "
            "NveEffectKit.getInstance().renderEffect(input, buffer); } }",
        )
        (self.root / "app/libs/NveEffectKit.aar").unlink()
        report = inspect_project(self.root, features=["recording"])
        self.assertIn(
            "NVE_FINAL_OUTPUT_SOURCE_MISSING",
            {
                item["code"]
                for item in report["prerequisite_gate"]["blockers"]
            },
        )

    def test_recording_mixed_with_effects_keeps_nve_resource_gate(self) -> None:
        (self.root / "app/libs/NveEffectKit.aar").unlink()
        report = inspect_project(
            self.root,
            features=["recording", "beauty-skin-buffing-skin"],
        )
        codes = {
            item["code"] for item in report["prerequisite_gate"]["blockers"]
        }
        self.assertIn("NVE_AAR_MISSING", codes)
        self.assertIn("LICENSE_FILE_MISSING", codes)
        self.assertEqual("mixed", report["feature_contract"]["prerequisite_mode"])

    def test_recording_validator_accepts_complete_host_contract_without_aar(
        self,
    ) -> None:
        make_project(self.root, GOOD_RECORDING_SOURCE)
        write_nve_final_output_source(self.root)
        (self.root / "app/libs/NveEffectKit.aar").unlink()
        findings = validate(
            self.root,
            profile="custom-gl",
            strict=True,
            features=["recording"],
        )
        errors = {item.code for item in findings if item.severity == "ERROR"}
        self.assertEqual(set(), errors)
        self.assertIn(
            "HOST_CAPABILITY_STATIC_CHECK_PASSED",
            {item.code for item in findings},
        )

    def test_recording_validator_rejects_private_only_minimal_implementation(
        self,
    ) -> None:
        make_project(
            self.root,
            "class RecordingController { void startRecording() { new MediaRecorder(); } }",
        )
        (self.root / "app/libs/NveEffectKit.aar").unlink()
        errors = {
            item.code
            for item in validate(
                self.root,
                profile="custom-gl",
                strict=True,
                features=["recording"],
            )
            if item.severity == "ERROR"
        }
        self.assertIn("RECORDING_MEDIASTORE_OUTPUT_MISSING", errors)
        self.assertIn("RECORDING_TIMESTAMP_REBASE_MISSING", errors)
        self.assertIn("RECORDING_BOUNDED_QUEUE_MISSING", errors)
        self.assertIn("NVE_FINAL_OUTPUT_SOURCE_MISSING", errors)

    def test_recording_module_cannot_run_a_second_effect_render(self) -> None:
        make_project(
            self.root,
            GOOD_RECORDING_SOURCE.replace(
                "class RecordingController {",
                "class RecordingController implements NveFinalTextureFrameSink {",
            ).replace(
                "void configure() {",
                "void rerender() { renderEffect(input, buffer); }\n"
                "  void configure() {",
            ),
        )
        errors = {
            item.code
            for item in validate(
                self.root,
                profile="custom-gl",
                strict=True,
                features=["recording"],
            )
            if item.severity == "ERROR"
        }
        self.assertIn("RECORDING_SECOND_EFFECT_RENDER_FORBIDDEN", errors)

    def test_combined_screen_may_render_once_and_feed_recording(self) -> None:
        source = GOOD_RECORDING_SOURCE.replace(
            "class RecordingController {",
            "class CombinedPreviewScreen {",
        ).replace(
            "void configure() {",
            "void renderPreview() {"
            " Object output = NveEffectKit.getInstance().renderEffect(input);"
            " Object texture = output.getTexture();"
            " draw2D(texture);"
            " NveEffectKit.getInstance().recycleOutput(output);"
            " }\n"
            "  void configure() {",
        )
        make_project(self.root, source)
        (self.root / "app/libs/NveEffectKit.aar").unlink()
        errors = {
            item.code
            for item in validate(
                self.root,
                profile="custom-gl",
                strict=True,
                features=["recording"],
            )
            if item.severity == "ERROR"
        }
        self.assertNotIn("RECORDING_SECOND_EFFECT_RENDER_FORBIDDEN", errors)
        self.assertNotIn("NVE_FINAL_OUTPUT_SOURCE_MISSING", errors)

    def test_unqualified_makeup_requires_explicit_scope_selection(self) -> None:
        report = contract_report(["美妆"])
        self.assertEqual(["makeup-suite"], report["scope_selection_required"])
        self.assertEqual(
            [
                "makeup-full",
                "makeup-single-full",
                "makeup-compose",
                "custom-leaf-features",
            ],
            [
                option["id"]
                for option in report["selection_requests"][0]["options"]
            ],
        )
        self.assertEqual(
            [],
            contract_report(["完整美妆"])["scope_selection_required"],
        )

    def test_beauty_contract_includes_exact_models_order_and_assets(self) -> None:
        report = contract_report(["beauty"])
        self.assertEqual(
            ["FACE", "FACE_COMMON", "ADVANCED_BEAUTY"],
            report["model_init_order"],
        )
        self.assertEqual(
            "facemodel/ms/ms_face240_v4.0.3.next.model",
            report["baseline_model_files"]["FACE"],
        )
        self.assertEqual(
            "B0B7A240-48B9-4983-B2C8-690FFA7211EB.2.facemesh",
            report["baseline_asset_files"]["beauty-shape-eye-angle"],
        )
        self.assertEqual(
            "54B2B9B4-5A7A-484C-B602-39A4730115A0.4.facemesh",
            report["baseline_asset_files"]["beauty-micro-shape-eye-angle"],
        )
        self.assertEqual({}, report["required_baseline_asset_files"])
        self.assertEqual(["beauty-suite"], report["scope_selection_required"])
        self.assertEqual(
            [
                "beauty-full",
                "beauty-skin-full",
                "beauty-shape-full",
                "beauty-micro-shape-full",
                "custom-leaf-features",
            ],
            [
                option["id"]
                for option in report["selection_requests"][0]["options"]
            ],
        )

        leaf = contract_report(["beauty-shape-face-size"])
        self.assertEqual(
            {
                "beauty-shape-face-size":
                    "63BD3F32-D01B-4755-92D5-0DE361E4045A.3.facemesh"
            },
            leaf["required_baseline_asset_files"],
        )
        self.assertEqual([], leaf["scope_selection_required"])
        self.assertEqual([], leaf["selection_requests"])

        full = contract_report(["完整美颜"])
        self.assertEqual("explicit-full", full["scope_policy"])
        self.assertEqual(
            full["candidate_baseline_asset_files"],
            full["required_baseline_asset_files"],
        )

    def test_requested_beauty_rejects_missing_face_model_init(self) -> None:
        source = GOOD_SOURCE.replace(
            "    boolean face = NveEffectKit.getInstance().initModel(\n"
            "        context, NveDetectionModelType.FACE, faceModel);\n",
            "",
        )
        make_project(self.root, source)
        errors = {
            item.code
            for item in validate(
                self.root,
                "camera2-oes-nv21",
                strict=True,
                features=["beauty"],
            )
            if item.severity == "ERROR"
        }
        self.assertIn("REQUIRED_MODEL_INIT_MISSING", errors)

    def test_requested_beauty_rejects_wrong_model_init_order(self) -> None:
        original = (
            "    boolean face = NveEffectKit.getInstance().initModel(\n"
            "        context, NveDetectionModelType.FACE, faceModel);\n"
            "    boolean faceCommon = NveEffectKit.getInstance().initModel(\n"
            "        context, NveDetectionModelType.FACE_COMMON, faceCommonModel);\n"
        )
        reversed_order = (
            "    boolean faceCommon = NveEffectKit.getInstance().initModel(\n"
            "        context, NveDetectionModelType.FACE_COMMON, faceCommonModel);\n"
            "    boolean face = NveEffectKit.getInstance().initModel(\n"
            "        context, NveDetectionModelType.FACE, faceModel);\n"
        )
        make_project(self.root, GOOD_SOURCE.replace(original, reversed_order))
        errors = {
            item.code
            for item in validate(
                self.root,
                "camera2-oes-nv21",
                strict=True,
                features=["beauty"],
            )
            if item.severity == "ERROR"
        }
        self.assertIn("REQUIRED_MODEL_INIT_ORDER_INVALID", errors)

    def test_explicit_beauty_leaf_stays_in_requested_subset(self) -> None:
        feature = "beauty-shape-face-size"
        self.assertEqual([feature], expand_features([feature]))
        self.assertEqual({"shape"}, beauty_controller_groups([feature]))

    def test_catalog_covers_all_stable_categories_and_aliases(self) -> None:
        for feature in (
            "beauty-suite", "beauty-skin", "beauty-shape", "beauty-micro-shape",
            "makeup-suite", "makeup-single", "makeup-compose", "filter-packaged",
            "face-prop", "custom-effect", "custom-effect-background-segmentation",
        ):
            self.assertIn(feature, FEATURE_GROUPS)
        self.assertEqual(["makeup-single", "makeup-compose"], expand_features(["makeup"]))
        self.assertEqual(["filter-packaged", "face-prop"], expand_features(["filter", "prop"]))
        self.assertEqual(
            ["filter-packaged", "face-prop", "custom-effect"],
            expand_features(["filters", "face-props", "custom-effects"]),
        )
        self.assertEqual(
            ["makeup-single", "makeup-compose", "filter-packaged", "face-prop"],
            expand_features(["美妆", "滤镜", "人脸道具"]),
        )
        self.assertIn("makeup-single-eyeball", implementation_features(["美妆"]))
        self.assertEqual(44, len(implementation_features(["美颜"])))

    def test_machine_catalog_matches_feature_router_item_scope(self) -> None:
        catalog_path = (
            SCRIPT_DIR.parent
            / "assets"
            / "baseline-3.16.1"
            / "catalog"
            / "feature-catalog.json"
        )
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        host_contract = json.loads(
            (
                SCRIPT_DIR.parent / "assets" / "host-capabilities.json"
            ).read_text(encoding="utf-8")
        )
        catalog_features = {
            *(f"beauty-skin-{item['id']}" for item in catalog["beauty"]["skin"]),
            *(f"beauty-shape-{item['id']}" for item in catalog["beauty"]["shape"]),
            *(
                f"beauty-micro-shape-{item['id']}"
                for item in catalog["beauty"]["micro_shape"]
            ),
            *(
                f"makeup-single-{item['id']}"
                for item in catalog["makeup"]["single"]
            ),
            "makeup-compose",
        }
        routed = set(implementation_features(["美颜", "美妆"]))
        self.assertEqual(catalog_features, routed)
        self.assertEqual(
            {
                **catalog["feature_contract"]["aliases"],
                **host_contract["aliases"],
            },
            FEATURE_ALIASES,
        )
        self.assertEqual(
            {
                *catalog["feature_contract"]["capabilities"],
                *host_contract["capabilities"],
            },
            {
                feature
                for feature, spec in FEATURE_GROUPS.items()
                if "parent" not in spec
            },
        )

    def test_contract_emits_deterministic_ui_modules(self) -> None:
        report = contract_report(["美颜", "单妆", "滤镜", "道具", "背景分割"])
        self.assertEqual(
            ["beauty", "custom-effect", "face-prop", "filter", "makeup"],
            report["ui_modules"],
        )
        self.assertEqual("bottom-toggle-per-module", report["default_ui"]["entry"])
        self.assertEqual(
            "candidate-types-confirm-against-target-aar-and-metadata",
            report["resource_semantics"],
        )
        self.assertIn("filter-packaged", report["scope_selection_required"])
        self.assertEqual(
            {
                "extensions": [".videofx"],
                "multiple": True,
                "license_policy": "explicit",
                "mapping_model": "packages-display-items",
            },
            report["required_asset_contracts"]["filter-packaged"],
        )
        self.assertNotIn(
            "filter-packaged",
            contract_report(["完整滤镜"])["scope_selection_required"],
        )

    def test_stage_gate_requires_previous_runtime_stages(self) -> None:
        payload = {
            "stages": {
                "raw-preview": {
                    "observed": True,
                    "frames_present": True,
                    "non_black": False,
                    "viewport_correct": True,
                    "direction_correct": True,
                },
                "no-effect": {},
            }
        }
        missing = validate_stage_evidence(
            payload, "no-effect", ["beauty-skin-buffing-skin"]
        )
        self.assertIn("stages.raw-preview.non_black", missing)
        self.assertIn("stages.no-effect.observed", missing)

    def test_stage_gate_blocks_flash_and_geometry_jump_without_evidence(self) -> None:
        payload = {
            "stages": {
                stage: {field: True for field in fields}
                for stage, fields in REQUIRED.items()
            }
        }
        payload["stages"]["no-effect"]["no_flash"] = False
        payload["stages"]["single-effect"]["no_geometry_jump_on_enable"] = False
        missing = validate_stage_evidence(
            payload, "single-effect", ["beauty-shape-face-size"]
        )
        self.assertIn("stages.no-effect.no_flash", missing)
        self.assertIn(
            "stages.single-effect.no_geometry_jump_on_enable", missing
        )

    def test_stage_gate_rejects_empty_feature_scope(self) -> None:
        self.assertEqual(
            ["features"],
            validate_stage_evidence({"stages": {}}, "raw-preview", []),
        )

    def test_restore_stage_requires_controller_and_logcat_evidence(self) -> None:
        payload = {
            "stages": {
                stage: {field: True for field in fields}
                for stage, fields in REQUIRED.items()
            }
        }
        payload["stages"]["scope"] = {
            "feature_checks": {
                "beauty-skin-buffing-skin": {
                    "visible": True,
                    "switch_remove_restore": True,
                    "failure_fallback": True,
                }
            }
        }
        payload["stages"]["restore"]["controllers_reregistered"] = False
        payload["stages"]["restore"]["no_ar_scene_null"] = False
        payload["stages"]["restore"]["frames_after_restore"] = False
        missing = validate_stage_evidence(
            payload, "restore", ["beauty-skin-buffing-skin"]
        )
        self.assertIn("stages.restore.controllers_reregistered", missing)
        self.assertIn("stages.restore.no_ar_scene_null", missing)
        self.assertIn("stages.restore.frames_after_restore", missing)

    def test_scope_stage_expands_full_product_catalog(self) -> None:
        payload = {
            "stages": {
                stage: {field: True for field in fields}
                for stage, fields in (
                    ("raw-preview", (
                        "observed", "frames_present", "non_black",
                        "viewport_correct", "direction_correct",
                    )),
                    ("no-effect", (
                        "observed", "non_black", "raw_fallback",
                        "output_texture_type_recorded", "gl_state_restored",
                    )),
                    ("single-effect", (
                        "observed", "visible_change", "disable_restores_raw",
                        "failure_fallback",
                    )),
                )
            }
        }
        payload["stages"]["scope"] = {"feature_checks": {}}
        missing = validate_stage_evidence(payload, "scope", ["完整美妆"])
        self.assertTrue(
            any("makeup-single-eyeball" in item for item in missing)
        )

    def test_unknown_feature_is_rejected_before_normal_validation(self) -> None:
        findings = validate(self.root, "custom-gl", strict=True, features=["made-up-effect"])
        self.assertEqual(["UNKNOWN_FEATURE"], [item.code for item in findings])

    def test_contract_routes_advanced_four_and_sharpness_to_beauty_reference(self) -> None:
        report = contract_report([
            "beauty-skin-advanced-buffing-4", "beauty-skin-sharpness"
        ])
        self.assertEqual(["references/beauty.md"], report["references"])
        self.assertEqual(2, len(report["intensity_evidence_features"]))

    def test_baseline_aar_requires_requested_leaf_implementation(self) -> None:
        files = list(iter_files(self.root))
        inventory = inspect_project(self.root, files=files)
        inventory["sdk_artifacts"]["nve_effect_kit_aars"][0]["baseline_match"] = True
        findings = validate(
            self.root,
            "camera2-oes-nv21",
            strict=True,
            features=["beauty-skin-advanced-buffing-4", "beauty-skin-sharpness"],
            inventory=inventory,
            documents=source_documents(self.root, files),
            project_files=files,
        )
        self.assertIn(
            "FEATURE_LEAF_IMPLEMENTATION_MISSING",
            {item.code for item in findings if item.severity == "ERROR"},
        )

    def test_baseline_full_beauty_requires_complete_item_catalog(self) -> None:
        files = list(iter_files(self.root))
        inventory = inspect_project(self.root, files=files)
        inventory["sdk_artifacts"]["nve_effect_kit_aars"][0]["baseline_match"] = True
        findings = validate(
            self.root,
            "camera2-oes-nv21",
            strict=True,
            features=["完整美颜"],
            inventory=inventory,
            documents=source_documents(self.root, files),
            project_files=files,
        )
        self.assertIn(
            "FEATURE_CATALOG_INCOMPLETE",
            {item.code for item in findings if item.severity == "ERROR"},
        )

    def test_makeup_catalog_accepts_local_metadata_category_evidence(self) -> None:
        source = GOOD_SOURCE.replace(
            "void finish()",
            "void makeup() { makeup.addSingleMakeUp(single); }\n  void finish()",
        )
        make_project(self.root, source)
        metadata = self.root / "app/src/main/assets/makeup/info.json"
        metadata.parent.mkdir(parents=True)
        metadata.write_text(
            json.dumps(
                {
                    "makeupId": "catalog",
                    "categories": [
                        "Lip",
                        "Eyebrow",
                        "Eyeshadow",
                        "Eyelash",
                        "Eyeliner",
                        "Blusher",
                        "Shadow",
                        "Brighten",
                        "Eyeball",
                    ],
                }
            ),
            encoding="utf-8",
        )
        files = list(iter_files(self.root))
        inventory = inspect_project(self.root, files=files)
        inventory["sdk_artifacts"]["nve_effect_kit_aars"][0]["baseline_match"] = True
        findings = validate(
            self.root,
            "custom-gl",
            strict=True,
            features=["美妆"],
            inventory=inventory,
            documents=source_documents(self.root, files),
            project_files=files,
        )
        self.assertNotIn(
            "FEATURE_CATALOG_INCOMPLETE",
            {item.code for item in findings if item.severity == "ERROR"},
        )
        self.assertIn(
            "RUNTIME_FEATURE_METADATA_BINDING",
            {
                item.code
                for item in findings
                if item.severity == "RUNTIME_REQUIRED"
            },
        )

    def test_baseline_rejects_initialize_directly_on_gl_queue(self) -> None:
        files = list(iter_files(self.root))
        source = GOOD_SOURCE.replace(
            "runOnUiThread(() -> NveEffectKit.getInstance().initialize());",
            "queueEvent(() -> NveEffectKit.getInstance().initialize());",
        )
        make_project(self.root, source)
        files = list(iter_files(self.root))
        inventory = inspect_project(self.root, files=files)
        inventory["sdk_artifacts"]["nve_effect_kit_aars"][0]["baseline_match"] = True
        findings = validate(
            self.root,
            "camera2-oes-nv21",
            strict=True,
            features=["beauty-skin-buffing-skin"],
            inventory=inventory,
            documents=source_documents(self.root, files),
            project_files=files,
        )
        self.assertIn(
            "BASELINE_INITIALIZE_ON_GL_THREAD",
            {item.code for item in findings if item.severity == "ERROR"},
        )

    def test_baseline_rejects_asset_install_on_background_executor(self) -> None:
        source = GOOD_SOURCE.replace(
            "void finish()",
            "void prepareShape() {\n"
            "    assetExecutor.execute(() -> {\n"
            "      String packageId = NveAssetPackageManagerUtil.installFxPackage(\n"
            "          packagePath, null, ASSET_PACKAGE_TYPE_FACE_MESH);\n"
            "      if (packageId == null) return;\n"
            "      publish(packageId);\n"
            "    });\n"
            "  }\n"
            "  void finish()",
        )
        make_project(self.root, source)
        files = list(iter_files(self.root))
        inventory = inspect_project(self.root, files=files)
        inventory["sdk_artifacts"]["nve_effect_kit_aars"][0]["baseline_match"] = True
        findings = validate(
            self.root,
            "camera2-oes-nv21",
            strict=True,
            features=["beauty-shape-face-size"],
            inventory=inventory,
            documents=source_documents(self.root, files),
            project_files=files,
        )
        self.assertIn(
            "BASELINE_ASSET_INSTALL_ON_BACKGROUND_THREAD",
            {item.code for item in findings if item.severity == "ERROR"},
        )

    def test_baseline_allows_asset_install_on_explicit_main_executor(self) -> None:
        source = GOOD_SOURCE.replace(
            "void finish()",
            "void prepareShape() {\n"
            "    mainThreadExecutor.execute(() -> {\n"
            "      String packageId = NveAssetPackageManagerUtil.installFxPackage(\n"
            "          packagePath, null, ASSET_PACKAGE_TYPE_FACE_MESH);\n"
            "      if (packageId == null) return;\n"
            "      publish(packageId);\n"
            "    });\n"
            "  }\n"
            "  void finish()",
        )
        make_project(self.root, source)
        files = list(iter_files(self.root))
        inventory = inspect_project(self.root, files=files)
        inventory["sdk_artifacts"]["nve_effect_kit_aars"][0]["baseline_match"] = True
        findings = validate(
            self.root,
            "camera2-oes-nv21",
            strict=True,
            features=["beauty-shape-face-size"],
            inventory=inventory,
            documents=source_documents(self.root, files),
            project_files=files,
        )
        self.assertNotIn(
            "BASELINE_ASSET_INSTALL_ON_BACKGROUND_THREAD",
            {item.code for item in findings},
        )

    def test_baseline_requires_host_gl_restore_after_render(self) -> None:
        source = GOOD_SOURCE.replace("      restoreHostGlState();\n", "")
        make_project(self.root, source)
        files = list(iter_files(self.root))
        inventory = inspect_project(self.root, files=files)
        inventory["sdk_artifacts"]["nve_effect_kit_aars"][0]["baseline_match"] = True
        findings = validate(
            self.root,
            "camera2-oes-nv21",
            strict=True,
            features=["beauty-skin-buffing-skin"],
            inventory=inventory,
            documents=source_documents(self.root, files),
            project_files=files,
        )
        self.assertIn(
            "HOST_GL_STATE_RESTORE_MISSING",
            {item.code for item in findings if item.severity == "ERROR"},
        )

    def test_baseline_rejects_host_gl_release_after_cleanup(self) -> None:
        source = GOOD_SOURCE.replace(
            "void pause() { queueEvent(() -> NveEffectKit.getInstance().cleanUp()); }",
            "void pause() { queueEvent(() -> { "
            "NveEffectKit.getInstance().cleanUp(); glDrawer.release(); }); }",
        )
        make_project(self.root, source)
        files = list(iter_files(self.root))
        inventory = inspect_project(self.root, files=files)
        inventory["sdk_artifacts"]["nve_effect_kit_aars"][0]["baseline_match"] = True
        findings = validate(
            self.root,
            "camera2-oes-nv21",
            strict=True,
            features=["beauty-skin-buffing-skin"],
            inventory=inventory,
            documents=source_documents(self.root, files),
            project_files=files,
        )
        self.assertIn(
            "BASELINE_GL_RELEASE_AFTER_CLEANUP",
            {item.code for item in findings if item.severity == "ERROR"},
        )

    def test_single_feature_contract_loads_only_its_category_reference(self) -> None:
        expected = {
            "beauty-skin-sharpness": "references/beauty.md",
            "makeup-single-lip": "references/makeup.md",
            "filter-packaged": "references/effects.md",
            "face-prop": "references/effects.md",
            "custom-effect": "references/effects.md",
        }
        for feature, reference in expected.items():
            with self.subTest(feature=feature):
                self.assertEqual([reference], contract_report([feature])["references"])

    def test_full_beauty_suite_requires_all_three_controllers(self) -> None:
        source = GOOD_SOURCE.replace("NveEffectKit.getInstance().setMicroShape(microShape);", "")
        make_project(self.root, source)
        codes = {
            item.code
            for item in validate(
                self.root,
                "camera2-oes-nv21",
                strict=True,
                features=["完整美颜"],
            )
            if item.severity == "ERROR"
        }
        self.assertIn("BEAUTY_MICRO_SHAPE_CONTROLLER_MISSING", codes)

    def test_skin_only_scope_does_not_require_visible_shape_controllers(self) -> None:
        source = GOOD_SOURCE.replace("NveEffectKit.getInstance().setShape(shape);", "").replace(
            "NveEffectKit.getInstance().setMicroShape(microShape);", ""
        )
        make_project(self.root, source)
        codes = {
            item.code
            for item in validate(
                self.root,
                "camera2-oes-nv21",
                strict=True,
                features=["beauty-skin-buffing-skin"],
            )
            if item.severity == "ERROR"
        }
        self.assertFalse(
            {"BEAUTY_SHAPE_CONTROLLER_MISSING", "BEAUTY_MICRO_SHAPE_CONTROLLER_MISSING"} & codes
        )

    def test_shape_only_scope_still_requires_internal_beauty_anchor(self) -> None:
        source = GOOD_SOURCE.replace("NveEffectKit.getInstance().setBeauty(beauty);", "")
        make_project(self.root, source)
        codes = {
            item.code
            for item in validate(
                self.root,
                "camera2-oes-nv21",
                strict=True,
                features=["beauty-shape-face-size"],
            )
            if item.severity == "ERROR"
        }
        self.assertIn("BEAUTY_AR_SCENE_ANCHOR_MISSING", codes)

    def test_invalid_named_aar_is_rejected(self) -> None:
        aar = self.root / "app/libs/NveEffectKit.aar"
        aar.write_text("not an aar", encoding="utf-8")
        report = inspect_project(self.root, "debug")
        self.assertEqual([], report["sdk_artifacts"]["nve_effect_kit_aars"])
        self.assertEqual(1, len(report["sdk_artifacts"]["invalid_named_aar_candidates"]))
        self.assertIn("NVE_AAR_INVALID", self.codes_without_rewriting())

    def test_fake_class_entry_in_aar_is_rejected(self) -> None:
        aar = self.root / "app/libs/NveEffectKit.aar"
        classes_buffer = io.BytesIO()
        with zipfile.ZipFile(classes_buffer, "w") as classes:
            classes.writestr("com/meishe/nveffectkit/NveEffectKit.class", b"plain text")
        with zipfile.ZipFile(aar, "w") as archive:
            archive.writestr("AndroidManifest.xml", '<manifest package="fake"/>')
            archive.writestr("classes.jar", classes_buffer.getvalue())
        self.assertIn("NVE_AAR_INVALID", self.codes_without_rewriting())

    def test_inventory_uses_only_project_and_explicit_resource_paths(self) -> None:
        (self.root / "app/libs/NveEffectKit.aar").unlink()
        provided = self.root.parent / f"{self.root.name}-provided-resources"
        if provided.exists():
            shutil.rmtree(provided)
        provided.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(provided, ignore_errors=True))
        write_valid_nve_aar(provided / "NveEffectKit.aar")
        (provided / "customer.lic").write_bytes(b"synthetic")
        (provided / "face.dat").write_bytes(b"synthetic")

        unprovided_report = inspect_project(self.root)
        self.assertEqual([], unprovided_report["sdk_artifacts"]["nve_effect_kit_aars"])
        self.assertEqual(0, unprovided_report["resources"]["license_file_count"])
        self.assertEqual(0, unprovided_report["resources"]["model_file_count"])
        self.assertEqual(
            [], unprovided_report["scan_scope"]["provided_resource_paths"]
        )

        provided_report = inspect_project(self.root, resource_paths=[provided])
        self.assertEqual(1, len(provided_report["sdk_artifacts"]["nve_effect_kit_aars"]))
        self.assertEqual(
            "provided-resource-1/NveEffectKit.aar",
            provided_report["sdk_artifacts"]["nve_effect_kit_aars"][0]["path"],
        )
        self.assertEqual(1, provided_report["resources"]["license_file_count"])
        self.assertEqual(1, provided_report["resources"]["model_file_count"])
        self.assertEqual(
            "user-provided-path-candidates-unverified",
            provided_report["resources"]["source_status"]["models"],
        )
        self.assertFalse(
            provided_report["scan_scope"]["implicit_parent_or_workspace_scan"]
        )

    def test_inventory_ignores_workspace_local_gradle_cache(self) -> None:
        for cached_jar in (
            self.root
            / ".gradle-local/caches/transforms-2/files/NvAndroidStreamingSdk.jar",
            self.root
            / ".gradle/caches/transforms-3/files/NvAndroidStreamingSdk.jar",
            self.root
            / "app/build/intermediates/transforms-4/files/NvAndroidStreamingSdk.jar",
        ):
            cached_jar.parent.mkdir(parents=True)
            cached_jar.write_bytes(b"generated cache entry")

        report = inspect_project(self.root)

        self.assertEqual(
            [],
            report["sdk_artifacts"]["standalone_streaming_sdk_jars"],
        )

        real_jar = self.root / "app/libs/NvAndroidStreamingSdk.jar"
        real_jar.parent.mkdir(parents=True, exist_ok=True)
        real_jar.write_bytes(b"declared project dependency")
        report = inspect_project(self.root)
        self.assertEqual(
            ["app/libs/NvAndroidStreamingSdk.jar"],
            report["sdk_artifacts"]["standalone_streaming_sdk_jars"],
        )

    def test_chained_init_result_must_be_checked(self) -> None:
        bad = GOOD_SOURCE.replace(
            "boolean licensed = NveEffectKit.getInstance().init(context, license);",
            "NveEffectKit.getInstance().init(context, license);",
        )
        self.assertIn("INIT_RESULT_UNCHECKED", self.codes(bad))

    def test_chained_init_model_result_must_be_checked(self) -> None:
        bad = GOOD_SOURCE.replace(
            "boolean faceCommon = NveEffectKit.getInstance().initModel(\n"
            "        context, NveDetectionModelType.FACE_COMMON, faceCommonModel);",
            "NveEffectKit.getInstance().initModel(context, "
            "NveDetectionModelType.FACE_COMMON, faceCommonModel);",
        )
        self.assertIn("MODEL_RESULT_UNCHECKED", self.codes(bad))

    def makeup_install_source(self, guard: str) -> str:
        return GOOD_SOURCE.replace(
            "void finish()",
            "void installMakeup() {\n"
            "    String packageId = NveAssetPackageManagerUtil.installFxPackage(\n"
            "        packagePath, licensePath, ASSET_PACKAGE_TYPE_MAKEUP);\n"
            f"    {guard}\n"
            "    NveSingleMakeUp singleMakeup = metadata;\n"
            "    singleMakeup.setPackageID(packageId);\n"
            "  }\n"
            "  void finish()",
        )

    def test_makeup_install_requires_zero_uuid_and_format_guard(self) -> None:
        source = self.makeup_install_source(
            "if (TextUtils.isEmpty(packageId)) return;"
        )
        self.assertIn("MAKEUP_PACKAGE_ID_GUARD_MISSING", self.codes(source))

    def test_makeup_resource_loader_requires_gson_runtime_dependency(self) -> None:
        loader = self.root / "app/src/main/java/sample/NveMakeupResourceLoader.java"
        loader.write_text(
            "package sample; class NveMakeupResourceLoader { "
            "void parse() { ParseJsonFile.fromJson(json, NveComposeMakeup.class); } }",
            encoding="utf-8",
        )
        codes = {
            item.code
            for item in validate(
                self.root,
                "camera2-oes-nv21",
                strict=True,
                features=["makeup-compose"],
            )
            if item.severity == "ERROR"
        }
        self.assertIn("MAKEUP_GSON_RUNTIME_DEPENDENCY_MISSING", codes)

        build = self.root / "app/build.gradle"
        build.write_text(
            build.read_text(encoding="utf-8")
            + "\ndependencies { implementation 'com.google.code.gson:gson:2.8.2' }\n",
            encoding="utf-8",
        )
        codes = {
            item.code
            for item in validate(
                self.root,
                "camera2-oes-nv21",
                strict=True,
                features=["makeup-compose"],
            )
            if item.severity == "ERROR"
        }
        self.assertNotIn("MAKEUP_GSON_RUNTIME_DEPENDENCY_MISSING", codes)

    def test_install_cannot_borrow_package_guard_from_another_file(self) -> None:
        source = self.makeup_install_source("use(packageId);")
        helper = self.root / "app/src/main/java/sample/PackageGuard.java"
        helper.write_text(
            'class PackageGuard { void check(String packageId) { '
            'if (TextUtils.isEmpty(packageId) || '
            '"00000000-0000-0000-0000-000000000000".equals(packageId)) return; '
            'UUID.fromString(packageId); } }',
            encoding="utf-8",
        )
        self.assertIn("MAKEUP_PACKAGE_ID_GUARD_MISSING", self.codes(source))

    def test_defensive_zero_uuid_guard_is_not_hardcoded_effect_id(self) -> None:
        source = self.makeup_install_source(
            'if (TextUtils.isEmpty(packageId) || '
            '"00000000-0000-0000-0000-000000000000".equals(packageId)) return; '
            "UUID.fromString(packageId);"
        )
        codes = self.codes(source)
        self.assertNotIn("MAKEUP_PACKAGE_ID_GUARD_MISSING", codes)
        self.assertNotIn("HARDCODED_EFFECT_PACKAGE_ID", codes)

    def test_nonzero_hardcoded_effect_uuid_is_still_rejected(self) -> None:
        source = self.makeup_install_source(
            'if (TextUtils.isEmpty(packageId) || '
            '"00000000-0000-0000-0000-000000000000".equals(packageId)) return; '
            'UUID.fromString(packageId); String fallback = '
            '"11111111-2222-3333-4444-555555555555";'
        )
        self.assertIn("HARDCODED_EFFECT_PACKAGE_ID", self.codes(source))

    def test_renamed_makeup_package_is_rejected_against_metadata_uuid(self) -> None:
        source = self.makeup_install_source(
            'if (TextUtils.isEmpty(packageId) || '
            '"00000000-0000-0000-0000-000000000000".equals(packageId)) return; '
            "UUID.fromString(packageId);"
        )
        make_project(self.root, source)
        assets = self.root / "app/src/main/assets/makeup"
        assets.mkdir(parents=True)
        (assets / "lip.makeup").write_bytes(b"synthetic")
        (assets / "info.json").write_text(
            '{"makeupId":"Lip","effectContent":{"makeupArgs":['
            '{"type":"Lip","uuid":"11111111-2222-3333-4444-555555555555"}]}}',
            encoding="utf-8",
        )
        self.assertIn("MAKEUP_PACKAGE_NAME_MISMATCH", self.codes_without_rewriting())

    def test_original_uuid_version_makeup_name_matches_metadata(self) -> None:
        source = self.makeup_install_source(
            'if (TextUtils.isEmpty(packageId) || '
            '"00000000-0000-0000-0000-000000000000".equals(packageId)) return; '
            "UUID.fromString(packageId);"
        )
        make_project(self.root, source)
        assets = self.root / "app/src/main/assets/makeup"
        assets.mkdir(parents=True)
        (assets / "11111111-2222-3333-4444-555555555555.8.makeup").write_bytes(b"synthetic")
        (assets / "info.json").write_text(
            '{"makeupId":"Lip","effectContent":{"makeupArgs":['
            '{"type":"Lip","uuid":"11111111-2222-3333-4444-555555555555"}]}}',
            encoding="utf-8",
        )
        self.assertNotIn("MAKEUP_PACKAGE_NAME_MISMATCH", self.codes_without_rewriting())

    def codes_without_rewriting(self) -> set[str]:
        return {item.code for item in validate(self.root, "camera2-oes-nv21", strict=True) if item.severity == "ERROR"}

    def test_nve_controller_only_file_is_included(self) -> None:
        controller = self.root / "app/src/main/java/sample/BeautyController.java"
        controller.write_text("class BeautyController { NveBeauty beauty; }", encoding="utf-8")
        self.assertIn("app/src/main/java/sample/BeautyController.java", inspect_project(self.root)["nve_source_files"])

    def test_explicit_include_adds_ui_file_to_effect_analysis(self) -> None:
        ui = self.root / "app/src/main/java/sample/EffectUi.java"
        ui.write_text(
            "class EffectUi { void onProgressChanged() { label.setText(\"x\"); } }",
            encoding="utf-8",
        )
        errors_without = {
            item.code for item in validate(self.root, "camera2-oes-nv21", strict=True)
            if item.severity == "ERROR"
        }
        errors_with = {
            item.code for item in validate(
                self.root,
                "camera2-oes-nv21",
                strict=True,
                includes=["app/src/main/java/sample/EffectUi.java"],
            )
            if item.severity == "ERROR"
        }
        self.assertNotIn("SLIDER_CALLBACK_SDK_UPDATE_MISSING", errors_without)
        self.assertIn("SLIDER_CALLBACK_SDK_UPDATE_MISSING", errors_with)

    def test_nve_tagged_ui_is_automatically_analyzed(self) -> None:
        ui = self.root / "app/src/main/java/sample/BeautyPanel.java"
        ui.write_text(
            """
class BeautyPanel {
  void bind(View chip, Feature feature) {
    chip.setContentDescription("nve-beauty-feature-face-size");
    chip.setOnClickListener(v -> {
      selectedFeature = feature;
      listener.onValueChanged(feature, storedValues.get(feature));
    });
  }
}
""",
            encoding="utf-8",
        )
        self.assertIn(
            "UI_SELECTION_REAPPLIES_EFFECT_VALUE",
            self.codes_without_rewriting(),
        )

    def test_shape_before_ar_scene_is_rejected(self) -> None:
        bad = GOOD_SOURCE.replace(
            "NveEffectKit.getInstance().setBeauty(beauty);\n    NveEffectKit.getInstance().setShape(shape);",
            "NveEffectKit.getInstance().setShape(shape);\n    NveEffectKit.getInstance().setBeauty(beauty);",
        )
        self.assertIn("AR_SCENE_REGISTRATION_ORDER", self.codes(bad, "RUNTIME_REQUIRED"))

    def test_micro_shape_before_ar_scene_is_rejected(self) -> None:
        bad = GOOD_SOURCE.replace(
            "NveEffectKit.getInstance().setBeauty(beauty);",
            "NveEffectKit.getInstance().setMicroShape(microShape);\n    NveEffectKit.getInstance().setBeauty(beauty);",
            1,
        )
        self.assertIn("AR_SCENE_REGISTRATION_ORDER", self.codes(bad, "RUNTIME_REQUIRED"))

    def test_controller_enable_before_reregistration_is_rejected(self) -> None:
        for register_call, enable_call in (
            ("setBeauty(beauty)", "beauty.setEnable(true)"),
            ("setShape(shape)", "shape.setEnable(true)"),
            ("setMicroShape(microShape)", "microShape.setEnable(true)"),
        ):
            with self.subTest(register_call=register_call):
                bad = GOOD_SOURCE.replace(
                    "void restore() { NveEffectKit.getInstance().setBeauty(beauty); }",
                    "void restore() { "
                    + enable_call
                    + "; NveEffectKit.getInstance()."
                    + register_call
                    + "; }",
                )
                self.assertIn(
                    "CONTROLLER_ENABLE_BEFORE_REGISTRATION",
                    self.codes(bad, "RUNTIME_REQUIRED"),
                )

    def test_controller_enable_after_reregistration_is_accepted(self) -> None:
        good = GOOD_SOURCE.replace(
            "void restore() { NveEffectKit.getInstance().setBeauty(beauty); }",
            "void restore() { "
            "NveEffectKit.getInstance().setBeauty(beauty); beauty.setEnable(true); "
            "NveEffectKit.getInstance().setShape(shape); shape.setEnable(true); "
            "NveEffectKit.getInstance().setMicroShape(microShape); "
            "microShape.setEnable(true); }",
        )
        self.assertNotIn(
            "CONTROLLER_ENABLE_BEFORE_REGISTRATION",
            self.codes(good) | self.codes(good, "RUNTIME_REQUIRED"),
        )

    def test_code_only_ignores_calls_in_comments_and_literals(self) -> None:
        source = (
            '// renderEffect() and shape.setEnable(true)\n'
            'String sample = "renderEffect()";\n'
            "/* microShape.setEnable(true); */\n"
            "output = effectKit.renderEffect(input);\n"
        )
        stripped = code_only(source)
        self.assertEqual(1, stripped.count("renderEffect("))
        self.assertNotIn("shape.setEnable(", stripped)
        self.assertNotIn("microShape.setEnable(", stripped)
        self.assertEqual(len(source), len(stripped))

    def test_ui_sdk_call_without_guard_is_rejected_per_handler(self) -> None:
        bad = GOOD_SOURCE.replace(
            "desiredEnabled = true;\n      if (!renderCoreReady || !capabilityReady) return;\n      beauty.setEnable(desiredEnabled);",
            "beauty.setEnable(true);",
        )
        self.assertIn("UI_SDK_CALL_UNGUARDED", self.codes(bad))

    def test_feature_selection_must_not_reapply_stored_effect_value(self) -> None:
        bad = GOOD_SOURCE.replace(
            "void onProgressChanged(SeekBar seekBar, int progress, boolean fromUser) {",
            """
  void bindFeature(View chip, Feature feature) {
    chip.setOnClickListener(v -> {
      selectedFeature = feature;
      listener.onValueChanged(feature, storedValues.get(feature));
    });
  }
  void onProgressChanged(SeekBar seekBar, int progress, boolean fromUser) {""",
        )
        self.assertIn("UI_SELECTION_REAPPLIES_EFFECT_VALUE", self.codes(bad))

    def test_feature_selection_can_refresh_ui_without_effect_write(self) -> None:
        good = GOOD_SOURCE.replace(
            "void onProgressChanged(SeekBar seekBar, int progress, boolean fromUser) {",
            """
  void bindFeature(View chip, Feature feature) {
    chip.setOnClickListener(v -> {
      selectedFeature = feature;
      refreshLabelAndSliderSilently(feature);
    });
  }
  void onProgressChanged(SeekBar seekBar, int progress, boolean fromUser) {""",
        )
        self.assertNotIn("UI_SELECTION_REAPPLIES_EFFECT_VALUE", self.codes(good))

    def test_slider_callback_without_sdk_or_state_handoff_is_rejected(self) -> None:
        bad = GOOD_SOURCE.replace(
            "desiredDegree = progress / 100.0f;\n    if (!renderCoreReady || !capabilityReady) return;\n    shape.setDegreeByType(FACE_SIZE, desiredDegree);\n    beauty.setBlurIntensity(desiredDegree);",
            'label.setText(String.valueOf(progress));',
        )
        self.assertIn("SLIDER_CALLBACK_SDK_UPDATE_MISSING", self.codes(bad))

    def test_slider_state_handoff_requires_runtime_proof(self) -> None:
        bad = GOOD_SOURCE.replace(
            "if (!renderCoreReady || !capabilityReady) return;\n    shape.setDegreeByType(FACE_SIZE, desiredDegree);\n    beauty.setBlurIntensity(desiredDegree);",
            "return;",
        )
        self.assertIn("RUNTIME_SLIDER_PROPAGATION", self.codes(bad, "RUNTIME_REQUIRED"))

    def test_slider_callback_must_not_request_render(self) -> None:
        bad = GOOD_SOURCE.replace(
            "beauty.setBlurIntensity(desiredDegree);",
            "beauty.setBlurIntensity(desiredDegree);\n    requestRender();",
        )
        self.assertIn("SLIDER_REQUEST_RENDER_FORBIDDEN", self.codes(bad))

    def test_slider_helper_must_not_request_render(self) -> None:
        bad = GOOD_SOURCE.replace(
            "shape.setDegreeByType(FACE_SIZE, desiredDegree);\n"
            "    beauty.setBlurIntensity(desiredDegree);",
            "enqueueBeautyValue(desiredDegree);",
        ).replace(
            "void onDrawFrame()",
            "void enqueueBeautyValue(float value) { requestRender(); }\n"
            "  void onDrawFrame()",
        )
        self.assertIn("SLIDER_REQUEST_RENDER_FORBIDDEN", self.codes(bad))

    def test_effect_value_callback_helper_must_not_request_render(self) -> None:
        bad = GOOD_SOURCE.replace(
            "void onDrawFrame()",
            "void onValueChanged(Object item, float value) { "
            "enqueueBeautyValue(value); }\n"
            "  void enqueueBeautyValue(float value) { requestRender(); }\n"
            "  void onDrawFrame()",
        )
        self.assertIn(
            "VALUE_CALLBACK_REQUEST_RENDER_FORBIDDEN",
            self.codes(bad),
        )

    def test_single_makeup_scope_ignores_unrelated_intensity_slider(self) -> None:
        source = GOOD_SOURCE.replace(
            "shape.setDegreeByType(FACE_SIZE, desiredDegree);\n    beauty.setBlurIntensity(desiredDegree);",
            "controller.updateZoom(desiredDegree);",
        )
        make_project(self.root, source)
        runtime_codes = {
            item.code
            for item in validate(
                self.root, "camera2-oes-nv21", strict=True, features=["makeup-single"]
            )
            if item.severity == "RUNTIME_REQUIRED"
        }
        self.assertNotIn("RUNTIME_SLIDER_PROPAGATION", runtime_codes)

    def test_oes_output_without_type_dispatch_is_rejected(self) -> None:
        bad = GOOD_SOURCE.replace("output.getTexture().getTextureType() == NV_OES_TEXTURE", "true")
        self.assertIn("OUTPUT_TEXTURE_TYPE_DISPATCH_MISSING", self.codes(bad))

    def test_strict_timestamp_equality_is_rejected(self) -> None:
        bad = GOOD_SOURCE.replace(
            "long delta = Math.abs(textureTimestampNs - bufferTimestampNs);",
            "boolean same = textureTimestampNs == bufferTimestampNs;",
        )
        self.assertIn("STRICT_TIMESTAMP_EQUALITY", self.codes(bad))

    def test_heavy_image_callback_is_rejected(self) -> None:
        bad = GOOD_SOURCE.replace("void frames()", "void onImageAvailable() { int jpeg = JPEG; BitmapFactory.decode(); }\n  void frames()")
        self.assertIn("HEAVY_IMAGE_CALLBACK", self.codes(bad))

    def test_unrelated_bitmap_and_non_effect_slider_do_not_trigger_effect_errors(self) -> None:
        ui = self.root / "app/src/main/java/sample/CameraControls.java"
        ui.write_text(
            "class CameraControls { void onProgressChanged() { zoom.set(progress); } "
            "void export() { BitmapFactory.decode(); int x = JPEG; } }",
            encoding="utf-8",
        )
        codes = self.codes_without_rewriting()
        self.assertNotIn("HEAVY_IMAGE_CALLBACK", codes)
        self.assertNotIn("SLIDER_CALLBACK_SDK_UPDATE_MISSING", codes)

    def test_yuv_conversion_in_image_callback_is_rejected(self) -> None:
        bad = GOOD_SOURCE.replace("void frames()", "void onImageAvailable() { int x = rowStride + pixelStride; }\n  void frames()")
        self.assertIn("YUV_CONVERSION_ON_CAMERA_CALLBACK", self.codes(bad))

    def test_camera_image_must_be_closed(self) -> None:
        bad = GOOD_SOURCE.replace("} finally { image.close(); }", "}")
        self.assertIn("CAMERA_IMAGE_CLOSE_MISSING", self.codes(bad))

    def test_camera_image_ownership_can_transfer_to_latest_only_pairer(self) -> None:
        source = GOOD_SOURCE.replace(
            "void frames()",
            "void offerLatestImage(Image image, int generation) { "
            "PendingImage previous = pendingImage; "
            "if (previous != null) previous.image.close(); }\n"
            "  void convertLatestImage(Image image) { "
            "try { use(image); } finally { image.close(); } }\n"
            "  void handoff(ImageReader reader, int generation) { "
            "Image owned = reader.acquireLatestImage(); "
            "offerLatestImage(owned, generation); }\n"
            "  void frames()",
        )
        self.assertNotIn("CAMERA_IMAGE_CLOSE_MISSING", self.codes(source))

    def test_surface_texture_callback_on_main_thread_is_rejected(self) -> None:
        bad = GOOD_SOURCE.replace(
            "void frames()",
            "void configureSurfaceTexture() { "
            "Handler mainHandler = new Handler(Looper.getMainLooper()); "
            "surfaceTexture.setOnFrameAvailableListener(this, mainHandler); }\n"
            "  void frames()",
        )
        self.assertIn(
            "SURFACE_TEXTURE_CALLBACK_ON_MAIN_THREAD",
            self.codes(bad),
        )

    def test_surface_texture_callback_on_handler_thread_is_accepted(self) -> None:
        good = GOOD_SOURCE.replace(
            "void frames()",
            "void configureSurfaceTexture() { "
            "HandlerThread frameThread = new HandlerThread(\"NveOesFrame\"); "
            "frameThread.start(); "
            "Handler frameHandler = new Handler(frameThread.getLooper()); "
            "surfaceTexture.setOnFrameAvailableListener(this, frameHandler); }\n"
            "  void frames()",
        )
        codes = self.codes(good)
        self.assertNotIn("SURFACE_TEXTURE_CALLBACK_ON_MAIN_THREAD", codes)

    def test_advanced_skin_as_only_default_is_rejected(self) -> None:
        self.assertIn("DEFAULT_SMOOTHING_IS_ADVANCED", self.codes(GOOD_SOURCE.replace("BuffingSkin", "AdvancedBuffingSkin1")))

    def test_recycle_outside_finally_is_rejected(self) -> None:
        self.assertIn("RECYCLE_NOT_IN_FINALLY", self.codes(GOOD_SOURCE.replace("} finally {", "} catch (Exception ignored) {", 1)))

    def test_render_cannot_borrow_unrelated_finally_in_same_file(self) -> None:
        bad = GOOD_SOURCE.replace(
            "} finally {\n      if (output != null) NveEffectKit.getInstance().recycleOutput(output);\n    }",
            "}",
        ).replace(
            "void finish()",
            "void unrelated() { Object other = null; try { use(other); } finally { "
            "NveEffectKit.getInstance().recycleOutput(other); } }\n  void finish()",
        )
        self.assertIn("RECYCLE_NOT_IN_FINALLY", self.codes(bad))

    def test_render_call_outside_try_is_rejected(self) -> None:
        bad = GOOD_SOURCE.replace(
            "NveRenderOutput output = null;\n    try {\n"
            "      output = NveEffectKit.getInstance().renderEffect(inputTexture, detectionBuffer);",
            "NveRenderOutput output = NveEffectKit.getInstance().renderEffect(inputTexture, detectionBuffer);\n"
            "    try {",
        )
        self.assertIn("RENDER_CALL_OUTSIDE_TRY", self.codes(bad))

    def test_direction_fields_cannot_be_swapped(self) -> None:
        self.assertIn("SENSOR_DISPLAY_ROTATION_MIXED", self.codes(GOOD_SOURCE.replace("int b =", "config.setDisplayRotation(sensorOrientation); int b =")))

    def test_raw_effect_mirror_constants_must_match(self) -> None:
        bad = GOOD_SOURCE.replace("void drawRawOes() {}", "void mirror() { drawRaw(true); drawEffect(false); } void drawRawOes() {}")
        self.assertIn("RAW_EFFECT_MIRROR_MISMATCH", self.codes(bad))

    def test_variant_inventory_resolves_build_type_suffix(self) -> None:
        self.assertEqual("sample.app.debug", inspect_project(self.root, "debug")["modules"][0]["resolved_application_id"])

    def test_inventory_reports_nonbaseline_aar_hash(self) -> None:
        aar = inspect_project(self.root)["sdk_artifacts"]["nve_effect_kit_aars"][0]
        self.assertRegex(aar["sha256"], r"^[0-9a-f]{64}$")
        self.assertFalse(aar["baseline_match"])
        runtime_codes = {
            item.code for item in validate(self.root, "custom-gl", strict=True)
            if item.severity == "RUNTIME_REQUIRED"
        }
        self.assertIn("AAR_BASELINE_MISMATCH", runtime_codes)

    def test_prerequisite_gate_blocks_missing_license_before_implementation(self) -> None:
        report = inspect_project(self.root, features=["beauty"])
        codes = {
            item["code"] for item in report["prerequisite_gate"]["blockers"]
        }
        self.assertEqual("blocked", report["prerequisite_gate"]["status"])
        self.assertIn("LICENSE_FILE_MISSING", codes)
        self.assertIn("FEATURE_SCOPE_UNRESOLVED", codes)

    def test_asset_gate_only_blocks_explicitly_selected_item(self) -> None:
        aar_path = self.root / "app/libs/NveEffectKit.aar"
        digest = hashlib.sha256(aar_path.read_bytes()).hexdigest()
        (self.root / "app/src/main/assets/sdk.lic").parent.mkdir(
            parents=True, exist_ok=True
        )
        (self.root / "app/src/main/assets/sdk.lic").write_bytes(b"synthetic")
        with mock.patch(
            "inspect_android_project.BASELINE_AAR_SHA256", digest
        ):
            broad = inspect_project(self.root, features=["beauty-shape"])
            broad_codes = {
                item["code"]
                for item in broad["prerequisite_gate"]["blockers"]
            }
            self.assertIn("FEATURE_SCOPE_UNRESOLVED", broad_codes)
            self.assertNotIn("REQUIRED_BASELINE_ASSET_MISSING", broad_codes)

            explicit = inspect_project(
                self.root, features=["beauty-shape-face-size"]
            )
            explicit_codes = {
                item["code"]
                for item in explicit["prerequisite_gate"]["blockers"]
            }
            self.assertIn("REQUIRED_ASSET_MAPPING_MISSING", explicit_codes)

            custom = (
                self.root
                / "app/src/main/assets/beauty/shape/custom_face_size_v2.facemesh"
            )
            custom.parent.mkdir(parents=True, exist_ok=True)
            custom.write_bytes(b"synthetic-custom-package")
            mapped = inspect_project(
                self.root,
                features=["beauty-shape-face-size"],
                asset_map={
                    "beauty-shape-face-size":
                        "app/src/main/assets/beauty/shape/custom_face_size_v2.facemesh"
                },
            )
            mapped_codes = {
                item["code"]
                for item in mapped["prerequisite_gate"]["blockers"]
            }
            self.assertNotIn("REQUIRED_ASSET_MAPPING_MISSING", mapped_codes)
            self.assertNotIn("DECLARED_ASSET_NOT_FOUND", mapped_codes)
            self.assertEqual(
                "declared-map",
                mapped["prerequisite_gate"]["resolved_asset_map"][
                    "beauty-shape-face-size"
                ]["source"],
            )

    def test_filter_gate_requires_explicit_multi_asset_and_license_mapping(self) -> None:
        assets = self.root / "app/src/main/assets"
        assets.mkdir(parents=True, exist_ok=True)
        (assets / "sdk.lic").write_bytes(b"synthetic-sdk-license")
        makeup_component = assets / "beauty/makeup/compose/filter.videofx"
        makeup_component.parent.mkdir(parents=True, exist_ok=True)
        makeup_component.write_bytes(b"not-a-standalone-filter")

        missing = inspect_project(self.root, features=["filter"])
        missing_codes = {
            item["code"] for item in missing["prerequisite_gate"]["blockers"]
        }
        self.assertIn("FEATURE_SCOPE_UNRESOLVED", missing_codes)
        self.assertIn("REQUIRED_ASSET_MAPPING_MISSING", missing_codes)

        filter_root = assets / "beauty/filter"
        filter_root.mkdir(parents=True, exist_ok=True)
        for stem in ("white", "peach"):
            (filter_root / f"{stem}.videofx").write_bytes(b"synthetic-filter")
            (filter_root / f"{stem}.lic").write_bytes(b"synthetic-asset-license")

        mapped = inspect_project(
            self.root,
            features=["filter"],
            asset_map={
                "filter-packaged": [
                    {
                        "id": "white",
                        "name": "白皙",
                        "asset": "app/src/main/assets/beauty/filter/white.videofx",
                        "license": "app/src/main/assets/beauty/filter/white.lic",
                    },
                    {
                        "id": "peach",
                        "name": "蜜桃",
                        "asset": "app/src/main/assets/beauty/filter/peach.videofx",
                        "license": "app/src/main/assets/beauty/filter/peach.lic",
                    },
                ]
            },
        )
        mapped_codes = {
            item["code"] for item in mapped["prerequisite_gate"]["blockers"]
        }
        self.assertNotIn("FEATURE_SCOPE_UNRESOLVED", mapped_codes)
        self.assertNotIn("REQUIRED_ASSET_MAPPING_MISSING", mapped_codes)
        self.assertNotIn("UNUSED_ASSET_MAP_ENTRY", {
            item["code"] for item in mapped["prerequisite_gate"]["warnings"]
        })
        resolution = mapped["prerequisite_gate"]["resolved_asset_map"][
            "filter-packaged"
        ]
        self.assertEqual(2, resolution["count"])
        self.assertEqual("白皙", resolution["items"][0]["name"])
        self.assertEqual(1, mapped["resources"]["license_file_count"])
        self.assertEqual(2, mapped["resources"]["asset_license_file_count"])
        mapped_validation_codes = {
            item.code
            for item in validate(
                self.root,
                "custom-gl",
                strict=True,
                features=["filter"],
                inventory=mapped,
                project_files=list(iter_files(self.root)),
            )
        }
        self.assertNotIn("FEATURE_SCOPE_UNRESOLVED", mapped_validation_codes)
        self.assertNotIn(
            "REQUIRED_ASSET_MAPPING_MISSING", mapped_validation_codes
        )

        aliases = inspect_project(
            self.root,
            features=["filter"],
            asset_map={
                "filter-packaged": {
                    "packages": [
                        {
                            "key": "shared",
                            "asset":
                                "app/src/main/assets/beauty/filter/white.videofx",
                            "license": None,
                            "license_basis":
                                "user-confirmed-no-separate-license",
                        },
                        {
                            "key": "peach",
                            "asset":
                                "app/src/main/assets/beauty/filter/peach.videofx",
                            "license": None,
                            "license_basis":
                                "delivery-confirmed-no-separate-license",
                        },
                    ],
                    "display_items": [
                        {
                            "id": "cat",
                            "name": "小猫咪",
                            "package": "shared",
                        },
                        {
                            "id": "chelsea",
                            "name": "切尔西",
                            "package": "shared",
                        },
                        {
                            "id": "peach",
                            "name": "蜜桃",
                            "package": "peach",
                        },
                    ],
                }
            },
        )
        alias_codes = {
            item["code"] for item in aliases["prerequisite_gate"]["blockers"]
        }
        self.assertNotIn("DUPLICATE_ASSET_MAPPING", alias_codes)
        alias_resolution = aliases["prerequisite_gate"][
            "resolved_asset_map"
        ]["filter-packaged"]
        self.assertEqual(2, alias_resolution["package_count"])
        self.assertEqual(3, alias_resolution["display_item_count"])
        self.assertEqual(
            {"shared"},
            {
                item["package"]
                for item in alias_resolution["display_items"]
                if item["name"] in {"小猫咪", "切尔西"}
            },
        )

        missing_basis = inspect_project(
            self.root,
            features=["filter"],
            asset_map={
                "filter-packaged": {
                    "packages": [
                        {
                            "key": "white",
                            "asset":
                                "app/src/main/assets/beauty/filter/white.videofx",
                            "license": None,
                        }
                    ],
                    "display_items": [
                        {
                            "id": "white",
                            "name": "白皙",
                            "package": "white",
                        }
                    ],
                }
            },
        )
        self.assertIn(
            "ASSET_LICENSE_BASIS_MISSING",
            {
                item["code"]
                for item in missing_basis["prerequisite_gate"]["blockers"]
            },
        )

        duplicate_display = inspect_project(
            self.root,
            features=["filter"],
            asset_map={
                "filter-packaged": {
                    "packages": [
                        {
                            "key": "white",
                            "asset":
                                "app/src/main/assets/beauty/filter/white.videofx",
                            "license": None,
                            "license_basis":
                                "user-confirmed-no-separate-license",
                        }
                    ],
                    "display_items": [
                        {
                            "id": "same",
                            "name": "白皙",
                            "package": "white",
                        },
                        {
                            "id": "same",
                            "name": "重复",
                            "package": "white",
                        },
                    ],
                }
            },
        )
        self.assertIn(
            "DUPLICATE_FILTER_DISPLAY_ID",
            {
                item["code"]
                for item in duplicate_display[
                    "prerequisite_gate"
                ]["blockers"]
            },
        )

        unresolved_license = inspect_project(
            self.root,
            features=["filter"],
            asset_map={
                "filter-packaged": {
                    "asset": "app/src/main/assets/beauty/filter/white.videofx"
                }
            },
        )
        self.assertIn(
            "ASSET_LICENSE_STATUS_UNRESOLVED",
            {
                item["code"]
                for item in unresolved_license["prerequisite_gate"]["blockers"]
            },
        )

        no_separate_license = inspect_project(
            self.root,
            features=["filter"],
            asset_map={
                "filter-packaged": {
                    "asset": "app/src/main/assets/beauty/filter/white.videofx",
                    "license": None,
                }
            },
        )
        no_license_codes = {
            item["code"]
            for item in no_separate_license["prerequisite_gate"]["blockers"]
        }
        self.assertNotIn("ASSET_LICENSE_STATUS_UNRESOLVED", no_license_codes)
        self.assertIsNone(
            no_separate_license["prerequisite_gate"]["resolved_asset_map"][
                "filter-packaged"
            ]["items"][0]["license"]
        )

        (assets / "sdk.lic").unlink()
        (filter_root / "peach.lic").unlink()
        asset_license_only = inspect_project(
            self.root,
            features=["filter"],
            asset_map={
                "filter-packaged": {
                    "asset": "app/src/main/assets/beauty/filter/white.videofx",
                    "license": "app/src/main/assets/beauty/filter/white.lic",
                }
            },
        )
        self.assertIn(
            "LICENSE_FILE_MISSING",
            {
                item["code"]
                for item in asset_license_only["prerequisite_gate"]["blockers"]
            },
        )
        self.assertEqual(
            0, asset_license_only["resources"]["license_file_count"]
        )
        self.assertEqual(
            1, asset_license_only["resources"]["asset_license_file_count"]
        )

        duplicate = inspect_project(
            self.root,
            features=["filter"],
            asset_map={
                "filter-packaged": [
                    {
                        "name": "白皙",
                        "asset": "app/src/main/assets/beauty/filter/white.videofx",
                        "license": None,
                    },
                    {
                        "name": "重复项",
                        "asset": "app/src/main/assets/beauty/filter/white.videofx",
                        "license": None,
                    },
                ]
            },
        )
        self.assertIn(
            "DUPLICATE_ASSET_MAPPING",
            {
                item["code"]
                for item in duplicate["prerequisite_gate"]["blockers"]
            },
        )

    def test_filter_catalog_inspector_preserves_shared_display_items(self) -> None:
        filter_root = self.root / "delivered-filter"
        filter_root.mkdir()
        first_id = "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"
        second_id = "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB"
        for package_id in (first_id, second_id):
            (filter_root / f"{package_id}.1.videofx").write_bytes(b"filter")
            (filter_root / f"{package_id}.png").write_bytes(b"cover")
        payload = {
            "infoList": [
                {
                    "name": "小猫咪",
                    "fxFileName": f"{first_id}.1.videofx",
                    "fxPackageId": first_id,
                    "fxLicFileName": f"{first_id}.lic",
                    "imageName": f"{first_id}.png",
                    "fitRatio": 127,
                },
                {
                    "name": "切尔西",
                    "fxFileName": f"{first_id}.1.videofx",
                    "fxPackageId": first_id,
                    "fxLicFileName": f"{first_id}.lic",
                    "imageName": f"{first_id}.png",
                    "fitRatio": 127,
                },
                {
                    "name": "蜜桃",
                    "fxFileName": f"{second_id}.1.videofx",
                    "fxPackageId": second_id,
                    "fxLicFileName": f"{second_id}.lic",
                    "imageName": f"{second_id}.png",
                    "fitRatio": 127,
                },
            ]
        }
        (filter_root / "info.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

        blocked = inspect_catalog(filter_root, "assets/filter", "require")
        self.assertIn(
            "FILTER_LICENSE_MISSING",
            {item["code"] for item in blocked["errors"]},
        )
        self.assertEqual(3, blocked["display_item_count"])
        self.assertEqual(2, blocked["unique_package_count"])

        report = inspect_catalog(
            filter_root,
            "app/src/main/assets/beauty/filter",
            "user-confirmed-no-separate-license",
        )
        self.assertEqual([], report["errors"])
        self.assertEqual(3, report["display_item_count"])
        self.assertEqual(2, report["unique_package_count"])
        self.assertEqual(1, report["shared_package_count"])
        mapping = report["asset_map"]["filter-packaged"]
        self.assertEqual(2, len(mapping["packages"]))
        self.assertEqual(3, len(mapping["display_items"]))
        self.assertEqual(
            mapping["display_items"][0]["package"],
            mapping["display_items"][1]["package"],
        )
        self.assertEqual(
            "user-confirmed-no-separate-license",
            mapping["packages"][0]["license_basis"],
        )

    def test_makeup_catalog_inspector_cross_checks_single_and_compose_assets(self) -> None:
        resource_root = self.root / "vendor-delivery"
        single_id = str(uuid.UUID(int=1)).upper()
        compose_id = str(uuid.UUID(int=2)).upper()
        compose_package_id = str(uuid.UUID(int=3)).upper()
        single_dir = resource_root / "skin-items/Lip" / single_id
        compose_dir = resource_root / "looks" / compose_id
        single_dir.mkdir(parents=True)
        compose_dir.mkdir(parents=True)
        (resource_root / "catalogs").mkdir()
        (resource_root / "catalogs/single-index.json").write_text(
            json.dumps(
                [
                    {
                        "displayName": "Lip",
                        "jsonPath": "catalogs/lip-items.json",
                    }
                ]
            ),
            encoding="utf-8",
        )
        (resource_root / "catalogs/lip-items.json").write_text(
            json.dumps(
                [
                    {
                        "name": "Lip item",
                        "cover": (
                            "skin-items/Lip/"
                            f"{single_id}/{single_id}.png"
                        ),
                        "infoPath": (
                            "skin-items/Lip/"
                            f"{single_id}/info.json"
                        ),
                        "parentPath": (
                            "skin-items/Lip/"
                            f"{single_id}"
                        ),
                    }
                ]
            ),
            encoding="utf-8",
        )
        (single_dir / "info.json").write_text(
            json.dumps(
                {
                    "makeupId": "Lip",
                    "effectContent": {
                        "makeupArgs": [
                            {"type": "Lip", "uuid": single_id}
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        (single_dir / f"{single_id}.1.makeup").write_bytes(b"single")
        (single_dir / f"{single_id}.png").write_bytes(b"cover")
        (compose_dir / "descriptor.json").write_text(
            json.dumps(
                {
                    "name": "Compose",
                    "uuid": compose_id,
                    "cover": "cover.png",
                    "effectContent": {
                        "makeup": [
                            {
                                "type": "Lip",
                                "params": [
                                    {
                                        "key": "Makeup Lip Package Id",
                                        "type": "string",
                                        "value": compose_package_id,
                                    }
                                ],
                            }
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        (compose_dir / f"{compose_package_id}.1.makeup").write_bytes(b"compose")
        (compose_dir / "cover.png").write_bytes(b"cover")

        report = inspect_makeup_catalog(resource_root)

        self.assertEqual([], report["errors"])
        self.assertEqual([], report["warnings"])
        self.assertEqual(1, report["single_category_count"])
        self.assertEqual(1, report["single_item_count"])
        self.assertEqual(1, report["compose_item_count"])

    def test_makeup_catalog_inspector_reports_mismatch_and_cover_candidates(self) -> None:
        resource_root = self.root / "another-layout"
        single_id = str(uuid.UUID(int=11)).upper()
        compose_id = str(uuid.UUID(int=12)).upper()
        missing_package_id = str(uuid.UUID(int=13)).upper()
        single_dir = resource_root / "parts/Blusher" / single_id
        compose_dir = resource_root / "presets" / compose_id
        single_dir.mkdir(parents=True)
        compose_dir.mkdir(parents=True)
        (resource_root / "indexes").mkdir()
        (resource_root / "indexes/categories.json").write_text(
            json.dumps(
                [
                    {
                        "displayName": "Blusher",
                        "jsonPath": "indexes/blusher-items.json",
                    }
                ]
            ),
            encoding="utf-8",
        )
        (resource_root / "indexes/blusher-items.json").write_text(
            json.dumps(
                [
                    {
                        "name": "Blusher item",
                        "cover": (
                            "parts/Blusher/"
                            f"{single_id}/{single_id}.png"
                        ),
                        "infoPath": (
                            "parts/Blusher/"
                            f"{single_id}/info.json"
                        ),
                        "parentPath": (
                            "parts/Blusher/"
                            f"{single_id}"
                        ),
                    }
                ]
            ),
            encoding="utf-8",
        )
        (single_dir / "info.json").write_text(
            json.dumps(
                {
                    "makeupId": "Blusher",
                    "effectContent": {
                        "makeupArgs": [
                            {"type": "Eyebrow", "uuid": single_id}
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        (single_dir / f"{single_id}.1.makeup").write_bytes(b"single")
        (single_dir / f"{single_id}.png").write_bytes(b"cover")
        (compose_dir / "preset-metadata.json").write_text(
            json.dumps(
                {
                    "name": "Compose",
                    "uuid": compose_id,
                    "cover": "cover.png",
                    "effectContent": {
                        "makeup": [
                            {
                                "type": "Lip",
                                "params": [
                                    {
                                        "key": "Makeup Lip Package Id",
                                        "type": "string",
                                        "value": missing_package_id,
                                    }
                                ],
                            }
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        (compose_dir / f"{compose_id}.png").write_bytes(b"candidate")

        report = inspect_makeup_catalog(resource_root)
        error_codes = {item["code"] for item in report["errors"]}
        warnings = {
            item["code"]: item for item in report["warnings"]
        }

        self.assertIn("MAKEUP_SINGLE_TYPE_MISMATCH", error_codes)
        self.assertIn("MAKEUP_COMPOSE_PACKAGE_MISSING", error_codes)
        self.assertTrue(
            warnings["MAKEUP_DECLARED_COVER_MISSING"][
                "requires_confirmation"
            ]
        )
        self.assertEqual(
            [f"presets/{compose_id}/{compose_id}.png"],
            warnings["MAKEUP_DECLARED_COVER_MISSING"]["cover_candidates"],
        )

    def test_makeup_catalog_inspector_requests_explicit_ambiguous_metadata(self) -> None:
        resource_root = self.root / "ambiguous-layout"
        compose_id = str(uuid.UUID(int=21)).upper()
        compose_dir = resource_root / "looks" / compose_id
        compose_dir.mkdir(parents=True)
        payload = {
            "name": "Compose",
            "uuid": compose_id,
            "effectContent": {"makeup": []},
        }
        for filename in ("delivery.json", "revised.json"):
            (compose_dir / filename).write_text(
                json.dumps(payload), encoding="utf-8"
            )

        report = inspect_makeup_catalog(resource_root, scope="compose")

        self.assertIn(
            "MAKEUP_COMPOSE_METADATA_AMBIGUOUS",
            {item["code"] for item in report["errors"]},
        )
        self.assertEqual(
            "--compose-metadata",
            report["selection_requests"][0]["argument"],
        )
        self.assertEqual(
            2, len(report["selection_requests"][0]["candidates"])
        )

    def test_apk_report_checks_mapped_project_assets(self) -> None:
        inventory = {
            "prerequisite_gate": {
                "resolved_asset_map": {
                    "filter-packaged": {
                        "items": [
                            {
                                "asset":
                                    "app/src/main/assets/beauty/filter/a.videofx",
                                "license": None,
                            }
                        ]
                    }
                }
            }
        }
        expected = expected_apk_asset_entries(inventory, "app")
        self.assertEqual(
            ["assets/beauty/filter/a.videofx"], expected
        )
        apk = self.root / "app-debug.apk"
        with zipfile.ZipFile(apk, "w") as archive:
            archive.writestr("lib/arm64-v8a/libNvStreamingSdkCore.so", b"so")
        missing = apk_report(apk, self.root, expected)
        self.assertEqual(expected, missing["missing_asset_entries"])

        with zipfile.ZipFile(apk, "a") as archive:
            archive.writestr("assets/beauty/filter/a.videofx", b"filter")
        complete = apk_report(apk, self.root, expected)
        self.assertEqual([], complete["missing_asset_entries"])

    def test_modern_kts_catalog_and_multiline_configuration_are_detected(self) -> None:
        (self.root / "settings.gradle").unlink()
        (self.root / "settings.gradle.kts").write_text(
            'include(\n  ":app",\n  ":effects"\n)\n', encoding="utf-8"
        )
        catalog = self.root / "gradle/libs.versions.toml"
        catalog.parent.mkdir(parents=True, exist_ok=True)
        catalog.write_text(
            '[versions]\nagp = "8.7.0"\n\n[plugins]\n'
            'android-application = { id = "com.android.application", version.ref = "agp" }\n',
            encoding="utf-8",
        )
        (self.root / "app/build.gradle").unlink()
        (self.root / "app/build.gradle.kts").write_text(
            'plugins { alias(libs.plugins.android.application) }\n'
            'android { namespace = "sample.app"\n defaultConfig { applicationId = "sample.app"\n'
            '  ndk { abiFilters += listOf(\n    "arm64-v8a",\n    "armeabi-v7a"\n  ) }\n }\n}\n',
            encoding="utf-8",
        )
        report = inspect_project(self.root, "debug")
        app = next(module for module in report["modules"] if module["name"] == ":app")
        self.assertEqual("application", app["plugin"])
        self.assertEqual(["arm64-v8a", "armeabi-v7a"], app["abi_filters"])

    def test_custom_gl_also_requires_texture_type_dispatch(self) -> None:
        bad = GOOD_SOURCE.replace("output.getTexture().getTextureType() == NV_OES_TEXTURE", "true")
        self.assertIn("OUTPUT_TEXTURE_TYPE_DISPATCH_MISSING", self.codes(bad, profile="custom-gl"))

    def test_custom_effect_requires_register_remove_and_null_guard(self) -> None:
        bad = GOOD_SOURCE.replace("void finish()", 'void custom() { Object fx = createVideoEffect("Confirmed Name"); }\n  void finish()')
        codes = self.codes(bad, profile="custom-gl")
        self.assertTrue({"CUSTOM_EFFECT_NOT_REGISTERED", "CUSTOM_EFFECT_REMOVE_MISSING", "CUSTOM_EFFECT_NULL_UNCHECKED"}.issubset(codes))

    def test_nonbeauty_features_have_feature_level_contracts(self) -> None:
        cases = {
            "makeup-single": "MAKEUP_SINGLE_REGISTRATION_MISSING",
            "makeup-compose": "MAKEUP_COMPOSE_REGISTRATION_MISSING",
            "filter-packaged": "FILTER_REGISTRATION_MISSING",
            "face-prop": "FACE_PROP_REGISTRATION_MISSING",
            "custom-effect": "CUSTOM_EFFECT_REGISTRATION_MISSING",
        }
        for feature, expected in cases.items():
            with self.subTest(feature=feature):
                codes = {
                    item.code
                    for item in validate(
                        self.root, "custom-gl", strict=True, features=[feature]
                    )
                    if item.severity == "ERROR"
                }
                self.assertIn(expected, codes)

    def test_compose_makeup_requires_conflict_state_transaction(self) -> None:
        source = GOOD_SOURCE.replace(
            "void finish()",
            "void compose() { NveEffectKit.getInstance().setMakeup(makeup); "
            "makeup.useComposeMakeup(compose); makeup.removeComposeMakeUp(); }\n"
            "  void finish()",
        )
        make_project(self.root, source)
        errors = {
            item.code
            for item in validate(
                self.root, "custom-gl", strict=True, features=["makeup-compose"]
            )
            if item.severity == "ERROR"
        }
        self.assertIn("MAKEUP_COMPOSE_STATE_TRANSACTION_MISSING", errors)

    def test_compose_plus_single_requires_provenance_and_independent_ui_state(self) -> None:
        source = GOOD_SOURCE.replace(
            "void finish()",
            "void snapshotComposeConflictState() {} "
            "void restoreComposeConflictState() {} "
            "void makeupFlow() { NveEffectKit.getInstance().setMakeup(makeup); "
            "makeup.useComposeMakeup(compose); "
            "makeup.addSingleMakeUp(type, single); "
            "makeup.removeSingleMakeUp(type); "
            "makeup.removeComposeMakeUp(); }\n"
            "  void finish()",
        )
        make_project(self.root, source)
        errors = {
            item.code
            for item in validate(
                self.root,
                "custom-gl",
                strict=True,
                features=["makeup-single", "makeup-compose"],
            )
            if item.severity == "ERROR"
        }
        self.assertIn("MAKEUP_COMPOSE_PROVENANCE_MISSING", errors)
        self.assertIn("MAKEUP_COMPOSE_UI_STATE_SEPARATION_MISSING", errors)

    def test_compose_plus_single_reports_external_policy_and_lifecycle_gaps(self) -> None:
        source = GOOD_SOURCE.replace(
            "void finish()",
            "Object decomposedComposeTypes; Object selectedComposeId; "
            "void snapshotComposeConflictState() {} "
            "void restoreComposeConflictState() {} "
            "void makeupFlow() { NveEffectKit.getInstance().setMakeup(makeup); "
            "makeup.useComposeMakeup(compose); "
            "makeup.addSingleMakeUp(type, single); "
            "makeup.removeSingleMakeUp(type); "
            "makeup.removeComposeMakeUp(); }\n"
            "  void finish()",
        )
        make_project(self.root, source)
        warnings = {
            item.code
            for item in validate(
                self.root,
                "custom-gl",
                strict=True,
                features=["makeup-single", "makeup-compose"],
            )
            if item.severity == "WARNING"
        }
        self.assertIn("MAKEUP_COMPOSE_EXTERNAL_POLICY_UNVERIFIED", warnings)
        self.assertIn("MAKEUP_SELECTION_LIFECYCLE_UNVERIFIED", warnings)

    def test_beauty_list_requires_cover_view(self) -> None:
        source = GOOD_SOURCE.replace(
            "void finish()",
            "void beautyUi() { "
            "String panel = \"NveCompactBeautyPanelView\"; "
            "String itemTag = \"nve-beauty-feature-skin-buffing-skin\"; "
            "String textOnly = \"磨皮\"; }\n"
            "  void finish()",
        )
        make_project(self.root, source)
        errors = {
            item.code
            for item in validate(
                self.root,
                "custom-gl",
                strict=True,
                features=["beauty-skin-buffing-skin"],
            )
            if item.severity == "ERROR"
        }
        self.assertIn("BEAUTY_COVER_VIEW_MISSING", errors)

    def test_compact_beauty_requires_host_cover_publish(self) -> None:
        source = GOOD_SOURCE.replace(
            "void finish()",
            "void setCovers(Map<String, Bitmap> covers) {} "
            "void beautyUi() { "
            "String panel = \"NveCompactBeautyPanelView\"; "
            "String itemTag = \"nve-beauty-feature-skin-buffing-skin\"; "
            "Bitmap cover = null; ImageView image = null; }\n"
            "  void finish()",
        )
        make_project(self.root, source)
        errors = {
            item.code
            for item in validate(
                self.root,
                "custom-gl",
                strict=True,
                features=["beauty-skin-buffing-skin"],
            )
            if item.severity == "ERROR"
        }
        self.assertNotIn("BEAUTY_COVER_VIEW_MISSING", errors)
        self.assertIn("BEAUTY_COVER_DATA_NOT_BOUND", errors)

    def test_compact_beauty_cover_publish_satisfies_ui_contract(self) -> None:
        source = GOOD_SOURCE.replace(
            "void finish()",
            "void setCovers(Map<String, Bitmap> covers) {} "
            "void beautyUi() { "
            "String panelName = \"NveCompactBeautyPanelView\"; "
            "String itemTag = \"nve-beauty-feature-skin-buffing-skin\"; "
            "Bitmap cover = null; ImageView image = null; "
            "panel.setCovers(covers); }\n"
            "  void finish()",
        )
        make_project(self.root, source)
        errors = {
            item.code
            for item in validate(
                self.root,
                "custom-gl",
                strict=True,
                features=["beauty-skin-buffing-skin"],
            )
            if item.severity == "ERROR"
        }
        self.assertNotIn("BEAUTY_COVER_VIEW_MISSING", errors)
        self.assertNotIn("BEAUTY_COVER_DATA_NOT_BOUND", errors)

    def test_makeup_material_list_requires_cover_binding(self) -> None:
        source = GOOD_SOURCE.replace(
            "void finish()",
            "void makeupUi() { NveEffectKit.getInstance().setMakeup(makeup); "
            "makeup.addSingleMakeUp(type, single); "
            "makeup.removeSingleMakeUp(type); "
            "String itemTag = \"nve-makeup-item-lip\"; }\n"
            "  void finish()",
        )
        make_project(self.root, source)
        errors = {
            item.code
            for item in validate(
                self.root,
                "custom-gl",
                strict=True,
                features=["makeup-single"],
            )
            if item.severity == "ERROR"
        }
        self.assertIn("MAKEUP_COVER_BINDING_MISSING", errors)
        self.assertNotIn("MAKEUP_NONE_ICON_CARD_MISSING", errors)

    def test_makeup_material_list_requires_filter_style_none_icon_card(self) -> None:
        source = GOOD_SOURCE.replace(
            "void finish()",
            "void makeupUi() { NveEffectKit.getInstance().setMakeup(makeup); "
            "makeup.addSingleMakeUp(type, single); "
            "makeup.removeSingleMakeUp(type); "
            "Bitmap cover = null; ImageView image = null; "
            "String panel = \"NveCompactMakeupPanelView\"; "
            "String itemTag = \"nve-makeup-item-lip\"; }\n"
            "  void finish()",
        )
        make_project(self.root, source)
        errors = {
            item.code
            for item in validate(
                self.root,
                "custom-gl",
                strict=True,
                features=["makeup-single"],
            )
            if item.severity == "ERROR"
        }
        self.assertNotIn("MAKEUP_COVER_BINDING_MISSING", errors)
        self.assertIn("MAKEUP_NONE_ICON_CARD_MISSING", errors)

    def test_makeup_cover_and_none_icon_card_satisfy_default_ui_contract(self) -> None:
        source = GOOD_SOURCE.replace(
            "void finish()",
            "void makeupUi() { NveEffectKit.getInstance().setMakeup(makeup); "
            "makeup.addSingleMakeUp(type, single); "
            "makeup.removeSingleMakeUp(type); "
            "Bitmap cover = null; ImageView image = null; "
            "int noneIconResource = 1; "
            "String panel = \"NveCompactMakeupPanelView\"; "
            "String itemTag = \"nve-makeup-item-lip\"; }\n"
            "  void finish()",
        )
        make_project(self.root, source)
        errors = {
            item.code
            for item in validate(
                self.root,
                "custom-gl",
                strict=True,
                features=["makeup-single"],
            )
            if item.severity == "ERROR"
        }
        self.assertNotIn("MAKEUP_COVER_BINDING_MISSING", errors)
        self.assertNotIn("MAKEUP_NONE_ICON_CARD_MISSING", errors)

    def test_face_prop_clear_without_disable_is_rejected(self) -> None:
        source = GOOD_SOURCE.replace(
            "void finish()",
            "void clearProp() { NveEffectKit.getInstance().setFaceProp(faceProp); "
            "faceProp.setPropId(\"\"); }\n  void finish()",
        )
        make_project(self.root, source)
        errors = {
            item.code
            for item in validate(
                self.root, "custom-gl", strict=True, features=["face-prop"]
            )
            if item.severity == "ERROR"
        }
        self.assertIn("FACE_PROP_DISABLE_INCOMPLETE", errors)

    def test_unrelated_model_gating_is_reported_for_requested_feature(self) -> None:
        source = GOOD_SOURCE.replace(
            "runOnUiThread(() -> NveEffectKit.getInstance().initialize());",
            "boolean propFace = initializePropModel(config, PropModel.FACE);\n"
            "    sdkReady = advancedBeauty && propFace;\n"
            "    runOnUiThread(() -> NveEffectKit.getInstance().initialize());",
        )
        make_project(self.root, source)
        runtime_codes = {
            item.code
            for item in validate(
                self.root, "camera2-oes-nv21", strict=True, features=["face-prop"]
            )
            if item.severity == "RUNTIME_REQUIRED"
        }
        self.assertIn("UNRELATED_MODEL_GATES_FEATURE", runtime_codes)

    def test_face_prop_ternary_disable_path_is_recognized(self) -> None:
        source = GOOD_SOURCE.replace(
            "void finish()",
            "void applyProp(Object option) { boolean enabled = option != null; "
            "faceProp.setEnable(enabled); NveEffectKit.getInstance().setFaceProp(faceProp); "
            "faceProp.setPropId(option == null ? \"\" : option.packageId); }\n  void finish()",
        )
        make_project(self.root, source)
        errors = {
            item.code
            for item in validate(
                self.root, "camera2-oes-nv21", strict=True, features=["face-prop"]
            )
            if item.severity == "ERROR"
        }
        self.assertNotIn("FACE_PROP_DISABLE_INCOMPLETE", errors)

    def test_camerax_buffer_contract_accepts_closed_image_proxy(self) -> None:
        source = GOOD_SOURCE.replace(
            "void frames()",
            "Object ImageAnalysis, BUFFER_TEXTURE; void analyze(ImageProxy image) { image.close(); }\n  void frames()",
        )
        codes = self.codes(source, profile="camerax-buffer")
        self.assertFalse({"CAMERAX_ANALYZER_MISSING", "IMAGE_PROXY_CLOSE_MISSING", "BUFFER_RENDER_MODE_MISSING"} & codes)

    def test_camerax_buffer_requires_image_proxy_close(self) -> None:
        source = GOOD_SOURCE.replace(
            "void frames()",
            "Object ImageAnalysis, BUFFER_BUFFER; void analyze(ImageProxy image) { use(image); }\n  void frames()",
        )
        self.assertIn("IMAGE_PROXY_CLOSE_MISSING", self.codes(source, profile="camerax-buffer"))

    def test_release_evidence_rejects_string_booleans(self) -> None:
        evidence_path = self.root / "evidence.json"
        evidence_path.write_text(
            '{"apk_sha256":"abc","device_matrix":["model"],"features":["beauty"],'
            '"effect_strengths":"true","feature_checks":{"beauty":{"visible":"true",'
            '"switch_remove_restore":true,"failure_fallback":true}},"interaction_repetitions":10,'
            '"lifecycle_repetitions":10,"four_display_rotations":"true","front_back_camera":true,'
            '"failure_fallback":true,"gl_context_recreated":true,"output_texture_types_verified":true,'
            '"recycle_stress_verified":true,"logcat_clean":true,"performance":{"fps":30,'
            '"frame_time_ms":33,"memory_mb":200,"temperature_note":"stable"}}',
            encoding="utf-8",
        )
        ready, _, missing = validate_manual_evidence(
            evidence_path, ["beauty-skin-buffing-skin"], "abc"
        )
        self.assertFalse(ready)
        self.assertTrue(any("effect_strengths" in item or "four_display_rotations" in item for item in missing))

    def test_recording_evidence_requires_only_recording_matrix(self) -> None:
        evidence_path = self.root / "recording-evidence.json"
        recording_checks = contract_report(["recording"])["manual_evidence"][
            "recording_checks"
        ]
        evidence_path.write_text(
            json.dumps(
                {
                    "apk_sha256": "abc",
                    "device_matrix": ["arm64-device"],
                    "features": ["recording"],
                    "lifecycle_repetitions": 10,
                    **{key: True for key in recording_checks},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        ready, _, missing = validate_manual_evidence(
            evidence_path, ["recording"], "abc", "custom-gl"
        )
        self.assertTrue(ready, missing)

    def test_apk_native_core_is_required_only_for_nve_capabilities(self) -> None:
        apk = {"native_core_entries": [], "missing_asset_entries": []}
        self.assertEqual([], apk_contract_blockers(apk, False))
        self.assertTrue(
            any(
                "libNvStreamingSdkCore.so" in item
                for item in apk_contract_blockers(apk, True)
            )
        )

    def test_release_evidence_rejects_unresolved_product_scope(self) -> None:
        evidence_path = self.root / "partial-beauty-evidence.json"
        evidence_path.write_text(
            '{"apk_sha256":"abc","device_matrix":["model"],'
            '"features":["beauty-skin","beauty-shape","beauty-micro-shape"],'
            '"feature_checks":{},"interaction_repetitions":10,'
            '"lifecycle_repetitions":10,"four_display_rotations":true,'
            '"front_back_camera":true,"failure_fallback":true,'
            '"gl_context_recreated":true,"output_texture_types_verified":true,'
            '"recycle_stress_verified":true,"logcat_clean":true,'
            '"performance":{"fps":30,"frame_time_ms":33,"memory_mb":200,'
            '"temperature_note":"stable"}}',
            encoding="utf-8",
        )
        ready, _, missing = validate_manual_evidence(
            evidence_path, ["美颜"], "abc"
        )
        self.assertFalse(ready)
        self.assertTrue(any("具体调节项" in item for item in missing))

    def test_single_makeup_leaf_custom_gl_needs_no_strength_or_camera_matrix(self) -> None:
        evidence_path = self.root / "makeup-evidence.json"
        evidence_path.write_text(
            '{"apk_sha256":"abc","device_matrix":["arm64-device"],'
            '"features":["makeup-single-lip"],"feature_checks":{"makeup-single-lip":{'
            '"visible":true,"switch_remove_restore":true,"failure_fallback":true}},'
            '"interaction_repetitions":10,"lifecycle_repetitions":10,'
            '"failure_fallback":true,"gl_context_recreated":true,'
            '"output_texture_types_verified":true,"output_texture_dimensions_verified":true,'
            '"recycle_stress_verified":true,"no_black_frames":true,"no_flash":true,'
            '"no_geometry_jump":true,"raw_effect_visual_contract_verified":true,'
            '"logcat_clean":true,"performance":{"fps":30,"frame_time_ms":33,'
            '"memory_mb":200,"temperature_note":"stable"}}',
            encoding="utf-8",
        )
        ready, _, missing = validate_manual_evidence(
            evidence_path, ["makeup-single-lip"], "abc", "custom-gl"
        )
        self.assertTrue(ready, missing)

    def test_sensitive_values_and_device_serial_are_redacted(self) -> None:
        source = (
            "https://user:pass@example.invalid/file?token=TOPSECRET&x=1\n"
            "Authorization: Bearer SECONDSECRET\n"
            "device emulator-5554"
        )
        result = sanitize_report({"output": source}, self.root, "emulator-5554")
        self.assertNotIn("TOPSECRET", result["output"])
        self.assertNotIn("SECONDSECRET", result["output"])
        self.assertNotIn("user:pass", result["output"])
        self.assertNotIn("emulator-5554", result["output"])
        self.assertIn("<redacted>", redact_text(source, self.root))

    def test_gradle_wrapper_matches_host_platform(self) -> None:
        (self.root / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
        (self.root / "gradlew.bat").write_text("@echo off\n", encoding="utf-8")
        expected = "gradlew.bat" if sys.platform.startswith("win") else "gradlew"
        self.assertEqual(expected, Path(gradle_wrapper(self.root)[0]).name)

    @mock.patch("verify_nve_delivery.gradle_wrapper", return_value=["gradle-wrapper"])
    @mock.patch("verify_nve_delivery.run")
    def test_delivery_gate_fails_fast_by_default(
        self, run_mock: mock.Mock, _wrapper_mock: mock.Mock
    ) -> None:
        run_mock.return_value = {
            "command": [],
            "exit_code": 1,
            "duration_seconds": 0.0,
            "output_tail": "unit failure",
        }
        with redirect_stdout(io.StringIO()):
            code = verify_main(
                [
                    str(self.root),
                    "--module",
                    "app",
                    "--variant",
                    "debug",
                    "--features",
                    "beauty-skin-buffing-skin",
                    "--profile",
                    "camera2-oes-nv21",
                ]
            )
        self.assertEqual(1, code)
        self.assertEqual(1, run_mock.call_count)
        command = run_mock.call_args.args[0]
        self.assertTrue(any("testDebugUnitTest" in item for item in command))
        self.assertTrue(any("assembleDebug" in item for item in command))

    def test_cli_values_reject_empty_or_path_like_input(self) -> None:
        errors = validate_cli_values("../app", "../debug", [])
        self.assertEqual(3, len(errors))
        self.assertTrue(validate_cli_values("..", "debug", ["beauty"]))

    def test_missing_project_root_is_not_created(self) -> None:
        missing = self.root / "does-not-exist"
        with redirect_stdout(io.StringIO()):
            code = verify_main([str(missing), "--module", "app", "--variant", "debug", "--features", "beauty", "--profile", "custom-gl"])
        self.assertEqual(2, code)
        self.assertFalse(missing.exists())

    @mock.patch("verify_nve_delivery.time.sleep", return_value=None)
    @mock.patch("verify_nve_delivery.run")
    def test_device_smoke_rejects_failed_launch(self, run_mock: mock.Mock, _sleep: mock.Mock) -> None:
        def fake_run(command: list[str], _root: Path, *_args: object, **_kwargs: object) -> dict[str, object]:
            output = ""
            exit_code = 0
            if "resolve-activity" in command:
                output = "sample.app/.MainActivity"
            elif "start" in command and "-W" in command:
                exit_code = 1
                output = "Error: Activity not started"
            elif "date" in command:
                output = "1721476800"
            return {"command": command, "exit_code": exit_code, "duration_seconds": 0.0, "output_tail": output}

        run_mock.side_effect = fake_run
        _, blockers, _ = device_smoke_gate("adb", "serial", "sample.app", self.root)
        self.assertTrue(any("冷启动失败" in blocker for blocker in blockers))

    def test_logcat_epoch_filter_drops_old_entries(self) -> None:
        result = {"exit_code": 0, "output_tail": "100.1 old\n200.2 current\nnoise"}
        self.assertEqual("200.2 current", logcat_since_epoch(result, 150)["output_tail"])

    @mock.patch("verify_nve_delivery.run")
    def test_process_resolution_accepts_colon_process(self, run_mock: mock.Mock) -> None:
        run_mock.side_effect = [
            {"command": [], "exit_code": 1, "duration_seconds": 0.0, "output_tail": ""},
            {
                "command": [],
                "exit_code": 0,
                "duration_seconds": 0.0,
                "output_tail": "u0_a123 4321 1 0 0 0 S sample.app:camera",
            },
        ]
        process_ids, steps = resolve_process_ids("adb", "serial", "sample.app", self.root)
        self.assertEqual({"4321"}, process_ids)
        self.assertEqual("process-list-fallback", steps[-1]["name"])

    def test_logcat_report_keeps_only_app_and_failure_lines(self) -> None:
        result = {
            "exit_code": 0,
            "output_tail": "200.1 1000 999 999 E FATAL EXCEPTION unrelated\n"
            "200.2 1000 123 123 E FATAL EXCEPTION app\n"
            "200.3 sample.app started\n200.4 NveEffectKit error",
        }
        filtered = relevant_logcat(result, "sample.app", {"123"})["output_tail"]
        self.assertNotIn("unrelated", filtered)
        self.assertIn("FATAL EXCEPTION app", filtered)
        self.assertIn("sample.app", filtered)
        self.assertIn("NveEffectKit", filtered)

    @mock.patch("verify_nve_delivery.run")
    def test_test_only_install_retries_with_t_flag_only_after_explicit_failure(
        self, run_mock: mock.Mock
    ) -> None:
        run_mock.side_effect = [
            {
                "command": [],
                "exit_code": 1,
                "duration_seconds": 0.0,
                "output_tail": "Failure [INSTALL_FAILED_TEST_ONLY]",
            },
            {
                "command": [],
                "exit_code": 0,
                "duration_seconds": 0.0,
                "output_tail": "Success",
            },
        ]
        steps, result = install_apk(
            "adb", "serial", self.root / "app-debug.apk", self.root
        )
        self.assertEqual(0, result["exit_code"])
        self.assertEqual(
            ["adb-install", "adb-install-test-only"],
            [step["name"] for step in steps],
        )
        self.assertNotIn("-t", run_mock.call_args_list[0].args[0])
        self.assertIn("-t", run_mock.call_args_list[1].args[0])

    @mock.patch("verify_nve_delivery.time.sleep", return_value=None)
    @mock.patch("verify_nve_delivery.run")
    def test_device_smoke_rejects_logcat_failure(self, run_mock: mock.Mock, _sleep: mock.Mock) -> None:
        def fake_run(command: list[str], _root: Path, *_args: object, **_kwargs: object) -> dict[str, object]:
            output = ""
            exit_code = 0
            if "resolve-activity" in command:
                output = "sample.app/.MainActivity"
            elif "date" in command:
                output = "1721476800"
            elif "start" in command and "-W" in command:
                output = "Status: ok"
            elif "pidof" in command:
                output = "1234"
            elif "dumpsys" in command:
                output = "mResumedActivity: sample.app/.MainActivity"
            elif "logcat" in command:
                exit_code = 1
                output = "logcat unavailable"
            return {"command": command, "exit_code": exit_code, "duration_seconds": 0.0, "output_tail": output}

        run_mock.side_effect = fake_run
        _, blockers, _ = device_smoke_gate("adb", "serial", "sample.app", self.root)
        self.assertIn("设备日志采集失败", blockers)


if __name__ == "__main__":
    unittest.main()
