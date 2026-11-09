# Hermes Kanban Lanes

[English](README.md) · **中文**


Hermes Kanban Lanes 是一个 fail-closed 的命令行工具，用于操作一条 Hermes
Kanban 车道。它的子命令可以校验配置与卡片（`validate-config`、`validate-card`）、
诊断单条车道（`doctor`）、执行一次 tick 或常驻轮询守护进程（`tick`、`run`），
以及渲染和管理用户级服务单元（`service`）。

当前版本为 `0.1.0.dev0`。只有下面列出的安装形态和平台承诺是已声明的；其余内容
一律视为未验证。

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
- `executors` 中声明的执行体二进制（`claude` 和/或 `codex`）位于 `PATH` 中，
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
python -m pip install /path/to/hermes_kanban_lanes-0.1.0.dev0-py3-none-any.whl
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

该命令会打印已安装版本（例如 `0.1.0.dev0`）并以 `0` 退出。

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
| `executors` | 是 | 包含 `claude` 和/或 `codex` 的对象，每项为 `{"argv": ["<binary>", ...]}`。 |
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
    "codex": {"argv": ["codex"]}
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
| 渲染 | `service render --platform launchd\|systemd --config PATH --out DIR` | 只把一个单元文件写入 `DIR`，不做其他事。 |
| 安装 | `service install --platform … --config PATH [--dry-run]` | 安装并加载用户级单元。 |
| 状态 | `service status --platform … --config PATH [--dry-run]` | 只读查询用户级单元状态。 |
| 重启 | `service restart --platform … --config PATH` | 重启已安装的用户级单元。 |
| 卸载 | `service uninstall --platform … --config PATH [--dry-run]` | 卸载 job 并删除单元文件。 |

- **macOS launchd** 是唯一声明支持生命周期的平台。单元是用户级 plist，通过当前
  用户 GUI 会话中的 `launchctl` 加载，服务以你的用户账户运行。
- **systemd** 单元只用于渲染检查，或供你自己操作的工具使用。`install`、
  `status`、`restart` 和 `uninstall` 会以 `service-platform-render-only`
  fail-closed，绝不执行 `systemctl`。
- **Windows** 不支持；`tick` 和 `run` 使用的锁原语在该平台不可用。
- 单元使用与 Hermes 安装绑定的启动命令，通过宿主的 `--print-runtime-command`
  解析。若解析失败，命令会 fail-closed，而不会回落到当前解释器。
- `uninstall` 只删除单元文件并卸载 job。它不会删除仓库、`work_root` 下的运行
  证据或 Kanban 看板数据。

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
