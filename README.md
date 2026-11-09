# AgentScope A2A 与 Nacos 单机部署

Friday 是一个由 DeepSeek 驱动的 AgentScope Agent。它通过 A2A 1.0 提供 JSON-RPC 服务。Nacos 发布它的 Agent Card，并根据实例的注册和心跳维护可用端点。客户端从 Nacos 发现端点后直接调用 Friday。运行轨迹由 Friday 保存，可按 A2A `contextId` 查询。

| 组件 | 本机入口 |
| --- | --- |
| Friday A2A JSON-RPC | `http://127.0.0.1:9999/` |
| Friday Agent Card | `http://127.0.0.1:9999/.well-known/agent-card.json` |
| Friday 会话轨迹 | `http://127.0.0.1:9999/traces` |
| Nacos HTTP API | `http://127.0.0.1:8848/nacos` |
| Nacos 控制台 | `http://127.0.0.1:8080/` |

## 1. 准备环境

使用 macOS 或 Linux，准备 Git、Python 3.12、uv、JDK 17、curl、jq、OpenSSL、`shasum` 和一枚可调用 `deepseek-flash` 的 DeepSeek API Key。以下终端示例使用 Bash。将工作目录切到本集成仓库根目录后安装依赖。

```bash
git clone https://github.com/XCBG-weixiao/agentscope-nacos.git
cd agentscope-nacos
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
java -version
```

AgentScope 从仓库内的 `vendor/agentscope` 安装。`vendor/nacos` 保留了对应的 Nacos 源码。下文使用同一版本的官方发布包启动 Nacos。

