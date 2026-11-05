#!/usr/bin/env python3
"""codex 的 ACP 入口：配置模型和登录方式，转发 unix socket ↔ codex-acp 的标准输入输出。

- 没有官方登录态时，模型走模型网关：写 config.toml 指向网关，并用 model_catalog_json 补上网关
  模型的元数据；缺了它，Codex 每一轮都会把 "Model metadata … not found" 警告当回复发出来。
  API key 只以环境变量名引用，不把值写进配置。
- $CODEX_HOME/auth.json 里有 ChatGPT 登录态时不写网关配置：模型请求直接发给官方，不把订阅
  请求送进模型网关。配置在容器启动时写，登录成功后入口让容器退出重启，新配置即生效。
- config.toml 只放行 Wonton MCP 这一个服务器（default_tools_approval_mode = "approve"）：
  Codex 不再为自己的确认发卡片，工具确认只归核心总线一张。实际连接由桥在 session/new 传入
  （codex-acp 会用它覆盖同名配置项），这里的条目只提供审批配置。
- codex-acp 的 ChatGPT 登录方式在本机起回调服务器等浏览器，无浏览器的容器里链接发不出去；
  入口把 initialize 里的登录方式改写成终端方式（codex login --device-auth），桥把链接和设备码
  转发进聊天。改写只发生在转发流里，不改 codex-acp 本身。
"""
import asyncio
import json
import os
import re
import signal
import socket
from pathlib import Path

SOCKET = Path(os.environ.get("WONTON_ACP_SOCKET", "/data/run/acp.sock"))
WORKSPACE = Path(os.environ.get("WONTON_AGENT_WORKSPACE", "/workspace"))
AGENT_HOME = Path(os.environ.get("HOME", "/data/agent/home"))
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(AGENT_HOME / ".codex")))
CODEX_BIN = "/usr/local/bin/codex"
PROMPT_MD = Path("/usr/local/share/wonton-codex/prompt.md")
SKILL_SOURCE = Path("/usr/local/share/wonton-codex/skills")

# 桥把终端登录的链接和提示原样发进聊天：去掉终端配色码。
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def install_skills() -> None:
    # HOME 是挂载点，包里的 configs 挂不进来；每次启动按镜像里的版本覆盖，跟着插件版本走。
    for source in SKILL_SOURCE.rglob("*"):
        if not source.is_file():
            continue
        target = AGENT_HOME / ".agents" / "skills" / source.relative_to(SKILL_SOURCE)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())


def chatgpt_logged_in() -> bool:
    try:
        return "tokens" in json.loads((CODEX_HOME / "auth.json").read_text())
    except (OSError, ValueError):
        return False


def write_config(mcp_url: str) -> None:
    """写 $CODEX_HOME/config.toml：MCP 审批配置总要写；模型按有没有官方登录态二选一。"""
    lines = [
        "# 由 wonton-plugin-codex 的入口脚本管理，手改会被覆盖。",
        "",
        "[mcp_servers.wonton]",
        f'url = "{mcp_url}"',
        "enabled = false",
        'default_tools_approval_mode = "approve"',
        "",
    ]
    if not chatgpt_logged_in():
        model = os.environ["OPENAI_MODEL"]
        base_url = os.environ["OPENAI_BASE_URL"]
        if not model.strip() or not base_url.strip() or not os.environ["OPENAI_API_KEY"].strip():
            raise ValueError("OPENAI_MODEL、OPENAI_BASE_URL 和 OPENAI_API_KEY 不能为空")
        catalog = CODEX_HOME / "model-catalog.json"
        write_catalog(catalog, model)
        lines = [
            "# 由 wonton-plugin-codex 的入口脚本管理，手改会被覆盖。",
            'model_provider = "wonton"',
            f'model = "{model}"',
            f'model_catalog_json = "{catalog}"',
            "",
            "[model_providers.wonton]",
            'name = "Wonton"',
            f'base_url = "{base_url}"',
            'env_key = "OPENAI_API_KEY"',
            'wire_api = "responses"',
            "",
            *lines[2:],
        ]
    else:
        lines[1] = "# 已有 ChatGPT 登录态：模型请求直接发给官方，不经模型网关。"
    (CODEX_HOME / "config.toml").write_text("\n".join(lines))


def write_catalog(path: Path, model: str) -> None:
    """网关模型的元数据，字段照 Codex 对未知模型的兜底元数据填（见 NOTICE 的来源）。"""
    entry = {
        "slug": model,
        "display_name": model,
        "supported_reasoning_levels": [],
        "shell_type": "default",
        "visibility": "none",
        "supported_in_api": True,
        "priority": 99,
        "base_instructions": PROMPT_MD.read_text(),
        "supports_reasoning_summaries": False,
        "support_verbosity": False,
        "truncation_policy": {"mode": "bytes", "limit": 10000},
        "supports_parallel_tool_calls": False,
        "experimental_supported_tools": [],
        "context_window": 272000,
        "max_context_window": 272000,
    }
    path.write_text(json.dumps({"models": [entry]}))


def rewrite_auth_methods(line: str) -> str:
    try:
        message = json.loads(line)
    except ValueError:
        return line
    if isinstance(message, dict) and "authMethods" in message:
        message["authMethods"] = [
            {
                "id": "chatgpt",
                "name": "ChatGPT 账号（设备码）",
                "type": "terminal",
                "args": ["login", "--device-auth"],
            }
        ]
        return json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    return line


