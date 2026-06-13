# Summarize Endpoint — GitHub Directory Summarizer

A FastAPI service that exposes a single `/summarize` endpoint. The endpoint
runs a LangGraph ReAct agent that uses the
[`@modelcontextprotocol/server-github`](https://www.npmjs.com/package/@modelcontextprotocol/server-github)
MCP server to list a directory in a GitHub repo and summarize every file
inside it with a local Ollama model.

## How it works

1. On startup, the FastAPI app spawns the GitHub MCP server via `npx` and
   loads its tools (`get_file_contents`, `search_code`, ...) as LangChain
   tools.
2. `create_react_agent` (from `langgraph.prebuilt`) wires those tools to a
   `ChatOllama` model in a standard ReAct loop (think → act → observe).
3. Each `POST /summarize` builds a user prompt from the request body
   (`owner`, `repo`, `path`, optional `branch`) and asks the agent to list
   the directory and produce a per-file Markdown summary.

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
pip install fastapi uvicorn pydantic python-dotenv \
            langchain-ollama langgraph langchain-core \
            langchain-mcp-adapters
```

## Configure

Edit `.env` and paste your GitHub PAT:

```
GITHUB_PERSONAL_ACCESS_TOKEN=ghp_xxx...
```

## Run the service

```bash
python server.py
# or
uvicorn server:app --host 0.0.0.0 --port 8000
```

The first request triggers `npx` to download
`@modelcontextprotocol/server-github`, so expect a short delay on the
first call.

## Call the endpoint

```bash
curl -s -X POST http://localhost:8000/summarize \
  -H 'Content-Type: application/json' \
  -d '{
        "owner": "kikecorona",
        "repo": "agentic-ai-capstone-project",
        "path": "synthetic-data/documentation/bp/business-cases",
        "branch": "module-5"
      }' | jq
```

The response is a JSON object with a single `summary` field whose value
is a Markdown bullet list — one entry per file in the directory.

## Health check

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```
