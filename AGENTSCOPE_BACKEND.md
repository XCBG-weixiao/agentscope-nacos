# Friday 后端配置：沙箱、工具与 MCP

本指南对应仓库里的 AgentScope Python SDK 2.0.11 和 [Friday A2A 服务](server.py)。现有部署步骤见 [README](README.md)。下面的改动都在 Python 后端完成，网页继续由 Node.js 托管。

```text
网页聊天窗 → Node.js /api/chat → A2A SendMessage → Friday → DeepSeek
                                                   └─ Toolkit → Python 工具 / MCP 工具
```

| 配置 | 代码入口 | 控制的行为 |
| --- | --- | --- |
| 沙箱 | `main()` 初始化 `Workspace` | 工具进程使用的文件系统、工作目录和资源 |
| 工具 | 创建 `Agent(..., toolkit=...)` | 模型可以调用的函数及其参数 |
| MCP | `Toolkit(mcps=[...])` 或 `workspace.list_mcps(...)` | 从 MCP 服务暴露给 Agent 的工具 |
| 权限 | `AgentState(permission_context=...)` 和工具权限检查 | 本次工具调用允许、拒绝或请求确认 |

默认的 `server.py` 每个 A2A `contextId` 创建一个 Agent，默认只提供聊天。下面以本机受信任用户测试为例，先接 Python 执行器，再按需接 MCP。

## 1. 选择沙箱

| 运行环境 | Workspace | 文件和网络边界 |
| --- | --- | --- |
| Apple Silicon、macOS 26+ | `AppleContainerWorkspace(cpus=1, memory="1G")` | 工具在 Linux VM 内运行，工作目录为 `/workspace`。默认无宿主目录挂载，关闭时删除容器。 |
| Linux，安装 `bwrap` | `BubblewrapWorkspace(host_workdir="/srv/friday-data", gateway_port=None)` | 指定的宿主目录以读写方式挂载到 `/workspace`。2.0.11 的 MCP 网关要求共享宿主网络。 |
| 宿主机上的受限业务工具 | `LocalWorkspace(workdir="/srv/friday-data")` | 设置工作目录，工具仍在宿主机上运行。这个选项适合由业务代码限制输入和文件访问的工具。 |

以下示例使用 Apple Container，保留一个沙箱直到 Python 服务退出。安装 Apple Container 的签名安装包后，运行：

```bash
container system start
container run --rm python:3.11-slim python3 --version
```

