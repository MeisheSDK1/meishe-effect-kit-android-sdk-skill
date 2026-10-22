# 兼容性基线与版本契约

本文件是 AAR/资料标识、版本边界、版本敏感实现事实和已知集成风险的唯一来源。通用门禁、渲染、资源安装和功能配方分别由对应 reference 维护，不在此重复。

## 目录

- [证据优先级](#证据优先级)
- [资料基线](#资料基线)
- [SDK 与宿主边界](#sdk-与宿主边界)
- [版本敏感实现契约](#版本敏感实现契约)
- [已核实的文档差异](#已核实的文档差异)
- [已知集成风险](#已知集成风险)
- [更新基线](#更新基线)

## 证据优先级

发生冲突时依次使用：

1. 客户工程实际 AAR API、ABI 和字节码。
2. 与该 AAR 匹配的正式接入资料和目标工程运行证据。
3. 本 skill 内登记 catalog、模板和回归测试共同定义的基线契约。
4. 本 skill 的通用配方。

目标 AAR 与当前基线哈希不一致时，本页的版本敏感规则只作为待重新核实的线索。不得为补充证据自行访问目标工程、用户显式附件或用户授权路径之外的文件。

## 资料基线

| 项目 | 基线值 |
| --- | --- |
| Skill 发布版本 | `1.0.0` |
| SDK 基线版本 | `3.16.1` |
| 核验日期 | `2026-07-27` |
| NveEffectKit AAR SHA-256 | `11e1bee1d462c17159ab0239d3ca6513f20da52893f553cf0f56d9cdb7bb5eff` |
| 中文接入说明 SHA-256 | `8e188704d6eef81074b96900679e1d216dbf0e045d2b0e99727751009890307a` |
| AAR 身份规则 | 以 SHA-256 精确匹配，不用文件名或展示版本代替 |
| 素材安装线程复核 | `2026-07-24` 真机：后台调用触发 `Main Thread Checker` |

哈希只用于识别事实来源，不是下载地址，也不要求客户使用相同文件。新 SDK/AAR 资料必须单独登记版本、哈希和核验日期，不能覆盖旧记录后沿用旧身份信息。

当前已核实：

- 接入说明要求美摄 Effect SDK 3.8.0 以上。
- `FACE_COMMON` 与 `ADVANCED_BEAUTY` 最低支持基线为 3.13.x。
- 登记 3.16.1 基线的基础美颜链路按 `FACE → FACE_COMMON → ADVANCED_BEAUTY` 初始化；
  精确模型文件与美型/微整形素材映射只在登记 catalog 中维护，由
  `feature_contract.py` 输出。
- `MAKEUP2` 在 3.13.x 以上已废弃。
- 基线 AAR 声明 minSdk 19、targetSdk 33。
- 基线 AAR 包含 `armeabi-v7a`、`arm64-v8a` 的 `libNvStreamingSdkCore.so`。
- 基线 AAR 内嵌 `NvAndroidStreamingSdk.jar`。

使用新 AAR 时重新检查 manifest、`aar-metadata.properties`、`classes.jar`、内嵌 JAR 和 `jni/`。解包只在工程外临时目录进行，结束后清理。

## SDK 与宿主边界

客户工程的 compileSdk、targetSdk、AGP、Gradle、JDK、Kotlin、相机和 UI 不属于 SDK 基线：

- 保留客户现有构建链和应用架构。
- 只在 AAR 元数据、构建错误或正式兼容要求明确需要时调整。
- 不为匹配目标工程外的构建配置而降级客户工程。
- 不复制未经授权的仓库配置、重复仓库或下载脚本。

基线可操作链路覆盖相机 OES + NV21 检测 Buffer 的 `TEXTURE_TEXTURE` 渲染、美颜、美妆、滤镜、人脸道具、预览和编码录制。切摄、闪光、变焦和曝光属于宿主相机能力，不由本 skill 替换。

OES 纹理使用显示方向尺寸，NV21 保持相机平面尺寸；传感器方向和显示旋转分别传入，效果帧
成对提交，消费 SDK 输出的真实 texture type/size。完整固化规则见
[camera2-oes-nv21.md](camera2-oes-nv21.md)。

“分割”和“调色”入口回调为空，不能作为已实现证据。背景分割只使用资料已确认的自定义 `NvsVideoEffect` 路径；没有可靠 API 时不生成调色实现。

## 版本敏感实现契约

| 基线事实 | 权威实现规则 |
| --- | --- |
| Beauty 建立共享 AR Scene，随后恢复 Shape、MicroShape、Makeup、Filter 和 FaceProp | [golden-templates.md](golden-templates.md)、[lifecycle-rendering.md](lifecycle-rendering.md) |
| 无效果时可能透传 OES 输入；输出类型和尺寸不能从开关或 texture ID 推断 | [lifecycle-rendering.md](lifecycle-rendering.md) |
| `initialize` 在 GL 就绪后由 Android 主线程调用；`cleanUp` 是登记基线最后一个 GL 调用 | [lifecycle-rendering.md](lifecycle-rendering.md) |
| 文件/元数据准备留在 worker；`getInstance` 和素材包安装在 Android 主线程调用 | [assets-and-models.md](assets-and-models.md)、[lifecycle-rendering.md](lifecycle-rendering.md) |
| 单妆在 `initialize()` 后准备；安装失败可能返回全零 UUID；交付文件名和目录不可改 | [assets-and-models.md](assets-and-models.md)、[makeup.md](makeup.md) |
| 高级磨皮 4 受高性能设备门禁，锐度范围 `[0,10]` | [beauty.md](beauty.md) |
| 高频磨皮/美白强度更新不重复提交相同 selector | [beauty.md](beauty.md) |
| Camera2 滑块拖动期间的帧节拍由独立回调线程和相机帧驱动 | [camera2-oes-nv21.md](camera2-oes-nv21.md)、[lifecycle-rendering.md](lifecycle-rendering.md) |

## 已核实的文档差异

| 原始问题 | 当前基线修正 |
| --- | --- |
| `NvBeautyWhiteningTypeEnum` 与 `NveBeautyWhiteningTypeEnum` 混用 | 使用 `NveBeautyWhiteningTypeEnum` |
| `Texture.setTextureType(...)` | 调用实例 `nvTexture.setTextureType(...)` |
| `NveImageBufferbuffer` 变量声明粘连 | 使用 `NveImageBuffer imageBuffer` |
| `NveEffectKitt.recycleOutput(...)` | 使用当前 `NveEffectKit` 实例 |
| 微整形示例误用 `shape` | 使用 `NveMicroShape microShape` |
| `setComposeMakeup` 与源码不一致 | 基线源码使用 `useComposeMakeup` |
| 示例素材路径与实际版本不一致 | 使用客户元数据和安装返回 ID |

不使用反射掩盖命名差异。编译失败时先确认 AAR 与资料是否匹配。

## 已知集成风险

| 不安全行为 | 风险 | 接入要求 |
| --- | --- | --- |
| 忽略 `init` 返回值 | 未授权仍开放 UI | 检查结果并执行能力门禁 |
| 一次初始化所有模型 | 启动慢、错误归因不清 | 只初始化目标功能需要的模型 |
| minSdk 19，但预览渲染只在 Android O 以上调用 | Android 5–7 无画面 | 实现目标 API 范围的渲染路径，或显式提高功能 minSdk |
| `onSurfaceCreated` 使用尚未初始化的 drawer | UI/GL 线程竞态空指针 | 注册 Renderer 前构造，或在 GL 回调内同步创建 |
| UI adapter 先可点击 | 控制器未注册时闪退 | 使用 readiness/capability guard |
| Shape 在 AR Scene 前启用 | AAR 内部空指针 | 遵守共享场景注册顺序 |
| 用纹理 ID 推测类型 | OES 透传时黑屏 | 读取输出真实 texture type |
| 输出纹理不判空并沿用输入尺寸 | 崩溃、拉伸或裁剪错误 | 检查 texture 并使用输出尺寸和布局 |
| 效果调用后不恢复宿主 GL 状态 | 黑屏、左下角小画面、FBO/Program 错乱 | 绘制前集中恢复 viewport/FBO/program/texture binding |
| 输出保存为跨帧字段、方法尾回收 | 异常漏回收或状态污染 | 使用局部变量，在同一 `try/finally` 单次回收 |
| 错误输出未回到统一原始路径 | 黑屏或方向跳变 | 使用原始预览的 shader、方向、镜像和 crop/viewport |
| 原始帧与效果帧分别决定镜像 | 开关前后方向变化 | 共用最终显示契约 |
| YUV 假设 UV 平面重叠 | 花帧或越界 | 使用通用 stride 转换与测试 |
| 相机回调执行 JPEG/Bitmap 转换 | 阻塞、积帧和发热 | 回调只交付必要数据并关闭 Image |
| 双路预览沿用设备最大共享尺寸 | NV21 转换和 GL 负载过高，人物移动时画面滞后 | 优先使用 OES/YUV 共同支持的 1280×720，并请求 30fps |
| 每帧复制 Y/U/V 三个平面后再生成 NV21 | 内存带宽、分配和 GC 压力造成预览卡顿 | latest-only Image 直接写入池化 NV21 Buffer；关闭 reader 时与转换串行化 |
| 使用固定素材 UUID 或只判非空 | 客户素材无效、全零哨兵误判成功 | 使用完整校验后的安装返回 ID |
| 道具“无”项只写空 ID | 恢复后重新出现 | 同步清空 ID、关闭控制器和 desired state |
| 首次“无”创建 disabled 道具后选择新素材不重新 enable | 后续道具始终无效果 | 每次有效选择同时设置 ID 和 `enable=true` |
| 滤镜默认强度未写入 desired state | 前后台恢复后强度变 0 | 选择时同步 desired/applied 强度 |
| 重命名单妆二进制包 | 包管理器安装失败 | 保留交付文件名和相对目录 |
| 固定延迟掩盖切摄旧帧 | 设备差异导致偶现 | 使用 generation token 丢弃旧帧 |
| 把 `OnFrameAvailable` 注册到 UI main 或每个滑块刻度 `requestRender` | 拖动参数时相机帧回调被触摸事件挤压，预览持续轻微抖动 | 独立帧回调线程；参数 latest-value 合并；只由相机新帧驱动渲染 |
| NV21 严格一次消费，缺帧时保留上一显示帧 | YUV/OES 节拍稍有错位就出现画面停顿和感知延迟 | 检测帧只在 100ms 窗口内有界复用并引用计数；无合格帧时绘制当前 raw OES |
| `cleanUp` 后先对 Beauty/Shape/MicroShape 调用 `setEnable` | 内部 AR Scene/effect 已失效，恢复时 NPE 或 `arSceneFx is NULL` | 逐个先向 EffectKit 重新注册，再 enable，最后恢复 desired state |
| 构建成功即宣称完成 | 视觉和交互缺陷漏检 | 按 [verification.md](verification.md) 限定结论 |

## 更新基线

1. 记录 AAR manifest、minSdk、targetSdk、ABI、内嵌 JAR 和原生库。
2. 计算 AAR 与对应资料 SHA-256，登记 SDK 版本和新的核验日期。
3. 核实公开 API、目标工程非空调用链和功能范围。
4. 只在本文件记录版本事实和与通用规则的差异。
5. 通用行为修改其唯一责任 reference。
6. 更新受影响测试并执行结构校验和必要前向测试。
