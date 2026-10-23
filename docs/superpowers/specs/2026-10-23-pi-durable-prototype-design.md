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

Only one host process may own a session database at a time. The Python launcher holds a POSIX file lock and passes the descriptor to the child so the lock remains held if the Python parent dies. On a timeout, the child is stopped and the SQLite record remains for a later resume. A tool whose external effect happened before the crash may require reconciliation; the prototype does not claim exactly-once effects.

## Configuration and user flow

`harness2 init --executor pi` produces a Pi model/channel configuration. `harness2 doctor` checks Node, the Pi runtime dependencies, the auth/model choice, and the harness surface before any paid call. `harness2 run --dry-run` keeps the existing call estimate; `harness2 run` records the Pi sessions and can be reissued to resume. `status`, `diff`, `eval`, `apply`, `judge` and `report` continue to consume the usual Harness² artifacts. The Pi runtime dependency is opt-in and pinned to 1.4.0 in a separate package.

## Acceptance

1. Offline tests prove Pi settings, surface import/export, all role dispatch paths, stable descriptor identity, single-owner lock, and resume behavior after a killed host, with no duplicate invocation.
2. A real Pi Durable + Code Mode smoke test uses one small task and verifies that a restarted host reads the same submission and SQLite record. It must not rely on output text alone; inspect the stored task/submission identity.
3. The project's Python unit suites and Ruff checks pass. Document any environment-specific or live-model limit precisely.

## Rulings and costs if wrong

- Use the general-purpose CLI fork as base; the Google release remains methodological reference. If the fork diverges from the method in a relevant way, the adapter may need rebasing.
- Keep one Durable database per model-backed call. This simplifies ownership and resume, but creates many small databases; later consolidation can change storage without changing call identity.
- Limit the first surface to instructions and skills. This gives a useful Pi harness without unsafe automatic loading of arbitrary extensions, but cannot optimize Pi CLI-specific hooks/MCP until a later contract defines them.
- Use the Pi SDK credential runtime, not a custom token store. This ties the prototype to Pi 1.4.0 APIs, which the package labels experimental.
