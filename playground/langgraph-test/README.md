# LangGraph Test

## Project dependencies 

### Python

* `pip install langchain-ollama langgraph langchain-core "langgraph-cli[inmem]"`

## Ollama
* For a local tool-calling backend on macOS use Ollama
  * `brew install --cask ollama && ollama serve`
  * `ollama pull llama3.1` (or any model with tool-calling support)
  * Point `ChatOpenAI(base_url="http://localhost:11434/v1", api_key="ollama", model="llama3.1")`

## LangGraph CLI tools
* For the Webchat UI install
  * `brew install langgraph-cli langgraph-api`

## Webchat with Agent Chat UI

The compiled graph in `ollama-with-langgraph.py` is exported as `graph` and
registered in [`langgraph.json`](langgraph.json) under the assistant id
`agent`, so it can be served by the LangGraph dev server and consumed by the
[Agent Chat UI](https://github.com/langchain-ai/agent-chat-ui).

### 1. Run the LangGraph dev server

From this directory:

```bash
langgraph dev
```

This boots an in-memory LangGraph server on `http://localhost:2024` exposing
the `agent` graph. Make sure `ollama serve` is already running and the
`llama3.1` model is pulled.

### 2. Run Agent Chat UI

In a separate terminal, clone and start the UI:

```bash
git clone https://github.com/langchain-ai/agent-chat-ui.git
cd agent-chat-ui
npm install 
npm run dev 
```

When the UI loads, configure the connection form:

* **Deployment URL:** `http://localhost:2024`
* **Assistant / Graph ID:** `agent`
* **LangSmith API Key:** leave blank (only needed for hosted deployments)

Submit messages like *"What is 4325 multiplied by 82?"* — the UI will stream
the LangGraph state, including the `calculate_basic_math` tool call, back to
the chat panel.

### Quick CLI smoke test (optional)

To verify the graph itself without the UI:

```bash
python ollama-with-langgraph.py
```