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
- `打开豆包，询问什么是计算机网络，然后截图发给我`：调用 `mcp__desktop__doubao_ask`，依次准备主窗口、验证输入与提交、等待回答和截图。无法访问控件时报告 `CONTROL_UNAVAILABLE`，不自动杀进程重启。
- 其他位置、模糊条件或需要先生成/压缩的文件请求由 Harness 处理；Harness 输出内部交付标记后，Bridge 自动校验、分片上传并发回当前会话。

Harness 模式强制要求 `WECOM_ALLOWED_USER_IDS`。内置 persona 会区分知识问题与执行指令，高风险、不可逆操作必须先请求下一条消息确认。GUI 操作只允许调用结构化 Desktop MCP 工具；仍明确禁止用 SendKeys、固定坐标、全局剪贴板或临时 OCR 脚本模拟 GUI。机器人进程应使用专门的低权限 Windows 账号运行，不能以管理员身份常驻。

## 当前边界

- 已支持企业微信文本接收、处理中提示、统一 Agent、会话续接、`end` 换代、异常会话隔离和 `/停止`。
- 已支持本地普通文件的企业微信媒体分片上传与主动发送。单文件上限 50 MiB，空文件和目录不会发送；目录需要先由 Harness 压缩成文件。
- 文件优先由 `mcp__desktop__deliver_file` 交付，工具使用本轮临时票据绑定接收对象，返回真实发送接口回执；不能把 `sent` 解读为用户已读。`<wechat-file>` 仅作为兼容入口，复用相同授权校验和交付去重。不自动发送任何未选中的中间截图。
- DeepSeek 返回余额、凭据、模型、上下文、限流、服务端、传输或超时错误时，Bridge 会在微信中显示安全且可执行的中文原因，并自动切换到干净会话。未知异常不会把上游原文、本机路径或密钥片段回发到微信；详细诊断仅保留在经过脱敏的电脑端日志中。
- 当前会立即回复“收到，正在处理…”，长任务每 30 秒刷新已用时，结束后返回最终结果。Harness 达到输出上限时会明确标记结果可能不完整，不再当成完整成功。
- Runtime 使用独立编号避免与磁盘日志冲突；本版起的用户请求、模型回复、交付事实持久记录在 `HARNESS_SESSION_ROOT/channel-history.sqlite3`。冷启动使用渠道事实重建，不是原生工具历史恢复，也不自动导入旧版日志；`end` 开启新逻辑上下文，取消及异常保留已记录事实。
- `HARNESS_RECOVERY_MAX_BYTES` 默认 `196608`，约束冷恢复或待回写渠道记录的 UTF-8 大小，不是模型 token 窗口。超出预算时使用“持久摘要＋近期完整轮次”，最多整理 8 批，每批输入不超过预算，摘要输出不超过预算的四分之一；至少保留最新一轮完整记录。扫描积压超过 9 倍预算、单轮不可拆分或摘要不合格时明确拒绝本轮操作。
- 摘要使用独立临时 Harness Profile，关闭 Desktop MCP、不加载用户扩展，并通过 `summary-no-tools.mjs` 在执行层拒绝所有工具调用。当前 Runtime 没有 guard 能力则拒绝启动摘要器。摘要会增加模型调用及延迟，计入现有任务超时；停止和 `end` 同样适用于恢复期间。
- 原始事实不删除，SQLite 检查点记录摘要覆盖范围；`end` 后拒绝旧 epoch 的摘要写入，晚到回执保留。摘要属于有损渠道记录重建，不是原生工具轨迹恢复，不保证记住每个历史细节；交付去重仍查询结构化数据库而非相信摘要。
- 活跃会话的原生 compaction 配置位于 `config/harness-wecom.patch.yml`：阈值 0.75、近期保留比例 0.16，与上述冷恢复摘要是两条不同路径。

### 执行结果与渠道回执

