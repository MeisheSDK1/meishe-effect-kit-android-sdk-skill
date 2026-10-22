from __future__ import annotations

import json
import re
import shutil
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
TEST_WORK_ROOT = Path(__file__).resolve().parents[3] / ".nve-skill-structure-tests"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))
from materialize_baseline_templates import (
    build_plan,
    existing_targets,
    gradle_dependency_locations,
    load_makeup_catalog_report,
    normalize_ui_profiles,
    partition_existing_targets,
    resolve_input_path,
    resolve_inside_project,
    select_templates,
    shared_component_issues,
    template_runtime_dependencies,
    unresolved_scopes,
    verify_baseline_aar,
)
from materialize_recording_templates import (
    build_plan as build_recording_plan,
    resolve_inside_project as resolve_recording_inside_project,
)

REFERENCES = SKILL_ROOT / "references"
SKILL_MD = SKILL_ROOT / "SKILL.md"


@contextmanager
def writable_test_directory(name: str):
    TEST_WORK_ROOT.mkdir(parents=True, exist_ok=True)
    root = TEST_WORK_ROOT / name
    if root.exists():
        shutil.rmtree(root)
    root.mkdir()
    try:
        yield root
    finally:
        if root.exists():
            shutil.rmtree(root)
FORBIDDEN_SUFFIXES = {
    ".aar",
    ".arscene",
    ".dat",
    ".facemesh",
    ".jar",
    ".lic",
    ".makeup",
    ".model",
    ".so",
    ".videofx",
    ".warp",
}
EXPECTED_REPORT_SCHEMAS = {
    "inspect_android_project.py": 9,
    "inspect_filter_catalog.py": 1,
    "inspect_makeup_catalog.py": 1,
    "materialize_baseline_templates.py": 5,
    "materialize_recording_templates.py": 1,
    "validate_nve_integration.py": 4,
    "verify_makeup_interactions.py": 1,
    "verify_nve_delivery.py": 6,
}
AUTOMATED_DELIVERY_STATES = {"BLOCKED", "STATIC_READY", "DEVICE_VERIFIED"}


def markdown_targets(text: str) -> list[str]:
    return re.findall(r"\[[^\]]*\]\(([^)]+)\)", text)


