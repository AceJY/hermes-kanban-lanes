# Hermes Kanban Lanes

[English](README.md) · **中文**

[![CI](https://github.com/AceJY/hermes-kanban-lanes/actions/workflows/ci.yml/badge.svg)](https://github.com/AceJY/hermes-kanban-lanes/actions/workflows/ci.yml)

**用 Hermes Kanban 驱动 Claude Code、Codex 和 DeepSeek Harness 干活——带验证门、可复核证据、评审/返工与可选合并闭环。**

Hermes Kanban Lanes（HKL）把一张 Hermes Kanban 看板变成编码 Agent 的控制面。卡上声明任务和允许改动的文件；HKL 领取后创建隔离的 git worktree，启动配置的执行体（`claude`、`codex` 或 `dsh`），对 diff 跑五项验证门，产出可复核的证据清单，再把卡推进到评审、返工或合并。看板始终是事实来源，执行体只负责改代码。

```text
Hermes Kanban 看板
        │  卡：lane + development 契约
        ▼
HKL daemon ── 领取 ──► 隔离 git worktree
        │                       │
        │                       ▼
        │            Claude Code / Codex / DSH
        │                       │
        ▼                       ▼
   门 1：范围 · 测试 · 构建 · 产物
        │
        ├─ 失败 ──► 返工卡（有轮次上限）
        └─ 通过 ──► 证据清单 ──► 评审 ──► 合并（可选）
```

### 支持的执行体

| 执行体 | 配置键 | 说明 |
|---|---|---|
| **Claude Code** | `claude` | Anthropic 编码 CLI；生产环境已验证。 |
| **Codex** | `codex` | OpenAI Codex CLI；适配器有测试覆盖。 |
| **DeepSeek Harness (DSH)** | `dsh` | DSH headless JSON 模式（`dsh --profile headless --json`）；生产环境已验证。 |

**为什么要一条车道，而不是一个 prompt：** 跑执行体是容易的部分。HKL 补的是判断“活是否真的干成了”的部分——文件范围约束、项目自己的测试/构建命令、可复核证据、有限轮次评审/返工、只从已评审 head 合并，以及 Hermes 升级后能重新解析 Python generation、不会静默死掉的常驻服务。

当前版本为 `0.2.0`。只有下面列出的安装形态和平台承诺是已声明的；其余内容一律视为未验证。

## 能力边界

- 它**不是 Hermes 本体**。它运行在 Hermes 宿主源码检出之上，并使用运行本产品的
  解释器导入宿主的 Kanban 数据库模块。
- 它**不接管、不代理、不管理 Hermes Gateway**。它只操作所配置 Hermes 宿主内的
  Kanban 看板。
- 它不是 Web 服务、远程 API 或凭证存储。所有子命令输出都是 JSON（`--version`
  和 `--help` 不输出 JSON）；v0.1 不提供人类可读模式。
- 它不会创建看板、仓库或宿主环境。它只校验并操作你配置好的对象。

## 安装

### 前置条件

- **Python 3.13 或更高版本。**
- **必须安装到与 Hermes 宿主相同的 Python 环境中。** 产品会用运行自身的解释器从
  `hermes_source` 导入宿主模块，因此宿主自身的依赖必须装在该解释器里。即使
  wheel 本身没有声明 Python 依赖，缺少宿主依赖的独立 Python 虚拟环境也不是受支持
  的安装形态。
- 一份位于 `hermes_commit` 所指定提交上的 Hermes 宿主源码检出。
- 一个包含 `tasks` 表的 Kanban 看板 SQLite 数据库。
- 一个供车道工作的可信 git 仓库。
- `executors` 中声明的执行体二进制（`claude`、`codex` 和/或 `dsh`）位于 `PATH` 中，
  或使用绝对路径给出。

平台承诺：

- **macOS launchd**：用户级生命周期（`install`、`status`、`restart`、
  `uninstall`）是本版本声明的服务平台。
- **systemd**：可以渲染单元文件，但 systemd 是**仅渲染**。生命周期命令会以
  fail-closed 方式给出指引并拒绝执行，绝不调用 `systemctl`。
- **Windows**：不支持。

### 安装软件包

项目仓库：<https://github.com/AceJY/hermes-kanban-lanes>。

从 wheel 安装：

```sh
python -m pip install /path/to/hermes_kanban_lanes-0.2.0-py3-none-any.whl
```

从源码检出安装：

```sh
python -m pip wheel . --no-build-isolation -w dist
python -m pip install dist/hermes_kanban_lanes-*.whl
```

确认已安装的分发包：

```sh
hermes-kanban-lanes --version
```

该命令会打印已安装版本（例如 `0.2.0`）并以 `0` 退出。

## 快速开始

1. 将 `examples/config.json` 复制为本地文件，并把每个
   `/absolute/path/to/...` 值替换为真实的本地绝对路径。将 `hermes_commit` 设为
   宿主提交：

   ```sh
   git -C /absolute/path/to/hermes-agent rev-parse HEAD
   ```

2. 校验配置文档：

   ```sh
   hermes-kanban-lanes validate-config /absolute/path/to/config.json
   ```

   成功时打印 `{"valid": true, "schema_version": 1, "kind": "config"}` 并以 `0`
   退出。校验只检查结构、类型和边界，不要求配置中的路径真实存在。

3. 诊断配置好的车道。`doctor` 是只读的：它不会领取卡片、写看板或配置、启动
   守护进程或服务，也不会运行模型。它会启动一个短命的探针解释器来验证宿主导入链。

   ```sh
   hermes-kanban-lanes doctor --config /absolute/path/to/config.json --json
   ```

   绿色报告（`"ok": true`，退出码 `0`）表示所有已配置路径、看板、宿主提交和
   执行体都已解析成功。继续之前请修复每个失败的检查项。

4. 只执行一次 tick：

   ```sh
   hermes-kanban-lanes tick --config /absolute/path/to/config.json --json
   ```

   退出码 `0` 表示 tick 报告没有错误；退出码 `3` 表示 tick 报告了错误，或另一个
   进程持有守护进程锁；退出码 `2` 表示配置或预检在任何领取动作之前失败。

5. 迁移到常驻服务（macOS launchd）。先渲染，再安装并检查状态：

   ```sh
   hermes-kanban-lanes service render --platform launchd \
     --config /absolute/path/to/config.json --out /absolute/path/to/rendered
   hermes-kanban-lanes service install --platform launchd \
     --config /absolute/path/to/config.json
   hermes-kanban-lanes service status --platform launchd \
     --config /absolute/path/to/config.json
   ```

   守护进程始终把 JSON 行写入 `<work_root>/logs/daemon.log`。配置项
   `service.log_root` 决定常驻服务把 stdout/stderr 写到哪里：launchd 使用
   `launchd.out.log` 和 `launchd.err.log`，systemd 渲染使用 `daemon.out.log` 和
   `daemon.err.log`。发生失败时，先读守护进程日志，再重新运行 `doctor`。

## 配置

`config.json` 是严格 JSON。未知键会被拒绝。所有路径值必须是绝对路径；开头的
波浪号和环境变量不会被展开。

| 键 | 必填 | 含义 |
|---|---|---|
| `schema_version` | 是 | 必须为 `1`。 |
| `hermes_source` | 是 | Hermes 宿主源码检出的绝对路径。 |
| `hermes_commit` | 是 | 期望的宿主 `HEAD`（40 或 64 位十六进制字符）。 |
| `boards` | 是 | 看板别名到绝对 SQLite 路径的映射对象。 |
| `repositories` | 是 | 仓库别名到 `{"path": "<absolute>", "trusted": true}` 的映射对象。 |
| `executors` | 是 | 包含 `claude`、`codex` 和/或 `dsh` 的对象，每项为 `{"argv": ["<binary>", ...]}`。 |
| `work_root` | 是 | 用于锁、运行、卡片和日志的绝对可写目录。 |
| `max_concurrent` | 是 | 正整数。 |
| `review` | 否 | 评审人名称、返工上限、授权目录和合并策略。 |
| `service` | 否 | 用户级服务设置，见下文。 |
| `logging` | 否 | 保留与轮转设置，见下文。 |

### `service`

| 键 | 默认值 | 可接受值 |
|---|---|---|
| `platform` | `null`（跳过服务检查） | `"launchd"`、`"systemd"` 或 `null`。 |
| `label` | `"com.hermes-kanban-lanes.daemon"` | 非空字符串。 |
| `log_root` | `<work_root>/logs` | 绝对路径或 `null`。 |
| `restart` | `"on-failure"` | `"on-failure"`、`"always"`、`"never"`。 |

### `logging`

| 键 | 默认值 | 范围 |
|---|---|---|
| `retention_days` | `14` | `>= 1` |
| `max_total_bytes` | `2147483648` | `>= 1048576` |
| `max_log_bytes` | `10485760` | `>= 65536` |
| `backups` | `3` | `>= 1` |
| `keep_failed_runs` | `10` | `>= 0` |

包含全部可选块的完整文档：

```json
{
  "schema_version": 1,
  "hermes_source": "/absolute/path/to/hermes-agent",
  "hermes_commit": "0123456789abcdef0123456789abcdef01234567",
  "boards": {
    "engineering": "/absolute/path/to/kanban/engineering.db"
  },
  "repositories": {
    "primary": {
      "path": "/absolute/path/to/repository",
      "trusted": true
    }
  },
  "executors": {
    "claude": {"argv": ["claude"]},
    "codex": {"argv": ["codex"]},
    "dsh": {"argv": ["dsh"]}
  },
  "work_root": "/absolute/path/to/work",
  "max_concurrent": 2,
  "service": {
    "platform": "launchd",
    "label": "com.hermes-kanban-lanes.daemon",
    "log_root": "/absolute/path/to/work/logs",
    "restart": "on-failure"
  },
  "logging": {
    "retention_days": 14,
    "max_total_bytes": 2147483648,
    "max_log_bytes": 10485760,
    "backups": 3,
    "keep_failed_runs": 10
  }
}
```

上面的所有值都是占位符。请在你自己的文件中替换为真实的绝对路径；
`examples/config.json` 有意使用与机器无关的占位路径。

## 服务

`hermes-kanban-lanes service` 渲染并管理**用户级**服务单元。它从不写入系统级
服务目录。

| 动作 | 命令 | 行为 |
|---|---|---|
| 渲染 | `service render --platform launchd\|systemd --config PATH --out DIR [--wheel WHEEL]` | 把一个单元文件写入 `DIR`；同时在服务元数据目录落盘与 generation 无关的 launcher 和 wheel。 |
| 安装 | `service install --platform … --config PATH [--dry-run] [--wheel WHEEL]` | 落盘 launcher/wheel，安装并加载用户级单元。 |
| 状态 | `service status --platform … --config PATH [--dry-run]` | 只读查询用户级单元状态。 |
| 重启 | `service restart --platform … --config PATH` | 重启已安装的用户级单元。 |
| 卸载 | `service uninstall --platform … --config PATH [--dry-run]` | 卸载 job 并删除单元文件。 |
| 元数据 | — | 卸载后保留供诊断，见下文。 |

- **macOS launchd** 是唯一声明支持生命周期的平台。单元是用户级 plist，通过当前
  用户 GUI 会话中的 `launchctl` 加载，服务以你的用户账户运行。
- **systemd** 单元只用于渲染检查，或供你自己操作的工具使用。`install`、
  `status`、`restart` 和 `uninstall` 会以 `service-platform-render-only`
  fail-closed，绝不执行 `systemctl`。
- **Windows** 不支持；`tick` 和 `run` 使用的锁原语在该平台不可用。
- **与 generation 无关的 launcher**：单元不再固化 generation Python 路径，而是
  启动 `$HOME/.hermes/kanban-lanes-service/runtime-launcher.py`。launcher 每次启动
  都通过宿主 `--print-runtime-command` 重新解析运行时，在该 generation 内探针
  `import hermes_kanban_lanes` 及 dist 版本，通过后才 exec 守护进程。只接受锚定在
  `HERMES_HOME/installs/*/environments/*/venv` 下的 generation
  `site-packages`/`dist-packages`；若解析到 store/tool Python 的 purelib，则
  fail-closed 为 `generation-unanchored:<path>`，绝不向那里安装。记录的
  `hermes_home` 锚点是 Hermes 根：会话 profile 路径
  `<root>/profiles/<name>` 会归一为 `<root>`；若候选根都没有 `installs/`，
  render/install 直接以 `service-hermes-home-unresolved` 失败。若新 generation
  没有插件（`hermes update` 切换 generation 后的常态），或已暂存 wheel 的 sha256
  与上次装入该 generation 的记录不同，launcher 会用随包 wheel 离线执行
  `pip install --no-index --no-deps --force-reinstall --upgrade --target
  <generation site-packages>`；generation 无 pip 时回退到
  `uv pip install --offline --no-deps --reinstall --python <generation python>
  --target <generation site-packages>`，随后再次探针。解析失败、wheel 缺失、安装
  失败或探针仍失败都会以非零码退出，并写 marker；绝不回落到 `sys.executable`
  或旧 generation。
- **服务元数据目录**（`$HOME/.hermes/kanban-lanes-service/`）：`state.json`（render/
  install 写入的状态文件：`product_version`、`hermes_source`/`hermes_launcher`、
  `hermes_home` 锚点、`module` 与 `daemon_argv`、`config_path`、已暂存
  `wheel` 及其 `wheel_sha256`、发现的 `uv`、以及各元数据路径）、
  `runtime-launcher.py`（生成的纯标准库 launcher）、`wheel/`（当前产品 wheel
  副本）、`installed.json`（最近一次成功安装：wheel sha256、时间、generation
  路径）、`launcher.log`（解析/探针/安装记录，含 pip/uv stderr 尾部，不含凭据或
  环境变量转储）和 `launch-failure.json`（任一次 launcher 失败时写入的 JSON
  marker：时间、运行时路径、generation、原因；安装失败时含 `install_error`
  stderr 尾部）。运行时解析失败时 `runtime` 与 `generation` 均为 `null`。wheel
  依次从显式 `--wheel`、环境变量 `HERMES_KANBAN_LANES_WHEEL`/`HKL_WHEEL`、当前
  安装的 `direct_url.json`、已暂存 wheel、本地 `dist/` 目录发现；全程不联网。
- **每次 Hermes 升级后必须运行 `hermes kanban-lanes doctor`**。`plugin-runtime`
  检查必须在新 generation 中 PASS；dist 缺失或版本不符会 FAIL，并打印确切的
  `service install` 命令与当前 generation 路径。若服务启动自愈失败，先读
  `$HOME/.hermes/kanban-lanes-service/launch-failure.json` 中的原因与运行时路径，再
  重新执行 `service install`。
- `uninstall` 只删除单元文件并卸载 job。它不会删除仓库、`work_root` 下的运行
  证据、Kanban 看板数据或服务元数据目录。

## 退出码

所有子命令都输出机器可读的 JSON；`--version` 和 `--help` 不输出 JSON。
`doctor` 和 `service status` 等报告型结果即使
`ok` 为 false 也留在 stdout；另一种错误报告以单个 JSON 对象写到 stderr。v0.1
不提供人类可读模式。

错误 JSON 始终在 `error` 之外同时携带 `reason`、`impact` 和 `next_steps`：

```json
{
  "ok": false,
  "error": "preflight-failed",
  "reason": "host-dependency-missing:ruamel",
  "impact": "tick and run cannot import the Hermes host import chain; the daemon cannot start.",
  "next_steps": [
    "Install hermes-kanban-lanes into the same Python environment as the Hermes host.",
    "Install the missing host dependency, then verify: python -c \"import ruamel\"",
    "Re-run: hermes-kanban-lanes doctor --config <absolute config path> --json"
  ]
}
```

| 退出码 | 含义 |
|---|---|
| `0` | 成功，包括 `run` 收到 SIGTERM/SIGINT 后正常退出，以及配置或卡片合法。 |
| `1` | 内部错误，包括凭证泄漏断言。 |
| `2` | 输入非法、预检失败、平台前置条件不受支持、`doctor` 检查失败，或服务命令失败（包括 `service status` 时其单元未安装）。 |
| `3` | 守护进程锁被占用、tick 报告包含错误、tick 致命失败，或服务前置条件缺失。 |

## 安全

- **不声明同一 UID 下的隔离。** 守护进程及其执行体以你的用户权限运行，可以读取
  你的用户能读取的任何内容。如果需要隔离，请用专用操作系统用户或外部沙箱运行
  该车道。
- **服务单元不写凭证。** 它只携带运行时的 `PATH`；仅当安装时环境中的
  `CODEX_HOME` 存在且为绝对路径（absolute path）时才写入该路径。提供方密钥不会
  被写入单元文件。
- **凭证值不会出现在输出中。** 错误和报告可以指出凭证来源，但不得打印秘密值。
  分享日志和证据前请先审阅。
- **fail-closed 行为是有意为之。** 缺少宿主依赖、启动命令无法解析、平台不受支持
  或检查失败时，命令会停止，而不是静默降级。
- **私下报告漏洞。** 请使用本仓库的 GitHub Security Advisories 页面——见
  [SECURITY.md](SECURITY.md)。不要为漏洞开公开 issue，也不要在报告中包含真实
  凭证。

## 排障

每个错误 JSON 都会给出 `reason`、对应的 `impact` 和 `next_steps`。最常见的三类
情况是：

**配置非法。** `validate-config`、`doctor`、`tick`、`run` 和服务命令都会以
`reason: "invalid-config"` 失败。影响：命令没有启动，看板没有被写入。下一步：
修复 `error` 指明的字段，然后重新运行 `validate-config` 和 `doctor`；上文的配置
表列出了所有可接受的键、默认值和范围。

**缺少 Hermes 宿主依赖。** `doctor` 会在 `host-hermes` 或 `service-runtime`
检查中报告 `host-dependency-missing:<module>`，`tick`/`run` 会在取得守护进程锁
之前返回同一个 `reason`。影响：Hermes 宿主导入链无法加载，因此无法领取任何
任务。下一步：把 Hermes Kanban Lanes 和宿主依赖安装到与 Hermes 宿主相同的
Python 环境，用运行 `hermes-kanban-lanes` 的解释器验证该模块，然后重新运行
`doctor`。

**服务未安装，或平台未声明。** `service status` 和 `service uninstall` 会返回
`reason: "service-not-installed"`，并给出应运行的确切 `service install` 命令。
对 systemd 而言，生命周期命令会返回 `reason: "service-platform-render-only"`
并给出 `service render` 指引；不会执行任何 `systemctl` 命令。下一步：如果只需要
单元内容就使用渲染，否则使用已声明的 macOS launchd 生命周期。

## 开发

在源码检出中运行测试套件：

```sh
python -m pytest tests -q
```

只运行离线包安装测试：

```sh
python -m pytest tests/unit/test_package_install.py -q
```

构建并冒烟测试 wheel：

```sh
python -m pip wheel . --no-build-isolation -w /tmp/hkl-dist
python -m pip install --no-index --no-deps /tmp/hkl-dist/hermes_kanban_lanes-*.whl
hermes-kanban-lanes --version
```

开发流程和提交规范见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

MIT。见 [LICENSE](LICENSE)。
