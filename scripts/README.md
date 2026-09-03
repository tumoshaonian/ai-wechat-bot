# Local Supervisor 与桌面启动器

`LocalSupervisor.ps1` 是独立于 Admin API 和企业微信 Bridge 的本机进程控制器。它由当前登录用户启动，因此 Bridge、Harness 和 Desktop Worker 会继续运行在用户交互会话中，可以使用桌面 UI Automation。

## 常用命令

```powershell
# 启动 Supervisor，并启动 Admin API 与 Bridge
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\LocalSupervisor.ps1 -Action start-supervisor

# 查询机器可读状态
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\LocalSupervisor.ps1 -Action status

# 单独启停服务
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\LocalSupervisor.ps1 -Action start -Service admin
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\LocalSupervisor.ps1 -Action restart -Service bridge
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\LocalSupervisor.ps1 -Action stop -Service all

# 强制终止经过身份校验的托管进程树
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\LocalSupervisor.ps1 -Action emergency-stop -Service all

# 优雅停止服务并退出 Supervisor
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\LocalSupervisor.ps1 -Action shutdown
```

桌面快捷方式仍指向 `WeComBotLauncher.ps1`。窗口提供启动/停止全部、单独启停机器人和后台、打开管理后台、日志查看及紧急强停。关闭窗口不会停止服务。

## 默认约定

- Admin API：`.venv\Scripts\wechat-aibot-admin.exe`
- Admin 健康检查：`http://127.0.0.1:8765/api/admin/v1/health`
- Admin 页面：`http://127.0.0.1:8765/admin/`
- Bridge：`.venv\Scripts\wechat-aibot-bridge.exe`
- Supervisor 状态：`.runtime\supervisor\status.json`
- Supervisor 日志：`local-supervisor.log`

可以用 `.env` 或进程环境变量覆盖：

- `ADMIN_API_ENABLED`
- `ADMIN_API_EXECUTABLE`
- `ADMIN_API_ARGUMENTS` 或 `ADMIN_API_ARGUMENTS_JSON`
- `ADMIN_API_WORKING_DIRECTORY`
- `ADMIN_API_BASE_URL`
- `ADMIN_API_HEALTH_URL`
- `ADMIN_API_UI_URL`
- `ADMIN_API_SHUTDOWN_URL`
- `BRIDGE_ENABLED`
- `BRIDGE_EXECUTABLE`
- `BRIDGE_ARGUMENTS` 或 `BRIDGE_ARGUMENTS_JSON`
- `BRIDGE_WORKING_DIRECTORY`
- `BRIDGE_HEALTH_URL`
- `BRIDGE_SHUTDOWN_URL`
- `BRIDGE_SHUTDOWN_FILE`（默认 `.runtime\supervisor\bridge.stop.request`）
- `SUPERVISOR_GRACEFUL_STOP_SECONDS`
- `SUPERVISOR_HEALTH_TIMEOUT_SECONDS`
- `SUPERVISOR_RUNTIME_DIR`
- `SUPERVISOR_LOG_DIR`

参数数组优先使用 JSON 格式，例如：

```dotenv
ADMIN_API_ARGUMENTS_JSON=["--host","127.0.0.1","--port","8765"]
```

## 安全与恢复规则

Supervisor 不根据端口或进程名接管服务，也不会扫描或终止 8080 端口上的 Java。每个托管实例都会记录 PID、UTC 启动时间、可执行文件绝对路径、完整命令行 SHA-256、实例 ID 和工作目录。停止前会再次验证这些信息；状态丢失或指纹不匹配时拒绝终止。

正常停止 Bridge 时，Supervisor 会先原子写入 `BRIDGE_SHUTDOWN_FILE`，让 Bridge 取消 channel、关闭 Harness 和 Desktop Worker 后自行退出。其他服务会先调用可选的 `*_SHUTDOWN_URL`，再尝试关闭主窗口并等待。只有在优雅停机超时后，才对已经再次验证的根进程使用 `taskkill /T /F`；强杀失败时仍会再次验证身份后使用更窄的根进程兜底。

## 测试

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\tests\LocalSupervisor.Tests.ps1
```

测试在系统临时目录中启动隔离的假服务，不读取或停止真实机器人进程。
