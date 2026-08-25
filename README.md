# AI WeCom Computer Agent

通过企业微信智能机器人与一个统一的本机 AI Agent 对话。Agent 自行判断是直接回答，还是调用文件、命令行等工具完成任务。

## 当前链路

```text
企业微信智能机器人
  -> WebSocket 长连接
  -> wechat-aibot-bridge
  -> UnifiedAgentBackend
  -> DeepSeek Harness
      -> 直接回答 / 文件工具 / 命令行工具
  -> 企业微信媒体分片上传（Agent 请求交付文件时）
```

新长连接入口位于 [`wechat-aibot-bridge`](wechat-aibot-bridge/README.md)。Spring Boot 聊天接口与旧的 [`wechat-gui-bridge`](wechat-gui-bridge/README.md) 均作为兼容实现保留，不再处于主消息链路。

## 快速开始

1. 复制 `.env.example` 为 `.env`，填写企业微信机器人的 `Bot ID` 和 `Secret`。
2. 在 `.env` 填写 `DEEPSEEK_API_KEY`、本人企业微信 UserID，并保持 `HARNESS_ENABLED=true`。
3. 双击桌面“企业微信电脑助手”，或根据 [`wechat-aibot-bridge/README.md`](wechat-aibot-bridge/README.md) 启动 Bridge。

## Windows 桌面启动器

运行 `scripts/WeComBotLauncher.ps1`，或使用安装到桌面的“企业微信电脑助手”快捷方式。启动器会：

- 自动启动统一 Agent 所需的 Python Bridge
- 需要调试旧接口时可单独点击 `Start Legacy Java`
- 实时显示 Java、企业微信和 Harness 日志
- 避免重复启动已有进程
- 通过 `STOP ALL` 同时终止 Bridge、Harness 子进程和 Java

启动器不依赖 Maven 在 `PATH` 中。手动运行 Maven 时，本机应使用完整路径：

```powershell
& 'D:\a-TuMo\enviroment\apache-maven-3.8.8\bin\mvn.cmd' spring-boot:run
```

Python 虚拟环境位于项目根目录。从项目根目录运行：

```powershell
.\.venv\Scripts\wechat-aibot-bridge.exe
```

如果当前目录是 `wechat-aibot-bridge`，则运行：

```powershell
..\.venv\Scripts\wechat-aibot-bridge.exe
```

## 统一 Agent 与控制命令

- 普通问题和电脑任务都直接发送给统一 Agent；Agent 自行决定是否调用工具。
- `/电脑 ...` 继续兼容，但已经不是必需前缀。
- `/聊天 ...` 强制本条消息只回答、不调用工具。
- `end` 或 `/电脑 结束会话` 保留旧记录并切换到不继承上下文的新会话。
- `/停止` 中断当前 Runtime 并为下一条消息切换新会话。
- `/状态` 查看当前会话代数和任务状态。
- 直接说“给我发送电脑桌面的 LeapMind 暑假开发计划文档”，机器人会按名称查找桌面文件并发送，不需要命令前缀或绝对路径。
- 其他位置或需要先生成、压缩的文件由统一 Agent 处理，Bridge 校验最终路径后上传并发送；单文件上限为 50 MiB。

对话上下文可以跨多条消息连续使用，但任务执行状态独立管理；Bridge 异常退出后会自动隔离未正常结束的会话。详细配置见 [`wechat-aibot-bridge/README.md`](wechat-aibot-bridge/README.md)。
