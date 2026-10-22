# 版本化黄金模板

本文件只维护登记模板的选择和宿主适配边界。功能参数读取对应功能 reference，UI 读取
[ui-integration.md](ui-integration.md)，渲染与生命周期读取
[lifecycle-rendering.md](lifecycle-rendering.md)，版本事实读取
[compatibility-contracts.md](compatibility-contracts.md)。

## 使用条件

- 先用 `inspect_android_project.py` 确认目标 AAR SHA-256。只有目标 AAR 属于
  SDK 3.16.1 且命中登记 SHA-256 才能落地本目录模板。
- 未命中时核实目标 AAR API、线程和顺序并创建新基线；禁止修改旧模板冒充兼容。
- 已有稳定桥接且输入输出、GL、方向和生命周期未变化时只生成 `controllers`。
- 首次接入或改变渲染契约时生成 `all`；Camera2 OES + NV21 使用
  `--profile camera2-oes-nv21`，不得拆开后重写其中公式。
- 模板不覆盖已有文件，不替换宿主相机、GL、线程、状态架构或产品 UI。

## 落地

门禁通过后，使用 [SKILL.md](../SKILL.md) 第 4 步的落地命令先 dry-run。脚本拒绝 AAR
hash 不匹配、未知/未解析范围、非法包名、项目外输出路径和覆盖已有文件；相对 AAR 和
asset-map 路径以目标工程根目录为基准。检查计划后去掉 `--dry-run`。传
`--test-source-dir` 时同时生成最终所选 controller 的登记回归测试。

## 模板组

| 组 | 选择条件 | 固定责任 |
| --- | --- | --- |
| `common/` | `layer=common/all` | SDK session、素材安装、渲染输出消费与回收、高频参数 latest-value 合并 |
| `camera2/` | `profile=camera2-oes-nv21` | 方向、低延迟预览参数、复用 Buffer 的 NV21 转换、100ms 有界帧复用、输入和 OES/2D 显示 |
| `features/` | 最终 leaf feature | desired/applied state、控制器顺序、移除和恢复；紧凑美妆 profile 追加动态目录 loader |
| `ui/` | 没有用户设计和宿主设计体系 | 已验证的紧凑默认 Views 面板、共享入口栏和互斥/收起交互 |
| `tests/` | 提供测试源码目录 | 控制器、状态机和资源发布回归 |
| `catalog/` | 所有登记路径 | 版本、功能、模型、素材、UI 和渲染契约的单一数据源 |

## 宿主适配

1. 替换 `__TARGET_PACKAGE__`，把模板接口接到宿主已有 owner、线程和数据源。
2. 只创建最终范围需要的 controller；不为未选择模块创建占位对象或 UI。
   紧凑美妆 loader 的 `assetRoot`、单妆索引和妆容元数据路径来自实际复制位置与校验器
   输出；不能写死供应方目录名、交付 UUID 或开发机路径。
   美妆模板必须传入 `inspect_makeup_catalog.py --json` 的零问题报告。dry-run 输出的
   `missing_gradle_dependencies`、`compatibility_issues` 未清零前不得实际落地。
3. `NveSessionOwner.initializeAfterGlReady` 的恢复器顺序为：美颜（Beauty → Shape →
   MicroShape）→ 美妆 → 滤镜 → 道具。美颜控制器在 `cleanUp` 后必须逐个先调用
   `EffectKit.setBeauty/setShape/setMicroShape`，再调用对应控制器的 `setEnable`，最后恢复
   desired state；禁止对失效控制器先 `setEnable`。未选择模块不传恢复器。
4. 把显示 target 映射到宿主真实 FBO/viewport/program/texture binding 恢复入口；raw、
   OES 和 2D 输出必须满足同一最终方向、镜像和 crop 契约。
5. 把 `SurfaceTexture.OnFrameAvailableListener` 注册到专用 `HandlerThread`；滑块把值写入
   `NveLatestValueQueue`，由下一次相机帧排空，禁止参数回调主动 `requestRender()`。
6. 固定生命周期：

```text
Android 主线程：init → initModel...
GL 线程：创建宿主纹理/FBO
Android 主线程：GL 就绪后 initialize → 恢复所选控制器
GL 线程：renderEffect → 恢复宿主 GL → 绘制 → recycleOutput
暂停：停止帧 → GL 线程释放宿主 GL → cleanUp
最后使用方：destroy
```

`cleanUp` 是登记基线的最后一个 GL 调用；非登记 AAR 重新核实。美妆用
`canApplyMakeup()`，道具用 `canApplyFaceProp()` 控制 readiness。登记 3.16.1 使用 worker
准备素材路径/元数据，通过 Android main executor 调用 `NveAssetInstaller`；不得把
`installFxPackage` 或 `installAssetPackage` 放进资源 worker。
若目标目录已有同名共享模板，内容相同则复用；内容不同仍拒绝覆盖。已有
`NveAssetInstaller` 必须显式列出本次素材类型，妆容至少覆盖 MAKEUP、FACE_MESH、WARP
和 VIDEOFX；缺失时先人工合并兼容保护，不能用泛型参数或无效 token 绕过严格检查。

## UI 选择

UI 优先级固定为：用户明确提供的设计/设计图 → 目标工程已有设计体系/效果编辑器 →
skill 内已固化并完成设备验收的登记基线默认 UI。只有前两者都不存在时才生成
`compact-*` profile；第三级只读取本 skill 的模板和 catalog，不访问、不搜索、也不依赖
任何未经用户授权的目标工程外资源。多个 profile 必须在一次命令中传入；落地器共同生成一份
`NveCompactEffectEntryBar`。宿主把全部面板传给入口栏并用其
`createPanelLayoutParams()` 依次加入预览根 `FrameLayout`，再用
`createBottomLayoutParams()` 加入入口栏；edge-to-edge 页面向重载方法传入底部
WindowInset。入口栏负责均匀布局、面板互斥、入口栏与面板互斥显示，以及点击面板外空白
区域后的入口恢复。

生成后按 [verification.md](verification.md) 执行静态、编译、阶段和设备验证；模板存在不等于
宿主适配或视觉验收完成。
