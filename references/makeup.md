# 美妆：单妆与妆容

只在请求 `makeup-suite`、`makeup-single`、`makeup-compose` 或单妆子项时读取本文件。
未限定的“美妆”先按 `feature_contract.py` 让用户选择完整美妆、仅单妆、仅妆容或具体项；
只有明确的“完整美妆”才展开妆容与九类单妆。

## 目录

- [单妆](#单妆)
- [妆容](#妆容)
- [组合妆与单妆叠加契约](#组合妆与单妆叠加契约)
- [默认模板与回归](#默认模板与回归)
- [异步与验收](#异步与验收)

## 单妆

先完成 `initialize()`，再在资源工作线程准备供应方原始路径/文件名的 `.makeup`；登记
3.16.1 把准备完成的参数提交到 Android 主线程安装。每次安装都在同一处理区域验证返回 ID
非空、UUID 格式有效、不是全零 UUID；失败项保持不可用。

```java
NveMakeup makeup = new NveMakeup();
makeup.setEnable(true);
NveEffectKit.getInstance().setMakeup(makeup);

String packageId = NveAssetPackageManagerUtil.installFxPackage(
        originalPackagePath, packageLicensePath,
        NvsAssetPackageManager.ASSET_PACKAGE_TYPE_MAKEUP);
if (singleMakeup != null && isUsablePackageId(packageId)) {
    singleMakeup.setPackageID(packageId);
    singleMakeups.put(NveMakeupTypeEnum.Lip, singleMakeup);
}

makeup.addSingleMakeUp(NveMakeupTypeEnum.Lip, singleMakeups.get(NveMakeupTypeEnum.Lip));
makeup.removeSingleMakeUp(NveMakeupTypeEnum.Lip);
```

支持登记项：`Lip`、`Eyebrow`、`Eyeshadow`、`Eyelash`、`Eyeliner`、`Blusher`、
`Shadow`、`Brighten`、`Eyeball`。同类型只保留一个实例。`Eyeball` 按素材声明和目标
AAR 决定是否追加 `EYEBALL` 模型；完整美妆必须覆盖该分类，不能因旧路由遗漏而跳过。
单妆默认是点击选择/再次点击移除，不要求强度滑块；只有客户明确要求且目标 AAR 已证明
对应 setter 对素材有效时才增加强度或颜色控件。
单妆分类元数据声明 `cover` 时，展示项必须绑定该封面和正式名称；资源线程完成路径校验与
缩略解码后再交给 UI。已有封面不得只显示文字，封面失败只降级当前条目。

## 妆容

妆容可能组合单妆、美颜、美型、微整形和滤镜。应用前保存冲突状态，移除后按产品定义恢复用户此前的独立状态。

```java
makeup.useComposeMakeup(composeMakeup);
makeup.removeComposeMakeUp();
```

资料存在 `setComposeMakeup` 与 `useComposeMakeup` 两种表述；当前示例源码使用 `useComposeMakeup`。以客户 AAR 编译期 API 为准，不用反射掩盖版本差异。

解析妆容时遍历 `.makeup`、`.facemesh`、`.warp`、`.videofx`，保留供应方目录结构并安装
全部引用包；全部成功后才原子切换。命中登记基线时使用
`features/NveMakeupAssetRepository.java.template` 编排安装，使用
`features/NveMakeupCoordinator.java.template` 的状态快照/恢复骨架，不能只调用
`useComposeMakeup` 而丢失用户独立美颜或滤镜状态。资源仓库只发布最新 generation 的
结果；UI 可展示该结果的失败原因，但只有 `Result.isComplete()` 才能开放并应用素材。
旧 generation 的完成回调不得改变可选项或当前效果。
妆容元数据声明封面时同样按稳定妆容 item ID 绑定，不能用某个组成单妆的封面代替。

### 组合妆与单妆叠加契约

- 把妆容视为基础层。选择某个单妆时只覆盖对应 `NveMakeupTypeEnum`，不得清空其他分类。
- SDK 若不能直接在妆容上覆盖单类，且解析结果已证明其美妆部分可由登记单妆完整重建，
  先把妆容拆成单妆并记录每类来源，再替换目标分类。妆容还包含美颜、美型、微整形或滤镜时，
  必须同时明确这些非单妆组成项的保留/恢复策略，不得假设九类单妆能完整重建整个妆容。
- 拆分是执行细节，不代表妆容已取消：仍有来源于妆容的组成项时，妆容入口保持选中。
- 单妆分类不得生成或展示虚拟“组合妆”选项；只显示“无”和该分类真实素材。
- 单妆分类的“无”只移除当前类型。妆容分类的“无”移除仍来源于妆容的组成项，但保留用户
  后续显式选择的单妆。
- 再次点击已选妆容与点击妆容分类的“无”语义相同。所有九类都已被显式覆盖或移除后，
  妆容选中态自动取消。
- UI 的妆容选中态与 SDK 当前是否仍持有 `NveComposeMakeup` 对象分开建模；禁止用
  `desiredCompose == null` 直接推导妆容 UI 为“无”。
- 妆容分类可登记多个真实组合妆条目，不能把实现写死成单个 `Compose` 对象。切换组合妆
  先结束旧基础层事务，再建立新事务；用户在选择妆容之前的显式单妆要保留为可恢复状态。
- UI 选择状态使用稳定 catalog ID 快照；旋转、Activity/Fragment 重建或 GLContext 恢复时，
  先恢复 `NveMakeupSelectionState`，再用已安装 ID 重建 SDK 对象并重放 coordinator 的
  desired state。不能从 SDK 当前对象反推 UI。

回归路径至少覆盖：妆容 → 切换单妆分类 → 选择一个单妆 → 妆容仍选中 → 妆容“无” →
仅保留刚才显式选择的单妆。

## 默认模板与回归

命中登记基线时，美妆至少落地 `NveMakeupSelectionState`、`NveMakeupCoordinator` 和
`NveMakeupAssetRepository`。有 Java 单测目录时同时落地
`NveMakeupSelectionStateTest`；不得删掉其中的组合妆叠加、分类“无”、妆容“无”、
切换组合妆和状态恢复场景。同时落地 `NveMakeupAssetRepositoryTest`，锁定旧 generation
不得发布和非法/全零 UUID 不得开放。宿主使用 Kotlin/Compose 时可以改写表示层，但必须
保留同一状态转移和回归断言。

使用 `--ui-profile compact-makeup` 时还会生成 `NveMakeupResourceLoader`。宿主传入
APK assets 中实际的授权根、校验器确定的单妆索引/妆容元数据路径、可选元数据路径前缀、
已明确解析的素材 lic 路径以及本次最终选择的单妆类型/是否包含妆容。loader 不发现或假设
供应方目录名，只读取这些显式路径；不得把交付 UUID、分类条目或封面文件名写死进业务
代码。元数据 UUID 只用于查找和交叉校验原始包，效果对象最终使用素材安装返回且通过校验
的 package ID。

该登记 loader 调用 AAR 的 `ParseJsonFile.fromJson`；3.16.1 基线因此要求目标模块
runtime classpath 提供 `com.google.code.gson:gson:2.8.2`。该依赖属于模板契约，不能因
Java 编译已经通过而省略。materializer dry-run 会通过 `required_gradle_dependencies` 和
`missing_gradle_dependencies` 报告，严格校验也必须在发现 loader 调用时检查声明或打包类。

## 异步与验收

解析/准备使用资源 worker，登记 3.16.1 的 SDK 安装使用 Android 主线程；两段都使用
generation token，只有最新选择且全部依赖成功时才在串行控制线程切换。逐项验证：可见、
切换不叠加、移除恢复、安装失败不开放 UI、全零 ID 不生效、生命周期恢复。单妆无强度
需求时不得因缺少滑块证据阻断。
