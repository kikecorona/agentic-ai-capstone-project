# GitHub MCP + LangGraph

A minimal LangGraph agent that calls the reference
[`@modelcontextprotocol/server-github`](https://www.npmjs.com/package/@modelcontextprotocol/server-github)
MCP server over stdio and uses a local Ollama model to summarize a file
fetched from a GitHub repo.

## How it works

1. `langchain-mcp-adapters` spawns `@modelcontextprotocol/server-github` via
   `npx` and exposes its tools (`get_file_contents`, `search_code`, etc.) as
   LangChain tools.
2. `ChatOllama` binds those tools to a local tool-calling model.
3. A small LangGraph state machine loops `agent → tools → agent` until the
   model produces a final summary.

## Prerequisites

* Node.js (so `npx` can fetch the MCP server package on demand).
* [Ollama](https://ollama.com/) running locally with a tool-calling model:
  ```bash
  brew install --cask ollama && ollama serve
  ollama pull llama3.1
  ```
* A GitHub personal access token with `repo` (or `public_repo`) scope.

## Install Python dependencies

```bash
pip install langchain-ollama langgraph langchain-core \
            langchain-mcp-adapters python-dotenv "langgraph-cli[inmem]"
```

## Configure

```bash
cp .env.example .env
# then edit .env and paste your token
```

## Quick CLI smoke test

```bash
python github-mcp-with-langgraph.py
```

The default query asks the agent to fetch
`synthetic-data/documentation/bp/business-cases/catalog-discovery.md` from
`kikecorona/agentic-ai-capstone-project` and summarize the business case it
describes. The first run downloads the
`@modelcontextprotocol/server-github` package via `npx`, so expect a short
delay.

## Webchat with the Agent Chat UI

The compiled graph is exported as `graph` and registered in
[`langgraph.json`](langgraph.json) under the assistant id `agent`, so it can
be served by the LangGraph dev server.

```bash
langgraph dev
```

In another terminal, run the
[Agent Chat UI](https://github.com/langchain-ai/agent-chat-ui) and point it at
`http://localhost:2024` with assistant id `agent`. Try a prompt like:

> Summarize `synthetic-data/documentation/bp/business-cases/catalog-discovery.md`
> from `kikecorona/agentic-ai-capstone-project`.

The chat panel will stream the `get_file_contents` tool call and the model's
final summary.

## Swapping the MCP server transport

If you'd rather run the official Go-based [`github-mcp-server`](https://github.com/github/github-mcp-server)
in Docker, swap the `command`/`args` in `github-mcp-with-langgraph.py` for
the Docker invocation — the rest of the graph stays the same.
