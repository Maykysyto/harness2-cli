import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import test from 'node:test';
import { createModels } from '@earendil-works/pi-ai/models';
import { fauxAssistantMessage, fauxProvider, fauxText, fauxToolCall } from '@earendil-works/pi-ai/providers/faux';
import { runDescriptor } from '../host.mjs';

test('reopening a settled call uses its original Pi submission', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'h2-pi-host-'));
  try {
    const faux = fauxProvider();
    faux.setResponses([fauxAssistantMessage('ONE')]);
    const models = createModels();
    models.setProvider(faux.provider);
    const descriptor = {
      version: 1, slot: 'rollout/one', fingerprint: 'same-request',
      requestId: 'h2-rollout-one', sessionDir: dir, cwd: dir,
      model: 'faux/faux-1', effort: 'low', system: 'Answer briefly.', prompt: 'Reply ONE.',
    };
    const path = join(dir, 'request.json');
    await writeFile(path, JSON.stringify(descriptor));
    const first = await runDescriptor(path, { models });
    assert.equal(first.answer, 'ONE');
    assert.equal(first.status, 'done');
    const sessionId = first.submissionId;
    await rm(join(dir, 'result.json'));
    const second = await runDescriptor(path, { models });
    assert.equal(second.answer, 'ONE');
    assert.equal(second.submissionId, sessionId);
    assert.equal(faux.getPendingResponseCount(), 0);
    assert.equal(JSON.parse(await readFile(join(dir, 'result.json'), 'utf8')).submissionId, sessionId);
    const trajectory = await readFile(join(dir, 'trajectory.jsonl'), 'utf8');
    assert.match(trajectory, /ONE/);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});


