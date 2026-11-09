# 将 Codex 注册到 Nacos 并通过 A2A 调用

`codex_server.py` 将开源 Codex CLI 包装成一个独立的 A2A 1.0 JSON-RPC Agent。Nacos 负责登记和发现 `Codex` 实例，客户端将 `SendMessage` 发给发现的地址。服务用 `codex exec --json` 执行每轮任务，并用 `codex exec resume` 继续同一会话。 [Codex 开源源码](https://github.com/openai/codex/tree/rust-v0.167.0) · [OpenAI Codex 非交互模式文档](https://learn.chatgpt.com/docs/non-interactive-mode)

## 1. 安装并登录 Codex

在 macOS 或 Linux 上准备 Node.js 和 npm。按主文档[安装 Python 依赖](README.md#1-准备环境)并[启动 Nacos 3.3.0](README.md#3-启动-nacos-330)。以下命令从集成仓库根目录用 Bash 执行。Codex 使用自己的登录凭据，可独立于 Friday 运行。

```bash
npm install -g @openai/codex@0.167.0
codex --version
codex login
codex login status
```

使用运行服务的系统账号登录。若用 OpenAI API Key，可在启动服务的终端设置 `CODEX_API_KEY`。需要单独保存 Codex 登录和会话文件时，在登录和启动服务的终端设置同一个 `CODEX_HOME`。 [OpenAI Codex 身份验证文档](https://learn.chatgpt.com/docs/non-interactive-mode#authenticate-in-automation)

## 2. 启动 Codex A2A 服务

指定一个 Git 工作目录。下面新建一个独立的演示目录，也可以把 `--workspace` 指向你要让 Codex 操作的 Git 仓库。

```bash
CODEX_WORKSPACE="$HOME/.local/share/codex-a2a/workspace"
mkdir -p "$CODEX_WORKSPACE"
git init "$CODEX_WORKSPACE"
.venv/bin/python codex_server.py --workspace "$CODEX_WORKSPACE"
```

默认监听 `127.0.0.1:10001`，沙箱为 `read-only`。需要编辑代码时，在启动命令中加 `--sandbox workspace-write`。这个模式允许写工作区和 Codex 沙箱允许的临时目录，文件读取范围由 Codex 沙箱决定。可用 `--codex` 指定 CLI 路径、`--model` 指定模型、`--timeout` 设置单轮超时时间，默认 300 秒。 [OpenAI Codex 沙箱文档](https://learn.chatgpt.com/docs/non-interactive-mode#permissions-and-safety)

在另一个终端读取 Agent Card，确认名称为 `Codex`，版本为 `1.0.0`。

```bash
curl -fsS http://127.0.0.1:10001/.well-known/agent-card.json | jq .
```

## 3. 发布并注册到 Nacos

在第二个终端输入 Nacos 管理员密码。首次运行时用 `--publish` 发布 Agent Card 并登记端点，保持进程运行以发送心跳。

```bash
read -rs -p 'Nacos password: ' NACOS_PASSWORD
printf '\n'
export NACOS_PASSWORD

.venv/bin/python nacos_agent.py register --publish \
  --card-url http://127.0.0.1:10001/.well-known/agent-card.json
```

在 Nacos 控制台的「AI 管理中心 → Agent 管理 → Codex → 运行时端点」确认地址为 `http://127.0.0.1:10001` 且状态健康。重启注册进程时，去掉 `--publish` 并保留 `--card-url`，即可复用已发布的版本。

## 4. 发现、调用和继续会话

在设置了 `NACOS_PASSWORD` 的调用终端执行以下命令。`discover` 输出健康实例地址，每次调用使用新的 `messageId`。

```bash
CODEX_URL="$(.venv/bin/python nacos_agent.py discover --name Codex --version 1.0.0)"

send_codex() {
  local message_id
  message_id="$(.venv/bin/python -c 'from uuid import uuid4; print(uuid4())')"
  jq -n --arg mid "$message_id" --arg text "$1" --arg context "${2-}" '{
    jsonrpc: "2.0", id: 1, method: "SendMessage",
    params: {message: (
      {messageId: $mid, role: "ROLE_USER", parts: [{text: $text}]} +
      (if $context == "" then {} else {contextId: $context} end)
    )}
  }' | curl -fsS "$CODEX_URL" \
    -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' --data-binary @-
}

FIRST_REPLY="$(send_codex '用 shell 查看当前工作目录，然后记住代号 Lantern。')"
jq . <<< "$FIRST_REPLY"
```

看到 `result.task.status.state` 为 `TASK_STATE_COMPLETED` 后，从 `result.task.artifacts[].parts[].text` 读取 Codex 的最终回复。将同一个 `contextId` 发给同一个实例，就可以继续会话。

```bash
CONTEXT_ID="$(jq -er '.result.task.contextId' <<< "$FIRST_REPLY")"
send_codex '我上一条消息让你记住的代号是什么？' "$CONTEXT_ID" | jq .
```

## 5. 看轨迹和停止服务

在本机打开 [Codex 会话轨迹](http://127.0.0.1:10001/traces)，填入 A2A `contextId`。可以看到每轮任务的 Codex 会话 ID、工具事件、回复和 Token 用量。轨迹与 A2A `contextId` 到 Codex 会话 ID 的对应关系保存在 `~/.local/state/codex-a2a/traces.sqlite3`。重启服务后，继续使用同一个工作目录、数据库和 `CODEX_HOME` 即可续接原会话。

先在注册终端按 `Ctrl+C` 注销运行端点，再在 Codex 服务终端按 `Ctrl+C` 停止服务。
