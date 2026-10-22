# 美颜：美肤、美型与微整形

只在请求 `beauty-suite`、`beauty-skin`、`beauty-shape`、`beauty-micro-shape` 或其子项时读取本文件。

## 范围

先运行 `feature_contract.py`。未限定的“美颜”及未说明完整/具体项的美肤、美型、微整形
都会返回 `scope_selection_required`；使用报告中的 `selection_requests` 询问并等待答复，
不要在文档外硬编码另一组选项。资源目录只授权读取；只有用户明确要求按交付能力接入时，
才由已授权资源映射具体 leaf feature。最终只展示所选集合。

美型或微整形依赖参数中性的 Beauty AR Scene 时，它只是内部依赖，不算已交付美肤。

登记基线的产品目录以
`assets/baseline-3.16.1/catalog/feature-catalog.json` 为机器可读事实：美肤 12 项、
美型 11 项、微整形 21 项。该目录用于映射和校验，不是默认资源要求。只有用户明确要求
完整美颜时才逐项实现；部分接入不得为未选项创建禁用按钮或索要无关素材。
命中基线时从 `features/NveBeautyCoordinator.java.template` 适配控制器和枚举映射。

先运行：

```text
python -B -X utf8 <skill-dir>/scripts/feature_contract.py --features <csv>
```

## 美肤

长期持有 `NveBeauty`，就绪后注册一次，后续直接更新同一对象：

```java
NveBeauty beauty = new NveBeauty();
beauty.setEnable(true);
beauty.setBlurType(NveBeautyBlurTypeEnum.BuffingSkin);
beauty.setBlurIntensity(0.5f);
NveEffectKit.getInstance().setBeauty(beauty);
```

磨皮与美白的“算法类型”和“强度”必须分别去重。滑块连续调节同一个磨皮项时只调用
`setBlurIntensity`，不得在每个刻度重复调用相同的 `setBlurType`；美白同理。只有用户切换
到另一算法、首次注册或生命周期恢复需要重建 applied state 时，才更新类型选择器。登记
3.16.1 上重复提交相同类型可能重置内部时序状态，使拖动磨皮时人脸出现轻微持续抖动。

| 能力 | API/类型 | 范围 |
| --- | --- | --- |
| 普通磨皮 | `BuffingSkin` + `setBlurIntensity` | `[0,1]` |
| 高级磨皮 1/2/3 | `AdvancedBuffingSkin1/2/3` | `[0,1]` |
| 高级磨皮 4 | `AdvancedBuffingSkin4` | `[0,1]`，只在 `NvsEffectSdkContext.getInstance().getDeviceCpuLevel() == NvsEffectSdkContext.DEVICE_CPU_POWER_LEVEL_HIGH` 时展示 |
| 美白 A/B | `setWhiteningType`、`setWhiteningIntensity` | `[0,1]` |
| 去油光/半径 | `setMatteIntensity`、`setMatteFillRadius` | `[0,1]` |
| 红润 | `setReddeningIntensity` | `[0,1]` |
| 清晰度 | `setDefinitionIntensity` | `[0,1]` |
| 锐度 | `setSharpnessIntensity` | `[0,10]` |

这里的 `NvsEffectSdkContext` 是 `com.meicam.effect.sdk.NvsEffectSdkContext`。`AdvancedBuffingSkin4` 的设备条件和 `setSharpnessIntensity` 名称属于当前登记 AAR 基线。非基线 AAR 必须以编译结果或字节码核实，禁止猜 API。普通磨皮不能默认替换成高级磨皮。

## 美型

使用长期持有的 `NveShape`；素材驱动类型安装 `.facemesh`/`.warp` 后，只使用安装返回且已通过空值、UUID 格式、全零 UUID 检查的 package ID。

常用类型均为 `[-1,1]`：`FACE_WIDTH`、`FACE_LENGTH`、`FACE_SIZE`、`FORE_HEAD`、`CHIN`、`EYE_SIZE`、`EYE_ANGLE`、`NOSE_WIDTH`、`NOSE_LENGTH`、`MOUTH_SIZE`、`MOUTH_CORNER`。

```java
shape.setShapeData(NveShapeTypeEnum.FACE_SIZE, new NveShapeData(0.5f, packageId));
NveEffectKit.getInstance().setShape(shape);
shape.setDegreeByType(NveShapeTypeEnum.FACE_SIZE, 0.2f);
```

## 微整形

使用长期持有的 `NveMicroShape`。`NASOLABIAL_FOLDS`、`DARK_CIRCLES`、`BRIGHTEN_EYES`、`WHITEN_TEETH` 为 `[0,1]`；头型、颧骨、下颌、太阳穴、眼部位置/宽高/弧度、人中、鼻梁/鼻头、眉毛粗细/角度/位置为 `[-1,1]`。

```java
microShape.setMicroShapeData(
        NveMicroShapeTypeEnum.HEAD_SIZE,
        new NveMicroShapeData(0.4f, packageId));
NveEffectKit.getInstance().setMicroShape(microShape);
microShape.setDegreeByType(NveMicroShapeTypeEnum.HEAD_SIZE, 0.2f);
```

枚举映射来自目标 AAR，不从本地化文案反射 `valueOf`。完整美颜的三个分类分别维护 `available/enabled/desired state`；单项失败只禁用受影响项。

调节项选择与效果写入必须分离：点击美肤/美型/微整形项只改变当前 UI selection，并静默把已保存
值显示到滑块；不得在选择回调中重新调用对应 setter、重新绑定 `NveShape`/`NveMicroShape`
或请求特殊渲染。只有滑块真实用户输入、显式重置、生命周期恢复和总开关迁移可以改变
desired/applied state，且 coordinator 必须去重相同 applied value。验收快速执行
`A → B → A`，确认 SDK setter 调用数不变、人物几何无轻微抖动。

登记的 3.16.1 基线中，美颜总开关关闭不得只调用 `NveMicroShape.setEnable(false)`：
该调用可能不会撤销已经生效的微整形参数。协调器必须分别保存 desired value 与 applied
value，关闭时保持 Beauty、Shape、MicroShape 控制器已注册且可用，并对已应用的美型和
微整形类型显式调用 `setDegreeByType(type, 0f)`；重新开启时再恢复 desired value。生命周期
恢复到“总开关关闭”状态时也必须走同一归零路径，不能先恢复非零值后只关闭控制器。

默认面板的“重置”统一调用 coordinator 的 `resetAll()`：恢复普通磨皮与美白 A 选择器，
清空所有美肤 desired value，并把美型、微整形 desired value 归零，再按当前总开关状态
应用；宿主不得另行维护一份 44 项重置清单。

## 验收

每个请求项验证 0、默认、明显强度，开关/重置、恢复和失败降级。高级磨皮 4 还要覆盖高性能设备显示与非高性能设备隐藏，不能在未验证设备上宣称可用。
