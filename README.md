# Telegram Private Relay Bot

一个轻量、安全、零第三方 Python 依赖的 Telegram 私信中转机器人。

当前版本：**v1.1.0**，新增可选 Cloudflare Turnstile 人机验证。详见 [更新记录](CHANGELOG.md)。

其他用户私聊机器人后，管理员会收到发送者身份卡片和原消息副本；管理员直接回复该消息，即可通过机器人匿名答复。

作者：[ffhmfd](https://github.com/ffhmfd) · [MIT License](LICENSE)

## 功能

- 双向私信中转，管理员身份不会暴露给发送者
- 显示来信者昵称、用户名、Telegram User ID 和消息类型
- 支持文字、图片、视频、文件、语音、贴纸、位置、联系人及投票等常见消息
- `/block`、`/unblock` 管理员封禁命令
- 可选 Cloudflare Turnstile 验证：首次来信先验证，通过后 30 天免重复验证
- `/recover` 管理员回复旧身份卡片，恢复过期的回复映射
- 默认每位发送者最多 20 条消息/60 秒
- SQLite 持久化回复映射，默认保留 30 天
- 不下载或保存消息正文及媒体文件
- systemd 自动启动和安全沙箱
- Bot Token 通过 systemd credential 传入，不出现在源码、命令行或普通环境变量中
- 使用 `getUpdates` 长轮询；启用 Turnstile 时，验证页面需要域名和 HTTPS

## 环境要求

- Debian 12、Ubuntu 22.04/24.04 或相近的 systemd Linux 发行版
- Python 3.10 或更高版本
- root 或 sudo 权限
- 一个由 [@BotFather](https://t.me/BotFather) 创建的 Telegram Bot Token
- 管理员的 Telegram 数字 User ID

## 获取管理员 User ID

创建机器人后，先向它发送一条私聊消息，再在项目目录运行：

```bash
python3 scripts/get_owner_id.py
```

按提示输入 Bot Token。输入不会显示，也不会写入磁盘。工具只会列出机器人最近收到的私聊发送者，供你确认自己的数字 ID；如果机器人已设置 webhook，工具会安全退出而不修改 webhook。

## 一键安装

```bash
git clone https://github.com/ffhmfd/telegram-private-relay-bot.git
cd telegram-private-relay-bot
sudo bash scripts/install.sh
```

首次安装时，脚本会在终端中安全询问：

1. Bot Token（输入不回显）
2. 管理员 Telegram User ID

安装程序随后会：

- 创建无登录权限的 `telegram-relay-bot` 系统用户
- 安装程序到 `/opt/telegram-relay-bot`
- 将凭据写入权限为 `0600` 的 `/etc/telegram-relay-bot`
- 首次清除旧 webhook 和历史测试更新
- 启用并启动 `telegram-relay-bot.service`

> 首次初始化会丢弃此前积压的 Bot 更新。更新现有安装时，脚本会保留凭据和数据库，也不会再次清除积压消息。

安装完成后，管理员先向机器人发送 `/start`。其他用户随后可以直接私聊机器人留言。

## 使用方法

### 发送者

- `/start` 或 `/help`：查看留言说明
- 直接发送文字或媒体：成功转交后收到“已送达”确认

### 管理员

- `/start` 或 `/help`：查看管理说明
- 回复身份卡片或其下方的来信：匿名回复原发送者
- 回复来信并发送 `/block`：封禁该发送者
- 回复旧来信并发送 `/unblock`：解除封禁

管理员单独发送且没有回复来信的内容不会被转发。

## 启用 Cloudflare Turnstile

机器人默认兼容原有直接留言方式。配置 `VERIFICATION_URL` 后，所有未验证
的发送者（包括以前联系过的人）必须先验证；管理员回复不受影响。
未验证的留言不会转发或缓存，验证成功后需要重新发送。

1. 在 Cloudflare 控制台的 **Turnstile → Add widget** 创建 Managed 小组件。
   Hostname 填验证子域名（例如 `verify.example.com`），关闭 Pre-clearance。
2. 为该子域名设置指向 VPS 的 A 记录。在下面的配置中使用同一域名。
3. 运行安装脚本更新程序，再在 `/etc/telegram-relay-bot/config` 添加：

   ```ini
   VERIFICATION_URL=https://verify.example.com
   TURNSTILE_SITE_KEY=从控制台复制的SiteKey
   VERIFY_PORT=8765
   ```

4. 使用 `sudoedit /etc/telegram-relay-bot/turnstile-secret` 写入一行 Secret Key
   （不加变量名），然后执行：

   ```bash
   sudo chown root:root /etc/telegram-relay-bot/turnstile-secret
   sudo chmod 600 /etc/telegram-relay-bot/turnstile-secret /etc/telegram-relay-bot/config
   ```

5. 将 `deploy/verify.Caddyfile.example` 的站点块加入现有 Caddyfile，替换域名，
   保留其他站点。使用 `sudo caddy validate --config /etc/caddy/Caddyfile` 检查，
   再执行 `sudo systemctl reload caddy`。如果使用 Cloudflare DNS 代理，SSL/TLS
   使用 **Full (strict)**，不要对验证 API 添加缓存规则。
6. 再运行 `sudo bash scripts/install.sh`，安装脚本会启用验证服务。

```bash
sudo systemctl status telegram-relay-verify telegram-relay-bot
sudo journalctl -u telegram-relay-verify --since today
```

验证服务只监听 `127.0.0.1:8765`，通过 systemd credential 读取 Secret Key。
Python 以 `-E -s -B` 运行：忽略 Python 环境变量、用户 site-packages，禁止写入
字节码；模块仅从 root 管理的程序目录和系统库加载。

### 验证规则和故障排查

- 验证链接 10 分钟有效，绑定生成链接的 Telegram 用户，成功后只使用一次。
  转发链接只能帮助原用户验证，不能给打开者的其他账号授权。
- 同一用户每 60 秒最多收到一个新链接；连续发送 `/start` 会受到此冷却限制。
- 每个链接最多校验 5 次，每次至少间隔 3 秒；超限后回到机器人获取新链接。
- 服务端检查 Cloudflare 的 `success`、hostname、action 和 cdata。
  验证失败、服务异常、链接过期时，不会转交未验证用户的消息。
- `/block` 会撤销已有验证和未使用的链接；解封后需要重新验证。
- 验证通过后请重新发送留言。网页直接打开时会提示使用机器人提供的链接。
- 遇到无法加载验证时，用系统浏览器打开机器人给出的**原链接**。
  页面使用 URL fragment 传递随机凭据，加载后从地址栏移除，避免进入访问日志。
- SQLite 增加验证到期时间和挑战摘要，不保存用户消息、网页 IP 或 Cloudflare Token。
  过期记录随机器人每日维护清理。

### 更新与回退

更新前备份 `/opt/telegram-relay-bot`、systemd 服务文件、配置、Caddyfile，
使用 SQLite backup API 备份数据库。更新代码后运行安装脚本，保留现有数据。
回退时停止 `telegram-relay-verify`，恢复旧程序、服务文件、配置和 Caddyfile，
执行 `systemctl daemon-reload` 并重启机器人。新增数据库表可保留；不要为了
回退程序覆盖实时数据库，否则可能丢失更新后的消息映射。

## 服务管理命令

```bash
# 查看状态
sudo systemctl status telegram-relay-bot

# 查看当天日志（日志不记录消息正文或 Token）
sudo journalctl -u telegram-relay-bot --since today

# 重启
sudo systemctl restart telegram-relay-bot

# 停止 / 启动
sudo systemctl stop telegram-relay-bot
sudo systemctl start telegram-relay-bot
```

## 更新

```bash
cd telegram-private-relay-bot
git pull --ff-only
sudo bash scripts/install.sh
```

更新安装会保留 `/etc/telegram-relay-bot` 中的凭据和 `/var/lib/telegram-relay-bot` 中的数据库。

## 修改配置或 Token

```bash
sudoedit /etc/telegram-relay-bot/config
sudoedit /etc/telegram-relay-bot/token
sudo chmod 600 /etc/telegram-relay-bot/config /etc/telegram-relay-bot/token
sudo systemctl restart telegram-relay-bot
```

配置示例：

```ini
OWNER_CHAT_ID=123456789
MAPPING_RETENTION_DAYS=30
RATE_LIMIT_COUNT=20
RATE_LIMIT_WINDOW_SECONDS=60
LOG_LEVEL=INFO
```

Token 文件只能包含一行 Bot Token，不要添加 `BOT_TOKEN=` 前缀。

## 卸载

```bash
sudo bash scripts/uninstall.sh
```

卸载脚本默认保留凭据和数据库，并显示手动彻底删除它们的命令，避免误删。重新安装时可以继续使用原配置。

## 本地测试

```bash
python3 -m unittest discover -s . -p 'test_*.py' -v
```

## 数据与安全边界

- SQLite 只保存 Telegram 数字 ID、消息 ID、封禁记录、限速状态和 update offset。
- 回复映射默认 30 天后清理；封禁记录保留至管理员解封。
- 机器人只处理私聊，并忽略其他机器人发送的消息。
- Telegram 不允许 `copyMessage` 复制服务消息、付费媒体、赠品、发票及部分测验，此类内容会被拒绝。
- 如果 Token 泄露，请立即通过 @BotFather 撤销并生成新 Token，再更新服务器 Token 文件。

更多安全建议见 [SECURITY.md](SECURITY.md)。
