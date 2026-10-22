# 工程接入

本文件只维护 Gradle、模块、AAR、ABI、Manifest 和宿主接入点；帧处理与 GL 运行时契约由 [lifecycle-rendering.md](lifecycle-rendering.md) 维护。

## 目录

- [接入前盘点](#接入前盘点)
- [选择目标模块](#选择目标模块)
- [引入 AAR](#引入-aar)
- [处理依赖与 ABI](#处理依赖与-abi)
- [Manifest 与资源](#manifest-与资源)
- [适配现有架构](#适配现有架构)
- [最小改动顺序](#最小改动顺序)

## 接入前盘点

先运行 `inspect_android_project.py`，再确认以下事实：

1. 哪个 application/library 模块拥有相机帧和 GLContext。
2. 工程使用 Groovy 还是 Kotlin DSL，主体代码是 Java 还是 Kotlin。
3. 输入是 OES/2D 纹理、NV21、YUV_420_888、RGBA，还是同时有纹理和检测 Buffer。
4. 输出由预览、编码器、推流器还是 CPU 算法消费。
5. 客户提供的 AAR、lic、模型和素材版本是否配套。
6. 前后摄、方向、镜像和 GL 线程由哪个组件负责。

不要通过创建独立 Activity 或复制目标工程外的相机实现来绕过这些问题。

## 选择目标模块

- 将 AAR 放入真正消费 NveEffectKit API 的模块 `libs/`，避免多个模块重复打包。
- 若业务通过内部 effect library 封装 SDK，将依赖设为该库的 `api` 或 `implementation`，取决于是否向上层暴露 Nve 类型。
- 优先隐藏第三方类型：业务层使用自有配置模型，适配层再映射到 `NveBeauty`、`NveFilter` 等类型。
- 不把目标工程外的美颜、美妆 UI 模块作为必要依赖；NveEffectKit 接入不依赖外部 UI 实现。

## 引入 AAR

Groovy：

```groovy
dependencies {
    implementation files('libs/NveEffectKit.aar')
}
```

登记 3.16.1 的紧凑美妆资源 loader 还需要：

```groovy
implementation 'com.google.code.gson:gson:2.8.2'
```

这是 AAR 内 `ParseJsonFile` 的运行时依赖；缺失时 Java 编译仍可能成功，但解析组合妆会
触发 `NoClassDefFoundError: com.google.gson.Gson`。以 baseline catalog 的
`template_runtime_dependencies` 为事实源，不从外部示例工程猜测其他依赖。

Kotlin DSL：

```kotlin
dependencies {
    implementation(files("libs/NveEffectKit.aar"))
}
```

使用 `files(...)` 时无需为单个 AAR 增加 `flatDir`。若公司已有内部 Maven 制品管理，沿用其坐标和校验流程，不同时保留本地 AAR。

接入后检查依赖树和最终 APK/AAB：

- 只能解析到一个 NveEffectKit 版本。
- 不能同时打包 AAR 内嵌和项目独立的 `NvAndroidStreamingSdk.jar`。
- 静态盘点只把工程源码/依赖目录中的 JAR 当作独立依赖；忽略 `.gradle`、
  `.gradle-local`、模块 `build/` 等 Gradle transform 缓存，避免把 AAR 解包后的内嵌
  JAR 再误判成一份项目依赖。最终仍以 Gradle 依赖树和 APK/AAB 内容复核。
- 其他美摄 SDK 模块必须与 AAR 内部版本兼容；无法确认时向供应方索取配套版本，不能靠排除规则强行编过。

## 处理依赖与 ABI

当前资料中的 AAR 自带 Streaming SDK JAR、`libNvStreamingSdkCore.so` 和 ARM 原生库。每次客户提供新 AAR 都重新检查，不要假设内部结构永久不变。

若 AAR 内嵌 `NvAndroidStreamingSdk.jar`：

1. 查找所有模块 `libs/`、版本目录和远程依赖中的独立 Streaming SDK。
2. 确认没有其他组件强依赖不同版本。
3. 删除或排除重复项后运行依赖报告和打包检查。

ABI 原则：

- `abiFilters` 只能选择 AAR 实际包含的 ABI。
- 若应用需要 x86/x86_64 测试，必须取得对应原生库；不能用 ARM 配置假装支持模拟器。
- App Bundle 仍需检查每个 split 是否包含配套 `.so`。
- 遇到 `UnsatisfiedLinkError`，先核对 APK 内实际路径、ABI 和进程位数，再检查加载顺序。

不要直接复制外部参考工程的 compileSdk、targetSdk、AGP、Gradle Wrapper、Kotlin 或第三方库版本。它们不是 SDK API 契约。

## Manifest 与资源

NveEffectKit 接入本身不要求复制外部工程的权限。按客户功能最小化声明：

```xml
<uses-permission android:name="android.permission.CAMERA" />
<uses-permission android:name="android.permission.RECORD_AUDIO" />
```

- 只做已有帧离线处理时，不因 SDK 示例增加相机、录音或存储权限。
- 使用系统媒体选择器或应用私有目录时，不复制旧版外部存储权限。
- 不复制位置、Wi-Fi、Profile 等与效果渲染无关的外部工程权限。
- lic、模型和素材放在客户约定目录；`assets:/...` 表示 APK assets 路径，绝对路径表示运行时本地文件。
- 不重命名客户提供的授权或素材文件来匹配示例；更新代码配置以匹配真实文件。

## 适配现有架构

### CameraX

- 在 `ImageAnalysis.Analyzer` 获取 YUV 数据时，确认 plane、rowStride、pixelStride 和旋转。
- `PreviewView` 不会显示 Analyzer 的处理结果。必须确认已有可写 Surface/GL/编码消费方；没有时先设计最小输出显示管线，再选择 `BUFFER_TEXTURE` 或 `BUFFER_BUFFER`。
- 使用 `STRATEGY_KEEP_ONLY_LATEST` 或容量 1–2 的自有队列；异步时先复制再关闭 `ImageProxy`。
- 若预览链路拥有 GPU 纹理，优先在其 GL 管线接入；不要仅为接入效果把每帧纹理读回 CPU。
- 正确关闭 `ImageProxy`，但只能在 NveEffectKit 不再读取该帧之后关闭。

### Camera2/ImageReader

- 在 `onImageAvailable` 或自有帧分发器建立接入点。
- 用 `StreamConfigurationMap` 验证目标尺寸同时支持 SurfaceTexture 与 YUV；双 Surface session 配置失败时回退到原始单 Surface 预览并禁用检测效果。
- `ImageReader.maxImages` 取能覆盖在途复制的最小有界值，回调使用 `acquireLatestImage`；始终在 `finally` 关闭 Image。
- 将 reader 的 listener 移除/close 与回调中的 acquire/copy/close 放在同一 handler 或同一
  短临界区；禁止在 UI 线程直接关闭正在被相机线程读取平面的 reader。
- 前后摄切换先关闭 session/reader/device，收到旧设备 `onClosed` 后再 open 新设备；用
  设备身份或 generation token 拒绝旧 open/session callback，切换完成前保持入口禁用。
- 避免在相机回调线程执行长时间 GL 初始化；把帧交给已有渲染线程。
- YUV stride 转换使用独立 latest-only worker，最多一个转换任务在途，不让 Camera2 状态
  callback 排在无界的逐帧转换 runnable 后面。
- 使用 `SurfaceTexture` 时同步纹理 timestamp 与检测 Buffer，避免人脸位置滞后一帧。
- `RENDERMODE_WHEN_DIRTY` 在新 OES 帧且有合格检测 Buffer（或允许无检测降级）时 `requestRender`；`onSurfaceChanged` 更新 viewport/crop，`onSurfaceCreated` 只创建当前 context 资源并触发受控 initialize/restore。
- 明确是否使用 `setPreserveEGLContextOnPause`。无论是否保留，都必须覆盖 context 实际丢失的重建路径。

### 自定义 OpenGL/推流

- 沿用现有 EGL 共享上下文、FBO 和编码器纹理协议。
- 明确输入纹理所有权和输出纹理有效期；`recycleOutput` 后不能继续引用 SDK 输出。
- 只在必要时转换 OES→2D 或 YUV→RGBA，并记录性能影响。
- 异步编码/共享 context 消费先复制到自有纹理，并按现有 EGL fence/同步协议交接；尺寸变化时重建 FBO 与编码目标。

## 最小改动顺序

1. 接入单一 AAR并解决重复依赖和 ABI。
2. 配置客户 lic 路径并调用 `init`。
3. 按本次功能初始化最小模型集合，逐项检查返回值。
4. 在现有 GL 线程准备纹理等资源，按当前 SDK 线程约定调用 `initialize`，再完成无效果单帧渲染。
5. 接通输出预览/编码，验证方向、镜像和分辨率。
6. 加入一个最简单效果并验证开关、参数和恢复。
7. 再接入素材安装、复杂美妆、道具或自定义特效。
8. 补齐 `cleanUp`、`destroy`、错误处理和静态验收。
