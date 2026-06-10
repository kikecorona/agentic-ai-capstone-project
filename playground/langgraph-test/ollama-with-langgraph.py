from typing import Annotated, Literal
from typing_extensions import TypedDict

from langchain_ollama import ChatOllama
from langchain_core.tools import tool
from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

# ==========================================
# 1. Define the Tools
# ==========================================
@tool
def calculate_basic_math(expression: str) -> str:
    """Safely evaluates simple mathematical string expressions.
    Use this strictly for calculations (e.g., '2 + 2', '350 * 12')."""
    try:
        # Note: Using eval safely for primitive math equations
        allowed_chars = "0123456789+-*/(). "
        if all(char in allowed_chars for char in expression):
            return str(eval(expression))
        return "Error: Invalid characters in expression."
    except Exception as e:
        return f"Failed to compute: {str(e)}"

tools = [calculate_basic_math]
tool_node = ToolNode(tools)

# ==========================================
# 2. Initialize Ollama and Bind Tools
# ==========================================
# Connects to your local server at http://localhost:11434 by default
llm = ChatOllama(model="llama3.1", temperature=0)
llm_with_tools = llm.bind_tools(tools)

# ==========================================
# 3. Define the Graph State & Nodes
# ==========================================
class AgentState(TypedDict):
    # This keeps track of the history of messages flowing through the service
    messages: Annotated[list[BaseMessage], add_messages]

def call_model(state: AgentState):
    """Executes the local model to determine the next action."""
    response = llm_with_tools.invoke(state["messages"])
    return {"messages": [response]}

def route_after_model(state: AgentState) -> Literal["tools", "__end__"]:
    """Conditional router that decides if a tool needs execution."""
    last_message = state["messages"][-1]
    # Check if the LLM generated a tool call
    if last_message.tool_calls:
        return "tools"
    return "__end__"

# ==========================================
# 4. Build the Workflow Graph
# ==========================================
workflow = StateGraph(AgentState)

# Add our executing nodes
workflow.add_node("agent", call_model)
workflow.add_node("tools", tool_node)

# Set up edges
workflow.add_edge(START, "agent")
workflow.add_conditional_edges(
    "agent",
    route_after_model,
)
workflow.add_edge("tools", "agent") # Back to agent to compile final text response

# Compile the graph into an executable service.
# `graph` is the symbol referenced from langgraph.json so that
# `langgraph dev` can serve it to the Agent Chat UI.
graph = workflow.compile()

# ==========================================
# 5. CLI smoke test
# ==========================================
# Run `python ollama-with-langgraph.py` for a quick local check.
# For the webchat experience, start the LangGraph dev server with
# `langgraph dev` and point the Agent Chat UI at http://localhost:2024.
if __name__ == "__main__":
    print("🤖 Agent Service Initialized!")

    # Example query requiring tool execution
    query = "What is 4325 multiplied by 82?"
    print(f"\nUser: {query}")

    inputs = {"messages": [HumanMessage(content=query)]}
    for output in graph.stream(inputs, stream_mode="values"):
        # Access the latest message emitted by the state updates
        latest_msg = output["messages"][-1]

    print(f"Agent response: {latest_msg.content}")
