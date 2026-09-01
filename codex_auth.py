#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
codex_auth.py — 借用浏览器中已登录的 ChatGPT 会话，生成 Codex 本地登录凭证 auth.json

思路（借鉴 codex- 项目，但全程本地、走标准 OAuth，不需要任何第三方 Worker）：

  1. 在本机 1455 端口临时起一个 OAuth 回调服务（与官方 `codex login` 完全一致）；
  2. 通过 Kimi WebBridge，在【你当前已登录 ChatGPT 的浏览器】里打开 OpenAI 授权链接
     —— 不新开独立浏览器、不重新输入账号密码，SSO 会话直接放行；
  3. 授权过程中的"登录 / 继续 / 授权"确认按钮由程序自动点击（仅限 OpenAI 官方域）；
  4. 授权后浏览器自动跳回 http://localhost:1455/auth/callback，本地拿到授权码 code；
  5. 本地用 PKCE verifier 换取 id_token / access_token / refresh_token；
  6. 按官方格式写入 auth.json（默认输出到程序根目录，即本脚本所在目录）。

本程序只负责"生成凭证"：确认按钮会自动点击，但绝不代填任何账号密码；
如果浏览器里没有有效的 ChatGPT 登录态，流程会停在账号密码页，等待超时后退出。

依赖：Python 3.8+（纯标准库）；Kimi WebBridge 守护进程（http://127.0.0.1:10086）
以及对应的浏览器扩展，且浏览器中已登录 ChatGPT。

用法：
  python codex_auth.py                 # 生成/刷新程序根目录下的 auth.json
  python codex_auth.py --output "%USERPROFILE%\.codex\auth.json"  # 直接写到 codex 默认位置
  python codex_auth.py --timeout 600   # 等待授权的最长时间（秒）
