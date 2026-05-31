# 02 — 架构设计

## 系统概览

```
┌─────────────────────────────────────────────────────────┐
│                      飞书客户端                          │
│                 (发起文字/语音/图片消息)                    │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│              飞书开放平台 WebSocket                       │
│          wss://msg-frontier.feishu.cn/ws/v2              │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│                  Native Bridge (Python)                  │
│                                                         │
│  ┌──────────────────┐   ┌──────────────────────────┐    │
│  │    bridge.py      │   │    voice_pipeline.py      │    │
│  │   WS 连接+生命周期  │   │  Whisper 常驻模型(CPU)     │    │
│  │   事件分发+回复策略  │   │  → DeepSeek 润色         │    │
│  └────────┬─────────┘   └──────────┬───────────────┘    │
│           │                        │                     │
│  ┌────────▼─────────┐   ┌──────────▼───────────────┐    │
│  │    intake.py      │   │  sender.py                │    │
│  │  消息队列(800ms)    │   │  send_card / send_post    │    │
│  │  抢占管理           │   │  / send_text / download   │    │
│  └────────┬─────────┘   └──────────┬───────────────┘    │
│           │                        ▲                     │
│  ┌────────▼─────────┐             │                     │
│  │  claude_runner.py │             │                     │
│  │  子进程管理         │────────────┘                     │
│  │  stream-json 解析  │                                  │
│  └────────┬─────────┘                                    │
│           │                                              │
│  ┌────────▼─────────┐                                    │
│  │session_manager.py │                                   │
│  │  每 chat 独立会话   │                                   │
│  │  JSON 持久化       │                                   │
│  └──────────────────┘                                    │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
             ┌─────────────────────┐
             │   Claude CLI (子进程)  │
             │   stream-json 输出    │
             └─────────────────────┘
```

## 核心模块

### bridge.py — 主控（~150 行）

职责：加载配置、创建 WS 连接、注册事件处理器、状态卡片管理、最终回复策略。

**关键设计**：
- 启动时初始化所有组件（voice_pipeline, sender, sessions, active_runs, pending_queue）
- 启动时 `clear_all()` 清空旧会话——旧 session_id 对应的 Claude 进程已死
- `on_flush()` 是核心回调：收到消息批次 → 发状态卡片 → 调 Claude → 流式更新卡片 → 最终替换

**状态卡片生命周期**：
```
1. 发送初始卡片（"🧠 思考中…"）
2. 循环：解析 stream-json → 检测内容块 → 更新卡片状态（1.5s 节流）
3. 最终：patch 卡片为完整回复（或 fallback 到 send_post）
```

### intake.py — 消息入口（~80 行）

职责：消息队列、去抖、抢占管理。

**ActiveRuns**：追踪每个 chat 的活跃 Claude 进程。
- `register()`: 注册新 run，自动中断同 scope 的旧 run
- `interrupt()`: kill 进程 + 标记为中断状态

**PendingQueue**：800ms 去抖窗口。
- 每次 `enqueue` 取消旧 timer、启动新 timer
- timer 到期 → `_flush` → 调用 callback（`on_flush`）
- 效果：快速连续消息自动合并为一批

### claude_runner.py — Claude 集成（~80 行）

职责：构建 prompt、启动子进程、解析 stream-json。

**prompt 构造**：
```
<bridge_context>
chat_id: oc_xxx
chat_type: p2p
sender_id: ou_xxx
</bridge_context>

[用户的消息内容]
```

**stream-json 解析的关键理解**：
- 顶层事件类型只有 `assistant`、`system`、`user`
- 内容块（thinking/tool_use/tool_result/text）都在 `assistant.message.content[]` 里
- 这是踩了坑才搞清楚的（见复盘条目 10）

### sender.py — 飞书发送（~110 行）

职责：发送消息、下载媒体文件。

| 方法 | 用途 | 消息类型 |
|---|---|---|
| `send_text()` | 纯文本（回退方案） | `text` |
| `send_post()` | Markdown 富文本 | `post` |
| `send_card()` | 交互卡片（状态/回复） | `interactive` |
| `download_media()` | 下载语音附件 | — |

**关键注意事项**：
- `markdown_to_post_ast()` 已包裹 `zh_cn`，不要再套一层
- 只有 `interactive` 类型可以被 `patch` 更新
- 媒体下载用直接 HTTP 请求（带 tenant_access_token），不用 SDK

### voice_pipeline.py — 语音管线（~100 行）

职责：常驻 Whisper 模型 + DeepSeek 润色。

**设计要点**：
1. `init()` 在桥启动时调用，加载 Whisper 模型到内存
2. 模型常驻——后续转写无冷加载，< 2s
3. 短文本（< 10 字符）跳过润色——不值得等待 API
4. 润色 prompt 包含具体 STT 错误修复规则（如 "拍摄" → "Python"）

### session_manager.py — 会话管理（~50 行）

职责：每 chat 的 session_id 持久化（JSON 文件）。

**关键策略**：
- 桥启动时 `clear_all()`：进程重启后所有旧 session_id 作废
- 运行期间正常 `save()` → `get()`：同一进程内保持对话连续性
- `prune()` 方法存在但未启用（预留空闲超时清理）

## 关键决策

### 决策 1：语音处理在桥内部 vs 作为 Claude 外部工具

**选型**：桥内部同步处理。

**理由**：
- 外部工具方案需要 2-3 轮模型交互（Bash → Read → 处理），额外 3-5s 延迟
- 模型常驻内存后转写仅需 1-2s，远快于工具调用
- Claude 不需要知道有音频这一步——减少 prompt 复杂度

### 决策 2：状态卡片 vs 文本流式显示进度

**选型**：交互卡片（CardKit 2.0）做状态，post 做最终回复。

**理由**：
- 文本消息不可 patch——无法更新进度
- 卡片是可 patch 的最小单元
- 进度卡片→最终回复的过渡通过 patch 内容实现，飞书上只有一条消息

### 决策 3：Session 生命周期

**选型**：启动时清空所有旧 session，运行期间正常管理。

**理由**：
- Claude session_id 生命周期 = Claude 进程生命周期
- 桥重启 = 所有旧进程已死，session_id 全部失效
- 一刀切禁用会话功能（第一次错误尝试）会让每次对话都是新 session

## 数据流

### 文本消息流

```
用户发消息 → WS 推送事件 → bridge.handle_message()
  → intake.enqueue() → 800ms 去抖 timer → on_flush()
    → 发状态卡片 → Claude 子进程 → stream-json 解析
      → 更新状态卡片（1.5s 节流） → 收集文本
        → patch 卡片为最终回复 / fallback send_post
```

### 语音消息流

```
用户发语音 → WS 推送事件 → bridge._handle_audio()
  → sender.download_media() → voice_pipeline.process()
    → faster-whisper 转写 → DeepSeek 润色
      → 组装为文本 prompt → PendingQueue.enqueue()
        → 后续同文本消息流
```
