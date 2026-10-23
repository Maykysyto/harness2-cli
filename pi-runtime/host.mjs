import { readdir, rename, readFile, writeFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { BACKGROUND_CONTEXT } from '@earendil-works/chord/context';
import { CodemodeSandbox } from '@earendil-works/pi-codemode';
import { ModelRuntime } from '@earendil-works/pi-coding-agent';
import { AssistantEntry, createRegistry, Harness } from '@earendil-works/pi-durable';
import { createCodemodeExtension } from '@earendil-works/pi-durable/codemode';
import { openNodeSqliteStorage } from '@earendil-works/pi-durable/storage/sqlite/node';
import { CodingTools } from '@earendil-works/pi-durable/tools';

const CONTEXT = BACKGROUND_CONTEXT;

function assertDescriptor(value, path) {
  if (!value || value.version !== 1) throw new Error('unsupported Pi descriptor version');
  for (const key of ['slot', 'fingerprint', 'requestId', 'sessionDir', 'cwd', 'model', 'prompt']) {
    if (typeof value[key] !== 'string' || !value[key]) throw new Error(`missing ${key}`);
  }
  if (resolve(value.sessionDir) !== dirname(resolve(path))) {
    throw new Error('sessionDir must be the descriptor directory');
  }
  if (!value.model.includes('/')) throw new Error('model must be provider/model');
  return value;
}

async function atomicJson(path, value) {
  const tmp = `${path}.${process.pid}.tmp`;
  await writeFile(tmp, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600 });
  await rename(tmp, path);
}

async function writeTrajectory(root, path) {
  const records = [];
  let cursor;
  do {
    const page = await root.entries({ order: 'ascending' }, 512, cursor, CONTEXT);
    for (const entry of page.items) records.push(JSON.stringify(entry));
    cursor = page.next;
  } while (cursor);
  const tmp = `${path}.${process.pid}.tmp`;
  await writeFile(tmp, `${records.join('\n')}\n`, { mode: 0o600 });
  await rename(tmp, path);
}

async function harnessInstructions(cwd, system) {
  const parts = [system || ''];
  try {
    const agents = await readFile(join(cwd, 'AGENTS.md'), 'utf8');
    if (agents.length > 8000) throw new Error('AGENTS.md exceeds the Pi surface cap (8000 chars)');
    parts.push(`# Project instructions\n${agents}`);
  } catch (error) { if (error.code !== 'ENOENT') throw error; }
  const skills = join(cwd, '.agents', 'skills');
  let directories = [];
  try { directories = await readdir(skills, { withFileTypes: true }); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  const index = [];
  for (const entry of directories.sort((a, b) => a.name.localeCompare(b.name))) {
    if (!entry.isDirectory()) continue;
    const path = `.agents/skills/${entry.name}/SKILL.md`;
    let body;
    try { body = await readFile(join(cwd, path), 'utf8'); }
    catch (error) { if (error.code === 'ENOENT') continue; throw error; }
    const description = body.match(/^description:\s*(.+)$/m)?.[1]?.trim();
    if (description) index.push(`- ${entry.name}: ${description} (${path})`);
  }
  if (index.length) parts.push(`# Available skills\nRead the matching SKILL.md on demand.\n${index.join('\n')}`);
  return parts.filter(Boolean).join('\n\n');
}

async function frozenInstructions(descriptor) {
  const path = join(descriptor.sessionDir, 'effective-system.txt');
  try { return await readFile(path, 'utf8'); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  const content = await harnessInstructions(descriptor.cwd, descriptor.system);
  const tmp = `${path}.${process.pid}.tmp`;
  await writeFile(tmp, content, { mode: 0o600 });
  await rename(tmp, path);
  return content;
}

function assistantText(entry) {
  const message = entry?.model?.find((part) => part?.role === 'assistant');
  return message?.content?.filter((part) => part.type === 'text').map((part) => part.text).join('') ?? '';
}

async function modelsFromPi() {
  const agentDir = process.env.PI_CODING_AGENT_DIR;
  if (!agentDir) throw new Error('PI_CODING_AGENT_DIR must name an existing Pi auth directory');
  return ModelRuntime.create({
    authPath: join(agentDir, 'auth.json'),
    modelsPath: join(agentDir, 'models.json'),
    modelsStorePath: join(agentDir, 'models-store.json'),
    refreshOnCreate: false,
    allowModelNetwork: false,
  });
}

export async function runDescriptor(path, { models } = {}) {
  const descriptor = assertDescriptor(JSON.parse(await readFile(path, 'utf8')), path);
  const resultPath = join(descriptor.sessionDir, 'result.json');
  let prior;
  try { prior = JSON.parse(await readFile(resultPath, 'utf8')); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  if (prior) {
    if (prior.fingerprint !== descriptor.fingerprint || prior.requestId !== descriptor.requestId) {
      throw new Error('stored Pi result does not match descriptor');
    }
    if (prior.status === 'done') return prior;
  }

  const instructions = await frozenInstructions(descriptor);
  const [provider, ...modelParts] = descriptor.model.split('/');
  const modelId = modelParts.join('/');
  const registry = createRegistry();
  const sandbox = new CodemodeSandbox({ memoryLimitBytes: 256 * 1024 * 1024 });
  registry.install(CodingTools);
  registry.install(createCodemodeExtension({ sandbox }));
  const harness = await Harness.open(
    await openNodeSqliteStorage(join(descriptor.sessionDir, 'session.sqlite')),
    { models: models ?? await modelsFromPi(), registry }, CONTEXT,
  );
  try {
    const root = await harness.root(CONTEXT, {
      agent: {
        model: { provider, modelId },
        thinkingLevel: descriptor.effort || 'high',
        cwd: descriptor.cwd,
        instructions,
      },
    });
    harness.resume();
    const submission = await root.submit({
      type: 'input', requestId: descriptor.requestId, content: descriptor.prompt,
    }, CONTEXT);
    const settled = await submission.wait(CONTEXT);
    if (settled.status !== 'done' || settled.type !== 'input') {
      throw new Error(`Pi submission ${submission.id} ${settled.status}`);
    }
    const answer = assistantText(await root.commit((tx) => tx.entry(AssistantEntry, settled.answer), CONTEXT));
    if (!answer.trim()) throw new Error(`Pi submission ${submission.id} returned an empty answer`);
    const result = {
      version: 1, slot: descriptor.slot, fingerprint: descriptor.fingerprint,
      requestId: descriptor.requestId, submissionId: submission.id,
      status: 'done', answer,
    };
    await writeTrajectory(root, join(descriptor.sessionDir, 'trajectory.jsonl'));
    await atomicJson(resultPath, result);
    return result;
  } finally {
    await harness.close(CONTEXT);
    await sandbox.close();
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const result = await runDescriptor(process.argv[2]);
    process.stdout.write(`${JSON.stringify(result)}\n`);
  } catch (error) {
    process.stderr.write(`${error?.stack ?? error}\n`);
    process.exitCode = 1;
  }
}
