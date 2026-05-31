# Native Feishu Bridge（飞书原生桥）

[English](./README.md)

一个完全透明的 Python 飞书桥，连接飞书/Lark 与 Claude Code。支持文字与语音消息、实时进度卡片、跨重启对话记忆。源码约 1,150 行，无框架、无编译、全透明。

## 功能

- **文字 & 语音** — 支持文字和语音消息；语音自动转写（Whisper）并润色（DeepSeek）
- **实时进度** — 交互卡片实时显示 Claude 思考 → 调用工具 → 回复中（🧠→🔧→✍️）
- **结构化排版** — 带表格或多级标题的回复自动转为排版卡片（蓝色标题、分隔线、脚注）
- **会话连续性** — 对话历史在桥重启后依然保留（通过 history.json 注入上下文）
- **抢占机制** — 新消息自动中断旧的 Claude 运行，不会排队积压
- **消息合并** — 快速连续发送的消息在 800ms 窗口内自动合并

## 前置条件

| 条件 | 获取方式 |
|---|---|
| **Python 3.9+** | `python3 --version`（macOS 自带） |
| **Claude Code CLI** | `npm install -g @anthropic-ai/claude-code` |
| **飞书应用**（免费） | 在 [open.feishu.cn](https://open.feishu.cn) 创建自建应用 — 见下文 |
| **DeepSeek API Key**（可选） | [platform.deepseek.com](https://platform.deepseek.com) — 语音润色用 |
| **macOS / Linux** | Windows 可运行但 launchd 自动启动仅支持 macOS |

## 快速开始

### 1. 下载 & 安装依赖

```bash
git clone https://github.com/ssrnone954-lang/Native-Feishu-Bridge.git
cd Native-Feishu-Bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. 创建飞书应用

1. 打开 [飞书开放平台](https://open.feishu.cn) → **创建自建应用**
2. 进入 **凭证与基础信息** → 复制 `App ID` 和 `App Secret`
3. 进入 **权限管理** → 添加以下权限：
   - `im:message` — 读写消息
   - `im:message:send_as_bot` — 以机器人身份发消息
   - `im:resource` — 下载媒体文件（语音用）
4. 进入 **事件订阅** → 开启，订阅 `im.message.receive_v1`
5. 进入 **应用发布** → **创建版本** → **发布**（仅需你自己审批）

### 3. 配置

```bash
cp config.yaml.example config.yaml
nano config.yaml  # 或使用任何文本编辑器
```

至少需要填写：
- `feishu.app_id` 和 `feishu.app_secret`（第 2 步获取的）
- `voice.summarize.api_key`（DeepSeek 的，或直接设 `voice.enabled: false` 关闭语音）

### 4. 运行

```bash
./bin/fq start     # 后台启动
./bin/fq status    # 查看运行状态
./bin/fq logs      # 查看日志
```

给你的飞书应用发一条消息 —— Claude 应该会回复。

### 开机自启（macOS）

```bash
./bin/fq load      # 加载 launchd，开机自动启动
./bin/fq unload    # 取消自启
```

## 配置参考

详见 [`config.yaml.example`](config.yaml.example)，每个字段都有中文注释。

主要配置项：

| 配置块 | 是否必填 | 说明 |
|---|---|---|
| `feishu` | **必填** | 飞书开放平台的应用凭证 |
| `claude` | **必填** | Claude CLI 路径 |
| `voice` | 可选 | 语音转写（Whisper）+ 润色（DeepSeek） |
| `messages` | 可选 | 去抖时间、回复格式偏好 |
| `paths` | 可选 | 会话/日志/媒体文件存放位置 |

## 数据目录

| 路径 | 默认值 | 说明 |
|---|---|---|
| `paths.sessions_dir` | `~/native-bridge-sessions/` | 会话 ID + 对话历史 |
| `paths.logs_dir` | `~/native-bridge-logs/` | 运行日志 |
| `messages.download_dir` | `~/native-bridge-media/` | 语音 OGG 文件（临时） |
| `paths.pid_file` | `/tmp/native-feishu-bridge.pid` | 进程 PID 文件 |

以下文件**不在 Git 仓库中**（已被 `.gitignore` 排除）：

| 文件 | 说明 |
|---|---|
| `config.yaml` | 含你的真实密钥，不会被提交 |
| `sessions/*.json` | 含聊天记录和 ID |
| `logs/*.log` | 运行时日志 |

## 架构

```
飞书 WS 事件 (im.message.receive_v1)
  │
  ▼
intake.py          ← 消息队列（800ms 去抖）、抢占、并发控制
  │
  ├─ 文字 → 直接进队列
  └─ 语音 → 下载 OGG → Whisper (1-2s) → DeepSeek 润色 (5-10s) → 进队列
  │
  ▼
claude_runner.py   ← 构建 prompt（含对话历史）、启动 Claude CLI
  │
  ▼
bridge.py          ← 解析 stream-json、状态卡片更新（🧠→🔧→✍️）
  │
  ▼
sender.py          ← 发送回复：简单内容用 post，结构化内容用交互卡片
```

### 模块（共约 1,150 行）

| 文件 | 行数 | 职责 |
|---|---|---|
| `bridge.py` | ~310 | 主控：WS 连接、生命周期、状态卡片、回复策略 |
| `sender.py` | ~290 | 飞书发送：text/post/card + rich card 构建 + 媒体下载 |
| `claude_runner.py` | ~85 | Claude 子进程管理 + prompt 构建 + stream-json 解析 |
| `intake.py` | ~85 | 消息队列、去抖、抢占管理 |
| `voice_pipeline.py` | ~100 | Whisper 常驻模型 + DeepSeek 润色 |
| `session_manager.py` | ~85 | 会话 ID + 对话历史持久化 |
| `bin/fq` | ~140 | CLI：start/stop/restart/status/logs/test/load/unload |

## CLI 参考

```bash
fq start              # 后台启动
fq stop               # 停止
fq restart            # 重启
fq status             # 查看运行状态（进程、会话数、历史、launchd 状态）
fq logs               # 实时日志
fq test "hello"       # 一次性测试（不需要飞书）
fq load               # 安装 launchd（开机自启）
fq unload             # 卸载 launchd
```

## FAQ & 已知问题

### 常见问题

**Q: 发消息后机器人不回复？**
A: 先用 `fq status` 确认桥在运行。如果停止了，用 `fq start` 启动。如果正在运行，查看 `fq logs` 是否有错误信息。

**Q: 语音转写失败了？**
A: 检查：1) 说话时长是否超过 2 秒（太短会失败）；2) 网络是否能访问 DeepSeek API；3) 如果不想用语音，在 config.yaml 中设 `voice.enabled: false`。

**Q: 怎么更新到最新版本？**
A: `git pull` 拉取最新代码，然后 `./bin/fq restart` 重启桥。

**Q: 可以在多台电脑上同时运行吗？**
A: 不能。飞书同一 App 只允许一个 WebSocket 连接。多台电脑需要创建多个飞书应用。

**Q: 支持 Lark（国际版）吗？**
A: 支持。在 config.yaml 中将 `feishu.domain` 改为 `lark`。

### 已知限制

| 问题 | 原因 | 缓解措施 |
|---|---|---|
| 移动端表格无法横向滚动 | 飞书消息 API 不支持原生表格组件 | 控制表格列数 ≤3 |
| 卡片标题无字号层级 | 飞书卡片 markdown 将 `##` 渲染为加粗而非 h2 | 标题密集的内容用 post 模式 |
| 语音识别对技术术语不够准 | Whisper `base` 模型（~140MB）精度有限 | 换成 `small` 模型（~1.2GB） |
| 语音润色延迟 5-10 秒 | DeepSeek API 网络往返 | 关闭 `voice.summarize.enabled` |
| 极短语音（<2 秒）可能转写失败 | 音频数据不足 | 至少说 3 秒 |
| 桥重启后第一条消息是冷启动 | `--resume` 失效 | history.json 提供最近上下文，第二条起恢复正常 |

## 灵感来源

本项目借鉴了三个优秀项目的思路：

- [lark-channel-bridge](https://github.com/zarazhangrui/feishu-claude-code-bridge) — WebSocket 连接模型、消息队列、去抖机制
- **Hermes** — 网关架构、会话管理、每聊独立进程隔离
- **OpenClaw** — 富文本卡片排版方向（混合内容在同一张卡片中呈现）

## 许可证

MIT — 详见 [LICENSE](LICENSE)。

---

Built with [Claude Code](https://claude.ai/claude-code).
