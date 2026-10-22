# Camera2 OES + NV21 已验证契约

本页固化登记 AAR 基线的已验证画面链路。首次接入或修改相机、方向、纹理、检测 Buffer、
FBO、viewport、镜像、帧配对时必须逐项遵守，并通过阶段证据门禁。

命中登记 AAR 时，首次/高风险接入必须通过落地器的
`--profile camera2-oes-nv21` 使用六个版本化组件，不再由 agent 临场重写：

```text
Camera2 Image callback
  → NveCamera2PreviewConfig（共享 1280×720 优先、请求 30fps）
  → NveCamera2FramePairer（Image 所有权转移，latest-only）
  → 单 background worker 调用 convertLatestImage
  → NveNv21Converter（直接写入池化 NV21 Buffer）
SurfaceTexture/updateTexImage
  → 独立 HandlerThread 上的 OnFrameAvailable（不得使用 UI main）
  → pairedFrame(timestamp, generation；100ms 内有界复用、引用计数）
  → NveCamera2RenderInputFactory
  → NveRenderBridge
  → NveCamera2TexturePresenter（宿主 Target 绑定真实 FBO）
```

宿主只适配相机启动/停止、OES texture、`SurfaceTexture`、worker 调度、generation 增长和
`Target.bindDisplayTarget()`；不得改动方向公式、输入字段含义、配对窗、输出类型分流或
raw/effect 显示矩阵。若目标 AAR hash 未命中，禁止套用这些版本敏感模板。

`ImageReader` 关闭必须与 `onImageAvailable` 中的 acquire/所有权转移以及 worker 中的
convert/close 串行化：回调通过 `offerLatestImage` 把 `Image` 所有权交给 pairer 后不得再
close；新 Image 原子替换并关闭未转换的旧 Image。关闭时使用与 acquire 相同的短临界区，
先移除 listener、停止新帧，再调用 `pairer.reset()` 等待当前转换退出，最后关闭 reader。
只捕获 `IllegalStateException` 不足以保护 `DirectByteBuffer.get`；reader 在转换中被关闭会
使 native buffer 失效并触发 SIGSEGV。stride/NV21 转换放到独立、最多一个在途任务的
worker，直接写入复用 Buffer，不能复制三份平面后再分配一份 NV21，也不能在 Camera2
生命周期 handler 逐帧追加转换任务。

OES 与 YUV 双输出先求共同支持尺寸，优先 1280×720；不可用时选择不超过该尺寸且最接近
16:9 的共同尺寸，避免按屏幕分辨率或设备最大尺寸把 1872×864 等高负载格式带入逐帧检测。
从 `CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES` 优先选择 `[30,30]`，并设置
`CONTROL_AE_TARGET_FPS_RANGE`。该值只是请求；弱光下厂商 HAL 可能为自动曝光把实际帧周期
拉长，验收必须同时记录 request 和实际 sensor frame duration，不能仅凭 request 宣称 30fps。

切换摄像头固定执行：禁用切换入口并递增 generation → 停 repeating/session → 按上述协议
关闭 `ImageReader` → `CameraDevice.close()` → 收到该设备 `onClosed` → 配置并打开下一设备。
不得在 `close()` 后立即 `openCamera`；每次 open/session callback 都要校验设备身份或 token，
过期回调只能关闭自己的资源，不能启用当前 UI。

`SurfaceTexture.OnFrameAvailableListener` 必须注册到专用 `HandlerThread`，不能绑定
`Looper.getMainLooper()` 或 UI `Handler`。滑块拖动会在主线程产生高频触摸与数值事件；
若相机帧回调也在主线程，帧节拍会被持续挤压，表现为只在拖动磨皮等参数时画面轻微抖动。
相机新帧是唯一渲染时钟：参数更新只进入有界 latest-value 槽位，由下一次相机帧排空；
滑块回调及其一层 helper 都不得调用 `requestRender()`。

OES 与检测 Buffer 的“成对提交”不能变成“成对后才消费 OES”。每次
`SurfaceTexture.onFrameAvailable` 都要唤醒 GL，并先执行 `updateTexImage()` 消费最新 OES
buffer；检测帧未就绪或不合格时，跳过本次 EffectKit 提交并立即绘制当前 raw OES，不能保留
上一显示帧。禁止在 `updateTexImage()` 前因 `hasFrame == false` 直接返回，否则 SurfaceTexture
队列可能填满且后续帧通知合并，表现为 Camera2 仍 Active、进程存活但画面永久冻结。切摄
递增 generation/清空配对队列时，必须保留尚未消费的 OES wake-up，或在 GL 线程显式 drain
后再清除；不能直接把 `frameAvailable` 重置为 false。

