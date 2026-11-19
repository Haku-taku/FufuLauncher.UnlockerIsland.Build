# FufuLauncher.UnlockerIsland.Build

用于发布 Windows x64 插件的小型公开构建仓库：**[Haku-taku/FufuLauncher.UnlockerIsland.Build](https://github.com/Haku-taku/FufuLauncher.UnlockerIsland.Build)**。
上游源码通过 submodule 引用，本地改动保存为补丁与构建覆盖层；不维护完整源码分叉，**不使用 GitHub Issues 作为更新或失败通知接口**。

## 仓库布局与发布流程

- `upstream/`：`https://github.com/FufuLauncher/FufuLauncher.UnlockerIsland.git`，跟踪 `master`；初始固定提交为 `1830f9d770ecb9c895ba98b9a933973eabe36e7a`。
- `patches/local.patch`：使用 `git diff --binary --full-index` 保存的本地源码改动。
- `overlay/build.sh`、`overlay/sync_config.py`：Linux 交叉编译与原样下载配置的覆盖层。原项目忽略的 `build.sh` 在这里由 `overlay/` 正式跟踪。
- `scripts/pipeline.py`：检查上游、准备源码、构建、打包、发布及状态写入的唯一入口。
- `.github/workflows/build.yml`：每小时第 23 分钟、推送 `main` 或手动触发；共享 `build-release` 并发组，不主动取消正在运行的任务。

流水线从 submodule remote 自动识别上游并抓取最新 `master`。上游 SHA 与 `overlay/`、`patches/`、`scripts/`、工作流的输入指纹和上次成功构建一致时跳过；手动运行时勾选 `force` 可强制重建（传入 `FORCE_BUILD`）。仅配置源远端变化不会自动触发重建，需要强制运行。

需要构建时，在可丢弃的 `.work/source` worktree 中使用 `git apply --3way --index` 应用补丁，再复制覆盖层、运行 `bash build.sh`。每次实际构建重新下载配置到 `dist/config.ini`，不沿用旧的构建产物配置。三方应用冲突、编译失败或配置下载失败都应视为失败，而不是发布不完整产物。

成功产物以**唯一 tag 的 GitHub Release** 发布，包括插件 ZIP、完整的对应已打补丁源码 `tar.gz`、许可证、清单与摘要以及 `update.json`。只有 Release 成功后，流水线才使用 GitHub API 更新 `main` 上的 submodule gitlink。该提交使用内置 `GITHUB_TOKEN`，不会递归触发新的 push 工作流。构建失败时不推进固定版本，并保留上次成功版本供客户端下载。

## 启用 GitHub Actions

1. 使用上述公开仓库，默认分支设为 `main`，在 **Settings → Actions → General** 允许运行所用 Actions。
2. 允许工作流使用 `contents: write`：用于创建 Release、上传资产、写入 `status` 分支及更新 `main` 的 gitlink。无需 Issues 权限，也无需额外 PAT 或仓库 Secret。若分支保护或 ruleset 禁止机器人直接写入，需由维护者调整规则，否则发布后的 gitlink 更新或状态写入可能失败。
3. 工作流使用 `GH_TOKEN: github.token`；不要把令牌写入仓库或分发给桌面客户端。
4. 在 **Actions → Build and release → Run workflow** 首次手动运行；需要重建相同输入时勾选 `force`。发布应从 `main` 运行。

运行环境为 Ubuntu 24.04、LLVM/Clang/LLD 19、runner 自带 Rust 和固定的 `xwin 0.10.0`（`cargo install xwin --version 0.10.0 --locked`）。其中 `clang-cl-19` 由 **`clang-tools-19`** 提供。工作流验证所有工具，并为脚本创建 `llvm-readobj` → `llvm-readobj-19` 链接。不执行 `curl | sh` 等远端安装脚本。

`xwin` 可执行文件和 SDK 分别缓存；SDK key 为 `xwin-0.10.0-ubuntu24-x64-v1`。`XWIN_DIR`、`XWIN_CACHE`、`CARGO_TARGET_DIR` 均位于 runner 临时目录。首次下载 MSVC CRT / Windows SDK 体积较大，需要网络并接受微软相应许可；缓存只是加速，不是源码或发布的权威来源。MinHook 每次在 `.work/source/.build/minhook` 获取源码，不单独缓存。

## 本地复现固定版本

```sh
git clone --recurse-submodules https://github.com/Haku-taku/FufuLauncher.UnlockerIsland.Build.git
cd FufuLauncher.UnlockerIsland.Build
git submodule status --recursive
python3 -m unittest discover -s tests -v
```

Ubuntu 24.04 安装工具（其他 Linux 发行版请安装等效组件）；Rust/Cargo 需预先安装：

```sh
sudo apt-get update
sudo apt-get install --yes build-essential ca-certificates git pkg-config libssl-dev \
  clang-19 clang-tools-19 lld-19 llvm-19 llvm-19-tools
cargo install xwin --version 0.10.0 --locked
export CLANG_CL=clang-cl-19 LLD_LINK=lld-link-19 LLVM_LIB=llvm-lib-19
export LLVM_RC=llvm-rc-19 LLVM_ML=llvm-ml-19
```

让版本化 LLVM 工具目录及 Cargo 位于 PATH 前端（无需修改系统 LLVM 默认版本）：

```sh
export PATH="/usr/lib/llvm-19/bin:$HOME/.cargo/bin:$PATH"
```

准备指定版本并编译：

```sh
python3 scripts/pipeline.py prepare --sha 1830f9d770ecb9c895ba98b9a933973eabe36e7a
bash .work/source/build.sh
```

`prepare` 只从本地已有 Git 对象准备源码，不查询最新版本、不发布 Release；指定 SHA 必须已在 `upstream` 本地对象库中，否则先自行 fetch。要复现其他 Release，应先检出其清单记录的构建仓库提交，再使用清单中的上游 SHA。`.work/source` 是可重建的临时工作区，**不要在其中保存唯一副本的改动**。以上本地编译输出位于 `.work/source/x64/`；它不等同于完整的 Release 打包。

“离线准备”不代表首次编译完全离线：`build.sh` 仍可能下载 SDK 和 MinHook。完全离线编译需预先准备 `XWIN_DIR` 及 `MINHOOK_SRC`。配置工具可用 `python3 overlay/sync_config.py --help` 查看本地模板选项，以 Release 中的配置及其来源记录复现，而不是重新下载可能已经变化的配置。SDK、系统软件包和外部配置的变化意味着这里并不承诺字节级可复现。

完整的自动检查／发布入口是 `python3 scripts/pipeline.py run`，强制入口为 `python3 scripts/pipeline.py run --force`；发布需要有目标仓库写权限的 `GH_TOKEN`，不是普通本地编译的必要步骤。

## 手动更新本地补丁

不要对日常开发目录或 `upstream/` 执行 `reset --hard` / `clean -fdx`。先保存自己的工作，查看 `git status` 与 `git -C upstream status`；以下示例从初始基线在**新的独立 worktree** 中维护补丁（路径已存在则换一个新路径，不要覆盖）：

```sh
mkdir -p .work
git -C upstream worktree add --detach ../.work/patch-edit 1830f9d770ecb9c895ba98b9a933973eabe36e7a
git -C .work/patch-edit apply --3way --index ../../patches/local.patch
# 在 .work/patch-edit 编辑源码；不要把下载缓存或编译产物加入补丁。
git -C .work/patch-edit add --all
git -C .work/patch-edit diff --cached --binary --full-index \
  1830f9d770ecb9c895ba98b9a933973eabe36e7a > patches/local.patch.new
# 审阅新补丁（包括新增/删除文件及二进制变更），确认无误后再替换。
git diff --no-index -- patches/local.patch patches/local.patch.new
```

`git diff --no-index` 检测到差异时退出码为 1，属于正常结果。确认新补丁完整且应用无冲突后执行 `mv patches/local.patch.new patches/local.patch`，再运行测试和 `prepare`／编译验证。若要迁移到新上游，先显式 fetch 并在新的目标 SHA worktree 中三方应用旧补丁、人工解决冲突，再相对该 SHA 导出；失败时不要自动丢弃旧补丁或工作区。构建脚本／配置下载器的改动放在 `overlay/`，不要混入源码补丁。

## 桌面端更新 API

**成功版本以 Releases 为准，状态信息只作补充。** 不读取 Issues，不把一次运行失败视为已经没有可用版本。

```sh
# 首次查询；保存响应头中的 ETag 及响应体。
curl --fail-with-body -D release.headers \
  -H 'Accept: application/vnd.github+json' \
  -H 'X-GitHub-Api-Version: 2022-11-28' \
  https://api.github.com/repos/Haku-taku/FufuLauncher.UnlockerIsland.Build/releases/latest

# 后续使用上次返回的实际 ETag；304 时复用已缓存响应体。
curl -i -H 'Accept: application/vnd.github+json' \
  -H 'If-None-Match: "此前响应中的实际ETag"' \
  https://api.github.com/repos/Haku-taku/FufuLauncher.UnlockerIsland.Build/releases/latest
```

在该响应的 `assets` 中查找 `name == "update.json"`，下载其 `browser_download_url`，读取构建来源 SHA 和资产 SHA-256／大小／下载地址，再校验目标 ZIP。不要硬编码 tag、ZIP 文件名、资产顺序或 GitHub 自动生成的 Source code 包；需要完整对应源码时下载本项目随 Release 上传的源码 `tar.gz`。`update.json` 的摘要用于完整性检查，不等于独立的数字签名；信任根仍是仓库与 GitHub HTTPS。首次成功发布前 `/releases/latest` 的 404 应显示为“尚无可用版本”。

公开查询不需要令牌。GitHub REST 未认证通常限制为每个来源 IP 每小时 60 次，认证请求通常为每小时 5,000 次（以实际响应头、账户类型及 GitHub 当前规则为准）；ETag 不能代替限流策略。缓存成功结果，避免短周期轮询，遵循 `X-RateLimit-*`、`Retry-After`，在 403／429 和网络失败时退避重试。**不要在桌面程序内嵌 PAT 或工作流令牌**。

可选状态端点：

```sh
curl --fail-with-body \
  https://raw.githubusercontent.com/Haku-taku/FufuLauncher.UnlockerIsland.Build/status/status.json
```

`status` 是独立分支，根目录 `status.json` 的字段包括：

| 字段 | 含义 |
| --- | --- |
| `schema_version`、`updated_at` | 状态格式版本与更新时间 |
| `upstream.repository`、`upstream.branch` | 检查的上游仓库和分支 |
| `upstream.pinned_sha`、`upstream.latest_sha` | 已固定的成功版本与此次发现的上游版本 |
| `build.state`、`build.stage` | 本次运行状态与阶段 |
| `build.run_url`、`build.error`、`build.attempt_id` | Actions 详情、错误信息及本次尝试标识 |
| `latest_release` | 最近成功的更新清单；后续失败时保留 |

客户端应容忍状态分支尚未创建、404、缓存延迟、未知 schema 或字段为空。构建中／失败状态仅用于提示，不覆盖 Releases 中最后成功版本。不要以 `updated_at` 推断“调度服务一定仍在运行”。

## 失败通知与日志

- 构建或发布失败让 Actions 作业保持失败，由 **GitHub Actions 通知**处理；不自动创建或更新 Issue。
- 在个人 **Settings → Notifications → System → Actions** 启用所需 Web／邮件通知，并按需选择只通知失败的工作流；确认订阅及邮件设置。GitHub 对收件人的选择与触发者、订阅和当前平台规则有关，不保证所有观察者都会收到每次失败提醒。
- 流水线将构建输出写入 `dist/build.log`，失败后仍保留；工作流始终尝试上传 `dist/` 为 Actions artifact，保留 **14 天**。若依赖安装等前置步骤失败，尚未开始编译便可能没有该文件，此时查看 Actions 步骤日志。
- 进入流水线前的测试／工具安装失败会调用 `report-setup-failure` 尝试写入状态；若 checkout、本身权限或 GitHub API 已不可用，报告也可能失败，Actions 结果仍是权威故障记录。
- GitHub 定时任务不保证准时，繁忙时可能延迟甚至丢弃任务；公开仓库 **60 天无仓库活动可能自动停用定时任务**，需维护者重新启用。取消、超时或平台中断也可能留下陈旧的“构建中”状态。关键使用场景应监控 Actions 并保留手动触发能力，不将此仓库当作有 SLA 的调度服务。

## 许可证与对应源码

本仓库遵循根目录 `LICENSE` 中的 **AGPL-3.0** 条款；上游及第三方代码保留各自版权和许可。每次发布同时提供该二进制对应的**完整已打补丁源码**，包含本地补丁／构建覆盖层所需内容、MinHook 源码与其许可证、项目 `LICENSE`、来源记录及摘要清单，而不仅是指向上游的链接。GitHub 自动生成的本构建仓库源码 ZIP 不能替代插件对应源码包。

分发二进制或再发布修改版本时，请一并履行 AGPL 对应源码与许可告知义务，并遵守第三方许可；微软 SDK 的缓存和工具链不属于可随意再分发的插件源码，不应打入发布包。来源 SHA 与清单便于追溯，不免除许可证义务。
