# 处理后画面录制

本文件定义宿主把现有 NveEffectKit 管线的最终输出纹理录成系统相册视频的固定实现、UI、
缺陷防线和验收契约。录制是宿主输出消费能力，不是 EffectKit 控制器；不创建第二条
`renderEffect` 管线，也不覆盖没有 NVE 最终输出的通用相机录制。

## 目录

- [范围与门禁](#范围与门禁)
- [固定数据流](#固定数据流)
- [线程、状态与所有权](#线程状态与所有权)
- [时间戳与音画同步](#时间戳与音画同步)
- [相册输出与音频](#相册输出与音频)
- [停止和生命周期](#停止和生命周期)
- [UI 设计](#ui-设计)
- [已知缺陷防线](#已知缺陷防线)
- [失败、限额与回滚](#失败限额与回滚)
- [验证矩阵](#验证矩阵)

## 范围与门禁

`feature_contract.py --features recording` 必须报告
`prerequisite_mode=host-only`、`requires_nve_prerequisites=false`。这里的 `host-only`
表示不重新执行 NVE AAR/lic/模型/素材门禁，不表示项目可以没有 NVE 输出管线。接入前确认：

- Android application 模块、`minSdk`、相机/预览入口和有效 GLContext 所在线程。
- 已有 `renderEffect` 最终输出消费点，以及最终纹理的 ID、target、宽高、方向、镜像和
  crop/viewport 契约。
- MediaRecorder 编码能力、可用空间、MediaStore 写入、麦克风权限和目标设备。

已有 NVE 管线的 AAR、lic、模型或素材不因单独新增录制而重新检查；未发现现有最终输出时
以 `NVE_FINAL_OUTPUT_SOURCE_MISSING` 停止，并说明本 skill 不覆盖通用相机录制。若请求同时
包含新增美颜等 NVE 能力，报告为 `mixed`，只对新增 NVE 能力执行原有资源门禁；录制等待
该管线产生最终输出后接入。

录制不调用登记基线落地器，也不要求 catalog AAR hash 命中。先 dry-run，再落地版本无关的
宿主模板：

```text
python -B -X utf8 <skill-dir>/scripts/materialize_recording_templates.py <project-root> \
  --source-dir <project-relative-java-dir> --package <java-package> \
  [--test-source-dir <project-relative-java-test-dir>] --dry-run
```

把模板的 `NveFinalTextureFrameSink.Renderer` 适配到现有最终纹理显示器；禁止把原始相机纹理
或新建的第二次 EffectKit 渲染接给录制器。

## 固定数据流

```text
相机帧 → NVE renderEffect → 最终输出纹理 → 预览显示
                                      └→ 预览 GL 线程复制到 3 个自有 RGBA 2D 纹理/FBO
                                          └→ 专用编码 EGL 线程
                                              → MediaRecorder input Surface
                                              → H.264 + 可选 AAC → MediaStore
```

只录最终输出，不从预览 View 截屏，也不重新执行一次特效。预览 GL 线程用
`glBlitFramebuffer` 或等价全屏绘制复制到自有 2D 纹理；跨共享 context 使用前执行
`glFinish()`。编码线程创建与预览共享的 EGLContext，把 2D 纹理绘制到 MediaRecorder
Surface，调用 `eglPresentationTimeANDROID` 后 `eglSwapBuffers`。

编码消费尺寸来自最终输出纹理，并按编码器能力对齐；方向和镜像必须沿用预览已经验证的
视觉契约。若预览输出为 OES，先在预览 context 转成自有 2D 纹理，不能把跨线程 OES
生命周期交给编码器。

## 线程、状态与所有权

状态只使用 `IDLE → STARTING → RECORDING → STOPPING → IDLE`：

- UI 线程只发起状态转换和显示结果；`STARTING`、`STOPPING` 时按钮禁用。
- 预览 GL 线程只复制最终帧并非阻塞入队；不得调用 MediaRecorder `stop()`。
- 编码 EGL 线程拥有 encoder EGLContext/EGLSurface、绘制和 swap。
- finalize worker 执行可能阻塞的 `MediaRecorder.stop()` 与 MediaStore 发布。

固定使用 3 个自有纹理槽或容量为 3 的等价有界池。槽位忙时丢弃最新录制帧并继续预览，
禁止等待编码线程。只有第一次 encoder swap 成功后才把状态切到 `RECORDING` 并提示开始；
成功入队不等于已经开始录制。

## 时间戳与音画同步

相机时间戳和 MediaRecorder 音频可能不在同一零点。`MediaRecorder.start()` 成功后立即记录：

```text
recorderStartSystemNs = System.nanoTime()
firstCameraTimestampNs = 第一帧相机时间戳
presentationNs = recorderStartSystemNs
    + max(0, cameraTimestampNs - firstCameraTimestampNs)
presentationNs = max(presentationNs, lastPresentationNs + 1)
```

不得把第一帧归一化为 `0` 或 `1ns`；这会使视频帧早于 recorder 启动而被丢弃。不得把原始
相机时间戳直接交给 muxer；不同时间域可能被误判为已达到最长时长。异常、回退或重建后的
时间戳仍须单调递增。

## 相册输出与音频

Android 10+ 在开始前创建 `MediaStore.Video` pending 行：

- `DISPLAY_NAME` 使用唯一 `.mp4` 文件名。
- `MIME_TYPE=video/mp4`。
- `RELATIVE_PATH=DCIM/Camera`，`IS_PENDING=1`。
- 通过 `ContentResolver.openFileDescriptor(..., "rw")` 的 FD 直接
  `MediaRecorder.setOutputFile(fd)`；成功停止并关闭 FD 后置 `IS_PENDING=0`。

Android 6–9 写公共 `DCIM/Camera`，完成后执行媒体扫描并处理对应存储权限。禁止先写应用
私有临时文件再整文件复制到相册；该做法会产生双倍 I/O 和瞬时空间占用。失败或空文件删除
pending 行/目标文件，不能把损坏视频发布到相册。

音频采用 `CAMCORDER`/`MIC` + AAC，并声明可选麦克风 feature。运行时请求
`RECORD_AUDIO`；拒绝时给瞬时说明并继续保存无声视频，不把拒绝权限当作崩溃或永久阻塞。

## 停止和生命周期

正常停止顺序固定为：

1. 状态进入 `STOPPING`，停止接收新录制帧。
2. 编码线程排空已接收帧并完成最后一次 swap，保持 encoder EGLSurface 有效。
3. finalize worker 调用 `MediaRecorder.stop()`。
4. stop 成功后才释放 encoder EGLSurface/EGLContext、MediaRecorder、FD，并发布 MediaStore。
5. UI 回到 `IDLE`，提示“录制完成，已保存到手机相册”。

不得在 `MediaRecorder.stop()` 前销毁 encoder EGLSurface；部分设备会返回 stop 错误或生成
缺少有效轨道的 MP4。页面 `onPause`、宿主停止或 GL teardown 时，先按上述流程停止并保存，
再停止相机/帧源、释放宿主 GL，最后 `cleanUp`。恢复前台后重建共享 context 和纹理槽，不复用
失效的 GL 对象；应用进程不应因普通前后台切换退出。

## UI 设计

优先级仍为用户设计 → 工程已有设计体系 → 本节默认值。默认入口：

- 放在现有最底部入口容器的精确中心；使用等权槽位，四个既有模块时插入 index 2。
- `ImageButton` 为 `44 × 44dp`，宽高必须相等，`GradientDrawable.OVAL`，图标
  `centerInside`、内边距 `6dp`；不能靠内容或非等比约束形成椭圆。
- 空闲态为黑色半透明正圆 + 白色圆形录制符号；录制态为红色正圆 + 白色停止符号。
- content description 固定为“录制视频”/“停止录制”，测试 tag 建议 `nve-record-entry`。
- 不展开效果面板，不显示持久“● 录制中”文本。开始、完成、权限降级和错误都用瞬时提示。
- 相机不可用时禁用；录制中禁用切摄，避免输出尺寸/context 在单个文件中途变化。

## 已知缺陷防线

| 缺陷 | 固定修复 |
| --- | --- |
| 预览 GL/UI 在切 context 或 stop 时卡住 | 专用编码 EGL 线程 + finalize worker |
| 私有临时文件再复制导致双倍 I/O/空间 | 直接写 MediaStore FD |
| 只有视频无音频 | AAC + 运行时权限；拒绝时显式无声降级 |
| stop 返回错误、MP4 轨道损坏 | `MediaRecorder.stop()` 前保持 encoder EGLSurface |
| 不同时间域被误判为超长视频 | 相机 delta 重基到 recorder 启动后的 `System.nanoTime()` |
| 第一帧归零后全部被丢弃 | 使用 system anchor，不从 0 开始 |
| UI 提前显示录制中 | 首次 swap 成功才进入 `RECORDING` |
| 切后台崩溃或进程退出 | 先停止保存录制，再释放相机/GL/EffectKit |
| 达时长/文件上限被当作渲染故障 | 将 limit info 作为正常收尾并保存有效文件 |

## 失败、限额与回滚

- 开始前检查至少 64MB 可用空间；按 codec capability 选择支持的尺寸、帧率和 bitrate。
- 默认最大时长 30 分钟、最大文件 1.8GB；同时注册 MediaRecorder error/info listener。
- STARTING 任一步失败都回滚 MediaStore、FD、Recorder、EGL 和纹理槽，恢复 `IDLE`。
- STOPPING 的 stop 失败删除未发布项并提示失败；不能显示成功提示。
- 达到时长/文件上限后 input Surface 被放弃可能是预期现象；以 info/状态为依据正常停止，
  不把后续 swap 失败覆盖成致命错误。

## 验证矩阵

静态严格校验使用 `--features recording`。真机至少确认：

1. H.264 视频轨道存在，允许麦克风时 AAC 轨道存在；拒绝权限时无声视频仍可播放。
2. 文件出现在系统相册 `DCIM/Camera`，从照片/系统播放器可直接打开。
3. 录制 3 秒、30 秒；画面方向、镜像、裁剪与预览一致，首帧正常，音画时长接近。
4. 录制期间预览不卡住；压力下可以丢录制帧，但 UI 和相机线程不能等待。
5. 录制中切后台会保存有效文件；连续 10 次前后台后进程仍在、预览恢复且无崩溃。
6. 空间不足、权限拒绝、codec 不支持、start/stop 异常、时长/文件上限均有正确提示和回滚。
7. 入口位于底部容器精确中心、是 `44 × 44dp` 正圆；开始/完成有瞬时提示且无持久录制文字。
