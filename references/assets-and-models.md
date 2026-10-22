# 模型与素材

本文件是模型类型、素材包、安装、UUID/元数据校验和升级的唯一实现来源；是否阻塞由 [prerequisites.md](prerequisites.md) 决定。

## 目录

- [授权与路径](#授权与路径)
- [模型能力矩阵](#模型能力矩阵)
- [素材包类型](#素材包类型)
- [安装与升级](#安装与升级)
- [元数据](#元数据)
- [安全与分发](#安全与分发)

## 授权与路径

SDK 初始化 lic 与素材包 lic 是不同概念：

- SDK lic 传给 `NveEffectKit.init(context, licensePath)`，通常与 applicationId 绑定。
- 素材 lic 传给素材安装 API；有些授权素材需要，有些内置/无独立授权素材允许空路径。
- `assets:/path/file` 表示 APK assets 内路径。
- 本地绝对路径用于下载或解压后的文件；由客户应用自己的存储策略生成，不写死开发机目录。

不要读取、展示、记录或提交 lic 内容。错误日志只记录授权类型、调用阶段和返回码。

## 模型能力矩阵

| `NveDetectionModelType` | 能力 | 备注 |
| --- | --- | --- |
| `FACE` | 基础人脸检测、人脸道具 | 240 点模型效果优于较低点位；低点位模型可能不支持美妆 |
| `FAKE_FACE` | 假脸检测 | 部分人脸道具需要 |
| `MAKEUP2` | 旧版美妆模型 | SDK 3.13.x 起废弃，不用于新接入 |
| `FACE_COMMON` | 人脸通用 | 美颜、美妆；最低支持基线为 3.13.x |
| `ADVANCED_BEAUTY` | 高级美颜 | 美颜、妆容；最低支持基线为 3.13.x |
| `EYEBALL` | 眼球检测 | 部分道具需要 |
| `SEGMENTATION` | 人像分割 | 背景分割类效果需要 |
| `AVATAR` | 表情驱动 Avatar | 特定 AR/Avatar 道具需要 |

模型文件名和版本随 SDK 交付变化。只能使用客户 SDK 包内配套文件和真实路径。只有 AAR
SHA-256 命中登记基线时，才使用 `feature_contract.py` 从
`assets/baseline-3.16.1/catalog/feature-catalog.json` 读取精确模型文件、顺序和素材映射。
catalog 中的文件名只用于识别登记素材，不是合法素材白名单；非基线不得复制为默认值。

按目标功能选择最小模型集合时读取 [prerequisites.md](prerequisites.md)；本文件只维护模型类型、版本和安装技术细节。每个 `initModel` 单独检查结果，不要把一个布尔值代表所有模型；失败时只禁用依赖该模型的功能。登记 3.16.1 美颜必须先 `FACE`，再
`FACE_COMMON`，最后按功能追加 `ADVANCED_BEAUTY`；文件存在但类型传错、顺序错误或返回值
未进入该功能 readiness，一律视为未加载。

## 素材包类型

| 扩展名 | `NvsAssetPackageManager` 类型 | 用途 |
| --- | --- | --- |
| `.videofx` | `ASSET_PACKAGE_TYPE_VIDEOFX` | 包滤镜、部分妆容组成项 |
| `.facemesh` | `ASSET_PACKAGE_TYPE_FACE_MESH` | 美型、微整形、部分妆容 |
| `.warp` | `ASSET_PACKAGE_TYPE_WARP` | 形变美型、部分妆容组成项 |
| `.makeup` | `ASSET_PACKAGE_TYPE_MAKEUP` | 单妆/妆容组成项 |
| `.arscene` | `ASSET_PACKAGE_TYPE_ARSCENE` | 人脸道具 |

不要只靠文件扩展名决定业务枚举；扩展名决定安装类型，素材元数据决定它对应哪个美型/美妆参数。
自制或更新素材必须通过可信元数据或显式配置建立 `leaf feature → 素材路径` 映射；禁止
根据文件名相似度、显示名或 UUID 猜业务枚举。

## 安装与升级

登记 SDK 3.16.1 基线中，先在素材工作线程完成文件 IO、JSON、路径和
元数据校验，再把纯安装参数提交到 Android 主线程调用安装 API。不得从 `Executor` worker、
`Dispatchers.IO` 或 `Thread` 直接调用 `NveEffectKit.getInstance()`、
`installFxPackage`、`installAssetPackage` 或升级 API；该 AAR 会触发 Main Thread Checker。
非登记 AAR 必须按其 API、正式配套资料和运行时证据重新确认线程，不能沿用此版本事实。

```java
Handler mainHandler = new Handler(Looper.getMainLooper());
Executor mainThreadExecutor = command -> mainHandler.post(command);
mainThreadExecutor.execute(() ->
        NveAssetInstaller.install(packagePath, packageLicensePath, packageType));
```

```java
String packageId = NveAssetPackageManagerUtil.installFxPackage(
        packagePath,
        packageLicensePath,
        packageType
);
```

安装流程必须处理：

1. 路径不存在或扩展名不支持。
2. lic 缺失或授权失败。
3. 首次安装成功。
4. 已安装同版本，可复用 package ID。
5. 文件版本高于已安装版本，需要升级。
6. 安装失败、返回空 ID、ID 格式非法或返回全零 UUID 哨兵，禁止继续创建效果。

安装成功的业务判定不能只写成“返回字符串非空”。当前基线中，安装失败可能返回
`00000000-0000-0000-0000-000000000000`；它是失败哨兵，不是可用 package ID。若安装 API
还返回状态码，同时记录不含授权信息的状态码用于诊断，但最终只把通过完整校验的安装返回 ID
写入效果对象：

```java
private static boolean isUsablePackageId(String packageId) {
    if (TextUtils.isEmpty(packageId)) {
        return false;
    }
    try {
        UUID uuid = UUID.fromString(packageId);
        return uuid.getMostSignificantBits() != 0L
                || uuid.getLeastSignificantBits() != 0L;
    } catch (IllegalArgumentException error) {
        return false;
    }
}
```

对单妆 `.makeup` 资源额外执行以下门禁：

- 保留供应方交付包的原始文件名和相对目录，包括文件名中的 UUID 与版本段；复制到 assets、私有目录或下载缓存时也不能改成 `lip.makeup`、`eyebrow.makeup` 等业务别名。
- UI 显示名、业务类型和资源路径使用配置映射，禁止通过重命名二进制素材表达业务语义。
- 将 `info.json`/单妆元数据 `effectContent.makeupArgs[].uuid` 与实际 `.makeup` 文件名中的 UUID 交叉检查；不匹配时视为资源准备失败，而不是继续尝试渲染。
- 当前兼容性基线在 `NveEffectKit.initialize()` 完成后安装单妆包。目标 AAR 如需不同阶段，必须由其 API、正式配套资料或运行时证据重新证明；安装不能放在每帧、滑块或重复点击回调中。
- 只有全部门禁通过后才设置 `NveSingleMakeUp.packageID` 和 `available=true`；否则禁用对应单妆入口并保留原始预览。

安装可在资源准备阶段完成，不要在每一帧或每次 SeekBar 变化时调用。登记 3.16.1 在主线程
串行安装并对同一素材去重，避免包管理器状态竞争；耗时文件准备不能因此回到主线程。

素材从网络下载时：

- 使用应用私有目录和安全文件名，校验下载结果、扩展名、大小及供应方提供的完整性信息。
- 解压时防止路径穿越，不允许归档条目写出目标目录。
- 不在日志打印带签名参数的下载 URL。
- 素材升级后使旧 package ID/缓存失效，再恢复效果状态。

## 元数据

滤镜列表通常需要：显示名、资源文件名、素材 lic、封面、预期 package/effect ID 和适配比例。
道具列表通常需要显示名、封面、`.arscene` 路径和安装 ID；元数据提供名称或封面时必须原样
绑定到对应展示项，不能退化成无法区分的“道具 1/2/3”纯文字列表。只有封面但没有名称时
只展示封面；名称和封面都缺失时才使用稳定的本地化编号文案。封面已声明但不可读时记录
可诊断错误并按双缺失规则降级。展示信息不代替安装返回 ID。
美型通常还需要业务枚举、强度范围和 facemesh/warp 映射。美妆需要解析单妆或妆容
`info.json`，安装其中引用的所有组成包；登记基线单妆包含 `Eyeball`，按素材声明决定是否
追加 `EYEBALL` 模型。单妆分类 `info.json` 中每个展示项的 `cover` 和妆容元数据的
`cover` 都属于展示契约：验证其路径位于已授权 assets 根内、图片可解码，并与 item ID、
正式名称一起传给 UI。封面缺失或不可读不改变安装返回 ID，但必须记录诊断并仅对该条目
使用文字占位，禁止让整个分类退化成纯文字列表。

美妆选定范围后，对用户明确授权的资源根运行
`scripts/inspect_makeup_catalog.py`。目录名不属于美妆契约：脚本按
`displayName/jsonPath` 分类索引和 `uuid/effectContent` 妆容字段结构发现候选；出现零个
或多个候选时，使用 `--single-index`、`--compose-root` 或可重复的
`--compose-metadata` 明确范围。元数据路径若带供应方逻辑前缀且授权根位于该前缀之下，
只能用 `--declared-path-prefix` 精确声明后剥离，禁止按路径后缀猜测。

脚本必须检查分类索引引用、单妆分类/`makeupArgs.type`/`makeupId` 一致性、元数据 UUID
与原始 `.makeup` 文件名、妆容全部组成包以及声明封面。声明封面不存在但目录中有其他
图片时，报告候选和 `requires_confirmation`，由用户明确选择后再修正元数据；不能自动
猜测或静默替换。

把最终 `--json` 输出保存到目标工程或临时目录，并作为
`materialize_baseline_templates.py --makeup-catalog-report` 的输入。报告必须为零
`errors`、零 `warnings`、零 `selection_requests`；目标范围内每个单妆分类和妆容都必须
存在。只允许在用户确认后修正目标侧副本，禁止把某批素材的 UUID、目录名或 `info*.json`
文件名固化为通用规则。

滤镜目录含 `info.json` 时先运行 `scripts/inspect_filter_catalog.py`，由脚本校验展示项、
唯一素材包、封面、素材 lic 声明和未引用包，并生成 `--asset-map`。多滤镜使用两层格式：

```json
{
  "filter-packaged": {
    "packages": [{
      "key": "package-key",
      "asset": "relative/path/package.videofx",
      "license": null,
      "license_basis": "user-confirmed-no-separate-license"
    }],
    "display_items": [
      {"id": "item-a", "name": "小猫咪", "package": "package-key"},
      {"id": "item-b", "name": "切尔西", "package": "package-key"}
    ]
  }
}
```

路径相对目标工程或某个已授权 `--resource-path`。用户明确确认无需独立素材 lic 时写
`"license": null, "license_basis": "user-confirmed-no-separate-license"`；正式交付确认时
使用 `delivery-confirmed-no-separate-license`。不得因目录里没有 lic 自行推断。同一
`.videofx` 可被多个展示项引用，但 packages 中只能声明一次；展示项保留各自名称、封面和
顺序。妆容元数据中的 filter 组成包只属于该妆容，除非独立滤镜目录和正式交付另行确认，
不得加入滤镜列表。旧扁平列表继续兼容，但不能表达共享包关系，新增接入不得优先使用。

处理元数据时：

- 使用显式数据类和字段校验，不把 JSON 显示名直接传给 SDK 枚举。
- 缺字段时跳过单个素材并给出可诊断错误，不让整个列表崩溃。
- 校验强度范围和颜色分量。
- 安装返回且通过非空、格式、全零哨兵校验的 ID 才是运行时事实；元数据中的 UUID 只能用于识别、查找和核对原始文件名，不能直接写入效果对象。
- 妆容引用多个包时，全部安装成功后再原子切换到新妆容。
- 美型/微整形自制素材缺少可识别元数据时，使用盘点脚本 `--asset-map` 接受一个 JSON
  文件映射。路径只能引用目标工程或明确授权资源目录；同名多文件必须写唯一相对路径。

## 安全与分发

- skill 不携带 AAR、SDK lic、素材 lic、模型、滤镜、道具、美妆包或商业封面。
- 生成代码时使用配置参数或占位变量，不生成看似可用的假 UUID 和授权文件名。
- 不将客户资源复制到测试夹具、文档或前向测试工程。
- 删除/替换资源前检查是否由其他功能共享；不要为解决冲突批量删除客户资产。
