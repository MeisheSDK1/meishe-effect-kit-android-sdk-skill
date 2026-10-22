# 故障排查

本文件只维护“现象 → 检查顺序 → 唯一责任文档”的诊断索引，不重复定义门禁、渲染、素材或功能契约。

## 目录

- [固定排查顺序](#固定排查顺序)
- [构建与启动](#构建与启动)
- [黑屏、方向与卡帧](#黑屏方向与卡帧)
- [效果无效](#效果无效)
- [交互与布局](#交互与布局)
- [性能和稳定性](#性能和稳定性)

## 固定排查顺序

一次只定位一层，不同时调整旋转、镜像、尺寸、线程和效果参数：

1. 用 `inspect_android_project.py` 确认 AAR、模块、ABI、applicationId 和接入点。
2. 用 [prerequisites.md](prerequisites.md) 判断缺项是否阻塞，不带着缺失 lic/模型/素材继续试错。
3. 用 `validate_nve_integration.py` 检查静态高风险结构。
4. 关闭全部效果，按 [lifecycle-rendering.md](lifecycle-rendering.md) 验证原始预览和无效果闭环。
5. 只启用一个依赖最少的效果，再检查对应功能配方。
6. 最后恢复组合效果、UI、录制和生命周期场景。

“控件可操作但没有效果”时，第 2 步不得跳过：先核对该功能要求的精确模型文件是否存在，
再核对 `NveDetectionModelType`、官方初始化顺序、每项返回值和功能 readiness。模型链未
证明前，不先改参数范围、UI 事件或反复试素材。

## 构建与启动

| 现象 | 优先检查 | 责任文档 |
| --- | --- | --- |
| Duplicate class | AAR 是否内嵌 Streaming SDK、多个模块是否重复打包 | [project-integration.md](project-integration.md) |
| `UnsatisfiedLinkError` | 设备 ABI、APK/AAB ABI、`abiFilters`、同名 `.so` 版本 | [project-integration.md](project-integration.md) |
| 旧 Gradle/JDK 失败 | 客户 AGP/Gradle 的 JDK 兼容矩阵，不为外部参考工程降级 | [compatibility-contracts.md](compatibility-contracts.md) |
| `init` 失败 | lic 路径、最终 applicationId、授权版本和有效期 | [prerequisites.md](prerequisites.md) |
| `initModel` 失败 | 模型类型、配套版本、真实路径和目标功能依赖 | [assets-and-models.md](assets-and-models.md) |
| `Main Thread Checker` 指向 `getInstance`/安装 API | 登记 3.16.1 是否从 worker 直接安装；准备留在 worker，SDK 安装切到 Android 主线程 | [assets-and-models.md](assets-and-models.md) |

不要用全局 `exclude` 随机消除重复类，也不要用其他工程资源验证当前客户工程。

## 黑屏、方向与卡帧

### 黑屏

按以下顺序检查：

1. 原始预览是否正常；不正常时先离开 SDK 路径定位宿主相机/GL。
2. `renderEffect` 是否返回空、错误或空 texture；异常必须走原始帧。
3. 输出真实 texture type 是否选择了正确 OES/2D shader。
4. 输出尺寸、viewport、FBO 和采样矩阵是否匹配。
5. 输出是否在绘制前被回收，或被异步消费者跨 GLContext 使用。

完整输入、输出、回收和线程契约只查 [lifecycle-rendering.md](lifecycle-rendering.md)。

### 方向或镜像错误

| 现象 | 单一优先变量 |
| --- | --- |
| 人脸效果偏移 | Buffer rotation、宽高、纹理/Buffer 帧同步 |
| 前摄左右相反 | 检测 mirror 与最终预览 mirror |
| 上下颠倒 | `SurfaceTexture` matrix、vertical mirror、纹理布局 |
| 横屏拉伸 | 输出尺寸、viewport、宽高比 |
| 预览正常但录像反向 | 编码路径独立镜像策略 |

使用不对称标记输入，每次只调整一个变量。方向责任定义只查 [lifecycle-rendering.md](lifecycle-rendering.md)。

### 开启效果爆闪或变形

1. 比较 raw、无效果 SDK 输出、单效果输出的 texture type、实际输出 width/height、
   viewport、crop、方向和镜像。
2. Camera2 OES + NV21 下确认 OES 使用显示方向尺寸、NV21 使用相机平面尺寸，sensor
   orientation 与 display rotation 没有互换。
3. 确认效果帧提交同一 100ms 窗口内的 OES/NV21；检测帧缺失时绘制当前 raw OES，且
   raw/effect 共用方向、镜像、crop 和 viewport。
4. 确认开关只改变控制器状态，不在点击时重建相机、SurfaceTexture、FBO 或 SDK 会话。
5. 确认每帧恢复宿主 GL 状态，并读取 SDK 输出对象的真实 type/size 后再绘制。

完整阻断契约见 [camera2-oes-nv21.md](camera2-oes-nv21.md)。只要 `no_flash`、
`no_geometry_jump_on_enable` 或 raw/effect 同视觉契约未取证，就不能继续扩功能或声明可发布。

### 人物移动时卡顿或感觉延迟

按固定顺序检查：

1. Camera2 OES 与 YUV 的共同输出尺寸是否优先 1280×720；不要按屏幕或最大相机尺寸选择
   1872×864 等高负载格式。
2. `CONTROL_AE_TARGET_FPS_RANGE` 是否请求 `[30,30]`，同时从 CameraService/Perfetto 记录
   实际 sensor frame duration。弱光下 HAL 可能接受请求但把曝光拉到约 60ms，此时 16–17fps
   是相机输入节拍，不应误判为应用积帧。
3. ImageReader 是否用 `acquireLatestImage`，pending Image 是否原子替换；转换任务是否始终
   最多一个在途。
4. 是否仍先复制 Y/U/V 三个数组再分配 NV21；应直接从 Image 写入复用 NV21 Buffer，对
   经验证的交错 VU 行批量复制，其余布局走通用 stride 路径。
5. `pairedFrame` 是否允许 100ms 内的相邻 OES 有界复用，并在 `renderEffect` 后于 `finally`
   调用 `releaseFrame`；超窗帧必须退役。
6. 无合格检测帧时是否仍绘制当前 raw OES；保留上一显示帧会直接表现为人物动作冻结。

使用 `simpleperf` 比较 FrameConversion、GLThread、CameraBackground 和 HeapTaskDaemon，
并以 SurfaceFlinger/Perfetto 的呈现时间戳验证画面实时刷新，不能只看平均 CPU。

### 偶发卡帧

优先确认纹理与检测 Buffer 是否在 100ms 窗口内、`ImageProxy/Image` 是否及时关闭、队列是否
只保留最新帧、Buffer 是否池化，以及渲染线程是否执行安装、JSON 或文件 I/O。

### 只在拖动滑块时持续轻微抖动

按固定顺序检查：

1. `SurfaceTexture.OnFrameAvailableListener` 是否绑定 UI main `Handler`；Camera2 OES 应使用
   专用 `HandlerThread`，用线程名或调用栈取证。
2. `onProgressChanged` 或其 helper 是否每个刻度调用 `requestRender()`；实时预览只由相机
   新帧触发渲染，参数更新进入 latest-value 槽位并在下一帧排空。
3. 同一磨皮/美白算法调强度时是否重复调用 `setBlurType`/`setWhiteningType`；selector 只在
   算法真实变化时提交。
4. 检测帧是否只在 100ms 窗口内复用，并且每次获取都有对应的 `releaseFrame`；禁止无界
   复用旧人脸结果。

若静止不抖、仅拖动时出现，先完成以上线程与调度检查，不先调整模型顺序、方向或参数范围。

## 效果无效

| 现象 | 检查顺序 |
| --- | --- |
| 美颜滑块无变化 | SDK init → 精确 `FACE`/`FACE_COMMON`/`ADVANCED_BEAUTY` 文件 → `initModel` 类型/官方顺序/返回值 → 功能 readiness → OES/NV21 检测输入 → 控制器 enable/注册 → setter/素材 ID |
| 单妆无变化 | 安装阶段与返回 ID → 原始文件名/元数据 → 同类型对象 → `addSingleMakeUp` → [makeup.md](makeup.md) |
| 滤镜无变化 | 安装 ID → 是否添加当前对象 → 强度 → 与妆容内滤镜冲突 → [effects.md](effects.md) |
| 道具无变化 | `.arscene` 安装 ID → 必需模型 → `setPropId` → enable/desired state → [effects.md](effects.md) |
| 自定义效果无变化 | effect name/参数类型 → create 判空 → add/remove → 插入顺序 → [effects.md](effects.md) |

所有素材安装、UUID、元数据和升级规则只查 [assets-and-models.md](assets-and-models.md)。切换后叠加通常是旧对象未移除，或妆容内能力与独立控制器同时生效。

## 交互与布局

- 点击控件闪退：检查 `RUNNING && available`、控制器是否为空以及共享场景注册顺序。
- 滑块只能点击不能拖动：检查父容器触摸拦截、嵌套滚动和透明覆盖层。
- 文案截断或按钮重叠：检查最窄屏幕、最长文案、默认与 `1.3x` 字体、安全区。
- UI 显示选中但画面无效：检查 desired state 与实际 readiness/安装结果是否分离。

完整 UI 规则只查 [ui-integration.md](ui-integration.md)。

## 性能和稳定性

先确认是否存在每帧对象创建、素材安装、JSON/磁盘访问、无界队列或跨上下文纹理消费。记录 FPS、帧耗时、内存和温升，不记录图像或授权内容。

稳定性回归和允许声明的结论只查 [verification.md](verification.md)；运行契约只查 [lifecycle-rendering.md](lifecycle-rendering.md)。
