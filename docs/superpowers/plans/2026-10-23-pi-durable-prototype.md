# Pi Durable Harness² Prototype Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run all built-in Harness² model calls through Pi Durable with Code Mode and stable resumable sessions.

**Architecture:** Python keeps orchestration and calls an opt-in Node Pi Durable host for each session. The host records one submission per stable call in SQLite. Pi rollouts and improver sessions retain their workspaces until Harness² records completion.

**Tech Stack:** Python 3.12–3.13, Node 22.19+, Pi 1.4.0 packages, SQLite, unittest, Ruff.

**Spec:** `docs/superpowers/specs/2026-10-23-pi-durable-prototype-design.md`

## Global Constraints

- The built-in tasks suite supports Pi as executor and all model channels; benchmark plugin behavior is unchanged.
- Credentials stay in `PI_CODING_AGENT_DIR`; no auth bytes in run artifacts.
- A repeated request with different content fails closed.
- Keep incomplete Pi workspaces and session databases; never claim exactly-once external effects.
- Pi runtime dependencies are opt-in and pinned to 1.4.0.

## Review Focus

- Parent dies while the Node host still owns SQLite: the next launcher must not open it concurrently.
- Child dies after Durable commits an answer but before host result write: reopening must reuse the submission.
- Harness fingerprint changes while a rollout is incomplete: recovery must reject it.
- Retry after Code Mode has made an external effect: no automatic fresh submission.
- Sensitive provider variables in logs or descriptors: only names and paths may appear.

---

### Task 1: Durable Pi host

**Files:** Create `pi-runtime/package.json`, `pi-runtime/host.mjs`; test `pi-runtime/test/host.test.mjs`.

**Interfaces:** `node pi-runtime/host.mjs <descriptor.json>` consumes a descriptor with stable `requestId`, `cwd`, `sessionDir`, `model`, `effort`, `system`, `prompt`; writes `result.json` atomically and returns a nonzero status on failure. It opens Pi Durable SQLite, installs CodingTools and Code Mode, uses `ModelRuntime` credentials, and reuses the same submission after restart.

- [ ] Add a failing offline host test for descriptor mismatch and persisted submission reuse.
- [ ] Run the host tests and observe the intended failure.
- [ ] Implement the minimal host and pinned package manifest.
- [ ] Run host tests and the real Code Mode smoke test if credentials are available.
- [ ] Commit.

### Task 2: Python Pi transport and surface

**Files:** Create `harness2/pi_runtime.py`, `harness2/substrate/pi.py`; modify `harness2/core/spec.py`; test `tests/test_pi_runtime.py`, `tests/test_pi_surface.py`.

**Interfaces:** `run_pi_session(...) -> PiResult` creates or validates a descriptor, locks the session, invokes the host and reads its result. `PiSubstrate` imports/exports the Pi harness surface and renders the host's trajectory into the existing grammar.

- [ ] Add failing tests for stable identity, mismatch, lock, surface and trajectory.
- [ ] Run focal tests and observe failure.
- [ ] Implement transport and surface.
- [ ] Run focal tests.
- [ ] Commit.

### Task 3: Resume rollouts and improver sessions

**Files:** Modify `harness2/workspace.py`, `harness2/suite.py`, `harness2/core/proposer.py`; test `tests/test_pi_workflow.py`.

**Interfaces:** The built-in adapter dispatches Pi executor to `run_pi_session`, retaining a fixed rollout workspace until terminal. `Proposer._pi` uses the same transport; Pi role workspaces are stable under its audit directory and are never discarded while incomplete.

- [ ] Add failing tests for killed host then resumed rollout and improver, same workspace and no second submission.
- [ ] Run focal tests and observe failure.
- [ ] Implement Pi-only persistence and dispatch, preserving other channels.
- [ ] Run focal tests.
- [ ] Commit.

### Task 4: CLI configuration, doctor and documentation

**Files:** Modify `harness2/settings.py`, `harness2/cli/init.py`, `harness2/cli/doctor.py`, `harness2/cli/guide.py`, `docs/configuration.md`, `README.md`, `CHANGELOG.md`; test `tests/test_pi_cli.py`.

**Interfaces:** `harness2 init --executor pi`, Pi role settings and `doctor` detect the runtime; quick start documents opt-in install and recovery.

- [ ] Add failing CLI tests for init, config and doctor.
- [ ] Run focal tests and observe failure.
- [ ] Implement CLI and docs.
- [ ] Run the Python core and plugin suites, Ruff check/format, Node host tests and one live smoke if possible.
- [ ] Commit.

## Review amendments to task contracts

Task 1 stores the Pi runtime version and extension fingerprint in the descriptor and reuses a settled submission to rebuild `result.json` after a host crash. It must reject an empty answer.

Task 2's identity uses a stable slot independent of temporary paths plus the immutable model/effort/runtime/prompt/system/context/harness fingerprint. The process lock begins before preparation and is inherited by Node. A different fingerprint or concurrent owner fails closed.

Task 3 records workspace preparation before launching Pi, keeps incomplete workspaces (including timeout/quota), and raises a recoverable stop instead of writing a completion marker. It gives judge runs their own stable context/audit directory and tests all roles.

Task 4 documents that arbitrary Pi CLI extensions and MCP servers are outside the prototype. The final gate includes separate-process crash/restart tests and checks events for no double count.
