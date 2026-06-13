"""FastAPI service exposing `/summarize`, which summarizes every file in a
GitHub repository directory using a LangGraph ReAct agent backed by the
reference `@modelcontextprotocol/server-github` MCP server.

Flow:
  * On startup, spawn the GitHub MCP server over stdio and load its tools
    (`get_file_contents`, `search_code`, ...) as LangChain tools.
  * `/summarize` builds a ReAct agent with those tools and a local Ollama
    model, then asks it to list a directory and produce a summary of each
    file.
"""

import asyncio
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_ollama import ChatOllama
from langgraph.prebuilt import create_react_agent

load_dotenv()

GITHUB_TOKEN = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")
if not GITHUB_TOKEN:
    raise RuntimeError(
        "GITHUB_PERSONAL_ACCESS_TOKEN is required. Set it in your shell or in .env"
    )

SYSTEM_PROMPT = (
    "You are an assistant that uses GitHub MCP tools to inspect a directory in "
    "a repository and summarize every file inside it.\n\n"
    "Workflow:\n"
    "  1. Call `get_file_contents` with the given owner/repo/path (and branch "
    "     if provided) to list the directory's entries.\n"
    "  2. For each file entry, call `get_file_contents` again to fetch the "
    "     file content.\n"
    "  3. Skip nested subdirectories (do not recurse) and binary/non-text "
    "     files you cannot read.\n"
    "  4. Produce a final answer as a Markdown list, one bullet per file, "
    "     each bullet starting with the filename followed by a one-to-two "
    "     sentence summary of its purpose and key contents.\n\n"
    "Do not invent details that are not in the files."
)


# A single agent is built per request so that we always pick up the latest
# tools list, but the underlying MCP client and tools list are resolved once
# at startup and kept on app.state.
@asynccontextmanager
async def lifespan(app: FastAPI):
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
    tools = await mcp_client.get_tools()
    llm = ChatOllama(model="llama3.1", temperature=0)
    app.state.agent = create_react_agent(llm, tools, prompt=SYSTEM_PROMPT)
    try:
        yield
    finally:
        # MultiServerMCPClient does not currently expose an explicit close;
        # subprocess cleanup happens when the parent process exits.
        pass


app = FastAPI(title="GitHub Directory Summarizer", lifespan=lifespan)


class SummarizeRequest(BaseModel):
    owner: str = Field(..., description="GitHub repository owner")
    repo: str = Field(..., description="GitHub repository name")
    path: str = Field(..., description="Directory path inside the repo")
    branch: str | None = Field(
        default=None, description="Optional branch (defaults to repo's default)"
    )


class SummarizeResponse(BaseModel):
    summary: str


@app.post("/summarize", response_model=SummarizeResponse)
async def summarize(req: SummarizeRequest) -> SummarizeResponse:
    agent = app.state.agent
    if agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    branch_clause = f" on branch `{req.branch}`" if req.branch else ""
    user_prompt = (
        f"Summarize every file in directory `{req.path}` of "
        f"`{req.owner}/{req.repo}`{branch_clause}. List the directory first, "
        "then summarize each file individually."
    )

    result = await agent.ainvoke(
        {"messages": [HumanMessage(content=user_prompt)]}
    )
    final_message = result["messages"][-1]
    return SummarizeResponse(summary=final_message.content)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