class SkillStructureTests(unittest.TestCase):
    def test_first_use_sdk_notice_is_exact_and_precedes_workflow(self) -> None:
        skill = SKILL_MD.read_text(encoding="utf-8")
        notice = (
            "版本提示：当前 Skill 基于美摄 SDK 3.16.1，并向后兼容；"
            "若目标项目使用更高版本 SDK，可能存在效果差异，请从"
            "[美摄官网开发者中心](https://www.meishesdk.com/downloads/)"
            "获取对应版本的 Skill。"
        )
        before_workflow = skill.split("## 固定五步", 1)[0]

        self.assertIn(notice, before_workflow)
        self.assertEqual(1, skill.count(notice))
        self.assertIn("每个新任务首次使用", before_workflow)
        self.assertIn("同一任务后续响应不重复", before_workflow)
        self.assertIn("不要求用户确认", before_workflow)
        self.assertIn("提示后继续执行", before_workflow)
        self.assertNotIn("Skill 版本", notice)

    def test_every_reference_is_directly_indexed_by_skill(self) -> None:
        targets = {
            target.split("#", 1)[0].replace("\\", "/")
            for target in markdown_targets(SKILL_MD.read_text(encoding="utf-8"))
            if target.startswith("references/")
        }
        expected = {f"references/{path.name}" for path in REFERENCES.glob("*.md")}
        self.assertEqual(expected, targets)

    def test_all_local_markdown_links_resolve(self) -> None:
        markdown_files = [SKILL_MD, *sorted(REFERENCES.glob("*.md"))]
        missing: list[str] = []
        for document in markdown_files:
            for target in markdown_targets(document.read_text(encoding="utf-8")):
                target = target.strip("<>").split("#", 1)[0]
                if not target or "://" in target or target.startswith(("mailto:", "/")):
                    continue
                if not (document.parent / target).resolve().is_file():
                    missing.append(f"{document.relative_to(SKILL_ROOT)} -> {target}")
        self.assertEqual([], missing)

    def test_compact_beauty_panel_contract_is_preserved(self) -> None:
        ui = (REFERENCES / "ui-integration.md").read_text(encoding="utf-8")
        self.assertIn("## 紧凑美颜面板基线", ui)
        self.assertIn("value → slider → reset → master-control", ui)
        self.assertIn("nve-beauty-reset", ui)
        self.assertIn("nve-beauty-intensity", ui)
        self.assertIn("位于滑块右侧", ui)
        for token in (
            "`64 × 32dp`",
            "`60 × 60dp`",
            "`42 × 42dp`",
            "`38 × 38dp`",
            "`58dp`",
            "`#FFFF6F9F`",
            "只显示当前数值",
            "文字加底部指示条",
            "由“开启”文字和 Switch 组成的整体",
            "`setCovers(...)`",
            "--ui-profile compact-beauty",
        ):
            self.assertIn(token, ui)
        self.assertIn("展开/收起时保持“美颜”文案不变", ui)
        self.assertNotIn("展开时文案简化为“收起”", ui)

    def test_compact_makeup_panel_contract_is_preserved(self) -> None:
        ui = (REFERENCES / "ui-integration.md").read_text(encoding="utf-8")
        makeup = (REFERENCES / "makeup.md").read_text(encoding="utf-8")
        for token in (
            "## 紧凑美妆面板基线",
            "--ui-profile compact-makeup",
            "具体效果项行 → 一级分类行",
            "入口始终显示“美妆”",
            "不得插入虚拟且默认选中的“组合妆”项",
            "`60 × 68dp`",
            "`6dp`",
            "`44 × 44dp`",
            "`noneIconResource`",
            "选中时改变预览框描边和图标颜色",
            "分类切换和列表重建不得执行 assets、文件或网络 I/O",
            "nve-makeup-entry",
        ):
            self.assertIn(token, ui)
        for token in (
            "### 组合妆与单妆叠加契约",
            "妆容视为基础层",
            "妆容入口保持选中",
            "后续显式选择的单妆",
            "UI 的妆容选中态与 SDK 当前是否仍持有",
        ):
            self.assertIn(token, makeup)

    def test_compact_filter_panel_visual_contract_is_preserved(self) -> None:
        ui = (REFERENCES / "ui-integration.md").read_text(encoding="utf-8")
        for token in (
            "## 紧凑滤镜与道具面板基线",
            "--ui-profile compact-filter",
            "`60 × 68dp`",
            "`48 × 48dp`",
            "`44 × 44dp`",
            "名称不得进入封面框或挤占 Bitmap 空间",
            "未提供或解码失败时保留中性预览框和正式名称",
            "选中时只把**封面框**改为 `2dp` 强调色描边",
            "`noneIconResource`",
            "value → slider → reset",
            "`48 × 30dp`",
            "无描边、无阴影、无强调色胶囊实底",
        ):
            self.assertIn(token, ui)

    def test_compact_face_prop_panel_visual_contract_is_preserved(self) -> None:
        ui = (REFERENCES / "ui-integration.md").read_text(encoding="utf-8")
        for token in (
            "道具卡片与滤镜卡片保持相同的",
            "`60 × 68dp` 触摸区",
            "`48 × 48dp` 预览框",
            "`44 × 44dp` 图片区",
            "选中描边只画在预览框",
            "条目根容器不得再画第二层边框",
            "`GONE` 释放高度",
            "使预览框在触摸区内上下居中",
            "当且仅当当前列表至少一个具体道具有可见名称时",
            "默认模板通过 `noneIconResource` 复用滤镜的“无”图标资源",
        ):
            self.assertIn(token, ui)

    def test_recording_contract_preserves_pipeline_defects_and_ui(self) -> None:
        recording = (REFERENCES / "recording.md").read_text(encoding="utf-8")
        skill = SKILL_MD.read_text(encoding="utf-8")
        host_contract = json.loads(
            (SKILL_ROOT / "assets/host-capabilities.json").read_text(
                encoding="utf-8"
            )
        )
        for token in (
            "requires_nve_prerequisites=false",
            "不表示项目可以没有 NVE 输出管线",
            "NVE_FINAL_OUTPUT_SOURCE_MISSING",
            "不覆盖通用相机录制",
            "禁止把原始相机纹理",
            "3 个自有纹理槽",
            "槽位忙时丢弃最新录制帧",
            "第一次 encoder swap 成功后",
            "recorderStartSystemNs = System.nanoTime()",
            "不得把第一帧归一化为 `0` 或 `1ns`",
            "`RELATIVE_PATH=DCIM/Camera`",
            "禁止先写应用",
            "`MediaRecorder.stop()` 前保持 encoder EGLSurface",
            "先按上述流程停止并保存",
            "`44 × 44dp`",
            "`GradientDrawable.OVAL`",
            "不显示持久“● 录制中”文本",
            "连续 10 次前后台",
        ):
            self.assertIn(token, recording)
        self.assertIn("仅为已有 NVE 最终输出管线增加 `recording` 时报告必须为 `host-only`", skill)
        self.assertIn("仅接入 `recording` 不执行上述 NVE 特效阶段门禁", skill)
        capability = host_contract["capabilities"]["recording"]
        self.assertTrue(capability["host_capability"])
        self.assertEqual("nve-final-output-only", capability["scope"])
        self.assertEqual(
            "bottom-center-action-no-panel", capability["ui_behavior"]
        )
        self.assertEqual(
            ["beauty", "makeup", "recording", "filter", "face-prop"],
            host_contract["ui"]["module_entry_order_with_recording"],
        )
        self.assertEqual(
            [44, 44], host_contract["ui"]["recording_entry_size_dp"]
        )

    def test_default_ui_is_only_third_priority_fallback(self) -> None:
        skill = SKILL_MD.read_text(encoding="utf-8")
        ui = (REFERENCES / "ui-integration.md").read_text(encoding="utf-8")
        golden = (REFERENCES / "golden-templates.md").read_text(encoding="utf-8")
        for document, existing_design_token in (
            (skill, "用户工程已有设计体系"),
            (golden, "目标工程已有设计体系"),
        ):
            self.assertLess(
                document.index("用户明确提供的设计"),
                document.index(existing_design_token),
            )
            self.assertIn("只有前两者都不存在", document)
        self.assertIn("只有前两级都不存在时", ui)
        for document in (skill, ui, golden):
            self.assertIn("不访问、不搜索", document)
            self.assertIn("不依赖", document)
        self.assertIn("skill 内已固化并完成设备验收", ui)

    def test_first_integration_routes_to_required_references_and_stage_exits(
        self,
    ) -> None:
        skill = SKILL_MD.read_text(encoding="utf-8")
        step_four = skill.split("### 4. 完成纵向闭环", 1)[1].split(
            "### 5. 验证和交付", 1
        )[0]
        for reference in (
            "project-integration",
            "lifecycle-rendering",
            "assets-and-models",
            "compatibility-contracts",
            "golden-templates",
            "verification",
            "camera2-oes-nv21",
            "recording",
        ):
            self.assertIn(reference, step_four)
        for stage in (
            "原始预览",
            "无效果",
            "单效果",
            "选定范围与恢复",
        ):
            self.assertIn(stage, step_four)
        for policy in (
            "观察未通过就停止",
            "没有设备不等同于",
            "标记运行时未验证",
            "不得继续批量扩展多个效果模块",
        ):
            self.assertIn(policy, step_four)

        step_five = skill.split("### 5. 验证和交付", 1)[1]
        self.assertIn("无设备时不伪造", step_five)

        verification = (REFERENCES / "verification.md").read_text(encoding="utf-8")
        self.assertIn("所选 NVE 能力缺少匹配", verification)
        self.assertIn("仅 `recording` 的 host-only 路径不重新校验", verification)

    def test_compact_entry_bar_visual_and_interaction_contract_is_preserved(
        self,
    ) -> None:
        ui = (REFERENCES / "ui-integration.md").read_text(encoding="utf-8")
        for token in (
            "统一入口栏",
            "入口栏和编辑面板不得同时显示",
            "点击面板外空白区域",
            "面板内部空白不得误收起",
            "`64 × 32dp`",
            "`36dp` 高槽位",
            "`24dp` 圆角",
            "左右和底部",
            "外边距均为 `8dp`",
            "`NveCompactEffectEntryBar`",
            "`createBottomLayoutParams()`",
            "`createPanelLayoutParams()`",
            "`createBottomLayoutParams(bottomInsetPx)`",
        ):
            self.assertIn(token, ui)

        entry_bar = (
            SKILL_ROOT
            / "assets/baseline-3.16.1/ui/NveCompactEffectEntryBar.java.template"
        ).read_text(encoding="utf-8")
        for token in (
            "new LinearLayout.LayoutParams(0, dp(36), 1f)",
            "dp(64), dp(32), Gravity.CENTER",
            "background.setCornerRadius(dp(24))",
            "params.leftMargin = dp(8)",
            "params.rightMargin = dp(8)",
            "params.bottomMargin = dp(8) + Math.max(0, bottomInsetPx)",
            "FrameLayout.LayoutParams createPanelLayoutParams()",
            "panel.collapse()",
            "setEntriesVisible(!expanded)",
            "panel.detachEntryButton()",
        ):
            self.assertIn(token, entry_bar)

        baseline = SKILL_ROOT / "assets" / "baseline-3.16.1" / "ui"
        for filename in (
            "NveCompactBeautyPanelView.java.template",
            "NveCompactMakeupPanelView.java.template",
            "NveCompactFilterPanelView.java.template",
            "NveCompactFacePropPanelView.java.template",
        ):
            panel = (baseline / filename).read_text(encoding="utf-8")
            for token in (
                "implements NveCompactEffectEntryBar.Panel",
                "panel.setClickable(true)",
                "setOnClickListener(view -> collapse())",
                "setClickable(expanded)",
                "setEntryVisible(!expanded)",
                "public View detachEntryButton()",
            ):
                self.assertIn(token, panel, filename)
            self.assertNotIn("panelParams.bottomMargin = dp(58)", panel)

    def test_distribution_contains_no_customer_or_binary_resources(self) -> None:
        forbidden = [
            str(path.relative_to(SKILL_ROOT))
            for path in SKILL_ROOT.rglob("*")
            if path.is_file() and path.suffix.lower() in FORBIDDEN_SUFFIXES
        ]
        self.assertEqual([], forbidden)

    def test_distribution_contains_no_runtime_artifacts(self) -> None:
        forbidden = [
            str(path.relative_to(SKILL_ROOT))
            for path in SKILL_ROOT.rglob("*")
            if path.name == "__pycache__"
            or path.name in {".pytest_cache", ".mypy_cache", ".ruff_cache", ".coverage"}
            or (path.is_file() and path.suffix.lower() in {".pyc", ".pyo"})
            or "build" in path.relative_to(SKILL_ROOT).parts
        ]
        self.assertEqual([], forbidden)

    def test_distribution_has_no_workspace_runtime_references(self) -> None:
        forbidden_patterns = {
            "external reference provenance": re.compile("de" "mo", re.IGNORECASE),
            "source document path": re.compile(r"NveEffectKit 接入文档\.md"),
            "Windows user path": re.compile(r"[A-Za-z]:[\\/]Users[\\/]"),
            "Unix user path": re.compile(r"/(?:Users|home)/[A-Za-z0-9][A-Za-z0-9._-]*/"),
        }
        findings: list[str] = []
        for path in SKILL_ROOT.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {
                ".md",
                ".py",
                ".yaml",
                ".json",
                ".template",
            }:
                continue
            text = path.read_text(encoding="utf-8")
            for label, pattern in forbidden_patterns.items():
                if pattern.search(text):
                    findings.append(f"{path.relative_to(SKILL_ROOT)}: {label}")
        self.assertEqual([], findings)

    def test_agent_metadata_matches_skill_name(self) -> None:
        skill_text = SKILL_MD.read_text(encoding="utf-8")
        name_match = re.search(r"(?m)^name:\s*([a-z0-9-]+)\s*$", skill_text)
        self.assertIsNotNone(name_match)
        name = name_match.group(1)
        metadata = (SKILL_ROOT / "agents/openai.yaml").read_text(encoding="utf-8")
        self.assertIn(f"${name}", metadata)
        self.assertRegex(metadata, r'(?m)^\s*display_name:\s*"[^\"]+"\s*$')
        self.assertRegex(metadata, r'(?m)^\s*short_description:\s*"[^\"]+"\s*$')

    def test_automated_delivery_states_stay_in_sync(self) -> None:
        script = (SKILL_ROOT / "scripts/verify_nve_delivery.py").read_text(encoding="utf-8")
        assigned = set(
            re.findall(
                r'(?:"status"\s*:\s*|report\["status"\]\s*=\s*)"([A-Z_]+)"',
                script,
            )
        )
        self.assertEqual(AUTOMATED_DELIVERY_STATES, assigned)
        for document in (SKILL_MD, REFERENCES / "verification.md"):
            text = document.read_text(encoding="utf-8")
            for state in AUTOMATED_DELIVERY_STATES:
                self.assertIn(f"`{state}`", text)
        self.assertNotRegex(script, r'report\["status"\]\s*=\s*"RELEASE_READY"')

    def test_report_schema_changes_are_intentional(self) -> None:
        for filename, expected in EXPECTED_REPORT_SCHEMAS.items():
            text = (SKILL_ROOT / "scripts" / filename).read_text(encoding="utf-8")
            versions = {int(value) for value in re.findall(r'"schema_version"\s*:\s*(\d+)', text)}
            self.assertEqual({expected}, versions, filename)

    def test_traceable_baseline_is_present(self) -> None:
        baseline = (REFERENCES / "compatibility-contracts.md").read_text(encoding="utf-8")
        catalog = json.loads(
            (
                SKILL_ROOT
                / "assets/baseline-3.16.1/catalog/feature-catalog.json"
            ).read_text(encoding="utf-8")
        )
        self.assertIn("Skill 发布版本", baseline)
        self.assertIn("SDK 基线版本", baseline)
        self.assertIn(
            f"| Skill 发布版本 | `{catalog['skill_version']}` |",
            baseline,
        )
        self.assertIn(
            f"| SDK 基线版本 | `{catalog['sdk_version']}` |",
            baseline,
        )
        self.assertNotIn("内部基线 ID", baseline)
        self.assertRegex(baseline, r"核验日期\s*\|\s*`\d{4}-\d{2}-\d{2}`")
        hashes = re.findall(r"`[0-9a-f]{64}`", baseline)
        self.assertGreaterEqual(len(hashes), 2)

    def test_long_references_have_a_table_of_contents(self) -> None:
        missing = []
        for path in REFERENCES.glob("*.md"):
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) > 100 and "## 目录" not in lines[:20]:
                missing.append(path.name)
        self.assertEqual([], missing)

    def test_baseline_golden_templates_and_catalog_are_complete(self) -> None:
        baseline = SKILL_ROOT / "assets" / "baseline-3.16.1"
        for relative in (
            "common/NveRenderBridge.java.template",
            "common/NveSessionOwner.java.template",
            "common/NveAssetInstaller.java.template",
            "common/NveLatestValueQueue.java.template",
            "camera2/NveCameraOrientation.java.template",
            "camera2/NveCamera2PreviewConfig.java.template",
            "camera2/NveNv21Converter.java.template",
            "camera2/NveCamera2FramePairer.java.template",
            "camera2/NveCamera2RenderInputFactory.java.template",
            "camera2/NveCamera2TexturePresenter.java.template",
            "ui/NveCompactEffectEntryBar.java.template",
            "ui/NveCompactBeautyPanelView.java.template",
            "ui/NveCompactMakeupPanelView.java.template",
            "ui/NveCompactFilterPanelView.java.template",
            "ui/NveCompactFacePropPanelView.java.template",
            "features/NveBeautyCoordinator.java.template",
            "features/NveMakeupCoordinator.java.template",
            "features/NveMakeupSelectionState.java.template",
            "features/NveMakeupAssetRepository.java.template",
            "features/NveMakeupResourceLoader.java.template",
            "features/NveFilterCoordinator.java.template",
            "features/NveFacePropCoordinator.java.template",
            "tests/NveBeautyCoordinatorTest.java.template",
            "tests/NveLatestValueQueueTest.java.template",
            "tests/NveCamera2FramePairerTest.java.template",
            "tests/NveFilterCoordinatorTest.java.template",
            "tests/NveFacePropCoordinatorTest.java.template",
            "tests/NveMakeupSelectionStateTest.java.template",
            "tests/NveMakeupAssetRepositoryTest.java.template",
            "catalog/feature-catalog.json",
        ):
            self.assertTrue((baseline / relative).is_file(), relative)

        catalog = json.loads(
            (baseline / "catalog" / "feature-catalog.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(12, catalog["schema_version"])
        self.assertEqual("1.0.0", catalog["skill_version"])
        self.assertEqual("3.16.1", catalog["sdk_version"])
        self.assertNotIn("baseline_id", catalog)
        self.assertNotIn("baseline_version", catalog)
        self.assertEqual(12, len(catalog["beauty"]["skin"]))
        self.assertEqual(11, len(catalog["beauty"]["shape"]))
        self.assertEqual(21, len(catalog["beauty"]["micro_shape"]))
        self.assertEqual(9, len(catalog["makeup"]["single"]))
        self.assertEqual(
            "NveMakeupResourceLoader",
            catalog["makeup"]["resource_loader_template"],
        )
        self.assertIn(
            "eyeball", {item["id"] for item in catalog["makeup"]["single"]}
        )
        self.assertEqual(
            "android-main-thread-after-gl-ready",
            catalog["runtime_contract"]["initialize_thread"],
        )
        self.assertEqual(
            "resource-worker",
            catalog["runtime_contract"]["asset_prepare_thread"],
        )
        self.assertEqual(
            "android-main-thread",
            catalog["runtime_contract"]["asset_install_thread"],
        )
        self.assertEqual(
            "dedicated-handler-thread-not-ui-main",
            catalog["runtime_contract"]["surface_texture_callback_thread"],
        )
        self.assertEqual(
            "camera-frame-only",
            catalog["runtime_contract"]["preview_render_clock"],
        )
        self.assertEqual(
            "coalesce-latest-value-no-request-render",
            catalog["runtime_contract"]["slider_update_policy"],
        )
        self.assertEqual(
            "effect-kit-clean-up",
            catalog["runtime_contract"]["teardown_order"][-1],
        )
        self.assertEqual(
            [
                "register-beauty",
                "enable-beauty",
                "register-shape",
                "enable-shape",
                "register-micro-shape",
                "enable-micro-shape",
                "reapply-desired-state",
            ],
            catalog["runtime_contract"]["beauty_controller_restore_sequence"],
        )
        self.assertEqual(
            "ui-only-never-reapply-current-effect-value",
            catalog["ui"]["selection_semantics"],
        )
        self.assertEqual(
            6,
            len(catalog["render_profiles"]["camera2-oes-nv21"]["materialized_templates"]),
        )
        self.assertEqual(
            "bounded-reuse-within-100ms-and-reference-counted-release",
            catalog["render_profiles"]["camera2-oes-nv21"][
                "detection_frame_consumption"
            ],
        )
        self.assertEqual(
            "one-time-log-per-texture-type-with-width-height",
            catalog["render_profiles"]["camera2-oes-nv21"][
                "output_observation"
            ],
        )
        self.assertEqual(
            {
                "beauty": "compact-beauty",
                "makeup": "compact-makeup",
                "filter": "compact-filter",
                "face-prop": "compact-face-prop",
            },
            catalog["ui"]["default_profiles"],
        )
        self.assertEqual(
            "text-with-accent-underline",
            catalog["ui"]["beauty_tab_presentation"],
        )
        self.assertEqual(
            "cover-and-label",
            catalog["ui"]["beauty_item_presentation"],
        )
        self.assertEqual([60, 60], catalog["ui"]["beauty_item_size_dp"])
        self.assertEqual([42, 42], catalog["ui"]["beauty_cover_size_dp"])
        self.assertEqual([38, 38], catalog["ui"]["beauty_bitmap_size_dp"])
        self.assertEqual(
            "setCovers-required-after-resource-publish",
            catalog["ui"]["beauty_cover_host_binding"],
        )
        self.assertEqual(
            "only-when-algorithm-changes",
            catalog["beauty"]["skin_selector_update_policy"],
        )
        self.assertEqual(
            ["value", "slider", "reset", "master-control"],
            catalog["ui"]["beauty_adjustment_row_order"],
        )
        self.assertEqual(
            "coordinator-reset-all",
            catalog["ui"]["beauty_reset_owner"],
        )
        self.assertEqual(8, catalog["ui"]["beauty_reset_master_gap_dp"])
        self.assertEqual(
            "label-and-switch",
            catalog["ui"]["beauty_master_presentation"],
        )

        self.assertEqual(
            ["effect-items", "primary-categories", "status"],
            catalog["ui"]["makeup_row_order"],
        )
        self.assertEqual(
            "cover-and-label",
            catalog["ui"]["makeup_item_presentation"],
        )
        self.assertEqual([60, 68], catalog["ui"]["makeup_item_size_dp"])
        self.assertEqual([44, 44], catalog["ui"]["makeup_cover_size_dp"])
        self.assertEqual(
            "resource-worker",
            catalog["ui"]["makeup_cover_decode_owner"],
        )
        self.assertEqual(
            "filter-style-icon-card",
            catalog["ui"]["makeup_none_presentation"],
        )
        self.assertEqual(
            ["effect-items", "value-slider-reset", "status"],
            catalog["ui"]["filter_row_order"],
        )
        self.assertEqual(
            "cover-frame-with-external-label",
            catalog["ui"]["filter_item_presentation"],
        )
        self.assertEqual([60, 68], catalog["ui"]["filter_item_size_dp"])
        self.assertEqual(
            [48, 48], catalog["ui"]["filter_preview_frame_size_dp"]
        )
        self.assertEqual([44, 44], catalog["ui"]["filter_bitmap_size_dp"])
        self.assertEqual(
            "preview-frame-2dp-accent-stroke",
            catalog["ui"]["filter_selection_presentation"],
        )
        self.assertEqual(
            "flat-translucent-no-stroke-no-elevation",
            catalog["ui"]["filter_reset_presentation"],
        )
        self.assertEqual(
            "stable-module-label-selected-state-only",
            catalog["ui"]["entry_label_behavior"],
        )
        self.assertTrue(catalog["ui"]["ui_profiles_repeatable"])
        self.assertEqual(
            "multiple-real-assets",
            catalog["makeup"]["compose_items"],
        )
        self.assertEqual(
            "latest-generation-all-dependencies-atomic",
            catalog["makeup"]["resource_publish_policy"],
        )
        self.assertEqual(
            [64, 32], catalog["ui"]["design_tokens"]["entry_size_dp"]
        )
        self.assertEqual(
            "match-parent-width-equal-weight-slots",
            catalog["ui"]["entry_bar_layout"],
        )
        self.assertEqual(
            "mutually-exclusive",
            catalog["ui"]["entry_panel_visibility"],
        )
        self.assertEqual(
            "outside-blank-area-only",
            catalog["ui"]["panel_collapse_gesture"],
        )
        self.assertEqual(
            "consume-without-collapse",
            catalog["ui"]["panel_internal_touch_behavior"],
        )
        self.assertEqual(0, catalog["ui"]["panel_bottom_margin_dp"])
        self.assertEqual(
            "host-window-inset-plus-8dp",
            catalog["ui"]["entry_bar_bottom_inset_policy"],
        )
        self.assertEqual(
            "match-parent-transparent-overlay",
            catalog["ui"]["panel_host_layout"],
        )
        self.assertEqual(58, catalog["ui"]["design_tokens"]["value_width_dp"])

    def test_runtime_templates_preserve_verified_restore_contracts(self) -> None:
        baseline = SKILL_ROOT / "assets" / "baseline-3.16.1"
        session = (baseline / "common/NveSessionOwner.java.template").read_text(
            encoding="utf-8"
        )
        installer = (baseline / "common/NveAssetInstaller.java.template").read_text(
            encoding="utf-8"
        )
        makeup_repository = (
            baseline / "features/NveMakeupAssetRepository.java.template"
        ).read_text(encoding="utf-8")
        filter_template = (
            baseline / "features/NveFilterCoordinator.java.template"
        ).read_text(encoding="utf-8")
        prop_template = (
            baseline / "features/NveFacePropCoordinator.java.template"
        ).read_text(encoding="utf-8")
        beauty_template = (
            baseline / "features/NveBeautyCoordinator.java.template"
        ).read_text(encoding="utf-8")
        presenter_template = (
            baseline / "camera2/NveCamera2TexturePresenter.java.template"
        ).read_text(encoding="utf-8")
        pairer_template = (
            baseline / "camera2/NveCamera2FramePairer.java.template"
        ).read_text(encoding="utf-8")

        for token in (
            "ControllerRestorer",
            "initializeAfterGlReady(ControllerRestorer... restorers)",
            "restorer.restore(effectKit)",
            "NveDetectionModelType.EYEBALL",
            "canApplyMakeup()",
            "canApplyFaceProp()",
            "if (state == State.UNINITIALIZED || state == State.SDK_READY)",
        ):
            self.assertIn(token, session)
        for token in (
            "ASSET_PACKAGE_TYPE_VIDEOFX",
            "ASSET_PACKAGE_TYPE_ARSCENE",
            "isUsablePackageId(packageId)",
            "import android.os.Looper;",
            "requireMainThread();",
            "Looper.myLooper() != Looper.getMainLooper()",
        ):
            self.assertIn(token, installer)
        self.assertIn("mainThreadExecutor.execute(() ->", makeup_repository)
        self.assertNotIn("worker.execute(() ->", makeup_repository)
        for token in (
            "desiredPackageId",
            "samePackage",
            "void setIntensity(float intensity)",
            "void restoreAfterInitialize()",
            "backend.add(desiredPackageId)",
        ):
            self.assertIn(token, filter_template)
        for token in (
            "interface Backend",
            'backend.apply(enabled ? desiredPackageId : "", enabled)',
            "void restoreAfterInitialize()",
        ):
            self.assertIn(token, prop_template)
        restore_start = beauty_template.index("void registerInBaselineOrder")
        restore_end = beauty_template.index("\n    void setSkin", restore_start)
        restore = beauty_template[restore_start:restore_end]
        expected_order = (
            "effectKit.setBeauty(beauty)",
            "beauty.setEnable(true)",
            "effectKit.setShape(shape)",
            "shape.setEnable(true)",
            "effectKit.setMicroShape(microShape)",
            "microShape.setEnable(true)",
            "applyCurrentMasterState()",
        )
        positions = [restore.index(token) for token in expected_order]
        self.assertEqual(sorted(positions), positions)
        self.assertIn("void resetAll()", beauty_template)
        self.assertIn('activeBlur = "buffing-skin"', beauty_template)
        self.assertIn('activeWhitening = "whitening-a"', beauty_template)
        self.assertIn("NVE output type=OES size=", presenter_template)
        self.assertIn("NVE output type=2D size=", presenter_template)
        self.assertNotIn("renderEffect()", pairer_template)

    def test_beauty_master_off_explicitly_neutralizes_geometry(self) -> None:
        template = (
            SKILL_ROOT
            / "assets/baseline-3.16.1/features/NveBeautyCoordinator.java.template"
        ).read_text(encoding="utf-8")
        reference = (REFERENCES / "beauty.md").read_text(encoding="utf-8")

        for token in (
            "void setMasterEnabled(boolean enabled)",
            "applyAllNeutralValues()",
            "applyGeometryValues(false)",
            "shape.setDegreeByType(type, applied)",
            "microShape.setDegreeByType(type, applied)",
            "return effectiveValue(masterEnabled, desiredValue);",
            "return enabled ? desiredValue : 0f;",
            "shouldChangeSkinSelector(id, appliedBlur, appliedWhitening)",
            "if (selectorChanged)",
            "appliedSkinValues.put(id, value)",
        ):
            self.assertIn(token, template)
        self.assertNotIn("microShape.setEnable(enabled)", template)
        self.assertIn("NveMicroShape.setEnable(false)", reference)
        self.assertIn("setDegreeByType(type, 0f)", reference)
        self.assertIn("不得在每个刻度重复调用相同的 `setBlurType`", reference)

    def test_camera_pairer_reuses_recent_detection_frame_with_safe_buffer_ownership(self) -> None:
        template = (
            SKILL_ROOT
            / "assets/baseline-3.16.1/camera2/NveCamera2FramePairer.java.template"
        ).read_text(encoding="utf-8")
        converter = (
            SKILL_ROOT
            / "assets/baseline-3.16.1/camera2/NveNv21Converter.java.template"
        ).read_text(encoding="utf-8")
        regression = (
            SKILL_ROOT
            / "assets/baseline-3.16.1/tests/NveCamera2FramePairerTest.java.template"
        ).read_text(encoding="utf-8")

        for token in (
            "AtomicReference<PendingImage>",
            "ConcurrentLinkedQueue<byte[]>",
            "frame.readers.incrementAndGet()",
            "void releaseFrame(Frame frame)",
            "synchronized (conversionLock)",
            "long timestampDeltaNs = frame.timestampNs - oesTimestampNs",
            "void publishConvertedFrame(Frame frame)",
            "boolean hasFrame(int generation)",
        ):
            self.assertIn(token, template)
        for token in (
            "static boolean convert(Image image, byte[] output)",
            "isInterleavedVu",
            "vBuffer.get(output, offset, copyLength)",
        ):
            self.assertIn(token, converter)
        self.assertNotIn("static final class Plane", converter)
        self.assertIn("convertedFrameCanBeReusedForNearbyOesFrames", regression)
        self.assertIn("pairer.releaseFrame(first)", regression)
        self.assertIn("futureDetectionIsRetainedForNextOesFrame", regression)

    def test_camera_preview_config_prefers_720p_and_requests_30fps(self) -> None:
        template = (
            SKILL_ROOT
            / "assets/baseline-3.16.1/camera2/NveCamera2PreviewConfig.java.template"
        ).read_text(encoding="utf-8")
        for token in (
            "PREFERRED_PREVIEW_WIDTH = 1280",
            "PREFERRED_PREVIEW_HEIGHT = 720",
            "PREFERRED_PREVIEW_FPS = 30",
            "chooseSharedPreviewSize",
            "choosePreviewFpsRange",
            "CONTROL_AE_TARGET_FPS_RANGE",
        ):
            self.assertIn(token, template)

    def test_camera_switch_contract_prevents_reader_close_race(self) -> None:
        camera_contract = (
            REFERENCES / "camera2-oes-nv21.md"
        ).read_text(encoding="utf-8")
        lifecycle_contract = (
            REFERENCES / "lifecycle-rendering.md"
        ).read_text(encoding="utf-8")
        verification = (REFERENCES / "verification.md").read_text(encoding="utf-8")

        for token in (
            "DirectByteBuffer.get",
            "收到该设备 `onClosed`",
            "最多一个在途任务",
            "`SurfaceTexture.onFrameAvailable` 都要唤醒 GL",
            "禁止在 `updateTexImage()` 前",
        ):
            self.assertIn(token, camera_contract)
        self.assertIn(
            "reader 关闭与 acquire/所有权转移/convert/close 必须通过同一短临界区",
            lifecycle_contract,
        )
        self.assertIn("前后摄往返切换至少 10 次", verification)
        self.assertIn("Surface 呈现时间戳证明画面仍在刷新", verification)

    def test_template_materializer_selects_scope_and_never_overwrites(self) -> None:
        source_dir = SKILL_ROOT / "generated" / "com" / "example" / "effect"
        plan = build_plan(
            SKILL_ROOT,
            source_dir,
            "com.example.effect",
            "3.16.1",
            ["完整美颜", "完整滤镜"],
            "all",
        )
        self.assertEqual(6, len(plan))
        targets = {item["target"] for item in plan}
        self.assertIn(
            "generated/com/example/effect/NveBeautyCoordinator.java", targets
        )
        self.assertIn(
            "generated/com/example/effect/NveFilterCoordinator.java", targets
        )
        for item in plan:
            self.assertIn("package com.example.effect;", item["content"])
            self.assertNotIn("__TARGET_PACKAGE__", item["content"])

        self.assertEqual(
            ["SKILL.md"],
            existing_targets([{"target": "SKILL.md"}], SKILL_ROOT),
        )

    def test_template_materializer_rejects_project_escape(self) -> None:
        with self.assertRaisesRegex(ValueError, "不得越过项目根目录"):
            resolve_inside_project(SKILL_ROOT, "../outside")

    def test_recording_materializer_is_version_independent_and_output_only(
        self,
    ) -> None:
        source_dir = SKILL_ROOT / "generated-recording" / "main"
        test_dir = SKILL_ROOT / "generated-recording" / "test"
        plan = build_recording_plan(
            SKILL_ROOT,
            source_dir,
            "com.example.recording",
            test_dir,
        )
        self.assertEqual(5, len(plan))
        targets = {item["target"] for item in plan}
        self.assertIn(
            "generated-recording/main/NveFinalTextureFrameSink.java",
            targets,
        )
        self.assertIn(
            "generated-recording/main/NveVideoRecorder.java",
            targets,
        )
        self.assertIn(
            "generated-recording/test/NveVideoRecordingConfigTest.java",
            targets,
        )
        combined = "\n".join(item["content"] for item in plan)
        self.assertIn("package com.example.recording;", combined)
        self.assertIn("NveFinalTextureFrameSink", combined)
        self.assertNotIn("__TARGET_PACKAGE__", combined)
        self.assertNotIn("renderEffect(", combined)
        self.assertNotIn("NveEffectKit.aar", combined)

    def test_recording_materializer_rejects_project_escape(self) -> None:
        with self.assertRaisesRegex(ValueError, "不得越过项目根目录"):
            resolve_recording_inside_project(SKILL_ROOT, "../outside")

    def test_materializer_resolves_relative_inputs_from_project_root(self) -> None:
        self.assertEqual(
            (SKILL_ROOT / "app/libs/NveEffectKit.aar").resolve(),
            resolve_input_path(SKILL_ROOT, Path("app/libs/NveEffectKit.aar")),
        )

    def test_controller_layer_does_not_add_common_or_unrequested_features(self) -> None:
        selected = select_templates(["完整滤镜"], "controllers")
        self.assertEqual(
            ("features/NveFilterCoordinator.java.template",),
            selected,
        )
        with self.assertRaisesRegex(ValueError, "common/all"):
            select_templates(["完整滤镜"], "controllers", "camera2-oes-nv21")

    def test_camera2_profile_materializes_verified_render_components(self) -> None:
        selected = select_templates(["完整美颜"], "all", "camera2-oes-nv21")
        self.assertEqual(11, len(selected))
        self.assertIn(
            "camera2/NveCamera2PreviewConfig.java.template", selected
        )
        self.assertIn(
            "camera2/NveCamera2RenderInputFactory.java.template", selected
        )
        self.assertIn(
            "camera2/NveCamera2TexturePresenter.java.template", selected
        )

        orientation = (
            SKILL_ROOT
            / "assets/baseline-3.16.1/camera2/NveCameraOrientation.java.template"
        ).read_text(encoding="utf-8")
        self.assertIn("360 - normalize(sensor + display)", orientation)
        self.assertIn("normalize(sensor - display)", orientation)

        render_input = (
            SKILL_ROOT
            / "assets/baseline-3.16.1/camera2/NveCamera2RenderInputFactory.java.template"
        ).read_text(encoding="utf-8")
        for token in (
            "setCameraOrientation(sensorOrientation)",
            "setDisplayRotation(previewRotation)",
            "setVerticalMirror(true)",
            "setMirror(frontCamera)",
            "setRowPitchY(frame.width)",
            "VIDEO_FRAME_PIXEL_FROMAT_NV21",
        ):
            self.assertIn(token, render_input)

        presenter = (
            SKILL_ROOT
            / "assets/baseline-3.16.1/camera2/NveCamera2TexturePresenter.java.template"
        ).read_text(encoding="utf-8")
        self.assertIn("Target.bindDisplayTarget()", presenter)
        self.assertIn("texture.getSize()", presenter)
        self.assertIn("GL_TEXTURE_EXTERNAL_OES", presenter)
        self.assertIn("GL_TEXTURE_2D", presenter)
        self.assertIn("lastTexture2dId", presenter)

    def test_compact_beauty_ui_profile_materializes_verified_design_system(self) -> None:
        selected = select_templates(
            ["完整美颜"], "controllers", None, "compact-beauty"
        )
        self.assertEqual(
            (
                "features/NveBeautyCoordinator.java.template",
                "ui/NveCompactEffectEntryBar.java.template",
                "ui/NveCompactBeautyPanelView.java.template",
            ),
            selected,
        )
        with self.assertRaisesRegex(ValueError, "只适用于美颜"):
            select_templates(["滤镜"], "controllers", None, "compact-beauty")

        template = (
            SKILL_ROOT
            / "assets/baseline-3.16.1/ui/NveCompactBeautyPanelView.java.template"
        ).read_text(encoding="utf-8")
        for token in (
            "COLOR_PANEL = 0xF21B1B24",
            "COLOR_ACCENT = 0xFFFF6F9F",
            "dp(68), dp(36)",
            "dp(58), dp(36)",
            "dp(60), dp(60));",
            "params.rightMargin = dp(4)",
            "new LinearLayout.LayoutParams(dp(42), dp(42))",
            "dp(38), dp(38), Gravity.CENTER",
            "masterLabel.setText(\"开启\")",
            "masterControlParams.leftMargin = dp(8)",
            "innerSwitchParams.leftMargin = dp(4)",
            "Button reset = flatActionButton(\"重置\")",
            "void setCovers(Map<String, Bitmap> itemCovers)",
            "private final class BeautyTabView",
            "private final class BeautyItemView",
            'String.format(Locale.US, "%.2f", value)',
            "chip.setSelectionStyle(item == selected)",
            "Selection is UI-only",
        ):
            self.assertIn(token, template)
        selection_region = template[
            template.index("chip.setOnClickListener")
            : template.index("private void setPanelExpanded")
        ]
        self.assertNotIn("listener.onValueChanged", selection_region)

        source_dir = SKILL_ROOT / "generated-ui" / "com" / "example" / "effect"
        plan = build_plan(
            SKILL_ROOT,
            source_dir,
            "com.example.effect",
            "3.16.1",
            ["完整美颜"],
            "all",
            "camera2-oes-nv21",
            "compact-beauty",
        )
        self.assertEqual(13, len(plan))
        self.assertIn(
            "generated-ui/com/example/effect/NveCompactBeautyPanelView.java",
            {item["target"] for item in plan},
        )

    def test_compact_makeup_ui_profile_materializes_verified_state_model(self) -> None:
        selected = select_templates(
            ["完整美妆"], "controllers", None, "compact-makeup"
        )
        self.assertEqual(
            (
                "features/NveMakeupCoordinator.java.template",
                "features/NveMakeupSelectionState.java.template",
                "features/NveMakeupAssetRepository.java.template",
                "ui/NveCompactEffectEntryBar.java.template",
                "ui/NveCompactMakeupPanelView.java.template",
                "features/NveMakeupResourceLoader.java.template",
            ),
            selected,
        )
        with self.assertRaisesRegex(ValueError, "只适用于美妆"):
            select_templates(["完整美颜"], "controllers", None, "compact-makeup")

        template = (
            SKILL_ROOT
            / "assets/baseline-3.16.1/ui/NveCompactMakeupPanelView.java.template"
        ).read_text(encoding="utf-8")
        for token in (
            'entryButton = compactButton("美妆")',
            "entryButton.setSelected(expanded)",
            "List<ComposeItem>",
            "NveMakeupSelectionState selectionState",
            "int noneIconResource",
            'new MakeupItemView(getContext(), "无", noneIconResource)',
            "image.setImageTintList(ColorStateList.valueOf(",
            "Bitmap cover()",
            "class MakeupItemView",
            "image.setImageBitmap(cover)",
            "new LinearLayout.LayoutParams(dp(60), dp(68))",
            "new FrameLayout.LayoutParams(",
            "dp(44), dp(44), Gravity.CENTER",
            "The panel never performs file or network I/O",
            "saveSelectionState()",
            "restoreSelectionState(",
            "listener.onComposeSelected(item.id())",
            "nve-makeup-none-",
        ):
            self.assertIn(token, template)
        self.assertLess(
            template.index("panel.addView(itemScroll"),
            template.index("panel.addView(categoryScroll"),
        )
        panel_expansion = template[
            template.index("private void setPanelExpanded")
            : template.index("private void updateEntryStyle")
        ]
        self.assertNotIn("setText(", panel_expansion)
        self.assertNotIn("nve-makeup-item-compose-component-", template)

        loader = (
            SKILL_ROOT
            / "assets/baseline-3.16.1/features/NveMakeupResourceLoader.java.template"
        ).read_text(encoding="utf-8")
        for token in (
            "String assetRoot",
            "String singleIndexAssetPath",
            "List<String> composeMetadataAssetPaths",
            "String declaredPathPrefix",
            "Set<NveMakeupTypeEnum> selectedSingleTypes",
            "boolean includeCompose",
            "ParseJsonFile.fromJson",
            "NveMakeupAssetRepository.PackageSpec",
        ):
            self.assertIn(token, loader)
        self.assertNotIn("customcompose", loader)
        self.assertNotIn("newCompose", loader)
        inspector = (
            SKILL_ROOT / "scripts/inspect_makeup_catalog.py"
        ).read_text(encoding="utf-8")
        for text in (loader, inspector, SKILL_MD.read_text(encoding="utf-8")):
            self.assertNotIn("customcompose", text)
            self.assertNotIn("newCompose", text)
        self.assertNotRegex(
            loader,
            r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
        )

        catalog = json.loads(
            (
                SKILL_ROOT
                / "assets/baseline-3.16.1/catalog/feature-catalog.json"
            ).read_text(encoding="utf-8")
        )
        dependencies = template_runtime_dependencies(catalog, selected)
        self.assertEqual(
            ["com.google.code.gson:gson:2.8.2"],
            [item["coordinate"] for item in dependencies],
        )

    def test_makeup_catalog_report_and_gradle_dependency_are_hard_gates(self) -> None:
        catalog = json.loads(
            (
                SKILL_ROOT
                / "assets/baseline-3.16.1/catalog/feature-catalog.json"
            ).read_text(encoding="utf-8")
        )
        payload = {
            "schema_version": 1,
            "errors": [],
            "warnings": [],
            "selection_requests": [],
            "single_category_count": 1,
            "single_item_count": 1,
            "compose_item_count": 1,
            "resource_root": "nve",
            "catalog": {
                "single_index": "beauty/makeup/info.json",
                "single_categories": [{"type": "Lip", "items": [{}]}],
                "compose_items": [{"metadata": "beauty/makeup/compose/info.json"}],
            },
        }
        with writable_test_directory(
            "makeup-catalog-and-dependency"
        ) as root:
            report = root / "makeup-catalog.json"
            report.write_text(json.dumps(payload), encoding="utf-8")
            loaded = load_makeup_catalog_report(
                report, catalog, ["makeup-single-lip", "makeup-compose"]
            )
            self.assertEqual(1, loaded["compose_item_count"])

            payload["warnings"] = [{"code": "UNRESOLVED"}]
            report.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "warnings"):
                load_makeup_catalog_report(
                    report, catalog, ["makeup-single-lip", "makeup-compose"]
                )

            app = root / "app"
            app.mkdir()
            build = app / "build.gradle"
            build.write_text(
                "dependencies { implementation 'com.google.code.gson:gson:2.8.2' }\n",
                encoding="utf-8",
            )
            dependency = {
                "coordinate": "com.google.code.gson:gson:2.8.2",
                "required_class": "com.google.gson.Gson",
            }
            self.assertEqual(
                ["app/build.gradle"],
                gradle_dependency_locations(root, dependency),
            )

    def test_existing_shared_templates_reuse_identical_and_guard_installer_types(
        self,
    ) -> None:
        with writable_test_directory("shared-template-reuse") as root:
            source = root / "src"
            source.mkdir()
            target = source / "Same.java"
            target.write_text("same\n", encoding="utf-8")
            reused, conflicts = partition_existing_targets(
                [
                    {"target": "src/Same.java", "content": "same\n"},
                    {"target": "src/Different.java", "content": "new\n"},
                ],
                root,
            )
            self.assertEqual(["src/Same.java"], reused)
            self.assertEqual([], conflicts)

            installer = source / "NveAssetInstaller.java"
            installer.write_text(
                "class NveAssetInstaller { int ASSET_PACKAGE_TYPE_MAKEUP; }\n",
                encoding="utf-8",
            )
            issues = shared_component_issues(
                source, ["makeup-single-lip", "makeup-compose"]
            )
            self.assertEqual(1, len(issues))
            self.assertIn("ASSET_PACKAGE_TYPE_FACE_MESH", issues[0])

    def test_makeup_coordinator_template_tracks_decomposed_compose_origin(self) -> None:
        template = (
            SKILL_ROOT
            / "assets/baseline-3.16.1/features/NveMakeupCoordinator.java.template"
        ).read_text(encoding="utf-8")
        for token in (
            "interface ComposeConflictSnapshot",
            "snapshotComposeConflictState()",
            "canDecomposeCompose(ComposeConflictSnapshot snapshot)",
            "onComposeDecomposed(ComposeConflictSnapshot snapshot)",
            "onComposeRemoved(ComposeConflictSnapshot snapshot)",
            "decomposedComposeTypes",
            "decomposedComposeTypes.remove(type)",
            "decomposedComposeTypes.addAll(composeSingles.keySet())",
            "desiredSingles.remove(type)",
            "restoreSingles()",
            "DesiredSnapshot snapshotDesiredState()",
            "void restoreDesiredState(DesiredSnapshot snapshot)",
        ):
            self.assertIn(token, template)

    def test_compact_filter_and_face_prop_profiles_are_materialized(self) -> None:
        filter_selected = select_templates(
            ["完整滤镜"], "controllers", None, "compact-filter"
        )
        self.assertEqual(
            (
                "features/NveFilterCoordinator.java.template",
                "ui/NveCompactEffectEntryBar.java.template",
                "ui/NveCompactFilterPanelView.java.template",
            ),
            filter_selected,
        )
        prop_selected = select_templates(
            ["道具"], "controllers", None, "compact-face-prop"
        )
        self.assertEqual(
            (
                "features/NveFacePropCoordinator.java.template",
                "ui/NveCompactEffectEntryBar.java.template",
                "ui/NveCompactFacePropPanelView.java.template",
            ),
            prop_selected,
        )
        with self.assertRaisesRegex(ValueError, "只适用于滤镜"):
            select_templates(["道具"], "controllers", None, "compact-filter")
        with self.assertRaisesRegex(ValueError, "只适用于道具"):
            select_templates(
                ["完整滤镜"], "controllers", None, "compact-face-prop"
            )

        baseline = SKILL_ROOT / "assets" / "baseline-3.16.1" / "ui"
        filter_panel = (
            baseline / "NveCompactFilterPanelView.java.template"
        ).read_text(encoding="utf-8")
        prop_panel = (
            baseline / "NveCompactFacePropPanelView.java.template"
        ).read_text(encoding="utf-8")
        for token in (
            "this view performs no assets, file, or network I/O",
            'entryButton = button("滤镜")',
            "nve-filter-none",
            "nve-filter-intensity",
            "nve-filter-reset",
            "dp(58), dp(36)",
            "int noneIconResource",
            'new FilterItemView(context, "无", noneIconResource)',
            "new LinearLayout.LayoutParams(dp(48), dp(48))",
            "dp(44), dp(44), Gravity.CENTER",
            "labelParams.topMargin = dp(1)",
            'resetButton = flatActionButton("重置")',
            "new LinearLayout.LayoutParams(dp(48), dp(30))",
            "rounded(COLOR_FLAT_CONTROL, 8, 0, COLOR_OUTLINE)",
            "listener.onFilterSelected(item, desiredIntensity)",
        ):
            self.assertIn(token, filter_panel)
        selection_style = filter_panel[
            filter_panel.index("void setSelectionStyle(boolean selected)")
            : filter_panel.index("private GradientDrawable rounded")
        ]
        self.assertIn("preview.setBackground(rounded(", selection_style)
        self.assertIn("label.setTextColor(", selection_style)
        self.assertNotIn("\n            setBackground(rounded(", selection_style)
        self.assertNotIn('icon.setText(none ? "\\u2298" : "")', filter_panel)
        for token in (
            "this view performs no assets, file, or network I/O",
            'entryButton = button("道具")',
            "nve-face-prop-none",
            "nve-face-prop-item-",
            "listener.onFacePropCleared()",
            "Context context, int noneIconResource, Listener listener",
            'new PropItemView(context, "无", null, noneIconResource)',
            "String displayLabel = normalizedLabel(item, index)",
            "boolean hasVisibleLabels = false",
            "hasVisibleLabels |= displayLabel != null",
            "noneItem.setLabelVisible(hasVisibleLabels)",
            'return item.cover() == null ? "道具 " + (index + 1) : null',
            "new LinearLayout.LayoutParams(dp(60), dp(68))",
            "params.rightMargin = dp(6)",
            "new LinearLayout.LayoutParams(dp(48), dp(48))",
            "dp(44), dp(44), Gravity.CENTER",
            "labelParams.topMargin = dp(1)",
            "label.setVisibility(visible ? VISIBLE : GONE)",
        ):
            self.assertIn(token, prop_panel)
        prop_selection_style = prop_panel[
            prop_panel.index("void setSelectionStyle(boolean selected)")
            : prop_panel.index("void setLabelVisible(boolean visible)")
        ]
        self.assertIn("preview.setBackground(rounded(", prop_selection_style)
        self.assertNotIn(
            "\n            setBackground(rounded(", prop_selection_style
        )
        self.assertNotIn("SeekBar", prop_panel)
        self.assertNotIn("CircleSlashView", prop_panel)
        self.assertNotIn('icon.setText(none ? "\\u2298" : "")', prop_panel)

        catalog = json.loads(
            (
                SKILL_ROOT
                / "assets/baseline-3.16.1/catalog/feature-catalog.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual([60, 68], catalog["ui"]["face_prop_item_size_dp"])
        self.assertEqual(
            [48, 48], catalog["ui"]["face_prop_preview_frame_size_dp"]
        )
        self.assertEqual([44, 44], catalog["ui"]["face_prop_cover_only_size_dp"])
        self.assertEqual([44, 44], catalog["ui"]["face_prop_cover_with_label_size_dp"])
        self.assertEqual(6, catalog["ui"]["face_prop_item_gap_dp"])
        self.assertEqual(
            "preview-frame-only-2dp-accent-outline-no-fill",
            catalog["ui"]["face_prop_selection"],
        )
        self.assertEqual(
            "filter-style-icon-card-with-conditional-label",
            catalog["ui"]["face_prop_none_presentation"],
        )
        self.assertEqual(
            "show-only-when-any-item-has-visible-label",
            catalog["ui"]["face_prop_none_label_policy"],
        )
        self.assertEqual(
            "gone-and-preview-vertically-centered",
            catalog["ui"]["face_prop_hidden_label_layout"],
        )
        self.assertEqual([24, 24], catalog["ui"]["face_prop_none_icon_size_dp"])

    def test_makeup_selection_state_and_regression_template_are_materialized(self) -> None:
        state = (
            SKILL_ROOT
            / "assets/baseline-3.16.1/features/NveMakeupSelectionState.java.template"
        ).read_text(encoding="utf-8")
        for token in (
            "COMPOSE_COMPONENT_ID",
            "selectCompose(",
            "selectSingle(",
            "clearSingle(",
            "Snapshot snapshot()",
            "void restore(Snapshot snapshot)",
            "isInheritedFromCompose(",
        ):
            self.assertIn(token, state)

        regression = (
            SKILL_ROOT
            / "assets/baseline-3.16.1/tests/NveMakeupSelectionStateTest.java.template"
        ).read_text(encoding="utf-8")
        for token in (
            "composeOverrideAndClearKeepsExplicitSingle",
            "categoryNoneClearsOnlyThatCategory",
            "switchingComposeReplacesBaseAndRestoresExplicitState",
            "snapshotRestoresComposeProvenance",
        ):
            self.assertIn(token, regression)
        repository_regression = (
            SKILL_ROOT
            / "assets/baseline-3.16.1/tests/NveMakeupAssetRepositoryTest.java.template"
        ).read_text(encoding="utf-8")
        self.assertIn("new UUID(1L, 2L).toString()", repository_regression)
        self.assertIn("new UUID(0L, 0L).toString()", repository_regression)
        self.assertNotRegex(
            repository_regression,
            r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
        )

        source_dir = SKILL_ROOT / "generated-makeup" / "main"
        test_source_dir = SKILL_ROOT / "generated-makeup" / "test"
        plan = build_plan(
            SKILL_ROOT,
            source_dir,
            "com.example.effect",
            "3.16.1",
            ["完整美妆"],
            "controllers",
            None,
            "compact-makeup",
            test_source_dir,
        )
        targets = {item["target"] for item in plan}
        self.assertIn(
            "generated-makeup/test/NveMakeupSelectionStateTest.java",
            targets,
        )
        self.assertIn(
            "generated-makeup/test/NveMakeupAssetRepositoryTest.java",
            targets,
        )

    def test_each_selected_controller_materializes_its_regression_test(self) -> None:
        source_dir = SKILL_ROOT / "generated-all-tests" / "main"
        test_source_dir = SKILL_ROOT / "generated-all-tests" / "test"
        plan = build_plan(
            SKILL_ROOT,
            source_dir,
            "com.example.effect",
            "3.16.1",
            ["完整美颜", "完整美妆", "完整滤镜", "道具"],
            "controllers",
            None,
            None,
            test_source_dir,
        )
        targets = {item["target"] for item in plan}
        for filename in (
            "NveBeautyCoordinatorTest.java",
            "NveMakeupSelectionStateTest.java",
            "NveMakeupAssetRepositoryTest.java",
            "NveFilterCoordinatorTest.java",
            "NveFacePropCoordinatorTest.java",
        ):
            self.assertIn(f"generated-all-tests/test/{filename}", targets)

    def test_camera_profile_materializes_frame_and_value_queue_regressions(self) -> None:
        source_dir = SKILL_ROOT / "generated-camera-tests" / "main"
        test_source_dir = SKILL_ROOT / "generated-camera-tests" / "test"
        plan = build_plan(
            SKILL_ROOT,
            source_dir,
            "com.example.effect",
            "3.16.1",
            ["完整美颜"],
            "all",
            "camera2-oes-nv21",
            None,
            test_source_dir,
        )
        targets = {item["target"] for item in plan}
        self.assertIn(
            "generated-camera-tests/test/NveLatestValueQueueTest.java",
            targets,
        )
        self.assertIn(
            "generated-camera-tests/test/NveCamera2FramePairerTest.java",
            targets,
        )

    def test_materializer_accepts_multiple_ui_profiles_in_one_call(self) -> None:
        self.assertEqual(
            ("compact-beauty", "compact-makeup"),
            normalize_ui_profiles(
                ["compact-beauty", "compact-makeup", "compact-beauty"]
            ),
        )
        selected = select_templates(
            ["完整美颜", "完整美妆"],
            "controllers",
            None,
            ["compact-beauty", "compact-makeup"],
        )
        self.assertEqual(
            (
                "features/NveBeautyCoordinator.java.template",
                "features/NveMakeupCoordinator.java.template",
                "features/NveMakeupSelectionState.java.template",
                "features/NveMakeupAssetRepository.java.template",
                "ui/NveCompactEffectEntryBar.java.template",
                "ui/NveCompactBeautyPanelView.java.template",
                "ui/NveCompactMakeupPanelView.java.template",
                "features/NveMakeupResourceLoader.java.template",
            ),
            selected,
        )

    def test_template_materializer_rejects_mismatched_aar_hash(self) -> None:
        fake_aar = SKILL_ROOT / "fake.aar"
        with mock.patch.object(Path, "is_file", return_value=True), mock.patch(
            "materialize_baseline_templates.sha256_file", return_value="0" * 64
        ):
            with self.assertRaisesRegex(ValueError, "AAR SHA-256 未命中"):
                verify_baseline_aar(fake_aar, "3.16.1")

    def test_materializer_rejects_unresolved_scope(self) -> None:
        self.assertEqual(["beauty-suite"], unresolved_scopes(["美颜"]))
        self.assertEqual(["makeup-suite"], unresolved_scopes(["美妆"]))
        self.assertEqual(["filter-packaged"], unresolved_scopes(["滤镜"]))
        self.assertEqual([], unresolved_scopes(["完整美颜"]))
        self.assertEqual([], unresolved_scopes(["完整美妆"]))
        self.assertEqual([], unresolved_scopes(["完整滤镜"]))
        empty_filter_map = {
            "filter-packaged": {"packages": {}, "display_items": []}
        }
        self.assertEqual(
            ["filter-packaged"],
            unresolved_scopes(["滤镜"], empty_filter_map),
        )
        self.assertEqual(
            [],
            unresolved_scopes(
                ["滤镜"],
                {
                    "filter-packaged": {
                        "packages": {
                            "filter-a": {
                                "asset": "assets/filter/a.videofx",
                                "license": "assets/filter/a.lic",
                            }
                        },
                        "display_items": [
                            {"id": "a", "package": "filter-a"}
                        ],
                    }
                },
            ),
        )


if __name__ == "__main__":
    unittest.main()
