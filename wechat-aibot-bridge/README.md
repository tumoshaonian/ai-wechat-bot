# 企业微信智能机器人长连接 Bridge

这个服务使用企业微信智能机器人 Python SDK 建立 WebSocket 长连接，替代旧版 OCR/桌面自动化消息入口。

## 为什么不使用 FastAPI

企业微信长连接由本机主动连接企业微信服务器，不需要对外暴露 HTTP 回调，因此核心运行时只需要 `asyncio`。当前引入 FastAPI 会增加部署面和生命周期协调，却不参与消息收发。

以后出现以下需求时，可以在 `adapters/http_admin.py` 中增加一个可选 FastAPI 适配器：

- `/health`、`/ready` 运维探针
- 本地任务查询和取消 API
- 浏览器管理界面
- 云端任务中继回调

## 架构

```text
wechat_agent/
  domain.py                 领域消息、Agent 回复和文件交付模型
  ports.py                  ChatBackend / ConversationResponder 接口
  application.py            消息编排、白名单、幂等、逐会话串行化
  config.py                 环境配置
  adapters/
    wecom_payload.py        企业微信消息转换
    wecom_channel.py        WebSocket、媒体分片上传和文件发送适配器
    spring_chat.py          现有 Spring Boot 聊天后端适配器
    routing.py              普通聊天/电脑命令显式路由
    deepseek_harness.py     DeepSeek Harness 电脑操作适配器
  desktop/
    mcp_server.py           Harness stdio MCP 工具服务器
    worker.py               Python/PowerShell UIA 边界
  scripts/windows_uia.ps1   Windows UI Automation 原生适配器
  config/harness-wecom.patch.yml     sdk Profile 的 WeCom/Desktop 增量配置
  __main__.py               组合根与进程生命周期
```

业务逻辑不直接依赖企业微信 SDK 或 Harness SDK。启用 Harness 后，所有自然语言都进入统一 Agent，由 Agent 自行决定直接回答还是调用工具；Spring Boot 仅作为 `HARNESS_ENABLED=false` 时的兼容后端。

## 安装

在项目根目录执行：

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install `
  -e ..\deepseek-harness\python\sdk-runtime `
  -e ..\deepseek-harness\python\sdk `
  -e .\wechat-aibot-bridge
```

项目根目录需要有 `.env`：

```env
WECHAT_BOT_ID=企业微信机器人 Bot ID
WECHAT_BOT_SECRET=企业微信机器人 Secret
SPRING_BOOT_URL=http://127.0.0.1:8080/api/wechat/reply
```

可选配置：

- `WECOM_ALLOWED_USER_IDS`：逗号分隔的企业微信 UserID 白名单。个人模式可暂时留空；多人模式强烈建议设置。
- `WECOM_REQUEST_TIMEOUT_SECONDS`：调用聊天后端的超时秒数，默认 `60`。
- `WECOM_PROGRESS_INTERVAL_SECONDS`：任务未完成时刷新进度的间隔，默认 `30` 秒。
- `WECOM_TASK_TIMEOUT_SECONDS`：单个任务总时限，默认 `480` 秒且必须小于 `590` 秒，保证在企业微信 10 分钟流过期前停止并提交结果。
- `WECOM_LOG_LEVEL`：日志级别，默认 `INFO`。

### 开启电脑操作

确认普通聊天已经正常后，再在 `.env` 中加入：

```env
HARNESS_ENABLED=true
HARNESS_COMMAND_PREFIX=/电脑
HARNESS_WORKSPACE=D:\a-TuMo\study\project
HARNESS_DSH_HOME=D:\a-TuMo\study\project\ai-wechat-bot\.harness-sessions
HARNESS_PROFILE=sdk
HARNESS_RUNTIME_MODE=exe
HARNESS_PERMISSION_MODE=danger-full-access
HARNESS_PROVIDER=deepseek-official
HARNESS_MODEL=deepseek-v4-flash
HARNESS_REASONING_EFFORT=max
HARNESS_MAX_TOKENS=49152
HARNESS_INITIALIZE_TIMEOUT_SECONDS=90
HARNESS_REQUEST_TIMEOUT_SECONDS=450
HARNESS_SHUTDOWN_TIMEOUT_SECONDS=10
DESKTOP_TOOLS_ENABLED=true
DESKTOP_ACTION_TIMEOUT_SECONDS=180
DESKTOP_SCREENSHOT_DIRECTORY=C:\Users\你的用户名\Pictures\WeComAgent
DOUBAO_LAUNCH_PATH=C:\Users\你的用户名\Desktop\豆包.lnk
DEEPSEEK_API_KEY=你的密钥
WECOM_ALLOWED_USER_IDS=你自己的企业微信UserID
```

