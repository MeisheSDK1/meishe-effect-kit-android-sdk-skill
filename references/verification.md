# 发布验证

本文件只维护证据等级、验证动作和允许声明的结论，不重新定义运行时实现契约。

## 目录

- [证据等级](#证据等级)
- [实施阶段证据](#实施阶段证据)
- [静态与构建门禁](#静态与构建门禁)
- [真机自动门禁](#真机自动门禁)
- [人工交付记录](#人工交付记录)
- [发布矩阵](#发布矩阵)

## 证据等级

`validate_nve_integration.py` 是保守的文本/结构启发式检查，只能发现高风险结构，不能证明控制流、线程安全、对象身份、画面方向、镜像或效果视觉正确。严格模式把高置信违规升级为错误，并把无法静态证明的规则标为运行时必验；误报时必须人工审查具体调用链并记录豁免依据，不能通过塞入无效 token 绕过。

统一放行状态：

| 状态 | 允许声明 |
| --- | --- |
| `BLOCKED` | 尚不可交付，列出失败阶段和解除动作 |
| `STATIC_READY` | 静态适配、单测、构建和包检查通过；运行时未验证 |
| `DEVICE_VERIFIED` | 指定设备完成安装、3 次冷启动、前台进程确认和 Home 后恢复，无已识别崩溃；功能矩阵未完整确认 |

没有设备时不能越过 `STATIC_READY`。所选 NVE 能力缺少匹配 lic、模型或素材时同样不能越过；
仅 `recording` 的 host-only 路径不重新校验既有 NVE 资源。
自动脚本最高输出 `DEVICE_VERIFIED`；发布结论由客户或测试负责人根据目标设备和功能矩阵签字确认。

状态等级与实施阶段是两套正交概念。脚本退出码 0、`STATIC_READY` 或自动冒烟得到的 `DEVICE_VERIFIED` 都不能证明画面方向、镜像、裁剪、效果可见或 UI 正确，也不能替代 `SKILL.md` 中逐阶段的运行时门禁。

## 实施阶段证据

首次接入或相机/渲染契约变更时，在进入下一阶段前保留最小运行时观察记录；不要求为每阶段运行完整发布脚本：

- 目标原始预览：记录前后摄、当前/受支持显示方向、sensor orientation、输入宽高、预览宽高、OES 采样矩阵来源、最终镜像和 crop/viewport 责任，以及实际朝向、镜像、裁剪结果。
- 无效果闭环：记录原始路径与 SDK 旁路路径在同一场景的方向、镜像、裁剪和宽高比对比，
  从 `NveTexturePresenter` 一次性日志记录实际输出 texture type/width/height，并连续观察
  无黑屏、爆闪和 raw/effect 交替。
- 单效果闭环：记录 0、默认值、明显强度的画面变化，以及开启瞬间无几何跳变/爆闪，关闭和
  渲染失败后回到相同视觉契约的原始预览。

观察结果未通过就是阶段阻塞，不用静态检查、构建成功或“代码看起来正确”代替。有设备时在高风险路径逐阶段观察；没有可用设备时，标准路径可以继续完成单个纵向切片、必要 UI 和最小编译，但必须标记运行时未验证。高风险相机/GL 路径在原始预览或无效果闭环无法证明时，不继续批量扩展多个效果模块。

用 `scripts/verify_nve_stages.py` 和 `assets/stage-evidence.example.json` 强制检查阶段顺序。
首次或高风险接入只要已有可用设备，每个检查点都必须运行轻量阶段门禁；这不等同于下面
完整构建/安装/冷启动的重型交付脚本，也不需要等待用户再次要求“完整验收”。

## 静态与构建门禁

任何代码接入或修复默认执行一次相关静态校验和目标模块可用的最小编译任务。首次/高风险
接入且检测到可用设备时，还必须在首次交付回答前执行目标 APK 的安装和冷启动冒烟；该动作
不需要等待用户再次要求完整验收。其他情况下，完整 `assemble`、依赖报告、APK 检查和设备
门禁只在用户要求完整构建、交付或发布验收时执行。

完整构建、交付或发布验收时依次执行；普通代码接入或修复只执行第 1 项、相关的第 2 项和目标模块可用的最小编译任务：

1. 工程盘点和严格契约校验。
2. 相关单元测试；YUV 转换至少覆盖紧凑平面、rowStride、pixelStride 1/2 和非法输入。
3. 目标 variant 的 `assemble`；存在 JVM 单测时执行对应 `test...UnitTest`。
4. 目标 runtime 依赖报告。
5. 检查本次构建的单一可安装 APK 内 ABI 和原生库，并结合依赖树确认重复 SDK 类来源和单一 NveEffectKit AAR。AAB/split APK 需在对应发布流水线另行检查，当前脚本不猜测其安装组合。
6. 对项目 `src/<sourceSet>/assets` 中已映射的素材，逐项确认 APK 内存在对应 `assets/...`
   条目；展示项可多于唯一包数，但 APK 缺任一已映射 package 时阻塞。

完整交付放行默认在首个失败处停止，避免继续执行无意义的构建和依赖任务；只有需要集中收集全部失败信息时才使用 `--continue-on-failure`。

严格校验重点：精确模型集合与初始化顺序、初始化返回值、UI readiness、控制器注册顺序、
OES/NV21 成对输入、显示方向/相机平面尺寸空间、OES/2D 输出 type/size 分流、稳定原始帧
降级、`finally` 回收、有界帧队列、状态恢复和能力移除。

仅为现有 NVE 最终输出新增 `recording` 时不重复执行 NVE 特效阶段门禁，但必须先发现可信
的最终输出消费点；严格校验改为检查共享 EGL 编码 Surface、三槽非阻塞队列、时间戳重基、
MediaStore 直写、录制状态机、生命周期停止和正圆入口。设备证据按
[recording.md](recording.md) 的验证矩阵收集。

## 真机自动门禁

设备必须是 AAR 支持 ABI 的 ARM 真机。完整交付脚本只有显式传入 `--device <serial>`
才执行安装、冷启动和 Home 恢复；未传时即使连接了设备也停在 `STATIC_READY`。这一限制
不豁免实施阶段的轻量观察和 `verify_nve_stages.py`。完整真机门禁执行：

1. 检查设备 ABI、安装单一 APK，并通过 Package Manager 解析真实启动 Activity。
   先使用 `adb install -r`；只有明确返回 `INSTALL_FAILED_TEST_ONLY` 时才以
   `adb install -r -t` 重试，不对其他安装错误泛化使用 `-t`。
2. 记录设备 Logcat 起始时间，不清空设备已有日志。
3. 执行 3 次 `force-stop → am start -W → 等待至少 3 秒覆盖异步模型/素材准备 →
   进程/前台 Activity 确认 → Home`，再执行一次 Home 后恢复；任一次命令失败都阻塞。

默认 Views 美妆面板还要执行交互语义门禁。组合妆和单妆 ID 必须来自本次已成功安装的真实
素材，且二者 ID 全局唯一：

```text
python -B -X utf8 <skill-dir>/scripts/verify_makeup_interactions.py \
  --package <application-id> --serial <device-serial> \
  --compose-item-id <real-compose-id> \
  --single-category lip --single-item-id <real-single-id>
```

该脚本通过稳定无障碍 ID 验证：组合妆选中、单妆分类没有虚拟组合妆项、选择单妆后组合妆
仍保持 UI 选中，以及点击妆容“无”后显式单妆仍保留。它验证的是交互状态，不替代对人脸上
实际视觉效果的人工/截图验收。
4. 收集本次时间窗口内的 Java `FATAL EXCEPTION`、ANR、native crash/tombstone、`UnsatisfiedLinkError` 和持续 Nve 渲染错误。
5. 记录设备 ABI、构建 APK 哈希和待确认的功能证据项。

自动冒烟门禁不操作业务按钮，也不能判断人脸效果是否美观、滑块是否真正改变画面、父容器是否抢占滑块手势、文本是否在目标字体缩放下完整显示或镜像是否符合产品定义，因此最多提升到 `DEVICE_VERIFIED`。这些项目必须由稳定 resource ID/contentDescription 驱动的项目级自动化或测试人员证据完成。

静态校验是保守启发式检查，不是 Java/Kotlin 编译器或完整数据流分析。无法逐 handler、跨线程或跨文件证明的条目必须显示为 `RUNTIME_REQUIRED`，不能描述为已通过。

## 人工交付记录

`--evidence` 接受可选 JSON，用于检查并记录人工证据是否完整，但不会自动输出发布通过结论：

`--features` 接受功能路由支持的名称，但证据统一展开为 `implementation_features`。
发布证据必须传最终选择的具体 leaf feature，不能直接传未解析的 `beauty-suite`、
`beauty-shape` 或 `makeup-suite`。仅用户显式要求完整美颜时覆盖 12 个美肤、11 个美型和
21 个微整形项；显式完整美妆覆盖九类单妆和妆容。部分接入只取证所选项，未选择项不进入
证据缺失列表。不能用一个大类结果替代已选择项，也不能在不同运行间改名逃避历史失败项。

JSON 报告中的 `requested_features` 保存规范化后的用户范围，`features` 保存逐项取证范围。
下面用两个明确单项示意；完整产品请求应由 `feature_contract.py` 输出生成键集合，不手写猜测。

```json
{
  "device_matrix": ["manufacturer/model/android-version/abi"],
  "apk_sha256": "由放行报告复制的 APK SHA-256",
  "features": ["beauty-skin-buffing-skin", "beauty-shape-face-size"],
  "effect_strengths": {
    "beauty-skin-buffing-skin": true,
    "beauty-shape-face-size": true
  },
  "feature_checks": {
    "beauty-skin-buffing-skin": {
      "visible": true,
      "switch_remove_restore": true,
      "failure_fallback": true
    },
    "beauty-shape-face-size": {
      "visible": true,
      "switch_remove_restore": true,
      "failure_fallback": true
    }
  },
  "interaction_repetitions": 10,
  "four_display_rotations": true,
  "front_back_camera": true,
  "lifecycle_repetitions": 10,
  "failure_fallback": true,
  "gl_context_recreated": true,
  "output_texture_types_verified": true,
  "output_texture_dimensions_verified": true,
  "recycle_stress_verified": true,
  "no_black_frames": true,
  "no_flash": true,
  "no_geometry_jump": true,
  "raw_effect_visual_contract_verified": true,
  "no_raw_effect_alternation": true,
  "logcat_clean": true,
  "performance": {
    "fps": 30,
    "frame_time_ms": 33.3,
    "memory_mb": 210,
    "temperature_note": "stable",
    "customer_target_met": true
  }
}
```

证据 JSON 是测试人员对指定 APK/设备矩阵的结构化记录，不是脚本自动生成的视觉证明，也不替代客户或测试负责人的最终签字。字段必须使用 JSON boolean，字符串 `"false"` 不算通过；每项 feature 都要有独立检查记录，并与放行报告中的 APK SHA-256 一致。

证据按契约裁剪：`effect_strengths` 只覆盖功能路由表标记为强度型的能力；单妆和人脸道具默认不要求滑块。`four_display_rotations` 与 `front_back_camera` 只对 `camera2-oes-nv21`、`camerax-buffer` 相机管线强制；`custom-gl` 仍必须提交输出类型、回收、降级、GLContext、性能与日志证据。

证据文件不得包含设备序列号、人脸图像、lic 内容、密钥、带签名 URL 或商业素材内容。脚本报告只保留目标 applicationId、目标进程和 Nve 相关的日志行，并对设备序列号、工程/用户目录、常见凭据字段和签名 URL 参数脱敏。客户没有性能目标时仍记录实测 FPS、帧耗时、内存和温升，结论仅限实测设备。

## 发布矩阵

- 每个强度型功能验证 0、默认值和明显强度；无强度契约的功能验证选择后可见，确认画面真实变化。
- 人脸道具逐项核对元数据名称、封面、安装结果和实际画面效果属于同一素材；素材已提供名称
  或封面时，列表不得退化成无法区分的编号纯文字项。只有封面时确认界面不附加编号文字；
  名称和封面都缺失时才允许编号回退；“无”入口必须居中显示统一的“圆圈加斜杠”图标且
  没有可见文字。所有展示项仍需保持稳定 ID、可访问名称和正确的选择/移除语义。
- 开关、重置、子项选择和连续滑动各重复至少 10 次。
- 滑块位于可滚动父容器时，从滑块轨道或滑块按钮开始拖动必须连续改变数值，父容器滚动位置保持不变；从滑块以外区域拖动时父容器仍能正常滚动。两个方向和取值边界都要覆盖，不能用点击轨道代替拖动验证。
- 在目标屏幕宽度、系统默认字体缩放和产品支持的最大字体缩放下检查按钮、页签、标签与数值；未定义字体范围时至少补测 `1.3x`。文案不得被意外裁切、重叠或挤出可点击区域，只有产品明确设计时才允许省略号；支持多语言时使用实际最长文案复测。
- 新增或修改 UI 时按 [UI 接入与验证面板](ui-integration.md) 检查每个大模块的独立底部按钮、面板收起/展开、模块互斥切换、横竖屏、安全区和失败状态；无产品 UI 要求的验证面板不得长期遮挡人脸主要区域或侵占大部分预览。无法运行界面时必须保留运行时待验证项。
- 相机管线验证前后摄、四个显示方向、前摄只镜像一次、切摄后状态恢复；非相机自定义 GL 管线不强制这些场景。
- Camera2 OES + NV21 在人物持续移动场景记录共享输出尺寸、FPS request、实际 sensor frame
  duration、Surface 呈现间隔和 15 秒 CPU profile。验证默认优先 1280×720、请求 30fps，
  缺少检测帧时当前 raw OES 仍持续更新；弱光导致 HAL 降帧时单独标注，不把 request 当实测。
- 前后摄往返切换至少 10 次；每次都等待入口重新可用，通过 CameraService 当前 ID 证明
  设备确实交替，并连续采样 Surface 呈现时间戳证明画面仍在刷新。只检查进程存活或按钮
  可用不算通过。最后检查 native crash/tombstone、Java fatal、ANR 和 Camera2
  `IN_USE/MAX_CAMERAS_IN_USE` 错误。
- 前后台和页面重进至少 10 次，并覆盖 GLContext 重建。每次恢复都确认 Beauty、Shape、
  MicroShape 已在 enable 前重新注册、恢复后继续产帧，并检查当前时间窗口没有
  `arSceneFx is NULL`、`NullPointerException`、`FATAL EXCEPTION` 或 ANR；任一命中即阻塞。
- 模拟 lic、模型、素材和单帧渲染失败，确认原始预览不中断。
- 美颜开关、每个控制器大类和切摄各连续操作，确认无爆闪、黑屏、拉伸、方向/镜像翻转和
  crop/viewport 跳变；任一出现即发布阻塞，不能用“随后恢复正常”豁免。
- 设备覆盖范围只限实际记录的型号/ABI；客户提供目标机型矩阵时逐台通过后才能作矩阵级发布声明。
