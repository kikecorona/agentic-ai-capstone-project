# Capstone Project — Research Agent for Org Knowledge

Enrique R. Corona Dominguez

## 1. Introduction (Module 1)

### 1.1 Background and scope of the problem

I work in a tech-org with systems that are 20+ years old and have evolved organically at different paces and with
different technologies, so modernization of our systems is a top priority to: 1) improve efficiency, 2) improve quality,
and 3) scale better.

As you can imagine, we have 20+ years of technical debt and also different levels of system design and documentation
spread across thousands of developers that have worked in the org at some point. We also have documentation with
different scopes — Product Managers have documentation based on features and user requirements, while developers have
documentation based on a high-level design and (if we are lucky) on the low-level design and implementation decisions (
which is not that common).

Additionally to my org's systems, our systems interact with downstream and upstream dependencies which most of the time
are a black box for us. While this is fine to specify system boundaries, it makes it harder to prioritize, measure, and
find all the required dependencies for projects that require integration with other orgs.

### 1.2 Why a stand-alone LLM would not be sufficient

Because of the nature of the problem, we'll need to deal with multiple data sources and also with incomplete and
potentially inaccurate data which needs to be refined either automatically or semi-automatically so we can provide a
reliable knowledge system.

### 1.3 Proposed solution

I'm proposing to implement a **Research Agent** that can help our leadership, developers, and product managers have a
complete view of the architecture, dependencies, progress, and known gaps of our systems.

> **Why this matters** — modernization efforts in a 20+ year old org stall on a single recurring problem: nobody
> has an accurate, current map of the system. Decisions get made on stale or incomplete documentation, dependencies
> get discovered late, and gap analysis becomes weeks of manual archaeology. A Research Agent that **continuously
> updates and enriches** the org's documentation collapses that lead time and gives every team — engineering,
> product, leadership — a single source of truth they can trust.

### 1.4 Principles for our agent

- **Read only** — The purpose of this agent is to gather, organize, present, and discover information and knowledge that
  otherwise would be very difficult to track. This agent is not meant to make updates to any kind of deliverable;
  however, it can provide suggestions that potentially can be implemented by other agents.
- **Up to date** — The knowledge base of the agent should be as near-to-real-time as possible.
- **Extensible** — The agent's architecture should be extensible to include new tools and evolve its environment.
- **Secure** — The agent won't use any PII data, won't have access to any undisclosed projects, and will follow the
  company's security guidelines for LLM usage.

---

## 2. Environment and Tools (Data Sources)

Tentatively our agent will use the following data sources to gather information:

- Collective documentation, such as: quip, confluence, word docs, presentations, pdfs.
- Source code (only from our org).
- External API definitions (such as swagger).
- Other unstructured documentation sources, such as: slack channels, email threads, meeting transcripts, etc.
- Read-only DB access, such as: Trino, Oracle, PostgreSQL, etc.

Each one of these data sources will require either specific tooling to ingest the data or other sub-agents to process
the data prior to ingestion.

### 2.1 Subject Matter Expert (SME) feedback

Most of the tooling required for our agent is focused on data retrieval; however, our agent also needs to verify the
veracity of its findings since the documents used might not be up to date. With this in mind, I'm proposing to provide
tooling to ask questions to the **subject matter experts (SMEs)** in our org to answer unknowns or clarify conflicting
documentation.

