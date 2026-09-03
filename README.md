# AI WeCom Computer Agent

通过企业微信智能机器人与一个统一的本机 AI Agent 对话。Agent 自行判断是直接回答，还是调用文件、命令行等工具完成任务。

## 当前链路

```text
企业微信智能机器人
  -> WebSocket 长连接
  -> wechat-aibot-bridge
  -> UnifiedAgentBackend
  -> DeepSeek Harness Python SDK
      -> dsh --profile sdk + WeCom profile patch
      -> 直接回答 / 文件工具 / 命令行工具
      -> Desktop MCP Worker
          -> Windows UI Automation（Value / Invoke / Text）
  -> 企业微信媒体分片上传（Agent 请求交付文件时）
```

新长连接入口位于 [`wechat-aibot-bridge`](wechat-aibot-bridge/README.md)。Spring Boot 聊天接口与旧的 [`wechat-gui-bridge`](wechat-gui-bridge/README.md) 均作为兼容实现保留，不再处于主消息链路。

## 快速开始

1. 复制 `.env.example` 为 `.env`，填写企业微信机器人的 `Bot ID` 和 `Secret`。
2. 在 `.env` 填写 `DEEPSEEK_API_KEY`、本人企业微信 UserID，并保持 `HARNESS_ENABLED=true`。
3. 按 [`wechat-aibot-bridge/README.md`](wechat-aibot-bridge/README.md) 安装/构建与 SDK 同版本的 Harness Runtime。
4. 双击桌面“企业微信电脑助手”，或根据 Bridge 文档启动。

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
- 直接说“打开豆包，询问什么是计算机网络，然后截图发给我”，Agent 会调用结构化 `doubao_ask` 工具；只有输入内容回读验证、发送按钮调用和回答状态检查都成功后才会报告完成。

## Windows 桌面工具

桌面能力通过独立 stdio MCP Worker 接入 Harness，不允许模型临时生成 SendKeys、固定坐标或剪贴板脚本。第一版提供：

- 枚举真实顶层窗口，避免把“进程存在”误判为“软件可操作”
- 检查 UI Automation 控件树和支持的 Pattern
- 通过 `ValuePattern` 写入并回读验证输入
- 通过 `InvokePattern` 调用按钮
- 通过目标 HWND 的 `PrintWindow` 截图并作为企业微信文件发送；窗口被其他应用遮挡时也不会误截前台应用，即使模型漏写内部文件标签，Bridge 也会从受信任的成功工具事件中恢复截图
- 豆包问答的端到端事务工具

顶层窗口枚举使用 Win32 `EnumWindows`，优先选取同进程中面积最大的主窗口，再进入 UI Automation 控件树，避免误选 Electron 的辅助浮窗。若豆包未暴露 Chromium 可访问性树，事务工具会仅重启已验证的豆包进程并加入 `--force-renderer-accessibility`，随后重新定位 `ProseMirror` 输入框；不会退化为盲目坐标点击。输入、提交、回答完成和截图分别独立验证，失败日志会标明准确阶段。

默认配置会自动寻找桌面的 `豆包.lnk`。找不到时在 `.env` 设置 `DOUBAO_LAUNCH_PATH`。UIA 运行日志会写入 `desktop-worker.log`，桌面启动器的 Python/Harness 日志页会一并显示。

当前版本按要求暂不解决用户和 Agent 同时操作同一桌面的焦点竞争，也不绕过锁屏、UAC 安全桌面或验证码。

对话上下文可以跨多条消息连续使用，但任务执行状态独立管理；Bridge 异常退出后会自动隔离未正常结束的会话。企业微信流式消息 10 分钟后失效，因此 Bridge 默认在 480 秒主动停止超长任务并提交明确结果；流本身失效时也会同步终止 Harness 和 Desktop Worker。详细配置见 [`wechat-aibot-bridge/README.md`](wechat-aibot-bridge/README.md)。

当前集成使用新版 Harness 的稳定 `DSH_HOME + session id` 语义：正常重启后继续原会话；只有 `end`、明确停止、任务异常或上次进程未干净退出时才切换会话代数。Bridge 在连接企业微信之前会先初始化 SDK Profile 和 Desktop MCP，配置或运行时损坏会直接写入电脑端日志，不再等到用户发第一条消息后才暴露。
