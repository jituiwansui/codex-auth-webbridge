# codex-auth-webbridge

借用浏览器中**已登录的 ChatGPT 会话**，通过 [Kimi WebBridge](https://www.kimi.com/zh-cn/features/webbridge) 一键生成 Codex 本地登录凭证 `auth.json`。
不新开独立浏览器、不重新输入账号密码、不经过任何第三方服务器 —— 全程本地、标准 OAuth 流程，拿到的是带 `refresh_token` 的正规凭证。

## 原理

与官方 `codex login` 完全一致的 OAuth 授权码流程（PKCE），区别只在浏览器由 WebBridge 驱动：

1. 本机 `1455` 端口临时启动 OAuth 回调服务；
2. 通过 WebBridge 在你当前已登录 ChatGPT 的浏览器里打开 OpenAI 授权链接，SSO 会话直接放行；
3. 授权过程中的"登录 / 继续 / 授权"确认按钮由程序**自动点击**（仅限 OpenAI 官方域）；
4. 浏览器跳回 `http://localhost:1455/auth/callback`，本地拿到授权码；
5. 用 PKCE verifier 换取 `id_token` / `access_token` / `refresh_token`；
6. 按官方格式写入 `auth.json`。

## 特性

- **免重新登录**：复用浏览器现有 ChatGPT 登录态，SSO 自动完成授权
- **自动点击**：授权页的确认按钮自动处理，全程无人值守
- **官方格式**：生成的 `auth.json` 与 `codex login` 产物字段一致（含 `refresh_token`，并尽力交换 `OPENAI_API_KEY`）
- **纯标准库**：Python 3.8+，零第三方依赖
- **安全兜底**：旧凭证自动备份；绝不代填账号密码，出现密码输入框时自动点击停用

## 环境要求

- Python 3.8+
- [Kimi WebBridge](https://www.kimi.com/zh-cn/features/webbridge) 守护进程 + 浏览器扩展
- 浏览器中已登录 ChatGPT

## 使用

```bash
python codex_auth.py                 # 生成/刷新程序根目录下的 auth.json
python codex_auth.py --output "%USERPROFILE%\.codex\auth.json"   # 直接写到 codex 默认位置（Windows）
python codex_auth.py --output ~/.codex/auth.json                 # macOS / Linux
python codex_auth.py --timeout 600   # 调整等待授权的最长时间（秒，默认 900）
```

凭证过期（约 10 天）或 codex 提示重新登录时，重跑一次即可。

## 注意事项

- `auth.json` 包含完整登录凭证，**不要提交到 git、不要发给任何人**（本仓库 `.gitignore` 已默认忽略）
- `auth.openai.com` 的 Cloudflare 人机校验可能需要等待 1~2 分钟，属正常现象；若浏览器停在验证页，切到该标签页看一眼即可
- 自动点击只作用于 `*.openai.com` / `*.chatgpt.com` 域内的按钮，并自动排除"退出登录 / 注册 / 用另一个账户登录"等选项

## 致谢

思路借鉴自 [chengchengking/codex-](https://github.com/chengchengking/codex-)（网页 token 提取 + Cloudflare Worker 嫁接方案）。本项目改为本地标准 OAuth 流程，无需第三方 Worker，且凭证带 `refresh_token` 可自动续期。
