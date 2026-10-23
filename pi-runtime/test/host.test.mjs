import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
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
      fauxAssistantMessage([fauxToolCall('codemode', { code: 'text(\"SCRIPT_OK\")' })], { stopReason: 'toolUse' }),
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