The questions are surfaced through the **Documentation Portal** (see [Section 8](PROJECT_ARCHITECTURE.md#8-high-level-architecture-module-5)) so SMEs can answer in a single
place; their replies enrich the knowledge base directly or provide new documentation to be ingested.

### 2.2 Initial feedback loop

Our agent will be an expert on our system, so it will need to understand our products, system's architecture, and
dependencies. The high-level feedback loop:

1. Retrieve documents/code/data.
2. Categorize per project(s).
3. Verify veracity and freshness.
4. Gather SMEs feedback if required.
5. Summarize and generate architecture and projects documentation.
6. Analyze gaps and provide feedback on improvements.

The agent runs this loop permanently, increasing & improving the knowledge base and documentation of our systems over
time.

---

## 3. Proposed Reasoning Loop (Module 2)

We'll tackle the problem from **two points of view (POVs)** and add an **Orchestration** layer on top.

### 3.1 Business & Product (B&P) POV

1. The agent retrieves existing business documentation of the project.
2. The agent retrieves existing design documentation of the project.
3. The agent retrieves and analyzes existing code mentioned in the documentation.
4. The agent analyzes if the implementation and the existing design match. If not:
    - The agent tries to fill the gaps; if it can't, then it asks an SME.
5. The agent generates documentation for the new project.

For the first phase of this project, we can ask the agent to build documentation project by project, and we'll provide
an initial list of SMEs.

### 3.2 System's Architecture (SA / SD) POV

1. The agent retrieves and analyzes existing codebases.
2. The agent retrieves and analyzes existing system design documentation.
3. The agent retrieves and analyzes existing call patterns between services (to figure out upstream dependencies).
4. The agent generates a usage summary per service which includes:
    - List of endpoints with a description based on their implementation.
    - Upstream and downstream dependencies.
    - Usage summary per endpoint.
5. Once we have details per service:
    - The agent can generate a refined system architecture based on the knowledge obtained per service and their
      dependencies.
    - The agent can add details on the products used per service and endpoints based on the B&P POV.
    - For any gap (i.e., missing details for a dependency), we can ask an SME.

For the first phase, we'll provide entry points for documentation, codebases, and service metrics.

### 3.3 Orchestration loop

Both POVs generate live documentation that requires constant updates. We also want the agent to be aware of new projects
and proactively ask questions about new endpoints and projects. For this we'll define an orchestration loop that will:

1. Keep a master list of projects, code entry points, documentation sources, and documentation generated.
2. Periodically check for new code or documentation and trigger tasks to update B&P and SA.
3. Update the master list based on #2.

---

## 4. Types of Memory

We have defined 3 main "sub-agents", each with different memory requirements.

### 4.1 Orchestration

- **Short-term memory** — Context window required to orchestrate ongoing tasks.
- **Long-term memory** — Used as storage for:
    - Progress of ongoing tasks, in case we need to resume them.
    - Master list of projects, code entry points, documentation sources, and documentation generated.

### 4.2 Business and Product

- **Short-term memory**
    - Context window required for reasoning and to analyze documentation.
    - Context window required when interacting with SMEs.
- **Long-term memory**
    - Generated documentation that will feed back into the agent to update B&P and SA.
    - Generated insights obtained from SMEs.

### 4.3 System Architecture

Same memory profile as **Business and Product**.

---

## 5. External Tools (MCPs)

Based on the original list of tooling to interact with the environment, these are the tools the agent will use.

1. **GitHub MCP** *(existing)*
    - To clone and analyze codebases.
    - To write generated documentation for both B&P and SA as MD files.
2. **Quip MCP** *(existing)*
    - To retrieve existing project documentation.
3. **Monitoring MCP** *(new)*
    - To summarize downstream and upstream dependencies per endpoint, backed by existing telemetry infrastructure and
      services.
4. **Trino MCP** *(existing)*
    - To analyze datasources used by the codebase.

> **Note:** To limit the scope of this version we'll discard more complex and intrusive data sources such as Slack
> channels and email threads.

---

## 6. Retrieval Design — RAG (Module 3)

This project will include a large amount of B&P documentation. Using **RAG** is a good fit to improve the retrieval and
query capabilities of our agents.

There are 2 use cases where we want to use RAG capabilities:

1. **Classic Q&A capabilities with augmented context.**
2. **Filter & select the relevant document(s)** either for direct Q&A or during the knowledge-building workflow of the
   agents.

> Note that #2 is different from traditional Q&A RAG systems where usually the full text that was embedded is added to
> the context. Here we want to use embeddings just to **select** the right document, and when possible (unless the
> document is too large) use the **whole document** as part of the context.
>
> **Note:** Both the **B&P** and **SD** agents use RAG, but neither runs the embedding pipeline
> directly any more. The indexing methodology, chunking strategies, and quality heuristic in this
> section live in the **RAG Service**
> ([Section 9.2](PROJECT_ARCHITECTURE.md#92-rag-service-design)) — a fourth component that owns the
> shared Embeddings Database, the embedding model, the Auto-RAG loop
> ([Section 9.2.1](PROJECT_ARCHITECTURE.md#921-autonomous-rag-loop)), and the ToT chunking-strategy
> sub-graph ([Section 9.2.2](PROJECT_ARCHITECTURE.md#922-tot-chunking-strategy)). At indexing time
> B&P/SD hand a normalized document to `RAG_MCP.index(domain, source_uri, document)`; the service
> picks a chunking strategy via ToT, embeds, and persists chunks tagged with the caller's `domain`.
> At query time they delegate to `RAG_MCP.retrieve(query, domain_filter, mode)` and compose around
> the response — no peer-MCP retrieval call, no merge step, no specialist-side embedding code.

### 6.1 Indexing methodology — principles & assumptions

- We are looking only for **grounded answers** → similarity settings need to be on the high end.
- We'll only index the **latest version** of a document; when a document is updated it will need to be re-indexed.
- We have access to LLMs with context windows that can load full documents. Expected document size is between **1500 and
  4500 characters** (1 to 3 pages).

### 6.2 Chunking strategies

For each document we'll use one of the following chunking strategies:

- Per paragraph.
- Per section.
- Per N characters.

### 6.3 For indexing each document

- **Generate and index summaries** — summarize the content with an LLM with the following focuses:
    - Create a one-paragraph summary and create an embedding on the paragraph.
    - Create a list of the main topics (similar to keywords) and create embeddings on each topic.
    - Create a list of the main takeaways of the document and create embeddings on each takeaway.
- **Split the document** with one of the chunking strategies above and create embeddings on each result.

### 6.4 To verify the quality for each document

Retrieval quality is very important. We'll use a simple heuristic approach with help from LLMs:

- Read the document with an LLM and ask it to generate **N questions and answers** as a student trying to understand the
  document.
- Perform a similarity search between the question and the embeddings generated for the document. If we have results
  over **M percent**, we are done; otherwise, replace the chunking strategy and try again.

### 6.5 Open questions

- What if we cannot reach a similarity result over M percent? Do we discard the document, tag it as a low-confidence
  source, or something else?

> The corresponding query-time question is resolved in the architecture: when the Autonomous RAG
> loop exhausts its rewrite budget at query time, the RAG Service returns `status=low_confidence`
> (or `exhausted`) and the calling specialist returns a low-confidence answer with the closest
> matches. SME escalation only fires from background page builds — see
> [Section 9.2.1](PROJECT_ARCHITECTURE.md#921-autonomous-rag-loop) and
> [Section 9.6](PROJECT_ARCHITECTURE.md#96-sme-interaction).


## 7. Applying Tree of Thoughts — ToT (Module 4)

> Disclaimer: this is a complex topic, unlike the previous sections, I did use Claude Code to help me identify where
> were the best places to use ToT for this project.

The reasoning loop defined in [Section 3](#3-proposed-reasoning-loop-module-2) follows a ReAct-style approach per sub-agent. However, some steps in our
pipeline are **decision points with multiple plausible paths** where the agent's first choice may not be the best one.
For those steps we'll apply **Tree of Thoughts (ToT)** — branching the reasoning into multiple candidate "thoughts,"
evaluating them, and pruning the weaker ones.

### 7.1 Where ToT helps in this project

ToT is a good fit when:

1. Intermediate reasoning steps are non-trivial.
2. It is relatively cheap to draft alternatives.
3. We have a way to rate them.

We have identified 3 places in our design where this applies:

1. **Chunking & indexing strategy selection ([Section 6](#6-retrieval-design--rag-module-3)).** Today we propose iterating chunking strategies sequentially
   if the quality check fails. We'll replace this with ToT:
    - **Thoughts** — each candidate chunking strategy (per paragraph, per section, per N characters, summary-only,
      hybrid).
    - **Expansion** — for each candidate, generate the embeddings and run the LLM-generated Q&A pairs against them.
    - **Evaluation** — the similarity-over-M heuristic becomes the value function.
    - **Search** — beam-search the top-K strategies and prune the rest. We keep the best chunking strategy per document
      instead of trying them one by one.

2. **Gap-filling between code and design ([Section 3.1](#31-business--product-bp-pov), step 4).** When the implementation and the existing design don't
   match, the agent has to decide *how to reconcile them*. Several hypotheses are possible: the design is stale, the
   code is buggy, there is an undocumented feature, or scopes diverged. With ToT the agent can:
    - **Branch** into one hypothesis per candidate explanation.
    - **Expand** each branch by gathering targeted evidence (additional code paths, commit history, related docs).
    - **Score** branches by a simple rubric: evidence coverage, internal consistency, and recency of sources.
    - **Promote to SME** only the top branch(es) instead of escalating every gap. This reduces SME load and improves the
      quality of the questions we ask.

3. **Inferring upstream/downstream dependencies ([Section 3.2](#32-systems-architecture-sa--sd-pov), step 3).** Call-pattern analysis can produce ambiguous
   results (multiple plausible upstream callers, indirect dependencies through brokers/queues). ToT lets the SA agent:
    - **Generate** several candidate dependency graphs.
    - **Verify** each against telemetry (Monitoring MCP), code references, and existing documentation.
    - **Prune** graphs that contradict observed traffic and **keep** the one(s) with the highest agreement score.

### 7.2 ToT loop

1. The agent receives the initial document or task to process.
2. The agent generates **K candidate thoughts**, depending on the decision point:
    - Candidate chunking strategies (RAG indexing).
    - Candidate reconciliation hypotheses (gap-filling between code and design).
    - Candidate dependency graphs (upstream/downstream inference).
3. For each level of depth (up to **D** levels), the agent:
    - Expands every candidate by running tools and retrieving more context.
    - Scores every candidate using an evaluator (an LLM with a rubric prompt, or a heuristic such as similarity-over-M
      for RAG and telemetry agreement for SA).
    - Keeps the top **B** candidates and prunes the rest (beam search).
    - Stops early if the best candidate already meets a quality threshold.
4. The agent returns the best candidate as the result of the decision point.

For the first phase, we'll start with **B=2–3** and **D=2–3** to keep wall-clock time manageable while still providing meaningful
exploration.

### 7.3 Evaluation and pruning

Each ToT use case has its own evaluator, but all of them follow the same pattern: score every candidate, prune the ones
below threshold, and break ties with a secondary signal.

**Scoring rubric** — depends on the decision point:

- **Chunking strategy (RAG, [Section 6](#6-retrieval-design--rag-module-3))** — similarity-over-M heuristic. Generate N student-style Q&A pairs from the
  document, run the questions against the candidate's embeddings, and compute the percentage of questions whose top hit
  lands in the right chunk.
- **Code/design gap reconciliation ([Section 3.1](#31-business--product-bp-pov))** — rubric-based score from a critic LLM. Three dimensions, each on a
  0–3 scale: evidence coverage, internal consistency, and recency of sources.
- **Dependency graph inference ([Section 3.2](#32-systems-architecture-sa--sd-pov))** — telemetry agreement score. The percentage of edges in the candidate
  graph that match observed traffic from the Monitoring MCP, weighted by call volume.

**Who performs the evaluation** — all three flavors are wired in as nodes (or tool calls invoked from nodes) of the
**LangGraph `StateGraph`** that drives the loop (see [Section 7.5](#75-mapping-tot-roles-to-tools)). The `score` node dispatches to the right evaluator
based on the decision point:

- **Heuristic checks** for RAG, written as plain Python functions and called directly from the `score` node.
- **Critic LLM** for gap reconciliation, run as a separate **CrewAI** role with the rubric prompt and invoked from the
  `score` node.
- **Tool calls** for dependency verification — the `score` node calls the **Monitoring MCP** to get the ground truth
  and computes the agreement score in graph state.

**Pruning thresholds** — applied by the `prune` node, which reads the scored candidates from graph state and writes
back the survivors:

- Keep a branch if its score is greater or equal than the per-use-case threshold:
    - RAG — similarity-over-M ≥ 0.7.
    - Gap reconciliation — rubric total ≥ 6/9.
    - Dependency graphs — agreement ≥ 0.8.
- Drop any branch below the threshold.
- After pruning, keep only the top **B** candidates per level.

**Failure conditions** — if no branch reaches the threshold at the maximum depth **D**, the agent falls back depending
on the use case:

- **RAG** — tag the document as a low-confidence source and continue (linked to the open question in [Section 6.5](#65-open-questions)).
- **Gap reconciliation** — escalate to an SME with the top branches as candidate explanations.
- **Dependency graphs** — keep the highest-scoring graph and flag it for SME review.

**Tie-breaking** — if two branches end up within a small margin (scores differ by less than 5%), we'll resolve the tie
with a secondary criterion:

- **RAG** — prefer the chunking strategy with the smaller average chunk size (cheaper retrieval).
- **Gap reconciliation** — run a second critic pass with a different rubric phrasing and take majority vote.
- **Dependency graphs** — prefer the simpler graph (fewer inferred edges).

### 7.4 Search strategy

We'll use **beam search** as our primary strategy.

Beam search is a middle ground between BFS and DFS: at every level we expand all surviving candidates, score them,
and keep only the top **B** (the "beam width") for the next level. The rest are pruned. This bounds the work at
**K + B × D** expansions while still keeping multiple alternatives alive in case the evaluator is noisy.

Why beam search fits our use cases:

- Our decision points have a **small, finite candidate space** (3–5 chunking strategies, 3–4 reconciliation hypotheses,
  a handful of plausible graphs). BFS would expand every branch wastefully, DFS could commit early to a bad path, and
  Monte Carlo sampling is overkill for spaces this small.
- Beam search keeps a **fixed number of candidates per level (B)**, which keeps the per-loop work predictable —
  important since these loops run inside the orchestration cycle ([Section 3.3](#33-orchestration-loop)) and we need
  to bound wall-clock time per project.
- It naturally supports falling back to the surviving beam if a level fails to produce candidates above threshold.

How the strategy is constrained:

- **Compute** — total LLM/tool calls per loop ≈ K + (B × D). With K=4, B=2, D=3 this is around 10 calls per decision.
- **Latency** — we cap depth at **D=3**, so a single ToT loop completes in roughly the time of 3 sequential ReAct turns.
- **Context** — each branch keeps its prompt within the local LLM's context window. Branches that would overflow are
  short-circuited and the highest-scoring partial result is kept.

### 7.5 Mapping ToT roles to tools

- **Thought generator** — a node inside the controller's **LangGraph `StateGraph`** that wraps an **LCEL prompt + LLM
  call**. It runs once at the start of the loop to produce the K initial candidate thoughts and writes them straight
  into the graph state. Keeping it as a node (instead of a separate LCEL chain) avoids a hand-off and lets the rest of
  the loop share the same typed state from the first step.
- **Critic / evaluator** — split across the three evaluation flavors:
    - **Heuristic checks** for RAG, written as plain Python functions and called directly from the `score` node of the
      LangGraph.
    - **Critic agent** for gap reconciliation, defined as a separate **CrewAI** role with the rubric prompt. Keeping the
      critic in CrewAI gives us a clean separation between the agent producing thoughts and the agent judging them.
    - **Tool calls** for dependency verification, executed against the **Monitoring MCP**.
- **Decision maker / controller** — implemented as a **LangGraph `StateGraph`**. The beam-search loop is a graph with
  nodes for `expand`, `score`, `prune`, and `check_threshold`, plus a cyclic edge back to `expand` until depth **D** or
  the threshold is hit. LangGraph fits better than plain LCEL here because the loop is stateful, cyclic, and has
  conditional early-exit edges. The controller lives inside the B&P or SA Service.
- **Memory / state manager**:
    - **LangGraph state** holds the in-flight branch state (active candidates, scores, accumulated evidence) — the
      typed state object is threaded through every node automatically.
    - **MCP** exposes that state externally so other components (or the orchestrator) can observe a ToT loop in
      progress if needed.
    - The B&P / SA long-term storage ([Section 4](#4-types-of-memory)) holds the final decision once the loop converges.

> Note: We replaced LangChain with LangGraph since LangChain is in a deprecation path. 

### 7.6 Insertion point in the architecture

Referring to the high-level architecture in [Section 8](PROJECT_ARCHITECTURE.md#8-high-level-architecture-module-5):

- **Inside the B&P Service** — ToT runs as a sub-routine of the RAG indexing pipeline that writes to the **Embeddings
  Database**. The insertion point is the step that selects the chunking strategy per document, before the embeddings are
  persisted.
- **Inside the SA Service** — ToT runs inside the call-pattern analysis step that produces dependency graphs. It sits
  between the **GitHub MCP** (code references) and the **Monitoring MCP** (telemetry verification).
- **Not inside the Orchestrator** — the orchestrator stays on a ReAct loop. It simply calls the B&P/SA MCPs and treats
  the ToT loop as an internal implementation detail of those services.

### 7.7 Risk and mitigation

**Risk — weak evaluation signals leading to pruning the best branch.**

The gap reconciliation use case relies on a critic LLM scoring branches against a subjective rubric. A noisy critic
could prune the correct hypothesis (e.g., score "the design is stale" lower than "the code is buggy" purely because of
phrasing variance), which would silently degrade the documentation we generate.

**Mitigation**:

- Run the critic with **N=3 independent passes** (different seeds and slightly different rubric phrasings) and average
  the scores. This is a cheap form of self-consistency.
- Apply a **safety floor** — never prune a branch that is supported by direct evidence from a tool call (e.g., a commit
  that explicitly modifies the design boundary, or telemetry that contradicts a reconciliation hypothesis). The
  heuristic check gets veto power over the LLM critic.
- If after mitigation the top branches are still within the tie margin, escalate to an SME instead of forcing a pick.

### 7.8 Recommendation

We'll adopt **ToT selectively** rather than globally:

- Use ToT inside the **B&P RAG indexing pipeline** to choose the chunking strategy per document.
- Use ToT inside the **SA agent** when reconciling code, design, and call patterns to produce dependency graphs.
- Keep the **Orchestrator** on a simpler ReAct loop — its job is scheduling, not deep reasoning.

---

The high-level and low-level design (Sections 8 and 9) lives in [PROJECT_ARCHITECTURE.md](PROJECT_ARCHITECTURE.md).

