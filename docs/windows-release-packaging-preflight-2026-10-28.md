# Windows packaging / signing / update preflight（2026-10-28）

状态：**设计 + 只读 preflight 已落地；未签名、未打包、未发布**。
适用范围：Browser CLI Windows `out/WinRelease`（non-component release-shape）及其后续
正式产物。实现：`scripts/preflight-windows-release.sh` +
`tests/windows-release/harness/artifact-preflight.ps1`。

本文件的核心目的只有一个：**把“内部未签名测试构建”和“可公开分发的签名产物”分成两个
不可混淆的类别**。任何“看起来像发布”的描述都必须先通过 preflight 的 `public` 模式，
而该模式在当前仓库必然失败（缺 Windows 发布者身份与更新契约），这是设计上的 fail closed，
不是待修 bug。

---

## 1. 两个产物类别

| 类别 | 判定 | 当前仓库能否产出 | 允许的说法 |
| --- | --- | --- | --- |
| `internal-unsigned-test-build` | `is_official_build=false`、`chrome.exe`/`chrome.dll` `NotSigned`、无 updater | **能**（`out/WinRelease`） | “内部未签名测试构建 / release-shape 验证构建” |
| `public-signed-artifact` | official 构建 + 有效 Authenticode（发布者匹配）+ 完整 SHA-256 清单 + `update-metadata.json` | **不能**（缺发布者身份、official 流水线、更新器） | 只有全部 preflight 通过后才能说“已签名发布产物” |

`scripts/preflight-windows-release.sh --mode internal` 对未签名构建返回 **exit 0**，但报告
`artifactClass=internal-unsigned-test-build`、`releasable=false` 并列出全部 release
blocker。`--mode public` 在缺任何一项时返回 **exit 1**；它**不会**尝试补签或生成密钥。

---

## 2. preflight 检查项（只读）

`tests/windows-release/harness/artifact-preflight.ps1` 对 artifact 目录执行：

1. **构建形态**：读取 `args.gn`，断言 `is_component_build=false`、
   `disable_fieldtrial_testing_config=true`；记录 `is_official_build`（当前为 false）。
2. **版本资源**：`chrome.exe` 的 `ProductName`（Browser CLI）、`FileVersion` 与
   `chromium/VERSION` 的 `version=` 一致。
3. **Authenticode**：`chrome.exe`、`chrome.dll` 的 `Get-AuthenticodeSignature` 状态与
   签名者 subject；public 模式要求 `Valid` 且 subject 含 `--expected-publisher`。
   internal 模式接受 `NotSigned`，并把“未签名”写入 blockers。
4. **私钥/证书材料**：拒绝 artifact 内出现 `*.pfx/*.p12/*.pem/*.key/*.pvk/*.spc/*.cer/*.crt`。
5. **更新/安装器契约**：public 模式要求 `update-metadata.json`；记录是否存在
   `chrome_updater.exe`/`updater.exe`/`setup.exe`/`chrome_installer.exe`。
6. **SHA-256 清单**：对 artifact 内全部 `.exe/.dll` 计算 `file/bytes/sha256`。
7. **PE 加固位**：`chrome.exe` 的 `DYNAMICBASE`、`HIGH_ENTROPY_VA`、`NX_COMPAT`、
   `GUARD_CF`、`FORCE_INTEGRITY`。
8. **结论**：`artifactClass`、`releasable`、`failCount`、`releaseBlockers`。

脚本不启动浏览器、不调用 `signtool`、不写证书存储、不联网、不改 artifact。

用法：

```bash
bash scripts/preflight-windows-release.sh \
  --checkout D:/c/src --artifact D:/c/src/out/WinRelease \
  --mode internal --out-json D:/c/logs/winrelease/preflight.json
```

---

## 3. 正式签名产物的最低要求（当前全部未满足）

### 3.1 构建来源

