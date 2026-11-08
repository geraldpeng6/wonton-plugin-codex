#!/usr/bin/env python3
"""codex 的 ACP 入口：配置模型和登录方式，转发 unix socket ↔ codex-acp 的标准输入输出。

- 模型网关可选：每次启动按 OPENAI_* 有没有值选路，整份重写 config.toml。有值时写网关配置
  （model_provider、base_url，key 只以环境变量名引用），并用 model_catalog_json 补上网关模型
  的元数据——缺了它，Codex 每一轮都会把 "Model metadata … not found" 警告当回复发出来。
  为空（网关没装、没就绪或被卸掉）时不写网关配置，也删掉残留的元数据文件：Codex 走内置
  openai provider，没有登录态时 new_session/prompt 返回 auth_required，桥把它转成“要先登录”
  的提示，容器不崩。
- $CODEX_HOME/auth.json 里有官方登录态（ChatGPT 或用户自己的 API key）时不写网关配置：模型
  请求直接发给官方，不把订阅或自备 key 的请求送进模型网关。配置在容器启动时写，登录成功后
  入口让容器退出重启，新配置即生效。
- config.toml 只放行 Wonton MCP 这一个服务器（default_tools_approval_mode = "approve"）：
  Codex 不再为自己的确认发卡片，工具确认只归核心总线一张。实际连接由桥在 session/new 传入
  （codex-acp 会用它覆盖同名配置项），这里的条目只提供审批配置。
- codex-acp 的 ChatGPT 登录方式在本机起回调服务器等浏览器，无浏览器的容器里链接发不出去；
  入口把 initialize 里的登录方式改写成终端方式：ChatGPT 用设备码（codex login --device-auth），
  自备 key 用 stdin（codex login --with-api-key），桥把链接、设备码和收到的 key 行转发进聊天。
  改写只发生在转发流里，不改 codex-acp 本身。
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


def has_own_auth() -> bool:
    """auth.json 里有 ChatGPT 登录态或用户自己的 API key：模型请求发官方，不经网关。"""
    try:
        auth = json.loads((CODEX_HOME / "auth.json").read_text())
    except (OSError, ValueError):
        return False
    return "tokens" in auth or auth.get("OPENAI_API_KEY")


def write_config(mcp_url: str) -> None:
    """写 $CODEX_HOME/config.toml：MCP 审批配置总要写；模型按网关变量和登录态三选一。

    每次启动整份重写：网关后来接入或被卸掉时，重启容器就切换，不会留着指向旧地址的配置。
    """
    lines = [
        "# 由 wonton-plugin-codex 的入口脚本管理，手改会被覆盖。",
        "",
        "[mcp_servers.wonton]",
        f'url = "{mcp_url}"',
        "enabled = false",
        'default_tools_approval_mode = "approve"',
        "",
        # Codex 0.137 起文件沙箱默认用 bubblewrap，在容器里起不了 userns，批准后的
        # apply_patch 一律失败；退回 Landlock（内核 LSM，容器内可用），沙箱才落得了地。
        "[features]",
        "use_legacy_landlock = true",
        "",
    ]
    gateway = (
        os.environ.get("OPENAI_MODEL", "").strip(),
        os.environ.get("OPENAI_BASE_URL", "").strip(),
        os.environ.get("OPENAI_API_KEY", "").strip(),
    )
    catalog = CODEX_HOME / "model-catalog.json"
    if has_own_auth():
        lines[1] = "# 已有官方登录态：模型请求直接发给官方，不经模型网关。"
    elif all(gateway):
        model, base_url, _ = gateway
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
        # 网关没接入（变量缺失或不全）：不写网关配置，也清掉上次留下的元数据文件；Codex 走内置
        # openai provider，没有登录态时 new_session/prompt 会返回 auth_required，桥把它
        # 转成“要先登录”的提示。
        catalog.unlink(missing_ok=True)
    (CODEX_HOME / "config.toml").write_text("\n".join(lines))


def write_catalog(path: Path, model: str) -> None:
    """网关模型的元数据，字段照 Codex 对未知模型的兜底元数据填（见 NOTICE 的来源）。"""
    entry = {
        "slug": model,
        "display_name": model,
        # 网关模型不在 Codex 自带目录里，缺这一项时 Codex 不注册 apply_patch，只能拿 shell 改文件；
        # 桥的改文件冲突表认 apply_patch 的 edit 工具调用，没有它就没有排队卡片。
        "apply_patch_tool_type": "freeform",
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
    # initialize 的应答是 JSON-RPC result：{"result": {"authMethods": [...]}}。
    if isinstance(message, dict) and isinstance(message.get("result"), dict) \
            and "authMethods" in message["result"]:
        message["result"]["authMethods"] = [
            {
                "id": "chatgpt",
                "name": "ChatGPT 账号（设备码）",
                "type": "terminal",
                "args": ["login", "--device-auth"],
            },
            {
                "id": "api-key",
                "name": "OpenAI API key",
                "type": "terminal",
                "args": ["login", "--with-api-key"],
            },
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
    args = request.get("args", [])
    # --with-api-key 的 CLI 读 stdin 到 EOF 才收 key，而桥发一行文本后保持连接。入口在转发
    # 第一行后关掉登录进程的 stdin，给 CLI 制造 EOF；桥那边照旧保持连接收输出帧。
    line_then_eof = any("--with-api-key" in arg for arg in args)
    process = await asyncio.create_subprocess_exec(
        CODEX_BIN, *args,
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
    if line_then_eof:
        line = await reader.readline()
        if not line:
            # 桥没发 key 就断开（取消、超时）：结束登录进程，收回这次串行的登录。
            stdin.close()
            process.kill()
            await process.wait()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            return
        stdin.write(line)
        await stdin.drain()
        stdin.close()
        code = await process.wait()
        # 输出泵收完再发退出帧：桥收到 exit 就停读，"Successfully logged in" 和失败原因
        # 不能跟退出帧竞态。
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
    else:
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
    login_socket = SOCKET.with_suffix(".login.sock")
    try:
        login_socket.unlink()
    except FileNotFoundError:
        pass
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(SOCKET))
    os.chmod(SOCKET, 0o600)
    listener.listen(8)
    listener.setblocking(False)
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
