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
- `WECOM_LOG_LEVEL`：日志级别，默认 `INFO`。

### 开启电脑操作

确认普通聊天已经正常后，再在 `.env` 中加入：

```env
HARNESS_ENABLED=true
HARNESS_COMMAND_PREFIX=/电脑
HARNESS_REPO_PATH=D:\a-TuMo\study\project\deepseek-harness
HARNESS_WORKSPACE=D:\a-TuMo\study\project
HARNESS_PROVIDER=deepseek-official
HARNESS_MODEL=deepseek-v4-flash
HARNESS_MAX_TOKENS=49152
HARNESS_REQUEST_TIMEOUT_SECONDS=900
DEEPSEEK_API_KEY=你的密钥
WECOM_ALLOWED_USER_IDS=你自己的企业微信UserID
```

Windows 下这里显式启动同级仓库已经构建好的 Node JSON-RPC 运行时，因为 Harness 当前的 Python SDK 默认单文件运行时只覆盖 Linux/macOS。Node.js 需要在 `PATH` 中；也可用 `HARNESS_NODE_BIN` 指定完整路径。

`HARNESS_WORKSPACE` 是 Harness 的默认工作目录。建议先限制在项目目录，不要一开始就设置为磁盘根目录。

## 启动

统一 Agent 模式不需要启动 Spring Boot，直接执行：

```powershell
.venv\Scripts\wechat-aibot-bridge.exe
```

看到 `WeCom AI Bot authenticated` 后，在企业微信中给机器人发送文本。Bridge 会先显示“收到，正在处理…”，再用流式消息提交最终回复。

- `你好`：由统一 Agent 直接回答。
- `在项目目录创建一份待办清单`：由统一 Agent 判断并调用工具。
- `/电脑 创建一份待办清单`：兼容旧用法，效果相同。
- `/聊天 怎么创建项目`：强制只回答，不调用工具。
- `end`：结束当前上下文、保留旧记录并创建新会话。
- `/停止`：中断当前 Harness Runtime，并自动切换新会话。
- `/状态`：查看是否有任务运行及当前会话代数。
- `给我发送电脑桌面的报告文档`：Bridge 先用本地文件能力按名称查找并发送，不需要命令前缀，也不依赖模型额度。
- 其他位置、模糊条件或需要先生成/压缩的文件请求由 Harness 处理；Harness 输出内部交付标记后，Bridge 自动校验、分片上传并发回当前会话。

Harness 模式强制要求 `WECOM_ALLOWED_USER_IDS`。内置 persona 会区分知识问题与执行指令，高风险、不可逆操作必须先请求下一条消息确认。当前没有正式 GUI 工具，因此 persona 明确禁止用 SendKeys、固定坐标、全局剪贴板或临时 OCR 脚本模拟 GUI。机器人进程仍应使用专门的低权限 Windows 账号运行，不能以管理员身份常驻。

## 当前边界

- 已支持企业微信文本接收、处理中提示、统一 Agent、会话续接、`end` 换代、异常会话隔离和 `/停止`。
- 已支持本地普通文件的企业微信媒体分片上传与主动发送。单文件上限 50 MiB，空文件和目录不会发送；目录需要先由 Harness 压缩成文件。
- 文件交付使用 `<wechat-file>绝对路径</wechat-file>` 作为 Harness 与 Bridge 的内部协议，用户不会看到该标签。不存在的路径会报告失败，不能把路径文本当作发送成功。
- DeepSeek 返回 `QUOTA`、鉴权或限流错误时，Bridge 会在微信中显示可执行的原因，并自动切换到干净会话；其中余额不足仍需充值或更换可用的 API Key，代码无法代替上游额度。
- 当前会立即回复“收到，正在处理…”，长任务每 30 秒刷新已用时，结束后返回最终结果。后续还可以把 Harness 的工具事件转换成更具体的阶段进度。

## 测试

```powershell
$env:PYTHONPATH = "wechat-aibot-bridge\src"
py -m unittest discover -s wechat-aibot-bridge\tests -v
```