| 对象 | 尺寸空间 | 方向 | 镜像 |
| --- | --- | --- | --- |
| `NveTexture`（OES） | 显示方向尺寸；90°/270° 时交换相机宽高 | camera sensor orientation | 与最终预览共享单次镜像契约 |
| `NveImageBuffer`（NV21） | 相机平面真实宽高，不交换 | display rotation | 与 OES 输入和原始预览一致 |
| SDK 输出纹理 | 读取输出对象的真实 type 和 size | 不二次猜测旋转 | 不额外重复镜像 |

效果帧必须提交同一时段的 OES 与 NV21。已转换检测帧可在 100ms 窗口内被相邻 OES 帧有界
复用，使预览在 YUV 转换稍晚时仍按相机节拍移动；超窗旧帧立即退役，领先帧保留给后续 OES。
每次 `pairedFrame` 成功获取都必须在 `renderEffect` 返回后的 `finally` 中调用
`releaseFrame`，Buffer 只有在被替换/重置且引用数归零后才能回池。没有合格检测帧、SDK 返回
空或错误时绘制当前原始 OES；降级路径的方向、镜像、crop 和 viewport 必须与效果路径完全
一致，避免 raw/effect 切换造成几何跳变。

## 高频画面问题与根因

| 现象 | 首查根因 | 阻断条件 |
| --- | --- | --- |
| 开启瞬间变形/画面跳变 | OES 用了相机平面宽高；输出仍按输入宽高绘制；raw/effect viewport 不同 | 未证明三条路径的方向、宽高比、crop、viewport 一致 |
| 爆闪 | raw/effect 显示契约不一致；FBO/GL 状态未恢复；重复创建或切换控制器 | `no_flash`、`raw_effect_visual_contract_verified` 未取证 |
| 黑屏 | 输出 texture type shader 错；输出 size 未读取；输出提前 recycle；跨 GLContext 使用 | OES 和 2D 输出分流未验证 |
| 方向错误 | sensor orientation 与 display rotation 混用；宽高只旋转未交换 | 前后摄和四方向未验证 |
| 镜像翻转 | 输入和最终显示重复镜像，或 raw/effect 使用不同 mirror | 前摄单次镜像未验证 |
| 效果贴脸偏移 | NV21 宽高/rotation/mirror 与 OES 不同；帧时间错配 | 输入 pair 契约未证明 |
| 来回切摄卡死或进程退出 | 未配对时不消费 OES/重置时丢失帧唤醒；复制 Image 平面时并发关闭 reader；旧设备未 `onClosed` 就打开新设备；Camera2 handler 积压转换任务 | 未证明 OES 持续消费、reader 关闭串行化、close→onClosed→open 和有界转换调度 |
| 人物移动时预览卡顿、有延迟 | 共享预览尺寸过大；四份 YUV/NV21 分配拷贝；检测帧严格一次消费；缺帧时保留旧显示 | 未证明 1280×720 优先、直接池化转换、100ms 有界复用和当前 raw OES 回退 |
| 只在拖动滑块时持续抖动 | OES 回调绑定主线程；滑块每刻度 `requestRender`；同一算法重复提交 selector | 未证明独立帧回调线程和 camera-frame-only 渲染时钟 |

顺序固定：原始预览 → 无效果 SDK 闭环 → 单效果。任何阶段出现黑屏、闪烁、拉伸、方向或
镜像变化，都先修复公共画面契约，不继续增加效果或 UI。

登记 3.16.1 在总开关迁移时可能短暂把上一帧的 2D texture name 标记成 OES。由于同一 GL
name 不能跨 `GL_TEXTURE_2D`/`GL_TEXTURE_EXTERNAL_OES` 目标采样，profile presenter
会记录上一 2D texture ID；若下一 OES 输出命中该 ID，则该帧稳定降级到原始 OES，避免
`GL_INVALID_OPERATION` 后黑屏。不得删除此基线专用保护，也不得用 texture ID 猜测普通
输出类型。