test('Pi Durable executes Code Mode and records its nested result', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'h2-pi-code-'));
  try {
    const faux = fauxProvider();
    faux.setResponses([
      fauxAssistantMessage([fauxToolCall('codemode', { code: 'await tools.write({path:"output/code-mode.txt",content:"SCRIPT_OK"}); const r = await tools.bash({command:"cat output/code-mode.txt"}); text(r.output)' })], { stopReason: 'toolUse' }),
      fauxAssistantMessage([fauxText('AFTER')]),
    ]);
    const models = createModels();
    models.setProvider(faux.provider);
    const descriptor = {
      version: 1, slot: 'role/improver', fingerprint: 'code-request',
      requestId: 'h2-code-one', sessionDir: dir, cwd: dir,
      model: 'faux/faux-1', effort: 'low', system: '', prompt: 'Use Code Mode.',
    };
    const path = join(dir, 'request.json');
    await writeFile(path, JSON.stringify(descriptor));
    const result = await runDescriptor(path, { models });
    assert.equal(result.answer, 'AFTER');
    assert.equal(await readFile(join(dir, 'output/code-mode.txt'), 'utf8'), 'SCRIPT_OK');
    const trajectory = await readFile(join(dir, 'trajectory.jsonl'), 'utf8');
    assert.match(trajectory, /SCRIPT_OK/);
    assert.match(trajectory, /codemode/);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('Durable host loads the staged Pi instructions and skill index', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'h2-pi-harness-'));
  try {
    await writeFile(join(dir, 'AGENTS.md'), 'Unique harness instruction: lantern.\n');
    const { mkdir } = await import('node:fs/promises');
    await mkdir(join(dir, '.agents/skills/checker'), { recursive: true });
    await writeFile(join(dir, '.agents/skills/checker/SKILL.md'), '---\nname: checker\ndescription: Check the work\n---\n');
    const faux = fauxProvider();
    faux.setResponses([(request) => fauxAssistantMessage(
      JSON.stringify(request.messages).includes('lantern') &&
      JSON.stringify(request.messages).includes('.agents/skills/checker/SKILL.md') ? 'YES' : 'NO',
    )]);
    const models = createModels();
    models.setProvider(faux.provider);
    const path = join(dir, 'request.json');
    await writeFile(path, JSON.stringify({
      version: 1, slot: 'rollout/harness', fingerprint: 'harness-request',
      requestId: 'h2-harness-one', sessionDir: dir, cwd: dir,
      model: 'faux/faux-1', effort: 'low', system: '', prompt: 'Can you see the harness?',
    }));
    const result = await runDescriptor(path, { models });
    assert.equal(result.answer, 'YES');
    const frozen = await readFile(join(dir, 'effective-system.txt'), 'utf8');
    await writeFile(join(dir, 'AGENTS.md'), 'Changed instruction.\n');
    await rm(join(dir, 'result.json'));
    await runDescriptor(path, { models });
    assert.equal(await readFile(join(dir, 'effective-system.txt'), 'utf8'), frozen);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('a process killed after settlement resumes without a second model response', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'h2-pi-kill-'));
  try {
    const descriptor = {
      version: 1, slot: 'role/selector', fingerprint: 'kill-request',
      requestId: 'h2-kill-one', sessionDir: dir, cwd: dir,
      model: 'faux/faux-1', effort: 'low', system: '', prompt: 'Say ONE.',
    };
    const path = join(dir, 'request.json');
    await writeFile(path, JSON.stringify(descriptor));
    const hostUrl = pathToFileURL(join(import.meta.dirname, '../host.mjs')).href;
    const source = `
      import { writeFile } from 'node:fs/promises';
      import { createModels } from '@earendil-works/pi-ai/models';
      import { fauxAssistantMessage, fauxProvider } from '@earendil-works/pi-ai/providers/faux';
      const { runDescriptor } = await import(${JSON.stringify(hostUrl)});
      const faux = fauxProvider();
      faux.setResponses([fauxAssistantMessage(process.env.CRASH ? 'ONE' : 'TWO')]);
      const models = createModels();
      models.setProvider(faux.provider);
      const result = await runDescriptor(${JSON.stringify(path)}, {
        models,
        afterSettled: process.env.CRASH ? () => process.kill(process.pid, 'SIGKILL') : undefined,
      });
      await writeFile(${JSON.stringify(join(dir, 'child-result.json'))},
        JSON.stringify({ answer: result.answer, pending: faux.getPendingResponseCount() }));
    `;
    const first = spawnSync(process.execPath, ['--input-type=module', '-e', source], {
      cwd: join(import.meta.dirname, '..'), env: { ...process.env, CRASH: '1' }, timeout: 10000,
    });
    assert.equal(first.signal, 'SIGKILL', first.stderr?.toString());
    await assert.rejects(readFile(join(dir, 'result.json')));
    const second = spawnSync(process.execPath, ['--input-type=module', '-e', source], {
      cwd: join(import.meta.dirname, '..'), env: { ...process.env, CRASH: '' }, timeout: 10000,
    });
    assert.equal(second.status, 0, second.stderr?.toString());
    const result = JSON.parse(await readFile(join(dir, 'child-result.json'), 'utf8'));
    assert.deepEqual(result, { answer: 'ONE', pending: 1 });
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('a process killed after a tool effect refuses automatic resume', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'h2-pi-effect-'));
  const childDir = await mkdtemp(join(import.meta.dirname, 'effect-child-'));
  try {
    const path = join(dir, 'request.json');
    await writeFile(path, JSON.stringify({
      version: 1, slot: 'role/improver', fingerprint: 'effect-request',
      requestId: 'h2-effect-one', sessionDir: dir, cwd: dir,
      model: 'faux/faux-1', effort: 'low', system: '', prompt: 'Write a file.',
    }));
    const hostUrl = pathToFileURL(join(import.meta.dirname, '../host.mjs')).href;
    const source = `
      import { createModels } from '@earendil-works/pi-ai/models';
      import { fauxAssistantMessage, fauxProvider, fauxToolCall } from '@earendil-works/pi-ai/providers/faux';
      const { runDescriptor } = await import(${JSON.stringify(hostUrl)});
      const faux = fauxProvider();
      faux.setResponses([
        fauxAssistantMessage([fauxToolCall('codemode', { code: 'text(await tools.write({path:"effect.txt",content:"done"}))' })], { stopReason: 'toolUse' }),
        fauxAssistantMessage('AFTER'),
      ]);
      const models = createModels(); models.setProvider(faux.provider);
      await runDescriptor(${JSON.stringify(path)}, {
        models, afterEffect: () => process.kill(process.pid, 'SIGKILL'),
      });
    `;
    const childFile = join(childDir, 'child.mjs');
    await writeFile(childFile, source);
    const killed = spawnSync(process.execPath, [childFile], {
      cwd: join(import.meta.dirname, '..'), timeout: 10000,
    });
    assert.equal(killed.signal, 'SIGKILL', `status=${killed.status} ${killed.stderr?.toString()}`);
    assert.match(await readFile(join(dir, 'unsafe-effect.json'), 'utf8'), /writeFile/);
    assert.equal(await readFile(join(dir, 'effect.txt'), 'utf8'), 'done');
    await assert.rejects(runDescriptor(path), /unfinished external effect/);
  } finally {
    await rm(childDir, { recursive: true, force: true });
    await rm(dir, { recursive: true, force: true });
  }
});
