# 微信 OCR Bridge

这个目录里的 Python bridge 用来把 Windows 新版微信 `4.x` 和 Spring Boot 聊天服务连起来。

## 当前方案

- 适配目标：个人微信 Windows 新版 `4.x`
- 对接方式：`前台截图 + OCR + 键鼠自动化`
- 读取方式：轮询指定联系人或群聊，识别聊天区最后一个消息气泡
- 回复方式：搜索会话、打开聊天、粘贴文本、`Alt+S` 发送
- 适用场景：少量联系人和少量群聊的低频自动回复

这版 bridge 不再依赖旧版 `wxauto`，因为它主要适配经典微信 `3.9.x`，不适合新版微信 `4.1.9.35`。当前方案已经在你的机器上验证过：把微信切到前台后，OCR 能读到真实聊天内容。

## 目录说明

- `bridge.py`：新版微信桥接主程序
- `config.example.json`：配置示例
- `requirements.txt`：Python 依赖

## 工作原理

1. Python 连接已登录的新版微信主窗口
2. 把微信切到前台
3. 逐个打开目标聊天
4. 截取聊天区域并做 OCR
5. 解析最后一个消息气泡
6. 把消息发送到 Spring Boot 的 `/api/wechat/reply`
7. 把模型回复重新发回微信

## 安装依赖

```bash
pip install -r wechat-gui-bridge/requirements.txt
```

## 配置

先复制配置文件：

```bash
copy wechat-gui-bridge\config.example.json wechat-gui-bridge\config.json
```

再编辑 `wechat-gui-bridge/config.json`：

- `spring_boot_url`：Spring Boot 回复接口，默认 `http://127.0.0.1:8080/api/wechat/reply`
- `listen_contacts`：需要监听的私聊备注名或昵称
- `listen_groups`：需要监听的群名称
- `group_trigger_prefixes`：群聊触发前缀，推荐配置，避免群里所有消息都自动回复
- `reply_prefix`：统一回复前缀，可留空
- `weixin_path`：可选，`Weixin.exe` 的绝对路径，找不到微信时建议填写
- `maximize_main_window`：是否在启动 bridge 时最大化微信窗口
- `chat_open_wait_seconds`：切换聊天后的等待时间
- `search_result_timeout_seconds`：输入搜索词后等待结果刷新的时间
- `send_delay_seconds`：粘贴消息后到发送前的等待时间
- `focus_window_wait_seconds`：把微信切到前台后的等待时间
- `debug_save_images`：是否保存 OCR 调试截图
- `ignore_self_messages`：是否忽略自己发出的消息，建议保持 `true`

## 启动顺序

先启动 Spring Boot：

```bash
mvn spring-boot:run
```

```bash
python wechat-gui-bridge\bridge.py --config wechat-gui-bridge\config.json
```

如果你已经在 `wechat-gui-bridge` 目录下，也可以写成：

```bash
python bridge.py --config config.json
```

## 建议的第一次联调

1. 先只配置一个 `listen_contacts`
2. 先把 `listen_groups` 留空
3. 保持微信主窗口打开，不要最小化
4. 尽量不要让其他窗口遮挡微信
5. 用主微信给辅助微信发一条简单文本
6. 看 bridge 日志里是否打印收到消息
7. 看辅助微信是否自动回复

## 群聊建议

- 先只监听一个测试群
- 推荐配置 `group_trigger_prefixes`
- 建议只在消息以 `@bot`、`bot `、`小助手` 这类前缀开头时触发
- OCR 对群成员昵称识别不一定每次都稳定，所以第一阶段先把“群里触发聊天”跑通，后面再精细化

## 注意事项

- 这版 bridge 必须把微信切到前台，所以运行过程中不要频繁抢焦点
- 如果微信被其他窗口遮挡，OCR 读取到的就不是微信内容
- 这版 bridge 是“轮询指定聊天最后一条消息”的方案，不适合大量聊天窗口高并发监听
- 如果同一个聊天在短时间内连续收到很多条消息，bridge 可能只处理到最后几条中的一条
- GUI 自动化依赖微信桌面界面，微信升级后可能需要调整截图区域和等待时间
- 运行 bridge 时，微信主窗口不要关闭
- 第一次启动时如果 bridge 提示找不到微信，请优先在配置里填写 `weixin_path`
- 如果你打开了 `debug_save_images=true`，会在 `wechat-gui-bridge/debug` 里保存调试截图
