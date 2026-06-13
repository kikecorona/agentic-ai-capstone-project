"""LangGraph agent that uses the GitHub MCP server to fetch and summarize a file.

The graph wires three pieces together:
  * `langchain-mcp-adapters` spawns `@modelcontextprotocol/server-github` over
    stdio (via `npx`) and exposes its tools (e.g. `get_file_contents`) as
    LangChain tools.
  * `ChatOllama` provides a local tool-calling LLM.
  * A small LangGraph state machine lets the model call tools, then write a
    final natural-language summary of the file.
"""

import asyncio
import os
from typing import Annotated, Literal

from typing_extensions import TypedDict
from dotenv import load_dotenv

from langchain_ollama import ChatOllama
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import StateGraph, START
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from langchain_mcp_adapters.client import MultiServerMCPClient

load_dotenv()

# ==========================================
# 1. Spin up the GitHub MCP server and load its tools
# ==========================================
GITHUB_TOKEN = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")
if not GITHUB_TOKEN:
    raise RuntimeError(
        "GITHUB_PERSONAL_ACCESS_TOKEN is required. Set it in your shell or in .env"
    )

# The reference GitHub MCP server is published on npm; `npx -y` will fetch
# and run it on demand. The server reads the PAT from this env var.
mcp_client = MultiServerMCPClient(
    {
        "github": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": GITHUB_TOKEN},
            "transport": "stdio",
        }
    }
)

# Tools are static for the lifetime of the process, so we resolve them once
# at import time. `langgraph dev` imports this module to find `graph`, so this
# runs before any event loop is started.
tools = asyncio.run(mcp_client.get_tools())
tool_node = ToolNode(tools)

# ==========================================
# 2. Initialize Ollama and bind the MCP tools
# ==========================================
llm = ChatOllama(model="llama3.1", temperature=0)
llm_with_tools = llm.bind_tools(tools)

SYSTEM_PROMPT = (
    "You are an assistant that uses the GitHub MCP tools to fetch source "
    "files and summarize them. When the user asks about a file, call the "
    "`get_file_contents` tool with the appropriate `owner`, `repo`, and "
    "`path` arguments, then return a concise summary of the file's purpose "
    "and key contents. Do not invent details that are not in the file."
)

# ==========================================
# 3. Define the graph state and nodes
# ==========================================
class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


def call_model(state: AgentState):
    """Invoke the model, prepending the system prompt on the first turn."""
    messages = state["messages"]
    if not any(isinstance(m, SystemMessage) for m in messages):
        messages = [SystemMessage(content=SYSTEM_PROMPT), *messages]
    response = llm_with_tools.invoke(messages)
    return {"messages": [response]}


def route_after_model(state: AgentState) -> Literal["tools", "__end__"]:
    last_message = state["messages"][-1]
    if last_message.tool_calls:
        return "tools"
    return "__end__"


# ==========================================
# 4. Build the workflow graph
# ==========================================
workflow = StateGraph(AgentState)
workflow.add_node("agent", call_model)
workflow.add_node("tools", tool_node)
workflow.add_edge(START, "agent")
workflow.add_conditional_edges("agent", route_after_model)
workflow.add_edge("tools", "agent")

# `graph` is the symbol referenced by langgraph.json so `langgraph dev` can
# serve it to the Agent Chat UI.
graph = workflow.compile()


# ==========================================
# 5. CLI smoke test
# ==========================================
# MCP-adapter tools are async-only (no `_run` implementation), so the graph
# must be driven via the async API. `langgraph dev` already runs graphs on
# an event loop, so the dev server flow is unaffected.
async def _run_cli(query: str):
    inputs = {"messages": [HumanMessage(content=query)]}
    latest_msg = None
    async for output in graph.astream(inputs, stream_mode="values"):
        latest_msg = output["messages"][-1]
    return latest_msg


if __name__ == "__main__":
    print("🤖 GitHub MCP + LangGraph agent ready")
    query = (
        "Fetch the file `synthetic-data/documentation/bp/business-cases/"
        "catalog-discovery.md` from owner `kikecorona`, repo "
        "`agentic-ai-capstone-project` on the branch module-5, and write a "
        "concise summary of the business case it describes."
    )
    print(f"\nUser: {query}\n")

    latest_msg = asyncio.run(_run_cli(query))
    if latest_msg is not None:
        print(f"Agent response:\n{latest_msg.content}")
