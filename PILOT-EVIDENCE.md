# Pilot evidence

Three small, real dispatches, each run through `router.dispatch.dispatch(execute=True)`, the same
implementation called by `codex-route --execute`,
against the actual installed `codex` and `claude` CLIs, with an output
sanity check afterwards. This is **not** a benchmark and makes no claim
about cost, speed, or output quality relative to any alternative — each
pilot only confirms that a dispatch to that lane actually ran, with the
model and effort the router requested, and produced a working answer.
`astra` was not exercised (it requires a named human approval per
dispatch and was intentionally left out of this pilot batch); no
fallback or override path was used in any of the three.

Full, unsanitised evidence (including local session/thread identifiers
and file paths, which do not belong in a public repository) is held
outside this repository.

## luna (Codex CLI)

- Requested: `gpt-5.6-luna`, effort `medium`.
- Model evidence: Codex CLI's own persisted session `turn_context`
  metadata reports the same model and effort that was requested. This is
  the CLI's own local record of what it ran, not an independent
  server-side attestation.
- Task: a short factual arithmetic question.
- Output check: **PASS** — correct, well-formed answer returned.

## terra (Codex CLI)

- Requested: `gpt-5.6-terra`, effort `medium`.
- Model evidence: same basis as `luna` above (Codex CLI's persisted
  `turn_context`).
- Task: a small, self-contained coding exercise (write a short function
  with inline test assertions).
- Output check: **PASS** — the returned code was syntactically valid and
  its own inline assertions held.

## claude (Claude CLI)

- Requested: `sonnet`, effort argument `medium`.
- Model evidence: the Claude CLI's own structured JSON result event
  reports `modelUsage["claude-sonnet-5"].canonicalModel = claude-sonnet-5` and
  `provider = firstParty` for the turn that ran — a provider-returned
  field in the CLI's own output, not a third-party attestation.
- Task: a short, deterministic text-processing task (sort a short list of
  words).
- Output check: **PASS** — the returned list was correctly sorted.
- Billing note: the CLI's own result also reports a `total_cost_usd`
  figure. That is a reported list-price metric from the CLI, not proof
  that any charge actually occurred — this router does not claim a hard
  cost cap or verified billing outcome anywhere, and this pilot doesn't
  change that.

## What this does and doesn't prove

**Proves:** for one real dispatch per lane, the router's classification,
gates, and argv construction produced a working `codex`/`claude`
invocation that ran the intended model at the intended effort level and
returned a usable answer.

**Does not prove:** anything about cost savings, latency, output quality,
or superiority versus any other routing approach. It also does not
substitute for the router's own `observed_model` field, which is always
`null` by design — see the README's Auth checks section for why. This
pilot's "model evidence" comes from inspecting each CLI's own session/
result metadata directly, outside the router, specifically because the
router currently leaves that field unpopulated.