"""

import argparse
import base64
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Windows 控制台默认 GBK，强制 UTF-8 输出避免中文乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# ---- 与官方 codex login 相同的 OAuth 参数（见 codex-rs/login/src/server.rs）----
ISSUER = "https://auth.openai.com"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
ORIGINATOR = "codex_cli_rs"
SCOPE = "openid profile email offline_access api.connectors.read api.connectors.invoke"
CALLBACK_PATH = "/auth/callback"
PORTS = (1455, 1457)  # Codex CLI 回调地址白名单里的两个端口

WEBBRIDGE = "http://127.0.0.1:10086"
WEBBRIDGE_SESSION = "codex-auth"
WEBBRIDGE_GROUP = "Codex 登录授权"


def log(msg: str) -> None:
    print(msg, flush=True)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def make_pkce():
    verifier = b64url(secrets.token_bytes(64))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def jwt_payload(jwt: str) -> dict:
    parts = jwt.split(".")
    if len(parts) != 3:
        raise ValueError("不是合法的 JWT")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(payload.encode()))


# ------------------------------ WebBridge ------------------------------

def webbridge(action: str, args: dict, timeout: float = 60.0) -> dict:
    body = json.dumps(
        {"action": action, "args": args, "session": WEBBRIDGE_SESSION}
    ).encode("utf-8")
    req = urllib.request.Request(
        WEBBRIDGE + "/command",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# 自动点击授权/登录确认按钮：只在 OpenAI 官方域内生效；有密码输入框时不动；
# 只点按钮，绝不填写任何输入框（isTrusted 校验严格的页面点不动时会静默跳过）。
_AUTO_CLICK_JS = r"""
(() => {
  const host = location.hostname;
  if (!/(^|\.)openai\.com$/.test(host) && !/(^|\.)chatgpt\.com$/.test(host)) return null;
  if (document.querySelector('input[type="password"]')) return null;
  const WANT = ['log in','login','sign in','continue','allow','authorize','accept','agree',
                '登录','继续','允许','授权','同意','接受'];
  const SKIP = ['log out','sign out','logout','sign up','register','退出','注册',
                'another account','其他账户','另一个账户','different account','切换'];
  const els = [...document.querySelectorAll('button, [role="button"], input[type="submit"], a')];
  for (const el of els) {
    const t = ((el.innerText || el.value || '') + '').replace(/\s+/g, ' ').trim().toLowerCase();
    if (!t || t.length > 60) continue;
    if (!(el.offsetWidth || el.offsetHeight)) continue;
    if (SKIP.some(s => t.includes(s))) continue;
    if (!WANT.some(w => t.includes(w))) continue;
    // 同一页面同一按钮 8 秒内只点一次，避免对无响应按钮连点
    const last = window.__codexLastClick;
    if (last && last.href === location.href && last.text === t && Date.now() - last.at < 8000) return null;
    window.__codexLastClick = {href: location.href, text: t, at: Date.now()};
    el.click();
    return 'clicked: ' + t.slice(0, 40);
  }
  return null;
})()
"""


def auto_click_once():
    """在授权标签页里自动点一次"登录/继续/授权"类按钮；返回动作描述或 None。"""
    try:
        resp = webbridge("evaluate", {"code": _AUTO_CLICK_JS}, timeout=10)
        return (resp.get("data") or {}).get("value")
    except Exception:
        return None


def ensure_webbridge() -> None:
    """确认 WebBridge 守护进程可用；不可用则尝试拉起，仍不行则退出并给出指引。"""
    try:
        webbridge("list_tabs", {}, timeout=5)
        return
    except Exception:
        pass
    exe = Path.home() / ".kimi-webbridge" / "bin" / (
        "kimi-webbridge.exe" if os.name == "nt" else "kimi-webbridge"
    )
    if exe.exists():
        log("[1/5] WebBridge 守护进程未响应，正在启动…")
        subprocess.Popen(
            [str(exe), "start"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(20):
            time.sleep(0.5)
            try:
                webbridge("list_tabs", {}, timeout=5)
                return
            except Exception:
                continue
    sys.exit(
        "无法连接 Kimi WebBridge（http://127.0.0.1:10086）。\n"
        "请先安装/启动 WebBridge 及其浏览器扩展："
        "https://www.kimi.com/zh-cn/features/webbridge"
    )


# --------------------------- OAuth 回调服务 ---------------------------

class _Callback:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.code = None
        self.error = None


def _page(title: str, text: str) -> bytes:
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<title>{title}</title></head>"
        "<body style=\"margin:0;height:100vh;display:flex;align-items:center;"
        "justify-content:center;font-family:system-ui,sans-serif;background:#f7f7f8\">"
        f"<div style=\"text-align:center\"><h2>{title}</h2><p>{text}</p></div>"
        "</body></html>"
    ).encode("utf-8")


def _make_handler(expected_state: str, cb: _Callback):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != CALLBACK_PATH:
                self.send_error(404)
                return
            params = dict(urllib.parse.parse_qsl(parsed.query))
            if params.get("state") != expected_state:
                self._reply(400, "State mismatch", "授权状态校验失败，请重试。")
                return
            if params.get("error"):
                cb.error = params.get("error_description") or params["error"]
                self._reply(200, "授权未完成", cb.error)
                cb.event.set()
                return
            code = params.get("code")
            if not code:
                self._reply(400, "缺少授权码", "回调中没有 code 参数。")
                return
            cb.code = code
            self._reply(200, "Codex 授权成功", "登录凭证正在写入本地，你可以关闭这个标签页了。")
            cb.event.set()

        def _reply(self, status: int, title: str, text: str):
            body = _page(title, text)
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # 保持终端安静
            pass

    return Handler


def start_callback_server(state: str, cb: _Callback):
    """在 1455/1457 端口启动回调服务，返回 (httpd, port)。"""
    last_err = None
    for port in PORTS:
        try:
            httpd = ThreadingHTTPServer(
                ("127.0.0.1", port), _make_handler(state, cb)
            )
            httpd.daemon_threads = True
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            return httpd, port
        except OSError as e:
            last_err = e
    sys.exit(f"端口 {PORTS} 均被占用，无法启动回调服务：{last_err}")


# ------------------------------ token 交换 ------------------------------

def post_form(url: str, data: dict, timeout: float = 60.0) -> dict:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e


def exchange_code(code: str, redirect_uri: str, verifier: str) -> dict:
    return post_form(
        ISSUER + "/oauth/token",
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
        },
    )


def try_obtain_api_key(id_token: str):
    """官方 login 还会用 id_token 换一个 API key 写进 OPENAI_API_KEY；失败可忽略。"""
    try:
        resp = post_form(
            ISSUER + "/oauth/token",
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "client_id": CLIENT_ID,
                "requested_token": "openai-api-key",
                "subject_token": id_token,
                "subject_token_type": "urn:ietf:params:oauth:token-type:id_token",
            },
        )
        return resp.get("access_token")
    except Exception:
        return None


# ------------------------------ 主流程 ------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="借用浏览器中已登录的 ChatGPT 会话，生成 Codex 登录凭证 auth.json"
    )
    ap.add_argument("--timeout", type=int, default=900, help="等待浏览器授权的最长时间（秒），默认 900")
    ap.add_argument(
        "--output",
        default=None,
        help="auth.json 输出路径，默认写入程序根目录（本脚本所在目录）",
    )
    args = ap.parse_args()
    auth_file = (
        Path(args.output)
        if args.output
        else Path(__file__).resolve().parent / "auth.json"
    )

    log("[1/5] 检查 Kimi WebBridge…")
    ensure_webbridge()

    verifier, challenge = make_pkce()
    state = b64url(secrets.token_bytes(32))
    cb = _Callback()
    httpd, port = start_callback_server(state, cb)
    redirect_uri = f"http://localhost:{port}{CALLBACK_PATH}"

    auth_url = ISSUER + "/oauth/authorize?" + urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "state": state,
            "originator": ORIGINATOR,
        }
    )

    log(f"[2/5] 本地回调服务已就绪（http://localhost:{port}），正在你的浏览器中打开授权页…")
    nav = {}

    def do_navigate():
        try:
            nav["resp"] = webbridge(
                "navigate",
                # 复用本会话的标签页（首次调用会自动新开一个）
                {"url": auth_url, "newTab": False, "group_title": WEBBRIDGE_GROUP},
                timeout=180,
            )
        except Exception as e:  # 授权页加载慢时 navigate 可能超时，不代表流程失败
            nav["error"] = e

    nav_started = time.monotonic()
    nav_thread = threading.Thread(target=do_navigate, daemon=True)
    nav_thread.start()
    log(f"      浏览器标签组：{WEBBRIDGE_GROUP}。授权页的登录/确认按钮会自动点击，无需手动操作。")

    log("[3/5] 等待授权回调…（auth.openai.com 的人机校验可能较慢，请耐心等一下）")
    deadline = time.monotonic() + args.timeout
    last_report = 0
    last_click = 0.0
    try:
        while time.monotonic() < deadline:
            if cb.event.wait(timeout=0.5):
                break
            # navigate 很快就报错（如扩展未连接）→ 直接失败，不干等
            if nav.get("error") and time.monotonic() - nav_started < 20:
                httpd.shutdown()
                sys.exit(f"WebBridge 打开授权页失败：{nav['error']}\n请确认浏览器扩展已连接后重试。")
            # 自动点击授权/登录确认按钮（每 3 秒巡检一次页面）
            now = time.monotonic()
            if now - nav_started >= 5 and now - last_click >= 3:
                last_click = now
                action = auto_click_once()
                if action:
                    log(f"      已自动点击：{action}")
            elapsed = int(now - nav_started)
            if elapsed // 15 > last_report:
                last_report = elapsed // 15
                log(f"      已等待 {elapsed}s…若浏览器停在验证/确认页，请切换到该标签页完成即可")
        else:
            httpd.shutdown()
            sys.exit(
                "等待授权超时。可能原因：浏览器未登录 ChatGPT（程序不会替你登录），"
                "或授权页停留在确认界面。请检查后重试。"
            )
    except KeyboardInterrupt:
        httpd.shutdown()
        sys.exit("已取消。")
    finally:
        httpd.shutdown()

    if cb.error:
        sys.exit(f"授权被拒绝：{cb.error}")

    log("[4/5] 拿到授权码，正在换取 token…")
    try:
        tokens = exchange_code(cb.code, redirect_uri, verifier)
    except Exception as e:
        sys.exit(f"token 交换失败：{e}")

    id_token = tokens["id_token"]
    access_token = tokens["access_token"]
    refresh_token = tokens["refresh_token"]

    claims = jwt_payload(id_token)
    auth_claims = claims.get("https://api.openai.com/auth") or {}
    account_id = auth_claims.get("chatgpt_account_id")
    email = claims.get("email") or (claims.get("https://api.openai.com/profile") or {}).get("email")
    plan = auth_claims.get("chatgpt_plan_type")

    api_key = try_obtain_api_key(id_token)

    log("[5/5] 写入 auth.json…")
    old = None
    if auth_file.exists():
        backup = auth_file.with_suffix(
            ".json.bak-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        )
        shutil.copy2(auth_file, backup)
        log(f"      已备份原凭证到 {backup}")
        try:
            old = json.loads(auth_file.read_text(encoding="utf-8"))
        except Exception:
            old = None

    auth = {
        "OPENAI_API_KEY": api_key or (old or {}).get("OPENAI_API_KEY"),
        "tokens": {
            "id_token": id_token,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "account_id": account_id,
        },
        "last_refresh": datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
    }

    auth_file.parent.mkdir(parents=True, exist_ok=True)
    auth_file.write_text(
        json.dumps(auth, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    if os.name != "nt":
        os.chmod(auth_file, 0o600)

    try:
        exp = datetime.fromtimestamp(jwt_payload(access_token)["exp"], tz=timezone.utc)
        exp_text = exp.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        exp_text = "未知"

    log("")
    log("完成。凭证已生成：")
    log(f"  文件      : {auth_file}")
    log(f"  账号      : {email or '未知'}（plan: {plan or '未知'}）")
    log(f"  account_id: {account_id or '未知'}")
    log(f"  token 有效期至: {exp_text}")
    default_auth = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"
    log("")
    if auth_file.resolve() != default_auth.resolve():
        log(f"提示：codex 默认读取 {default_auth}，如需直接生效可执行：")
        log(f"  python codex_auth.py --output \"{default_auth}\"")
        log("或将生成的 auth.json 复制到该位置。")
    log("凭证过期后重跑一次本脚本即可。")


if __name__ == "__main__":
    main()
