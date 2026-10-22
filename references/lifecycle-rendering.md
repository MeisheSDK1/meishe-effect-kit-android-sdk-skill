# 生命周期与渲染

本文件是初始化层级、帧输入输出、GL/线程、方向、回收和恢复的唯一规则来源。

## 目录

- [生命周期层级](#生命周期层级)
- [SDK 与模型初始化](#sdk-与模型初始化)
- [选择渲染模式](#选择渲染模式)
- [构造输入](#构造输入)
- [渲染和回收](#渲染和回收)
- [方向与镜像](#方向与镜像)
- [线程约束](#线程约束)
- [恢复与销毁](#恢复与销毁)

## 生命周期层级

运行状态统一为 `UNINITIALIZED → SDK_READY → GL_READY → RUNNING ↔ PAUSED → DESTROYED`。`PAUSED` 表示进程级 SDK 仍有效但帧源或 GL 会话不可用；GLContext 丢失时从 `PAUSED` 重新建立 `GL_READY` 再进入 `RUNNING`。状态迁移必须幂等。

UI 始终先更新 desired state；只有 `RUNNING && available && controller != null` 才同步应用到 SDK。非 RUNNING 期间的修改缓存到 desired state，恢复时按固定顺序合并应用，不在失效 GL/控制器上调用。

遵循以下顺序：

```text
进程/业务会话：init → initModel...
GL 渲染会话：initialize → renderEffect... → cleanUp
进程/业务会话结束：destroy
```

- `init` 绑定授权和底层 SDK。多页面共享时由进程级或业务级 owner 管理，禁止每个 View 各自初始化/销毁。
- `initModel` 在需要对应能力前调用。记录每种模型的成功状态，失败功能应禁用或降级。
- `initialize` 建立渲染核心。GLContext 重建后重新调用并恢复效果控制器。
- `cleanUp` 释放渲染核心，必须在持有对应有效 GLContext 的线程调用。
- `destroy` 释放 SDK 全局资源。调用后再次使用必须重新 `init` 和初始化模型。

## SDK 与模型初始化

Java 示例：

```java
NveEffectKit effectKit = NveEffectKit.getInstance();
boolean sdkReady = effectKit.init(appContext, licensePath);
if (!sdkReady) {
    // 保持原始预览，禁用全部 Nve 能力，不继续 initialize/renderEffect。
    return;
}

boolean faceReady = effectKit.initModel(
        appContext,
        NveDetectionModelType.FACE,
        faceModelPath
);
if (!faceReady) {
    // 禁用依赖 FACE 的功能；日志只记录模型类型，不输出授权内容。
}
```

Kotlin 项目按现有风格等价改写，不额外包装协程；初始化是否异步由客户 SDK 版本和现有线程模型决定。

不要写死示例 lic 或模型文件名。路径只接受客户提供的 `assets:/...` 或本地绝对路径。

## 选择渲染模式

| 模式 | 输入 | 输出 | 典型用途 |
| --- | --- | --- | --- |
| `TEXTURE_TEXTURE` | 纹理，可同时提供检测 Buffer | 纹理 | 已有 OpenGL 预览、编码或推流管线 |
| `BUFFER_TEXTURE` | Buffer | 纹理 | CameraX/ImageReader 输入，后续由 GPU 显示或编码 |
| `BUFFER_BUFFER` | Buffer | Buffer | 纯 CPU/离线帧管线 |

选择原则：

- 与现有管线的数据形态一致，减少 CPU/GPU 往返。
- OES 纹理通常使用 `NveTextureType.NV_OES_TEXTURE`；普通 2D 纹理使用 `NV_COMMON_TEXTURE`。
- `TEXTURE_TEXTURE` 场景若效果需要人脸检测，可同时提交与纹理同一时刻的 NV21/YUV Buffer。
- 不把模式名称理解成资源所有权；始终按当前 AAR 契约处理输入和输出生命周期。
- CameraX `PreviewView + ImageAnalysis` 本身没有把 Analyzer 处理结果送回 `PreviewView` 的通道。若工程没有可写 Surface/GL/编码消费方，先明确新增的输出显示管线；不能只在 Analyzer 调用 SDK 就宣称预览已生效。

## 构造输入

纹理输入的核心结构：

```java
NveRenderInput input = new NveRenderInput();
input.setPixelFormat(NveFormatType.VIDEO_FRAME_PIXEL_FROMAT_NV21.getType());

NveRenderConfig config = new NveRenderConfig()
        .setRenderMode(NveRenderConfig.NveRenderMode.TEXTURE_TEXTURE)
        .build();
config.setCameraOrientation(cameraOrientation);
input.setConfig(config);

NveTexture texture = new NveTexture();
texture.setTextureId(inputTextureId);
texture.setTextureType(NveTextureType.NV_OES_TEXTURE);
texture.setSize(new NveSize(textureWidth, textureHeight));
texture.setOrientation(cameraOrientation);
texture.setMirror(isFrontCamera);
texture.setVerticalMirror(needsVerticalMirror);
input.setTexture(texture);
```

需要检测 Buffer 时：

```java
NveImageBuffer imageBuffer = new NveImageBuffer();
imageBuffer.setData(frameData);
imageBuffer.setSize(new NveSize(bufferWidth, bufferHeight));
imageBuffer.setDisplayRotation(displayRotation);
imageBuffer.setMirror(isFrontCamera);
// SDK 版本暴露 rowPitchY/rowPitchUV setter 时，传入真实 stride。
input.setImageBuffer(imageBuffer);
```

注意：

- `pixelFormat` 必须与 Buffer 真实格式一致，不因输出是纹理就随意填写。
- `NveSize` 表示 Buffer 的真实平面布局尺寸。旋转 90/270 度后是否交换宽高，以实际 SDK 契约和上游数据为准。
- CameraX 的 YUV_420_888 通常不是连续 NV21；必须正确转换或使用 SDK 支持的原始布局，不得直接拼接错误平面。
- 输入 byte array/ByteBuffer 在 `renderEffect` 返回前不得复用或释放。

## 渲染和回收

```java
NveRenderOutput output = null;
try {
    output = effectKit.renderEffect(input);
    if (output == null
            || output.getErrorCode().getCode() != NveRenderError.NO_ERROR.getCode()) {
        // 降级显示原始帧，记录错误码。
        return;
    }
    NveTexture texture = output.getTexture();
    if (texture == null) {
        // 降级显示原始帧。
        return;
    }
    if (texture.getTextureType() == NveTextureType.NV_OES_TEXTURE) {
        consumeOesTexture(texture);
    } else {
        consume2DTexture(texture);
    }
} finally {
    if (output != null) {
        effectKit.recycleOutput(output);
    }
}
```

- 在回收前完成绘制、复制或编码提交；若下游异步使用纹理，先复制到自有纹理/FBO。
- 空输出或错误输出时使用原始帧降级，不能让预览黑屏。
- 无效果或总开关关闭时可能透传 OES 输入；读取输出 `textureType` 分流 OES/2D shader，不能用 texture ID 或开关状态推断。
- 使用输出纹理自身的尺寸和布局设置采样/FBO/viewport；不能默认沿用输入宽高。输出纹理为空时按错误输出降级。
- `renderEffect` 后先恢复宿主 viewport、framebuffer、program、active texture 和纹理 binding，
  再绘制 SDK 输出或原始降级帧。优先调用集中 `restoreHostGlState/setupFrameInfo`，不让不同
  效果分支各自猜测 SDK 遗留状态。
- 所有早退、异常和录制分支都要回收非空输出。
- `renderEffect`、输出消费和 `recycleOutput` 保持在约定的 GL/渲染线程，避免跨上下文纹理失效。

## 方向与镜像

将以下变量分别建模，不合并成一个“旋转角”：

1. 传感器方向。
2. 当前设备显示旋转。
3. 相机前后摄。
4. 预览是否镜像。
5. 输入纹理是否上下翻转。
6. 输出目标的坐标系和宽高。

推荐用带标记的测试图或不对称人脸验证四个设备方向、前后摄和录制结果。预览镜像不等于编码镜像；分别配置。

`CanonicalPreviewTransform` 表示“最终视觉契约”，不是要求 OES 与 2D 使用数值完全相同的矩阵。两条路径的采样矩阵可以不同，但显示结果必须同向、同裁剪、同镜像：

| 字段/阶段 | 单一责任 |
| --- | --- |
| `SENSOR_ORIENTATION` / texture orientation | 描述相机像素相对自然方向，不代替显示旋转 |
| `displayRotation` | 描述当前显示方向，用于检测坐标/输出归一化，以当前 AAR 契约为准 |
| `SurfaceTexture` matrix / vertical mirror | 只修正 OES 采样坐标与上下翻转 |
| Buffer mirror | 只描述检测 Buffer 与纹理是否同一人脸坐标系 |
| 最终预览镜像 | 产品定义的前摄水平镜像，只在最终像素链执行一次 |
| viewport/crop | 统一的宽高比和 center-crop/fit 策略 |

基线 AAR 中输入 metadata 的 mirror 可能影响检测/效果坐标，不能简单视为已经完成最终屏幕镜像。为原始 OES 和效果 2D 分别计算采样矩阵，再用测试卡确认它们满足同一最终视觉契约；开关效果不得改变结果。

Camera2 双输出采用有界最新帧槽位：ImageReader 回调把最新 Image 所有权转交 pairer，旧待处理 Image 立即关闭；stride 转换离开回调线程、最多一个任务在途，并直接写入池化 NV21 Buffer，不创建 Y/U/V 三份中间数组。统一把时间戳归一为纳秒，按最近值与最大帧年龄配对，不要求严格相等；登记基线的最大帧年龄为 100ms，检测帧只在该窗口内复用于相邻 OES 帧。每次获取必须在 `renderEffect` 后 `finally` 释放引用，Buffer 被替换/重置且引用归零后才能回池。每个 OES 通知都先在 GL 线程 `updateTexImage()`；无合格 Buffer 时跳过 EffectKit 提交并绘制当前 raw OES，禁止把检测就绪作为消费或呈现 OES 的前置条件。切摄、暂停、销毁时递增 generation 并清空配对队列，但不得丢弃尚未消费的 OES wake-up；reader 关闭与 acquire/所有权转移/convert/close 必须通过同一短临界区和 pairer conversion lock 串行化，防止转换期间 native buffer 失效。切摄必须等待旧 `CameraDevice.onClosed` 后再打开下一设备，旧 callback 由设备身份或 generation token 拒绝。

## 线程约束

线程角色固定，具体调用线程由目标 AAR API、正式配套资料与运行时证据决定并写入项目适配层：

| 角色 | 责任 |
| --- | --- |
| 主/串行控制线程 | 状态迁移、控制器参数/注册、素材结果提交；与渲染并发时按 AAR 线程安全契约串行化 |
| GL 渲染线程 | 纹理/FBO、`renderEffect`、输出消费、回收、`cleanUp` |
| 相机回调线程 | acquire、Image 所有权转移与轻量入队，不安装素材、不渲染 |
| 素材/转换工作线程 | YUV 转换、文件 IO、JSON、路径/元数据校验和封面解码；用 generation 把最新结果提交到控制线程 |

- 登记基线在 GL 资源就绪、相机开始前由主线程调用 `initialize`，渲染与 `cleanUp` 在 GL 会话线程；若目标 AAR 不同，以其实际 API 和验证结果为准并保持全程一致。
- 登记 `3.16.1` AAR 的 `getInstance`/素材包安装调用必须在 Android 主线程执行；真机从后台
  worker 调用会触发 Main Thread Checker。只把准备完成的路径、lic 路径和包类型提交到
  主线程安装，文件 IO、JSON 和 Bitmap 解码仍留在素材工作线程。非登记 AAR 重新核实。
- Camera2 OES 的 `SurfaceTexture.OnFrameAvailableListener` 使用独立 `HandlerThread`；
  禁止注册到 UI main `Handler`。否则滑块拖动期间主线程触摸事件会挤压帧回调，造成持续抖动。
- Camera2 状态/session callback 不与逐帧 YUV 转换共用可积压的任务队列；转换 worker 使用
  latest-only Image 槽位和单个调度标志，直接写入复用 NV21 Buffer。关闭 reader 时先阻止
  新 callback，并通过 pairer reset 等待在途 Image 转换退出；不能依赖 Java 异常捕获防止
  native DirectByteBuffer 失效。
- 高频滑块只保留最新 desired value；每个控件最多有一个待提交更新，避免跨线程积压和乱序。
  相机新帧是实时预览的唯一渲染时钟；在下一帧开始时排空 latest-value，不从
  `onProgressChanged` 或其 helper 调用 `requestRender()`。
- 通过 `GLSurfaceView.queueEvent`、现有 GL Handler 或渲染队列切换线程；不要在任意主线程直接释放 GL 资源。
- `cleanUp` 前停止新帧入队，并等待正在处理的帧完成。等待必须有超时；GLSurfaceView 已暂停且队列不再执行时，不可无限阻塞主线程，报告清理失败并由 owner 在仍有效的 context 阶段重试。
- 不在持锁状态下等待主线程和 GL 线程互相回调，避免页面退出死锁。

## 恢复与销毁

页面恢复或 GLContext 重建：

1. 重建纹理/FBO。
2. 按目标基线线程契约调用 `initialize`。
3. 先恢复参数中性的 Beauty AR Scene 锚点，再按 `NveShape`、`NveMicroShape`、
   `NveMakeup`、滤镜、道具、自定义效果的已确认顺序恢复状态。登记 3.16.1 基线中，
   Beauty、Shape、MicroShape 都必须先调用对应的 `EffectKit.setX(controller)` 重新绑定，
   再调用 `controller.setEnable(true)`；`cleanUp` 后禁止在重新绑定前操作失效控制器。
4. 恢复帧提交。

页面暂停且 GLContext 确定销毁：

1. 停止相机/帧源和录制。
2. 在 GL 线程消费或丢弃队列尾帧。
3. 在旧 EGLContext 仍有效时按目标 AAR 基线释放宿主与 EffectKit GL 资源。
4. 登记 SDK 3.16.1 基线固定先释放宿主
   shader/FBO/纹理，再把 `cleanUp` 作为最后一个 GL 调用；它可能改变当前 EGL 状态。
5. 非登记 AAR 必须由其 API、正式配套资料或运行时证据重新证明先后顺序，再销毁 EGLContext/Surface。

连续单帧错误需要有熔断：按时间窗口限频记录错误，达到项目定义阈值后暂停 EffectKit 路径并保持原始预览；只在生命周期重建或显式恢复策略下重新 `initialize`，不能逐帧无限抛异常和刷日志。

只有最后一个 SDK 使用方退出时才调用 `destroy`。多页面或后台服务共享时使用引用计数/集中 owner，不能由单个 View 无条件销毁。