Agent 执行返回后，结果先写入任务事件和结果摘要，再发送最终回复。最后一条微信回复失败或回执丢失时，任务记录为部分完成，保留执行结果；文件交付和回复回执分别展示。UNKNOWN 表示不能确认，不允许据此自动重做电脑操作。相同消息的持久去重继续有效，但用户主动发送一条新的相似请求不属于同消息去重。

后台终态不会被晚到进度覆盖，冲突事件仅进入审计；取消请求不等于停止已完成。当前 Bridge 内的后台精确取消已覆盖排队任务；微信 stop 停止当前会话活动及排队请求，end 排空旧请求后建立不继承旧记忆的新会话，结束期间收到的新请求明确拒绝。多进程续跑、持久排队及回复补发尚未实现，既有错误投影不会自动重算。等待确认能力见下节。前端独立测试可运行 `node wechat-aibot-bridge/tests/smoke_task_outcome_ui.cjs`（项目根目录，需要可解析的 Playwright 和已安装 Edge，使用模拟接口，不连接微信）。

### 一次性确认交互

Agent 需要明确授权时调用 `request_confirmation(task_ticket, operation)`，`operation` 必须包含具体 action、target、effect。系统向原用户发送一次性编号；在90秒内回复 `/确认 编号` 或 `/拒绝 编号`，单独“确认”不会授权。编号限定原连接、会话、用户、任务和逻辑会话代次，不可重复使用；stop、end、超时与服务重启后不会继续利用旧编号。

等待期间可用控制命令，后台状态为等待确认。拒绝、过期及交互失败会终止所属 Runtime；批准只允许继续该次描述的操作，不代表操作完成。记录保存于渠道 SQLite 的 confirmations 表及渠道事实；数据库中属于旧进程的未决记录仅供审计，不是可恢复授权。

意图确认不等于实际工具授权；现在另有下述执行前守卫。当前不提供后台替代原用户审批。真实模型集成自测：项目根目录运行 `.\.venv\Scripts\python.exe wechat-aibot-bridge/tests/smoke_harness_confirmation.py`（设置 PYTHONPATH 为 wechat-aibot-bridge/src，使用模拟确认和虚拟操作，不向微信发送文件）。

### 工具执行前守卫

生产 Bridge 在自定义 Harness 配置后强制追加 `config/execution-policy.mjs` 插件。它接入 `tools/pre-execute`、单调 `tools.guard` 和 `tools/execute`，而不是只修改模型提示词。缺少守卫接口或授权服务时不允许无保护执行；摘要器继续使用独立的全部工具禁用守卫。

- `list_windows`、`inspect_window` 是当前仅有的观察豁免；`request_confirmation` 走原有受校验交互。它们也要通过当前 Runtime、任务及调用身份校验。
- 其他工具（含文件读取／写入、shell、文件交付、UI 操作、委派以及未知工具）默认每次询问。确认由实际工具名和完整参数生成，不使用模型自述替代参数。后台现在可设置精确规则及默认 ask/deny，非观察工具不允许配置为自动放行。
- 每次批准限定当前执行对象及参数；进入执行器前消费，不能用于另一调用、修改后的参数或重试。运行中取消会使未执行批准失效；拒绝／过期停止所属 Runtime。并行授权冲突明确拒绝，Agent 应顺序调用。
- 参数展示超过1600字符时拒绝，不截断后继续授权。任务票据不在确认文本中展示，但包含在实际调用指纹中。后台事件记录 `tool.authorization`、调用指纹和确认编号。
- 原生子任务仅在创建元数据可追溯到当前根会话时获得独立逐次校验，否则拒绝；未知/无 Agent 的直接工具执行同样拒绝。真实子任务与 PTC 路径尚未单独验收。

这不是 Windows 进程沙箱：获批 shell 脚本内部的多个动作无法逐个拦截，也不能限制受信任插件、同权限进程或程序内部行为。审批的是这份参数，不保证文件内容／界面不会随后变化；不要批准不理解的脚本。最终回复中的兼容文件标签仍走原交付校验，不属于 Harness 工具调用。尚不建议向不可信用户开放完整电脑控制。