async def relay(client: socket.socket, process: asyncio.subprocess.Process) -> None:
    loop = asyncio.get_running_loop()

    async def socket_to_agent() -> None:
        while True:
            data = await loop.sock_recv(client, 512 * 1024)
            if not data:
                return
            if process.stdin:
                process.stdin.write(data)
                await process.stdin.drain()

    async def agent_to_socket() -> None:
        buffer = b""
        while True:
            if process.stdout is None:
                return
            data = await process.stdout.read(512 * 1024)
            if not data:
                return
            buffer += data
            *lines, buffer = buffer.split(b"\n")
            payload = "".join(
                rewrite_auth_methods(line.decode(errors="replace")) + "\n" for line in lines
            ).encode()
            await loop.sock_sendall(client, payload)

    tasks = [asyncio.create_task(socket_to_agent()), asyncio.create_task(agent_to_socket())]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in done:
        task.result()


async def serve_login(listener: socket.socket) -> None:
    """终端类登录：桥连这个 socket，入口用 Codex CLI 启动登录进程并转发 stdio。

    协议（与桥 login/terminal.rs 约定）：桥先发一行 JSON {"methodId","args","env"}；
    之后桥发的每行文本写入登录进程 stdin；登录进程的 stdout/stderr 每行回 {"out"}/{"err"}，
    结束时回 {"exit": 数字}。退出码 0 表示成功，桥随后退出重连。
    """
    loop = asyncio.get_running_loop()
    while True:
        client, _ = await loop.sock_accept(listener)
        try:
            await relay_login(client, loop)
        finally:
            client.close()


async def relay_login(client: socket.socket, loop: asyncio.AbstractEventLoop) -> None:
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), client.makefile("rb"))
    request_line = await reader.readline()
    if not request_line:
        return
    request = json.loads(request_line)
    process = await asyncio.create_subprocess_exec(
        CODEX_BIN, *request.get("args", []),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **request.get("env", {})},
        cwd=WORKSPACE,
    )
    stdin = process.stdin

    async def to_socket(frame: dict) -> None:
        await loop.sock_sendall(client, (json.dumps(frame, ensure_ascii=False) + "\n").encode())

    async def pump_output(stream, key) -> None:
        assert stream is not None
        while line := await stream.readline():
            text = ANSI.sub("", line.decode(errors="replace")).rstrip("\n")
            await to_socket({key: text})

    async def pump_input() -> None:
        assert stdin is not None
        while line := await reader.readline():
            stdin.write(line)
            await stdin.drain()

    stdout_task = asyncio.create_task(pump_output(process.stdout, "out"))
    stderr_task = asyncio.create_task(pump_output(process.stderr, "err"))
    input_task = asyncio.create_task(pump_input())
    exit_task = asyncio.create_task(process.wait())
    done, _ = await asyncio.wait({exit_task, input_task}, return_when=asyncio.FIRST_COMPLETED)
    if input_task in done:
        # 桥断开（取消、超时、桥退出）：登录进程可能还在等 stdin，必须结束并收回，
        # 否则串行的 serve_login 会一直卡在这次登录上。
        stdin.close()
        try:
            code = await asyncio.wait_for(process.wait(), 2)
        except asyncio.TimeoutError:
            process.terminate()
            try:
                code = await asyncio.wait_for(process.wait(), 2)
            except asyncio.TimeoutError:
                process.kill()
                code = await process.wait()
    else:
        code = exit_task.result()
    input_task.cancel()
    await asyncio.gather(stdout_task, stderr_task, exit_task, return_exceptions=True)
    try:
        await to_socket({"exit": code})
    except OSError:
        return
    if code == 0:
        # 登录态已写入数据卷。让容器退出重启，入口按新登录态重写 config.toml，
        # 模型请求从这一刻起走官方而不是网关；桥也在登录成功后退出重连。
        await asyncio.sleep(2)
        os._exit(0)


async def main() -> None:
    for directory in (AGENT_HOME, WORKSPACE, SOCKET.parent, CODEX_HOME):
        directory.mkdir(parents=True, exist_ok=True)
    install_skills()
    write_config(os.environ["WONTON_MCP_URL"])
    try:
        SOCKET.unlink()
    except FileNotFoundError:
        pass
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(SOCKET))
    os.chmod(SOCKET, 0o600)
    listener.listen(8)
    listener.setblocking(False)
    login_socket = SOCKET.with_suffix(".login.sock")
    for path in (SOCKET, login_socket):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    login_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    login_listener.bind(str(login_socket))
    os.chmod(login_socket, 0o600)
    login_listener.listen(2)
    login_listener.setblocking(False)
    login_task = asyncio.create_task(serve_login(login_listener))
    process = await asyncio.create_subprocess_exec(
        "/usr/local/bin/codex-acp",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=None,
        cwd=WORKSPACE,
    )
    loop = asyncio.get_running_loop()
    try:
        while process.returncode is None:
            accept_task = asyncio.create_task(loop.sock_accept(listener))
            exit_task = asyncio.create_task(process.wait())
            done, pending = await asyncio.wait(
                [accept_task, exit_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if exit_task in done:
                break
            client, _ = accept_task.result()
            client.setblocking(False)
            try:
                await relay(client, process)
            finally:
                client.close()
    finally:
        if process.returncode is None:
            process.send_signal(signal.SIGTERM)
        await process.wait()
        login_task.cancel()
        await asyncio.gather(login_task, return_exceptions=True)
        listener.close()
        login_listener.close()
        for path in (SOCKET, login_socket):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
