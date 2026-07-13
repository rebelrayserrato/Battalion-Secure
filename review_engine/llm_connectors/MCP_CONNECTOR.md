# MCP Multi-Model Connector (RAYAAAA-246, Phase B1)

Battalion acts as an **MCP client** connecting *out* to three model backends and
exposes a uniform provider abstraction the B3 chat surface consumes to route a
prompt to one provider or fan out to all of them at once (the compare view).

| Provider  | Schema                     | Default model                          | Key env var        |
|-----------|----------------------------|----------------------------------------|--------------------|
| `openai`  | OpenAI `chat/completions`  | `gpt-4o-mini`                          | `OPENAI_API_KEY`   |
| `hermes`  | OpenAI-compatible (OpenRouter default) | `nousresearch/hermes-3-llama-3.1-70b` | `HERMES_API_KEY`   |
| `anthropic` | Anthropic `messages`     | `claude-sonnet-5`                      | `ANTHROPIC_API_KEY`|

OpenRouter can proxy Codex + Claude under one key, so `HERMES_BASE_URL` /
`OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL` and the `*_MODEL` vars let you repoint any
adapter without code changes.

## Safety posture (synthetic-only until Phase C)

- **OFF by default.** Real egress happens only when `MCP_CONNECTOR_ENABLED=1`.
  Otherwise every provider runs in deterministic **mock mode** — no network I/O,
  no key required, safe for build/CI/tests and demos.
- `MCP_MOCK=1` forces mock mode even when the connector is enabled.
- A provider with no key silently falls back to mock mode (never emits an
  unauthenticated request).
- Keys are read from the environment only; **nothing secret lives in git**
  (RAYAAAA-225 pattern). Egress must stay behind the internal auth gate
  (RAYAAAA-192/223). **No real client PII** flows through this connector until the
  Phase C gate — synthetic documents only.

## Usage (the interface B3 consumes)

```python
from review_engine.llm_connectors import build_default_client, ProviderRequest

client = build_default_client()                 # honours the feature flag
req = ProviderRequest(prompt="Summarise the discrepancies.",
                      context=["SRC-1: ...", "SRC-2: ..."])  # B2 supplies context

# route to one provider
resp = client.generate("anthropic", req)        # -> ProviderResponse

# fan out to all providers for the compare view
results = client.fan_out_sync(req)              # {name: ProviderResponse}  (sync, e.g. Streamlit)
results = await client.fan_out(req)             # async equivalent
```

`ProviderResponse` fields: `provider`, `model`, `text`, `ok`, `error`, `mock`,
`latency_ms`. Failures are isolated — one slow/erroring provider never breaks the
batch; it comes back as `ok=False` with an `error` string.

Retrieval (B2) is intentionally kept separate: callers pass already-retrieved
context snippets into `ProviderRequest.context`.

## Env template (secret-free — do NOT commit real keys)

```bash
# Feature flag: 1 to allow live egress, unset/0 for mock-only (default)
MCP_CONNECTOR_ENABLED=0
MCP_MOCK=0
# Provisioned out-of-band; never commit real values
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
HERMES_API_KEY=
# Optional overrides
# OPENAI_MODEL=gpt-4o-mini
# ANTHROPIC_MODEL=claude-sonnet-5
# HERMES_MODEL=nousresearch/hermes-3-llama-3.1-70b
# HERMES_BASE_URL=https://openrouter.ai/api/v1
```