可重复测试：`node wechat-aibot-bridge/tests/test-execution-policy.mjs`；真实 Harness 测试为 `.\.venv\Scripts\python.exe wechat-aibot-bridge/tests/smoke_execution_policy.py`（同样设置 PYTHONPATH，使用现有模型凭据、临时目录及模拟确认，不操作桌面或发微信）。测试要求未批准前无副作用、批准执行一次、拒绝不执行。

### 后台策略发布

打开“Agent 配置 → 新版本”，填写模型、提示词、任务时限及权限表单。观察工具可选自动允许/确认/禁止；其他精确工具规则每行填写 `bash=deny` 或 `mcp__desktop__invoke=ask`，不支持通配符。确认时限为10–90秒，计入任务总时限。默认规则仅允许 ask/deny，确认交互通道不可被策略关闭。

策略使用已有不可变配置版本，创建/发布/回滚沿用后台权限和审计。发布后需要结束当前任务并重启 Bridge；配置在进程启动时固化，不会热更新运行任务。任务详情的“权限与确认记录”展示实际版本、策略快照、确认内容（脱敏）、决定人和调用指纹，具体结果仍以工具记录为准。后台不代替微信原用户批准。

旧 `{ "shell": false }` 转换为 `bash=deny`，true 转换为 ask，不意味着无确认执行。不认识的策略字段会拒绝发布/加载，需创建修正版本，不修改旧历史。默认 `ADMIN_MANAGED_AGENT_CONFIG=true` 时，配置数据库不可读、选择指针损坏或策略非法会拒绝启动，不回退到更宽松权限；显式关闭后台托管则使用内置保守默认策略。

真实微信验收见 `docs/wecom-live-acceptance.md`（仓库根目录），目前待手机端配合，不能把模拟测试解释为已完成实际收件验收。

### 应用无关的能力边界

本项目面向通用电脑操作，不将软件名称作为任务路由白名单。Harness 按当前窗口／控件证据调用通用的窗口准备、观察、输入、动作和截图工具；现有 `doubao_ask` 等只是可选快捷适配，不是支持其他应用的前置条件。通用文档生成／导出和受控视觉闭环仍待完善，不能把提示词调整等同于这些执行能力已经实现。新视觉模型、截图传输权限与成本应统一确认，不针对某两个软件单独设计。
- 企业微信进度流失效或任务达到总时限时，Bridge 会先关闭 Harness Runtime、Desktop MCP 和正在运行的原生适配器，再结束当前会话，避免后台任务在微信已经无法回传后继续操作电脑。
- UIA 操作需要可访问的 `ValuePattern` / `InvokePattern` 控件。自绘画布、锁屏、UAC 安全桌面和验证码不降级为盲目点击。视觉模型操作尚未启用。
- 按当前实施范围，尚未隔离用户和 Agent 对同一桌面的并发使用。

### 后台实时更新

后台首次连接事件流时从当前游标开始，历史数据由页面查询加载，不再逐条回放旧事件触发刷新；断线重连仍从上次游标补齐事件。自动更新只响应当前页面相关的事件，合并后至少间隔 5 秒，不显示加载骨架、不抢焦点，并保留搜索草稿和滚动位置。输入控件、按钮获得焦点、弹窗／详情抽屉打开或页面隐藏时暂缓更新；手动刷新仍可立即执行。

接口暂时失败时保留原内容并提示“更新暂不可用”，不会反复切换错误页。实时日志继续增量追加。更新本版本后需重启后台服务，并在浏览器刷新一次以加载新脚本。

隔离浏览器回归：在项目根目录运行 `node wechat-aibot-bridge/tests/smoke_live_refresh.cjs`，需要可解析的 Playwright 和已安装 Edge。测试使用模拟接口和事件流，不连接微信；覆盖事件风暴、历史去重、输入／滚动保护、详情抽屉、请求失败及重连。

## 测试

```powershell
$env:PYTHONPATH = "wechat-aibot-bridge\src"
py -m unittest discover -s wechat-aibot-bridge\tests -v
```
