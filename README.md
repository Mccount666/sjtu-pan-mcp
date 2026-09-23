# sjtu-pan-mcp — 交大云盘 MCP 工具

> ⚠️ **AI 辅助生成，谨慎使用**

让 AI agent 直接浏览上海交通大学云盘（[pan.sjtu.edu.cn](https://pan.sjtu.edu.cn)）里的文件，并下载到本地。

基于对交大云盘 Web 前端实际接口的逆向（两层 API：`user_token` 会话层 → 空间 `access_token` 层），**不接触账号密码**，只复用一次扫码登录产生的会话。

适用任何支持 MCP 的客户端：ZCode、Claude Code、Claude Desktop、Cherry Studio 等。

## 功能

| 工具 | 作用 |
| --- | --- |
| `pan_login` | 弹出浏览器窗口扫码登录（jAccount），自动保存登录态 |
| `pan_account` | 账户信息与登录态自检（用户、组织、空间列表） |
| `pan_list_spaces` | 列出全部空间（个人空间/团队空间） |
| `pan_list_dir` | 列出某个目录下的文件和子目录 |
| `pan_search` | 全局搜索文件（关键词） |
| `pan_file_info` | 查看单个文件/目录的元信息 |
| `pan_download_file` | 下载单个文件到本地 |
| `pan_download_dir` | 递归下载整个目录（保留子目录结构） |

## 安装

```bash
pip install "sjtu-pan-mcp[gui,cookie]"
```

或从源码安装：

```bash
git clone https://github.com/Mccount666/sjtu-pan-mcp.git
cd sjtu-pan-mcp
pip install ".[gui,cookie]"
```

- `gui`：弹窗扫码登录用的 Playwright（装完执行一次 `playwright install chromium`）
- `cookie`：可选的“从本地浏览器 cookie 库提取登录态”备选路径

安装后得到命令行入口 `sjtu-pan-mcp`。

## 配置（三步）

### 第一步：登录

交大云盘用 jAccount 登录。本工具不接触你的账号密码——运行：

```bash
sjtu-pan-mcp login
```

会发生这些事：

1. 自动弹出一个浏览器窗口，打开交大云盘登录页并自动跳到 jAccount 统一身份认证页
2. 你用**交我办 App 或微信扫码**（也可以账号密码登录）
3. 登录态被自动抓取并校验，写入配置文件，窗口自动关闭

也可以让 agent 代劳：对 agent 说“登录交大云盘”，它会调用 `pan_login` 工具，同样弹窗，并阻塞等待你扫码完成。

如果弹窗登录不可用（比如无桌面环境），用手动兜底：浏览器登录 pan.sjtu.edu.cn 后，F12 → 应用程序/Application → Cookies → `https://pan.sjtu.edu.cn` → 复制 `USER_TOKEN` 的值，写入配置文件（见下一步）的 `user_token` 字段。

### 第二步：确认登录态

```bash
sjtu-pan-mcp status
```

输出账户、组织和空间列表即成功。登录态默认存放在：

```
~/.sjtu-pan-mcp/config.json        # Windows: C:\Users\<你>\.sjtu-pan-mcp\config.json
```

完整配置示例（仓库里的 `config.example.json`）：

```json
{
  "user_token": "登录后自动写入；也可手动粘贴 USER_TOKEN cookie 值",
  "default_download_dir": "~/Downloads/sjtu-pan",
  "organization_id": 0,
  "allowed_url_hosts": [
    "pan.sjtu.edu.cn",
    ".sjtu.edu.cn",
    ".myqcloud.com",
    ".qcloud.com"
  ]
}
```

| 配置项 | 说明 |
| --- | --- |
| `user_token` | 登录态（必填）。`sjtu-pan-mcp login` 会自动写入 |
| `default_download_dir` | 默认下载目录，不填则为 `~/Downloads/sjtu-pan` |
| `organization_id` | 固定组织（多组织账号用，0 = 自动选择） |
| `allowed_url_hosts` | 出站请求主机白名单（安全加固，见下） |

也可以用环境变量（优先级高于配置文件）：

| 环境变量 | 作用 |
| --- | --- |
| `SJTU_PAN_USER_TOKEN` | 直接传登录态 |
| `SJTU_PAN_CONFIG` | 指定配置文件路径 |
| `SJTU_PAN_HOME` | 指定配置目录 |

### 第三步：接入 MCP 客户端

在客户端的 MCP 配置里加一个 stdio 服务器（以安装后的可执行文件路径为准）：

```json
{
  "mcpServers": {
    "sjtu-pan": {
      "command": "sjtu-pan-mcp",
      "args": [],
      "env": {}
    }
  }
}
```

Windows 上如果 `sjtu-pan-mcp` 不在 PATH，写成完整路径，例如：

```json
{
  "mcpServers": {
    "sjtu-pan": {
      "command": "C:\\Users\\<你>\\AppData\\Local\\Programs\\Python\\Python313\\Scripts\\sjtu-pan-mcp.exe",
      "args": [],
      "env": {}
    }
  }
}
```

登录态在 `~/.sjtu-pan-mcp/config.json` 里全局共享，多个客户端无需分别登录。配置好后重启客户端，对 agent 说“看看我交大云盘里有什么”即可。

### token 过期怎么办

登录态有效期取决于登录时是否勾选“记住登录态”（约 10 天，否则约 2 小时）。过期时任何工具会返回 `InvalidUserToken`，重新跑一次 `sjtu-pan-mcp login` 或让 agent 调 `pan_login` 扫码即可。

## 安全约束

- 所有出站请求仅允许 `http/https`，且目标主机必须在白名单内（默认 `pan.sjtu.edu.cn`、`.sjtu.edu.cn` 及其 S3 存储域名）
- 请求前校验主机名及其解析 IP，拒绝 localhost、环回、私有、链路本地、保留和多播地址——防止 SSRF 打到内网
- 重定向逐跳重新校验；远程文件名会做净化（剥离路径分隔符、NTFS ADS、保留设备名），且本地落盘路径被限制在目标目录内
- 本项目不存储、不上传任何账号密码；配置文件中只有会话 token，`config.json` 已被 `.gitignore` 排除

## 使用示例

- “看看我交大云盘个人空间根目录有什么” → `pan_list_dir`
- “把 /论文/2026 整个目录下载到 D:\pan-download” → `pan_download_dir`
- “搜一下云盘里叫 paper 的文件” → `pan_search`
- “下载 /论文/2026/paper.pdf 到桌面” → `pan_download_file`

## 开发备注

- 项目结构：`src/sjtu_pan_mcp/`（`client.py` 两层 API 客户端、`login.py` 弹窗登录、`auth.py` 登录态解析、`security.py` URL 校验、`server.py` MCP 工具）
- 测试：`python smoke_test.py`（本地逻辑 + 有 token 时的真实列目录/下载）、`python smoke_mcp.py`（MCP 协议握手）、`python test_gui_login_e2e.py`（弹窗登录全链路，注入 cookie 模拟扫码）
- 已知细节：文件下载是 302 到预签名 S3 URL，跟随重定向时不能携带原 query 参数否则签名失效；`user_id` 参数仅用于流量统计
- 接口响应字段未公开文档，客户端对 JSON 形状做了容错解析；如交大云盘改版，优先改 `client.py` 里的归一化函数

## License

[MIT](LICENSE)
