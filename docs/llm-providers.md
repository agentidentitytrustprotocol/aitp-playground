# LLM providers

The agent workers can run against either OpenAI or Anthropic, or fall
back to a deterministic stub when no provider is configured. The choice
is process-wide and made via env vars; there's no per-scenario LLM
config.

Source: `agents/base/llm.py`.

## How provider selection works

`select_provider()` reads two things:

1. `LLM_PROVIDER` (default `openai`). Lowercased and trimmed.
2. The matching API key:
   - `openai` requires `OPENAI_API_KEY`.
   - `anthropic` requires `ANTHROPIC_API_KEY`.

If the key for the requested provider is missing, `select_provider()`
returns `None` and the caller falls back to its deterministic stub.
There is no cross-provider failover — the playground's job is to
*demonstrate* the AITP path, so silently swapping providers behind
your back would make debugging harder.

The model name is per-provider, overridable:
- OpenAI: `OPENAI_MODEL` (default `gpt-4o-mini`).
- Anthropic: `ANTHROPIC_MODEL` (default `claude-sonnet-4-6`).

## Two builder functions

`build_chat_model()` constructs a LangChain chat model for LangChain
and LangGraph workers (writer, analyzer). Returns `None` if no
provider is configured or the LangChain package for the provider isn't
installed:

```python
def build_chat_model():
    provider = select_provider()
    if provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model_name("openai"), temperature=0.2)
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model_name("anthropic"))
    return None
```

`build_crewai_llm()` returns a `crewai.LLM` for CrewAI (researcher).
CrewAI uses litellm under the hood, so the model string is in the
`provider/model` form:

```python
def build_crewai_llm():
    provider = select_provider()
    if provider is None: return None
    from crewai import LLM
    return LLM(model=f"{provider}/{model_name(provider)}")
```

## How workers use it

Every worker's business logic checks whether an LLM is available; if
not, it returns its stub:

```python
# agents/writer/chain.py
async def run_writer(findings, *, style="casual"):
    llm = build_chat_model()
    if llm is not None:
        from langchain_core.messages import HumanMessage, SystemMessage
        out = await llm.ainvoke([...])
        return getattr(out, "content", str(out))
    return _stub_article(findings, style)
```

The CrewAI worker is slightly different — it builds the LLM and passes
it as an explicit `Agent(llm=...)` so litellm sees the right provider
prefix:

```python
# agents/researcher/crew.py
def _crewai_ready() -> bool:
    if select_provider() is None: return False
    try: import crewai
    except: return False
    return True

if _crewai_ready():
    from crewai import Agent, Crew, Process, Task
    llm = build_crewai_llm()
    researcher = Agent(role=..., goal=..., llm=llm, ...)
    ...
return _StubCrew(topic, depth)
```

LangGraph (analyzer) gates on both an available chat model **and**
`langgraph` being importable; both conditions must hold to take the
real path.

## Stubs

Each worker has a deterministic stub:
- `_StubCrew` in `agents/researcher/crew.py` — produces a bullet list
  with a recognizable phrase ("continues to attract significant
  research interest").
- `_stub_article` in `agents/writer/chain.py` — produces a markdown
  article that starts with `# Article ({style})`.
- `_stub_analyze` in `agents/analyzer/graph.py` — produces a
  `{summary, risks}` dict mentioning "Misaligned grants…".

The marker strings are how `tests/integration/test_llm_e2e.py` detects
that a real LLM call did *not* happen — see [testing.md](testing.md).

## Async behavior

All three workers are async-first. CrewAI ≥1.0 refuses sync
`kickoff()` inside a running event loop, so the researcher uses
`crew.kickoff_async()` when available:

```python
result = await crew.kickoff_async() if hasattr(crew, "kickoff_async") else crew.kickoff()
```

The stub falls back to the sync variant. Writer and analyzer use
`await llm.ainvoke(...)` directly.

## Adding a new provider

If you want to add, say, a Gemini provider:

1. Pick the provider string — let's say `"gemini"`.
2. Update `Provider` and the two builders in `agents/base/llm.py`:

   ```python
   Provider = Literal["openai", "anthropic", "gemini"]

   _GEMINI_DEFAULT = "gemini-1.5-flash"

   def select_provider():
       requested = (os.environ.get("LLM_PROVIDER") or "openai").strip().lower()
       if requested == "openai" and os.environ.get("OPENAI_API_KEY"):
           return "openai"
       if requested == "anthropic" and os.environ.get("ANTHROPIC_API_KEY"):
           return "anthropic"
       if requested == "gemini" and os.environ.get("GOOGLE_API_KEY"):
           return "gemini"
       return None

   def model_name(provider):
       if provider == "gemini":
           return os.environ.get("GEMINI_MODEL") or _GEMINI_DEFAULT
       ...

   def build_chat_model():
       ...
       if provider == "gemini":
           from langchain_google_genai import ChatGoogleGenerativeAI
           return ChatGoogleGenerativeAI(model=model_name("gemini"))
       ...
   ```

3. For CrewAI, follow whatever model-string scheme litellm expects
   (often `gemini/<model>`).
4. Add the LangChain package to `pyproject.toml`'s extras
   (`researcher`, `writer`, `analyzer`, `all-agents`).
5. Document the new env var in `.env.example`.

No worker changes required — they all go through the shared selector.

## Env reference

| Var | Default | Notes |
| --- | --- | --- |
| `LLM_PROVIDER` | `openai` | `openai` or `anthropic` today. |
| `OPENAI_API_KEY` | _(empty)_ | Required when `LLM_PROVIDER=openai` for the real path. |
| `OPENAI_MODEL` | `gpt-4o-mini` | Any model your key can access. |
| `ANTHROPIC_API_KEY` | _(empty)_ | Required when `LLM_PROVIDER=anthropic`. |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Any model your key can access. |

When in doubt about whether the real path ran, look for `llm.started`
and `llm.complete` events in the run log alongside the stub marker
check from `test_llm_e2e.py`.
