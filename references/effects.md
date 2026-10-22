# 滤镜、道具与自定义特效

只读取当前请求对应的章节。素材安装和 ID 校验统一遵循 [assets-and-models.md](assets-and-models.md)，前置资源由 [prerequisites.md](prerequisites.md) 判定，渲染恢复规则由 [lifecycle-rendering.md](lifecycle-rendering.md) 维护。

## 包滤镜

适用于 `filter-packaged`。

先让用户选择“全部已授权滤镜”或具体项，再用 `inspect_filter_catalog.py` 和
`--asset-map` 建立唯一 package 与展示项引用。多个展示项可以共享同一 `.videofx`，但不得
重复安装。不得从妆容 `filter` 组成字段、任意 `.videofx` 或固定 UUID 推断独立滤镜目录。
门禁未解析范围、素材或 lic 时不生成滤镜 UI/控制器。

```java
NveFilter activeFilter = new NveFilter(installedEffectId);
NveEffectKit.getInstance().addFilter(activeFilter);
activeFilter.setFilterIntensity(0.5f);

NveEffectKit.getInstance().removeFilter(activeFilter);
activeFilter = null;
```

- 单选时先移除旧滤镜再添加新滤镜；“无滤镜”是移除当前对象。
- 只用安装 API 返回且完整校验后的 ID 创建 `NveFilter`；元数据固定 UUID 只用于核对素材身份。
- 分离 UI display ID 与安装返回的 package ID；共享包别名之间切换只更新 UI 选择，不得
  重装、移除/添加或叠加同一个滤镜。
- 在串行资源准备线程按唯一 package 安装并缓存结果，不在帧、滑块或列表绑定回调安装。
  单包失败只禁用引用它的展示项；其他滤镜和“无滤镜”保持可用。
- 强度为 `[0,1]`，UI、desired state 与 setter 必须连通。
- 妆容可能内含滤镜，必须明确与独立滤镜的优先级。
- GLContext 恢复时用缓存的安装返回 ID 恢复 desired state，不重复安装素材。
- 登记模板必须调用 `NveFilterCoordinator.restoreAfterInitialize()`；不得只恢复 UI 选中态。
  同一安装返回 package ID 的展示别名切换不得 remove/add 或重建 filter。
- 验证可见、0/默认/明显强度、快速切换、共享包别名、移除和恢复。

## 人脸道具

适用于 `face-prop`。

```java
NveFaceProp faceProp = new NveFaceProp();
faceProp.setEnable(true);
NveEffectKit.getInstance().setFaceProp(faceProp);
faceProp.setPropId(installedArSceneId);

faceProp.setPropId("");
faceProp.setEnable(false);
```

- 复用同一个控制器；切换道具不重复创建或注册。
- 选择有效道具时同时设置 ID 和 `enable=true`；“无”同时执行空 ID、`enable=false` 和
  desired state 清空。只清 ID 或只 disable 都不构成完整关闭。
- 假脸、眼球、分割、Avatar 等附加模型只按素材声明初始化。
- 异步安装使用 generation token 丢弃过期结果。
- 登记模板必须调用 `NveFacePropCoordinator.restoreAfterInitialize()`；恢复时同时重放
  package ID 和 `enable=true`，清空时同时重放空 ID 和 `enable=false`。
- 道具列表优先显示素材元数据提供的正式名称和封面：只有封面时只显示封面，不额外生成
  编号文字；名称和封面都缺失时才使用“道具 1/2/3”一类稳定的本地化回退文案。封面路径
  已声明但不可读时记录可诊断错误并按信息缺失规则降级。“无”入口居中显示统一的
  “圆圈加斜杠”图标，不附加可见文字。
  展示名称、封面和 metadata ID 只用于 UI 与素材身份核对，运行时仍只使用安装 API 返回且
  校验通过的 package ID。
- 道具默认没有强度滑块。验证可见、切换不叠加、关闭和恢复。

## 自定义特效与背景分割

适用于 `custom-effect` 和 `custom-effect-background-segmentation`。

```java
NvsVideoEffect effect = NvsEffectSdkContext.getInstance()
        .createVideoEffect(confirmedEffectName, new NvsRational(9, 16));
if (effect != null) {
    applyConfirmedTypedParameters(effect);
    NveEffectKit.getInstance().addEffect(effect);
}
NveEffectKit.getInstance().removeEffect(effect);
```

- effect name、参数键、类型和插入顺序必须来自目标 AAR 匹配资料或登记基线。
- 创建结果判空，添加与移除成对，参数 desired state 在恢复时重新应用。
- 背景分割只初始化已确认需要的人脸/分割模型；任一必需前置缺失时按门禁停止。
- 只有空回调或占位入口的分割/调色功能不构成实现证据。
- 非相机自定义 GL 不要求前后摄和四方向证据，但仍验证输出分流、回收、降级、GLContext、性能和日志。
