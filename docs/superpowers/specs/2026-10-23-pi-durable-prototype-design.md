# Harness² CLI with Pi Durable and Code Mode — prototype design

## Purpose

Run every model-backed call of the built-in `tasks` suite through Pi's SDK/runtime: task rollouts, improver, selector, reflector, consolidation and judge. Keep each Pi conversation, tool call and Code Mode script in Pi Durable storage, while preserving Harness²'s task library and run artifacts. An interrupted run should resume the same Pi submission and workspace when invoked again.

The reference implementation is `3ae3ae/harness2-cli` at `0c811344a4edfe34960b2fc8f286369d7e81fb27`; `google-research/harness2` supplies the method. This prototype targets Pi 1.4.0 and POSIX, using Node 22.19+ and Python 3.12–3.13.

## Boundaries

- `harness2` remains the Python orchestrator and keeps its existing recursion, library, events, diff and apply logic.
- A Node host invokes `@earendil-works/pi-durable` directly, with `ModelRuntime` from `@earendil-works/pi-coding-agent`, `CodingTools`, and the Durable Code Mode extension. It uses Pi's auth directory (`PI_CODING_AGENT_DIR`) and does not read or copy credentials into run artifacts.
- A Python Pi transport creates a stable descriptor for each call and invokes the host. A call has one stable workspace, SQLite database, and `requestId`. A repeated invocation must match the descriptor fingerprint and return or resume the original submission; a changed request fails closed.
- The Pi substrate exposes `AGENTS.md`, `.agents/skills/*/SKILL.md` and skill scripts as the initial editable surface. The host injects `AGENTS.md` and a concise skill index; scripts can read skill files through Code Mode. The prototype does not load arbitrary Pi CLI extensions or MCP servers into the Durable host.
- Pi is available as both `executor.name = "pi"` and each model channel (`improver`, `selector`, `reflector`, `judge`). Other executors and channels retain their current behavior.

## Data and recovery

Each Harness² run still has `run.json`, `events.jsonl`, `rollouts/`, `adapt/`, `library/` and `consolidated/`. Every Pi-backed call additionally retains a request descriptor, `session.sqlite`, host result and diagnostics under its existing rollout/audit directory. The descriptor stores only paths, model choices, request text and a content hash; no token or key. The host opens the same database and submits with the same `requestId` after restart. It writes its terminal result atomically.

An incomplete Pi rollout retains its project worktree or file workspace. The workspace baseline is recorded before starting Pi. Harness² never replaces an incomplete Pi directory or silently resumes it with a different harness fingerprint. The improver's context directory is also retained until its output has been harvested. Completed calls use the existing Harness² cache and completion marker. A failed or parked Durable task remains inspectable and is reported; it is not converted to an empty answer or an automatic fresh run.

Only one host process may own a session database at a time. The Python launcher holds a POSIX file lock and passes the lock descriptor to the child so it remains held if the Python parent dies. On timeout, Python asks Node to clean up active command process groups before exiting. The host records an `unsafe-effect.json` marker before a write or shell command and clears it only after the submission settles. If the host dies while that marker exists, automatic recovery stops until the user inspects the workspace and process tree and manually clears it. The prototype does not claim exactly-once external effects.

## Configuration and user flow

`harness2 init --executor pi` produces a Pi model/channel configuration. `harness2 doctor` checks Node, the Pi runtime dependencies, auth-file presence and model syntax before any paid call; `doctor --live` checks that the model can respond. `harness2 run --dry-run` keeps the existing call estimate; `harness2 run` records the Pi sessions and can be reissued to resume. `status`, `diff`, `eval`, `apply`, `judge` and `report` continue to consume the usual Harness² artifacts. The Pi runtime dependency is opt-in and pinned to 1.4.0 in a separate package.

## Acceptance

1. Offline tests prove Pi settings, surface import/export, all role dispatch paths, stable descriptor identity, single-owner lock, and resume behavior after a killed host, with no duplicate invocation.
2. A real Pi Durable + Code Mode smoke test uses one small task and verifies that a restarted host reads the same submission and SQLite record. It must not rely on output text alone; inspect the stored task/submission identity.
3. The project's Python unit suites and Ruff checks pass. Document any environment-specific or live-model limit precisely.

## Rulings and costs if wrong

- Use the general-purpose CLI fork as base; the Google release remains methodological reference. If the fork diverges from the method in a relevant way, the adapter may need rebasing.
- Keep one Durable database per model-backed call. This simplifies ownership and resume, but creates many small databases; later consolidation can change storage without changing call identity.
- Limit the first surface to instructions and skills. This gives a useful Pi harness without unsafe automatic loading of arbitrary extensions, but cannot optimize Pi CLI-specific hooks/MCP until a later contract defines them.
- Use the Pi SDK credential runtime, not a custom token store. This ties the prototype to Pi 1.4.0 APIs, which the package labels experimental.

## Architecture review amendments

The independent review found seven integration risks. The following contracts resolve them for this prototype:

- A Pi call's identity is its stable run-relative slot (role/task/candidate/sample) plus an immutable request fingerprint. Model, effort, Pi runtime version, prompt, system instructions, initial context digest and harness digest are in that fingerprint. An incomplete workspace's later edits do not alter identity. A mismatch fails closed.
- Before invoking Pi, Harness² writes a preparation manifest with initial context digest, baseline commit for project workspaces, and descriptor fingerprint. On resume it reuses that workspace exactly; it never reinstalls the harness or copies context over the mutated workspace. If preparation has no manifest and no Pi database, it may be recreated.
- A Pi timeout, lost host, blocked task or other nonterminal condition raises a recoverable stop through the orchestrator. It does not produce a final `meta.json`, `.adapt_complete`, judge result or fallback choice. A completed Durable submission can regenerate missing Harness² artifacts on the next call.
- The same file lock covers preparation, Node execution and export. The lock descriptor is inherited by the Node child, so a surviving child keeps ownership if Python exits. The next process waits for release or returns a clear busy result; it never opens SQLite concurrently.
- The judge's context and audit directory are stable inside its rollout directory. All roles use the same Pi transport, and all Pi sessions retain their database and transcript. A host result with an empty answer is a terminal error, not a valid candidate.
- `CodingTools` and Code Mode are installed in a fixed order before `resume()`, with a `NodeExecutionEnv` bound to the persistent workspace. Code Mode uses the same Pi 1.4.0 sandbox implementation after restart. An interrupted write or shell command leaves `unsafe-effect.json`; recovery stops until it is reconciled, and never creates a new submission automatically.
- The prototype's deterministic tests use separated processes for recovery and duplicate ownership. A live model smoke is additional evidence, not a replacement for those tests.

The review found no previously recorded ruling to contradict. These amendments supersede the shorter statements above where more precise.