首次初始化 AgentScope Workspace 会拉取镜像并安装网关依赖。沙箱访问所需的软件源应可用。Apple Container 示例的文件系统与宿主分开，网络按 Apple Container 的配置运行。 [Apple 安装说明](https://github.com/apple/container#get-started) · [AgentScope Apple Container 示例](https://github.com/agentscope-ai/agentscope/blob/v2.0.11/examples/workspace/apple-container-workspace.md) · [AgentScope Workspace 文档](https://docs.agentscope.io/en/versions/2.0.11/building-blocks/workspace/run-workspace)

## 2. 写 Python 执行工具

在仓库根目录新建 `friday_tools.py`，保存以下代码。AgentScope 根据函数签名和说明生成工具参数。示例镜像提供 `python3` 和 GNU `timeout`。执行参数作为列表传给沙箱后端，代码作为 Python 的一个参数传入。

```python
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool
from agentscope.workspace import WorkspaceBase


def make_python_tool(workspace: WorkspaceBase) -> FunctionTool:
    backend = workspace.get_backend()

    async def run_python(code: str) -> dict:
        """在沙箱工作目录执行短 Python 代码，返回退出码和输出。"""
        if not code.strip() or len(code) > 8000:
            return {"error": "请输入非空代码，最多 8000 字符。"}

        result = await backend.exec_shell(
            ["timeout", "-k", "1s", "8s", "python3", "-I", "-c", code],
            cwd=workspace.workdir,
            timeout=12,
        )
        return {
            "exit_code": result.exit_code,
            "stdout": result.stdout[:4000].decode("utf-8", errors="replace"),
            "stderr": result.stderr[:4000].decode("utf-8", errors="replace"),
        }

    return FunctionTool(
        run_python,
        is_concurrency_safe=False,
        permission=PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="允许本机测试实例调用沙箱中的 Python 执行器。",
        ),
    )
```

容器内的 `timeout` 约束示例命令的执行时间，外层调用最多等待 12 秒。返回值最多携带各 4000 字节的标准输出和错误输出，沙箱后端仍会先收集进程输出。只需要创建固定文件时，可以另外写一个接受正文的函数，把文件路径固定在服务端，再用 `FunctionTool` 注册。 [Python 工具文档](https://docs.agentscope.io/en/versions/2.0.11/building-blocks/tool/python-tool) · [执行后端接口源码](https://github.com/agentscope-ai/agentscope/blob/v2.0.11/src/agentscope/tool/_builtin/_backend.py)

## 3. 挂到 Friday 并验证

以下补丁适用于本仓库的 `server.py`。把代码块内容保存为 `/tmp/friday-tools.patch`，在仓库根目录运行 `git apply --check /tmp/friday-tools.patch`，通过后运行 `git apply /tmp/friday-tools.patch`。

```diff
--- a/server.py
+++ b/server.py
@@ -41,18 +41,26 @@
 from agentscope.event import TextBlockDeltaEvent
 from agentscope.message import UserMsg
 from agentscope.model import DeepSeekChatModel
-
+from agentscope.permission import PermissionContext, PermissionMode
+from agentscope.state import AgentState
+from agentscope.tool import Toolkit
+from agentscope.workspace import AppleContainerWorkspace, WorkspaceBase
+
+from friday_tools import make_python_tool
 from traces import DEFAULT_TRACE_DB, TraceRun, TraceStore


 class AgentScopeExecutor(AgentExecutor):
     """Run one AgentScope agent per A2A context."""

-    def __init__(self, model: str, api_key: str, traces: TraceStore) -> None:
+    def __init__(
+        self, model: str, api_key: str, traces: TraceStore, workspace: WorkspaceBase,
+    ) -> None:
         """Remember how to build an agent for a new conversation."""
         self._model = model
         self._api_key = api_key
         self._traces = traces
+        self._workspace = workspace
         self._agents: dict[str, Agent] = {}

     async def execute(
@@ -83,6 +91,15 @@
                     credential=DeepSeekCredential(api_key=self._api_key),
                     model=self._model,
                     stream=True,
+                ),
+                toolkit=Toolkit(
+                    tools=[make_python_tool(self._workspace)],
+                    mcps=await self._workspace.list_mcps(
+                        agent_id="Friday", session_id=context.context_id,
+                    ),
+                ),
+                state=AgentState(
+                    permission_context=PermissionContext(mode=PermissionMode.DONT_ASK),
                 ),
             )
         agent = self._agents[context.context_id]
@@ -149,6 +166,7 @@

 def create_app(
     base_url: str, model: str, api_key: str, trace_db: Path = DEFAULT_TRACE_DB,
+    *, workspace: WorkspaceBase,
 ) -> Starlette:
     """Build the A2A 1.0 application serving the agent."""
     card = AgentCard(
@@ -175,7 +193,8 @@
         ],
     )
     traces = TraceStore(trace_db)
-    return create_a2a_app(card, AgentScopeExecutor(model, api_key, traces), traces)
+    executor = AgentScopeExecutor(model, api_key, traces, workspace)
+    return create_a2a_app(card, executor, traces)


 def create_a2a_app(card: AgentCard, executor: AgentExecutor, traces: TraceStore) -> Starlette:
@@ -209,15 +228,17 @@
             "running this demo.",
         )

-    app = create_app(
-        base_url=f"http://{args.host}:{args.port}",
-        model=args.model,
-        api_key=api_key,
-        trace_db=args.trace_db,
-    )
-    await uvicorn.Server(
-        uvicorn.Config(app, host=args.host, port=args.port),
-    ).serve()
+    async with AppleContainerWorkspace(cpus=1, memory="1G") as workspace:
+        app = create_app(
+            base_url=f"http://{args.host}:{args.port}",
+            model=args.model,
+            api_key=api_key,
+            trace_db=args.trace_db,
+            workspace=workspace,
+        )
+        await uvicorn.Server(
+            uvicorn.Config(app, host=args.host, port=args.port),
+        ).serve()


 if __name__ == "__main__":
```

`async with` 在监听 A2A 端口前初始化 Workspace，退出时释放。这里每个会话有自己的 Agent 和 Toolkit，同一个本机实例的会话共用容器。网页暂时没有工具确认界面，`DONT_ASK` 会拒绝原本需要询问的调用。上面的 Python 工具在注册时明确授权，其他工具按各自的权限和规则判断。 [权限系统文档](https://docs.agentscope.io/en/versions/2.0.11/building-blocks/permission-system/overview)

按 [README](README.md#本机运行) 设置 `DEEPSEEK_API_KEY`，重新启动 Python 后端。原服务若还占用 9999，先停止原进程。Node.js 启动方式相同。

```bash
.venv/bin/python server.py --host 127.0.0.1 --port 9999 --model deepseek-flash
```

打开 [网页演示](http://127.0.0.1:3000/)，发送「请调用 `run_python` 执行 `print(sum(range(1, 101)))`」。预期标准输出是 `5050`。展开聊天窗口的「会话 ID」，在 [轨迹页](http://127.0.0.1:9999/traces) 查看 `TOOL_CALL_END` 和 `TOOL_RESULT_END`，核对工具名、退出码和输出。

若要把通用代码执行开放给第三方访客，应先把执行环境按用户或会话隔离，并由执行端强制限制网络、CPU、内存、进程数和输出。示例中的 `is_concurrency_safe=False` 告诉 Agent 如何调度工具，单次调用的权限也由 Agent 判断，容器和部署环境负责隔离。给不同会话创建不同的 Workspace 才能分开它们的文件。

## 4. 接入已有的 MCP 服务

MCP 的工具名由服务端提供。下面假设你有一个 HTTP MCP 服务，提供原名为 `lookup_report` 的工具。把 `FRIDAY_MCP_URL` 设置为该服务的实际 MCP 地址。

```python
from agentscope.mcp import HttpMCPConfig, MCPClient

reports = MCPClient(
    name="reports",
    is_stateful=False,
    mcp_config=HttpMCPConfig(url=os.environ["FRIDAY_MCP_URL"]),
    enable_tools=["lookup_report"],
)
```

在补丁的 `main()` 中创建上述 `reports`，并把 Workspace 构造改为 `AppleContainerWorkspace(cpus=1, memory="1G", default_mcps=[reports])`。补丁中的 `workspace.list_mcps(agent_id=..., session_id=...)` 会取得该会话的网关代理，再交给 Toolkit。HTTP MCP 的工具逻辑在远程服务上执行，该服务需要能从容器访问。

如果 MCP 应直接从 Python 后端连接，可在创建 Toolkit 时传 `mcps=[reports]`。无状态 HTTP 客户端随调用建立连接。直连有状态 HTTP 或 STDIO 时，服务启动先 `await client.connect()`，退出时 `await client.close()`。如果要把本地 STDIO MCP 进程启动在沙箱里，用 `StdioMCPConfig(command=..., args=[...])` 和 `is_stateful=True`，交给 Workspace 的 `default_mcps` 或 `await workspace.add_mcp(...)`。相关可执行文件需安装在沙箱里。 [MCP 客户端文档](https://docs.agentscope.io/en/versions/2.0.11/building-blocks/tool/mcp) · [Workspace MCP 网关源码](https://github.com/agentscope-ai/agentscope/blob/v2.0.11/src/agentscope/workspace/_sandboxed_base.py)

`enable_tools` 用服务端原始工具名做筛选。模型看到的是 `mcp__reports__lookup_report`。如果该工具需要显式授权，在 `server.py` 导入 `PermissionBehavior` 和 `PermissionRule`，把创建 `AgentState` 时传入的权限配置替换为：

```python
permission_context=PermissionContext(
    mode=PermissionMode.DONT_ASK,
    allow_rules={
        "mcp__reports__lookup_report": [
            PermissionRule(
                tool_name="mcp__reports__lookup_report",
                rule_content=None,
                behavior=PermissionBehavior.ALLOW,
                source="friday",
            ),
        ],
    },
),
```

`rule_content=None` 会允许这个已注册工具的全部参数。参数级限制应由 MCP 服务或工具自己的权限检查强制执行。HTTP 认证信息从服务端配置读取，避免放入网页或提交到 Git。 [权限规则文档](https://docs.agentscope.io/en/versions/2.0.11/building-blocks/permission-system/overview)

## 5. 排查

| 现象 | 检查 |
| --- | --- |
| Python 服务没有开始监听 | 确认 `container system start` 已完成，查看镜像或 Workspace 网关初始化日志。 |
| 模型回复了答案，轨迹里没有工具调用 | 确认后端已重启且连接的是修改后的实例。在提示中明确要求调用 `run_python`。 |
| 工具被拒绝 | 查看轨迹里的权限结果，检查工具名、`PermissionContext` 和工具级权限。 |
| MCP 找不到工具 | 核对 URL、工具原名和 `enable_tools`。使用 Workspace 网关时，从沙箱检查网络和工具安装。 |