新版 Python SDK 通过 `dsh --profile sdk` 启动，并要求显式 `DSH_HOME`。Windows x64 正式运行时包含原生 exe 与 `rg.exe` sidecar，不再要求系统 Node.js。若使用同级源码仓库开发，先在 `deepseek-harness` 根目录执行官方构建：

```powershell
pnpm install --frozen-lockfile
pnpm exec tsx scripts/build-exe-for-python-sdk.ts --targets=node24-win-x64
```

默认 `HARNESS_RUNTIME_MODE=exe`。仅调试源码时可设为 `node`，但同样要先运行上述构建以生成 dev-only Node carrier。也可以用 `HARNESS_DSH_BIN` 指向经过验证的自定义 dsh 可执行文件。

Windows 上 `DESKTOP_TOOLS_ENABLED` 默认开启。项目把 `harness-wecom.patch.yml` 作为官方 `sdk` Profile 的 invocation patch，使用 Harness MCP Client 启动当前虚拟环境里的 Desktop Worker，不需要另开 FastAPI 服务或端口。旧的 `HARNESS_RUNTIME_BIN_JS`、`HARNESS_NODE_BIN`、`HARNESS_CORDIS_CONFIG` 配置已经废弃，应从私有 `.env` 中删除。

新版 SDK server 会等待完整 Loader 树，MCP Client 也会等待首次连接和工具发现，因此 SDK 初始化成功就代表 Desktop MCP 已就绪；不再需要项目自定义的 JSON-RPC 启动门。若工具发现失败，`failOnStartupError=true` 会让 Bridge 在连接企业微信前明确启动失败。

Desktop MCP 注册前还会执行一次窗口枚举预检。预检使用独立的 25 秒上限，不会占用普通桌面动作的 180 秒预算。日志中的 `Desktop MCP server started preflight_windows=N` 表示原生窗口通道已在限定时间内返回；PowerShell 子进程的 stdin 与 MCP JSON-RPC stdin 完全隔离，避免原生适配器误读或占用协议数据。`doubao_ask` 对外允许的窗口等待和回答等待上限分别是 30 秒与 120 秒，并在启动时校验总体预算确实落在 MCP/任务截止时间内。

升级 Harness 后可以先做不调用模型的初始化检查，再做一次只读桌面工具的真实端到端检查：

```powershell
.\.venv\Scripts\python.exe wechat-aibot-bridge\scripts\smoke_harness_runtime.py
.\.venv\Scripts\python.exe wechat-aibot-bridge\scripts\smoke_harness_runtime.py --timeout 180 --prompt '请实际调用 mcp__desktop__list_windows，只回复窗口数量。'
```

第一条看到 `HARNESS_RUNTIME_INIT_OK` 代表 exe、Profile、patch 和 MCP 工具发现成功；第二条只有在模型真实完成工具调用后才会输出 `HARNESS_RUNTIME_PROMPT_OK`。

`HARNESS_WORKSPACE` 是 Harness 的默认工作目录。建议先限制在项目目录，不要一开始就设置为磁盘根目录。

`sdk` Profile 默认权限为 `workspace-write + ask`，但 Python SDK 当前没有把审批问题转发到企业微信的响应协议。为了满足本项目“本人通过微信无人值守操作整台电脑”的目标，示例显式使用 `HARNESS_PERMISSION_MODE=danger-full-access`，并继续强制 `WECOM_ALLOWED_USER_IDS` 白名单和高风险操作二次确认。不要将该模式开放给不受信任用户；如果以后支持多人，应先实现真正的微信审批适配器。

## 启动

统一 Agent 模式不需要启动 Spring Boot，直接执行：

```powershell
.venv\Scripts\wechat-aibot-bridge.exe
```