- official Builder（PGO + official optimization + orderfile），`is_official_build=true`；
  当前 `out/WinRelease` 是 `is_official_build=false`，不能作为公开产物。
- checkout HEAD == `chromium/VERSION`，patch 系列来自 `chromium/patch-manifest.json`，
  生成 `build-provenance.json`（revision、patch SHA-256、GN args、编译器版本、构建时间）。
- non-component 形态必须保持：pre-start CIG 依赖 `!COMPONENT_BUILD`。
- `disable_fieldtrial_testing_config=true` 必须保持；禁止 `BrowserDynamicCodeDisabled`
  （Sogou TSF/Xbyak 会 `ExitProcess(3)`）。

### 3.2 签名

- Windows Authenticode 证书：EV 或 OV；SHA-256；RFC3161 时间戳。
- 签名对象：`chrome.exe`、`chrome.dll`，以及安装目录内所有随包 `.exe/.dll`；安装器
  单独签名。逐文件 `signtool verify /pa /all` 通过。
- 私钥只在 HSM/硬件令牌/受控签名服务中；仓库与构建机不保存私钥；preflight 拒绝
  artifact 内任何 key/cert 文件。
- 发布者身份必须成为**唯一配置源**（`config/product-identity.env` 现在只有 macOS
  Team ID/codesign SHA-1，没有 Windows 发布者），并且 preflight 的
  `--expected-publisher` 从该源读取；当前为空 → public 模式 fail closed。
- 禁止 ad-hoc/自签名作为发布；禁止把测试 mock keychain 或任何测试签名配置带入产品。

### 3.3 打包

- 由构建产物生成**确定性文件清单**：文件路径、大小、SHA-256、版本资源；安装器/压缩包
  的清单哈希写入更新元数据。
- 安装目录边界：只包含运行时资源（locales、icudtl.dat、v8 snapshot、pak、icons 等），
  不包含 PDB、obj、日志、测试 profile、密钥材料。
- 安装/卸载：每用户安装路径、版本化目录、原子切换；不写系统级 PATH；不注册全局
  `--no-sandbox` 或任何降低安全性的开关。

### 3.4 更新

公开产物必须有更新契约（当前无实现，属于 release blocker）：

```json
{
  "channel": "stable",
  "version": "156.0.8067.0",
  "minSupportedVersion": "156.0.8000.0",
  "artifact": { "file": "browser-cli-<ver>-x64.zip", "sha256": "..." },
  "manifestSha256": "...",
  "signature": "<detached signature over the manifest>",
  "rollbackVersion": "<previous version>",
  "rollout": { "percent": 100 }
}
```

要求：TLS 传输；更新包验签 + 哈希校验；anti-downgrade；原子替换与失败回滚；
更新器自身签名；崩溃安全（更新中断不破坏现有安装）；版本/通道与二进制版本资源一致。
`scripts/preflight-windows-release.sh --mode public` 目前会因缺 `update-metadata.json`
直接失败。

### 3.5 信任与运行环境

- SmartScreen/EDR 信誉需要时间积累；EV 证书/MS 信誉与签名一致性是发布前置。
- 企业 AppLocker/WDAC 允许列表以发布者签名匹配为前提；未签名构建不能作为该证据。
- 逐模块签名：non-component 构建的 browser/renderer/GPU/network 只应加载
  Microsoft 签名模块 + allowlist 的 `chrome.dll`/`chrome_elf.dll`；正式产物需要逐模块
  清单与签名验证结果（本轮只做运行时模块面统计，见构建/安全文档）。

---

## 4. 当前明确不能做的事

- 不生成、不导入、不导出、不删除任何证书或私钥；不运行 `signtool sign`；
- 不把 `out/WinRelease` 描述为“已签名”“可发布”“release ready”；
- 不用 ad-hoc/自签名产物替代正式签名；
- 不在缺少 `update-metadata.json` 时声称更新链路可用；
- 不把内部 unsigned 构建的 process mitigation 数据当成已签名产物的安全等价证明。
