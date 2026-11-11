# Friday 网页聊天组件

在网页引入 `embed.js` 后，右下角会出现「问 Friday」按钮。点击展开聊天窗，再点一次即可收起。Node.js 托管组件，并通过 A2A 1.0 JSON-RPC 直接调用 AgentScope 的 Friday。DeepSeek API Key 由 Python 服务读取。

## 本机运行

需要 Python 3.12、uv、Node.js 20 或更高版本，以及可调用 `deepseek-flash` 的 DeepSeek API Key。下面命令使用 Bash，在仓库根目录执行。

在终端 A 安装依赖并启动 AgentScope。

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
read -rs -p 'DeepSeek API Key: ' DEEPSEEK_API_KEY
printf '\n'
export DEEPSEEK_API_KEY
.venv/bin/python server.py --host 127.0.0.1 --port 9999
```

在终端 B 启动 Node.js。这个网页服务使用 Node.js 内置模块。

```bash
npm --prefix web start
```

打开 [网页演示](http://127.0.0.1:3000/)，点击右下角「问 Friday」发送消息。Node 默认将请求发到 `http://127.0.0.1:9999/`，可用 `AGENTSCOPE_URL` 指定 AgentScope 的 A2A 地址。`PORT` 默认 `3000`，`HOST` 默认 `127.0.0.1`。

## 嵌入第三方网页

例如目标网页在 `http://127.0.0.1:3001`，启动 Node 时允许该网页的源。

```bash
EMBED_ORIGINS=http://127.0.0.1:3001 npm --prefix web start
```

在目标网页 HTML 里加入这一行。

```html
<script defer src="http://127.0.0.1:3000/embed.js"></script>
```

部署到你的网站时，把脚本地址换成访客可访问的 Node 服务 HTTPS 地址。`EMBED_ORIGINS` 填目标页面的源，多个源用逗号隔开，每个源写协议、主机和可选端口。若目标页使用 CSP，在 `script-src`、`style-src` 和 `frame-src` 中允许组件的源。聊天 iframe 从 Node 同源请求 `/api/chat`。

## 配置 AgentScope 后端

要为 Friday 添加沙箱、Python 执行工具或 MCP，请按 [后端配置指南](AGENTSCOPE_BACKEND.md) 操作。

## 会话与轨迹

收起再展开聊天窗会保留当前对话。刷新页面或点击「新对话」会开启新会话。展开聊天窗底部的「会话 ID」可以复制 A2A `contextId`，在本机用 [Friday 轨迹页](http://127.0.0.1:9999/traces) 查看这轮及后续消息的运行记录。

历史部署文档见 [Nacos](NACOS.md) 和 [Codex](CODEX.md)。