启动时会先看到 `Initializing DeepSeek Harness` 和 `Unified DeepSeek Harness Agent ready`，随后才连接企业微信。看到 `WeCom AI Bot authenticated` 后发送文本。这样 SDK/Profile/MCP 配置错误会在电脑端启动阶段暴露，而不是等第一条微信消息才失败。

- `你好`：由统一 Agent 直接回答。
- `在项目目录创建一份待办清单`：由统一 Agent 判断并调用工具。
- `/电脑 创建一份待办清单`：兼容旧用法，效果相同。
- `/聊天 怎么创建项目`：强制只回答，不调用工具。
- `end`：结束当前上下文、保留旧记录并创建新会话。
- `/停止`：中断当前 Harness Runtime，并自动切换新会话。
- `/状态`：查看是否有任务运行及当前会话代数。
- `给我发送电脑桌面的报告文档`：Bridge 先用本地文件能力按名称查找并发送，不需要命令前缀，也不依赖模型额度。
- `打开豆包，询问什么是计算机网络，然后截图发给我`：调用 `mcp__desktop__doubao_ask`，依次验证主窗口、输入值、问题确实进入会话、回答稳定和 HWND 绑定截图。若豆包的 Chromium 可访问性树未开启，工具会仅重启豆包并自动加上 `--force-renderer-accessibility` 后继续；首次可能看到豆包自动重启一次。
- 其他位置、模糊条件或需要先生成/压缩的文件请求由 Harness 处理；Harness 输出内部交付标记后，Bridge 自动校验、分片上传并发回当前会话。

Harness 模式强制要求 `WECOM_ALLOWED_USER_IDS`。内置 persona 会区分知识问题与执行指令，高风险、不可逆操作必须先请求下一条消息确认。GUI 操作只允许调用结构化 Desktop MCP 工具；仍明确禁止用 SendKeys、固定坐标、全局剪贴板或临时 OCR 脚本模拟 GUI。机器人进程应使用专门的低权限 Windows 账号运行，不能以管理员身份常驻。

## 当前边界

- 已支持企业微信文本接收、处理中提示、统一 Agent、会话续接、`end` 换代、异常会话隔离和 `/停止`。
- 已支持本地普通文件的企业微信媒体分片上传与主动发送。单文件上限 50 MiB，空文件和目录不会发送；目录需要先由 Harness 压缩成文件。
- 文件交付使用 `<wechat-file>绝对路径</wechat-file>` 作为 Harness 与 Bridge 的内部协议，用户不会看到该标签。对于 `doubao_ask` 和 `capture`，Bridge 会从根 Agent 以及已发现的子 Agent 工具事件中恢复已配对且执行成功的截图，并按会话与调用 ID 防止串配，因此不依赖模型重复标签。不存在的路径不会加入发送队列，不能把路径文本当作发送成功。
- DeepSeek 返回余额、凭据、模型、上下文、限流、服务端、传输或超时错误时，Bridge 会在微信中显示安全且可执行的中文原因，并自动切换到干净会话。未知异常不会把上游原文、本机路径或密钥片段回发到微信；详细诊断仅保留在经过脱敏的电脑端日志中。
- 当前会立即回复“收到，正在处理…”，长任务每 30 秒刷新已用时，结束后返回最终结果。Harness 达到输出上限时会明确标记结果可能不完整，不再当成完整成功。
- 正常 Bridge 重启会复用稳定的 Harness 会话；`end`、`/停止`、Runtime 异常或上次进程未干净退出才会切换会话。任一 SDK 传输、协议或超时异常都会销毁整个 Runtime，防止失败任务污染下一条消息。
- 企业微信进度流失效或任务达到总时限时，Bridge 会先关闭 Harness Runtime、Desktop MCP 和正在运行的原生适配器，再结束当前会话，避免后台任务在微信已经无法回传后继续操作电脑。
- UIA 只会操作暴露 `ValuePattern` / `InvokePattern` 的控件；豆包的 Electron 可访问性树可由受控重启开启。其他自绘画布、锁屏、UAC 安全桌面和验证码仍不会降级成盲目坐标点击。
- 按当前实施范围，尚未隔离用户和 Agent 对同一桌面的并发使用。

## 测试

```powershell
$env:PYTHONPATH = "wechat-aibot-bridge\src"
py -m unittest discover -s wechat-aibot-bridge\tests -v
```