| 上游项目 | 固定版本 | Fork | 源码提交 |
| --- | --- | --- | --- |
| AgentScope | `v2.0.11` | [XCBG-weixiao/agentscope](https://github.com/XCBG-weixiao/agentscope) | `8af9d25784e704a4b11d71bd673257c68505fcb8` |
| Nacos | `3.3.0` | [XCBG-weixiao/nacos](https://github.com/XCBG-weixiao/nacos) | `e61ba821e025d713fc02d2020a338d4ce4251912` |

## 2. 启动 AgentScope 实例

在终端 A 中输入 DeepSeek API Key，并启动 Friday。`read -s` 会隐藏输入。

```bash
read -rs -p 'DeepSeek API Key: ' DEEPSEEK_API_KEY
printf '\n'
export DEEPSEEK_API_KEY
.venv/bin/python server.py --model deepseek-flash --host 127.0.0.1 --port 9999
```

在另一个终端读取 Agent Card，确认 `name` 是 `Friday`，`version` 是 `1.0.0`，`supportedInterfaces` 包含 A2A `1.0` 的 `JSONRPC` 地址。

```bash
curl -fsS http://127.0.0.1:9999/.well-known/agent-card.json | jq .
```

## 3. 启动 Nacos 3.3.0

将 [Nacos 3.3.0 发布包](https://github.com/alibaba/nacos/releases/tag/3.3.0) 解压到本机数据目录。以下命令用于首次安装。

```bash
export NACOS_DEMO_DIR="$HOME/.local/share/agentscope-nacos"
mkdir -p "$NACOS_DEMO_DIR"
curl -fL https://github.com/alibaba/nacos/releases/download/3.3.0/nacos-server-3.3.0.tar.gz \
  -o "$NACOS_DEMO_DIR/nacos-server-3.3.0.tar.gz"
printf '%s  %s\n' \
  '18d5ec3067e9e310401028ecc35e6e9c3a854c9b14d084f48b83bfea53208107' \
  "$NACOS_DEMO_DIR/nacos-server-3.3.0.tar.gz" | shasum -a 256 -c -
tar -xzf "$NACOS_DEMO_DIR/nacos-server-3.3.0.tar.gz" -C "$NACOS_DEMO_DIR"
```

编辑 `$NACOS_DEMO_DIR/nacos/conf/application.properties`，在末尾加入 `server.address=127.0.0.1`，让控制台和 HTTP API 绑定本机回环地址。将 `JAVA_HOME` 指向 JDK 17 的安装目录，再启动单机模式。在 macOS 上，如果 JDK 已登记到系统，可用 `export JAVA_HOME="$(/usr/libexec/java_home -v 17)"`。 [Nacos 单机启动](https://nacos.io/docs/latest/quickstart/quick-start/)

```bash
(cd "$NACOS_DEMO_DIR/nacos" && bash bin/startup.sh -m standalone)
tail -f "$NACOS_DEMO_DIR/nacos/logs/startup.log"
```

首次启动时，Nacos 的脚本会提示填写 token 签名密钥和服务端身份键值。签名密钥可用 `openssl rand -base64 48` 生成，身份键可填写 `agentscope-demo-identity`，身份值可用 `openssl rand -hex 24` 生成。打开 [Nacos 控制台](http://127.0.0.1:8080/)，按页面提示给管理员账号 `nacos` 初始化登录密码，然后用该账号登录控制台，确认凭据生效。服务端身份和签名密钥用于 Nacos 自身鉴权。 [Nacos 鉴权和初始化](https://nacos.io/docs/latest/manual/admin/auth/)

## 4. 发布 Agent Card 并注册实例

在终端 B 中输入刚设置的 Nacos 管理员密码。第一次运行时，`--publish` 从 Friday 读取 Agent Card，向 Nacos 发布 Agent 定义，然后以独立的客户端 ID 注册运行端点并发送心跳。看到 `Registered Friday at http://127.0.0.1:9999` 后保留这个进程。

```bash
read -rs -p 'Nacos password: ' NACOS_PASSWORD
printf '\n'
export NACOS_PASSWORD
.venv/bin/python nacos_agent.py register --publish
```

在 Nacos 控制台依次打开「AI 管理中心 → Agent 管理 → Friday → 运行时端点」，选择 `1.0.0` 版本和 `a2a` 协议，确认地址为 `http://127.0.0.1:9999` 且状态健康。后续重启注册进程时，运行 `.venv/bin/python nacos_agent.py register`，复用已发布的定义。 [Nacos 端点注册与心跳](https://nacos.io/docs/latest/manual/user/ai/rad-discovery/)

同一个 Agent 可有多个运行端点。例如在另一终端用 `--port 10000` 启动第二个 Friday，再运行 `nacos_agent.py register --card-url http://127.0.0.1:10000/.well-known/agent-card.json`。每个实例使用自己的地址和注册进程。

## 5. 发现并调用 Friday

在终端 C 中设置 `NACOS_PASSWORD`，从 Nacos 选择一个健康的运行端点，然后发一条 A2A `SendMessage`。把 `AGENT_URL` 保留在同一终端。

```bash
read -rs -p 'Nacos password: ' NACOS_PASSWORD
printf '\n'
export NACOS_PASSWORD
AGENT_URL="$(.venv/bin/python nacos_agent.py discover)"
MESSAGE_ID="$(.venv/bin/python -c 'from uuid import uuid4; print(uuid4())')"

jq -n --arg mid "$MESSAGE_ID" --arg text '请只回复：Friday 已连接' '{
  jsonrpc: "2.0", id: 1, method: "SendMessage",
  params: {message: {messageId: $mid, role: "ROLE_USER", parts: [{text: $text}]}}
}' | curl -fsS "$AGENT_URL" \
  -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' --data-binary @- | jq .
```

成功时，`result.task.status.state` 为 `TASK_STATE_COMPLETED`。回复可能被分成多个文本片段；将最后一个 `jq .` 换成 `jq -r '[.result.task.artifacts[].parts[].text] | join("")'` 可合并输出。继续同一个会话时，向同一个 Friday 实例发送新的 `messageId`，并在 `params.message.contextId` 填入上次返回的 `result.task.contextId`。 [A2A 1.0 规范](https://a2a-protocol.org/latest/specification/)

如果用 dsh 发起调用，将同一条命令交给 dsh headless 的 Bash 工具，并让它报告实际返回的 `taskId`、状态和回复。此前验证使用 dsh `0.2.2-rc.1`、Node.js `24.15.0` 和同一枚 DeepSeek API Key。

## 6. 查看轨迹与停止服务

打开 [Friday 会话轨迹](http://127.0.0.1:9999/traces)，填入 `result.task.contextId`，即可按 `taskId` 展开每轮输入、模型调用、Token 用量、工具事件和回复。轨迹默认写入 `~/.local/state/agentscope-demo/traces.sqlite3`。可在启动 Friday 时用 `--trace-db` 指定其他路径。会话上下文保存在运行中的 Agent 实例内，轨迹记录由 SQLite 保存。

先在终端 B 按 `Ctrl+C` 注销运行端点，再在终端 A 按 `Ctrl+C` 停止 Friday。Nacos 用以下命令关闭。

```bash
bash "$NACOS_DEMO_DIR/nacos/bin/shutdown.sh"
```

`server.py` 改编自 [AgentScope A2A 示例](https://github.com/agentscope-ai/agentscope/blob/v2.0.11/examples/a2a/server.py)，增加了 DeepSeek 模型和运行轨迹。上游源码保留了原有许可文件。
