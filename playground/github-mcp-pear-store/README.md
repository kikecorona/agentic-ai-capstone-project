# GitHub MCP smoke test — `kikecorona/pear-store`

A standalone LangGraph agent that points the reference
[`@modelcontextprotocol/server-github`](https://www.npmjs.com/package/@modelcontextprotocol/server-github)
MCP at the **docs repo** the Capstone POC writes into
([§8.5 page storage](../../PROJECT_ARCHITECTURE.md#85-considerations-for-the-poc)
names `kikecorona/pear-store` as the production target).

Same shape as [`../github-mcp-test/`](../github-mcp-test/), with two changes:

- defaults the agent to `kikecorona/pear-store` instead of `kikecorona/agentic-ai-capstone-project`;
- accepts `--path` / `--branch` CLI flags so you can poke any folder in the
  repo without editing the prompt.

Use this script to confirm your PAT can reach the docs repo and that
`get_file_contents` returns sensible content **before** wiring the GitHub
MCP into the BP and SD services as their production `PageStore` /
`SourceStore`.

## Prerequisites

- Node.js (for `npx` to fetch the MCP server package on demand).
- [Ollama](https://ollama.com/) running locally with a tool-calling model:
  ```bash
  ollama serve
  ollama pull llama3.1
  ```
- A GitHub PAT with `repo` (or `public_repo`) scope.

## Install Python dependencies

```bash
pip install langchain-ollama langgraph langchain-core \
            langchain-mcp-adapters python-dotenv "langgraph-cli[inmem]"
```

## Configure

```bash
cp .env.example .env
# edit .env — paste your PAT, override GITHUB_OWNER/REPO/BRANCH if needed
```

## Quick CLI smoke test

```bash
# default: list documentation/bp/ and summarize each file
python github-mcp-pear-store.py

# pick a different folder or file
python github-mcp-pear-store.py --path documentation/sd
python github-mcp-pear-store.py --path documentation/bp/products/catalog-discovery.md

# different branch
python github-mcp-pear-store.py --path documentation --branch dev

# completely custom prompt
python github-mcp-pear-store.py --query "List the top-level entries in the repo root."
```

The first run downloads `@modelcontextprotocol/server-github` via `npx`,
so expect a short delay.

## Webchat with the Agent Chat UI

The compiled graph is exported as `graph` and registered in
[`langgraph.json`](langgraph.json) under the assistant id `agent`, so it
can be served by the LangGraph dev server.

```bash
langgraph dev
```

Point the
[Agent Chat UI](https://github.com/langchain-ai/agent-chat-ui) at
`http://localhost:2024` with assistant id `agent`. Try a prompt like:

> Summarize `documentation/bp/products/catalog-discovery.md`
> from `kikecorona/pear-store`.

The chat panel will stream the `get_file_contents` tool call and the
model's final summary.

## Why this exists

Once this smoke test works, the same MCP wiring lives inside the BP and
SD services as the real `PageStore` / `SourceStore` — every read and
every write goes through the GitHub MCP, exactly as
[§8.5 page storage](../../PROJECT_ARCHITECTURE.md#85-considerations-for-the-poc)
describes. The playground is the cheapest way to confirm a workstation
can actually reach the docs repo before paying the cost of bringing up
five service processes.
