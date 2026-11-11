import { randomUUID } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { createServer } from 'node:http';

const host = process.env.HOST ?? '127.0.0.1';
const port = Number(process.env.PORT ?? '3000');
const agent = new URL(process.env.AGENTSCOPE_URL ?? 'http://127.0.0.1:9999/');
if (!['http:', 'https:'].includes(agent.protocol)) {
  throw new Error('AGENTSCOPE_URL must be an HTTP or HTTPS URL.');
}

const frameOrigins = (process.env.EMBED_ORIGINS ?? '').split(',').map(value => value.trim())
  .filter(Boolean).map(value => {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol) || value !== url.origin) {
      throw new Error(`EMBED_ORIGINS expects origins such as https://example.com (${value}).`);
    }
    return url.origin;
  });
const framePolicy = `frame-ancestors ${["'self'", ...frameOrigins].join(' ')}; `
  + "default-src 'self'; base-uri 'none'; object-src 'none'; form-action 'none'";

const assets = new Map([
  ['/', ['demo.html', 'text/html; charset=utf-8']],
  ['/demo.css', ['demo.css', 'text/css; charset=utf-8']],
  ['/demo.js', ['demo.js', 'text/javascript; charset=utf-8']],
  ['/embed.js', ['embed.js', 'text/javascript; charset=utf-8']],
  ['/embed.css', ['embed.css', 'text/css; charset=utf-8']],
  ['/widget/', ['chat.html', 'text/html; charset=utf-8']],
  ['/widget/chat.css', ['chat.css', 'text/css; charset=utf-8']],
  ['/widget/chat.js', ['chat.js', 'text/javascript; charset=utf-8']],
]);
const uuid = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;

function json(response, status, payload) {
  response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  response.end(JSON.stringify(payload));
}

async function chat(request, response) {
  if (request.headers['content-type']?.split(';')[0].trim() !== 'application/json') {
    return json(response, 415, { error: '请使用 JSON 发送消息。' });
  }

  const chunks = [];
  let bytes = 0;
  for await (const chunk of request.iterator({ destroyOnReturn: false })) {
    bytes += chunk.length;
    if (bytes > 24 * 1024) {
      request.resume();
      return json(response, 413, { error: '消息太长，请缩短后再发。' });
    }
    chunks.push(chunk);
  }

  let input;
  try {
    input = JSON.parse(Buffer.concat(chunks));
  } catch {
    return json(response, 400, { error: '消息格式无效。' });
  }
  const text = typeof input?.message === 'string' ? input.message.trim() : '';
  const contextId = input?.contextId;
  if (!text || text.length > 4000 || (contextId !== undefined && (
    typeof contextId !== 'string' || !uuid.test(contextId)
  ))) {
    return json(response, 400, { error: '请输入 1 至 4000 个字符，并使用有效的会话 ID。' });
  }

  const messageId = randomUUID();
  try {
    const upstream = await fetch(agent, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'A2A-Version': '1.0' },
      body: JSON.stringify({
        jsonrpc: '2.0', id: messageId, method: 'SendMessage',
        params: { message: {
          messageId, role: 'ROLE_USER', parts: [{ text }],
          ...(contextId && { contextId }),
        } },
      }),
      redirect: 'error',
      signal: AbortSignal.timeout(120_000),
    });
    if (!upstream.ok) throw new Error(`AgentScope HTTP ${upstream.status}`);
    const data = await upstream.json();
    const task = data.result?.task;
    const reply = task?.artifacts?.flatMap(artifact => artifact.parts)
      .map(part => part.text ?? '').join('');
    if (task?.status?.state !== 'TASK_STATE_COMPLETED' || !reply || !uuid.test(task.contextId)) {
      throw new Error(`AgentScope returned ${data.error?.code ?? task?.status?.state ?? 'an invalid task'}`);
    }
    return json(response, 200, { reply, contextId: task.contextId });
  } catch (error) {
    console.error('AgentScope request failed:', error.message);
    if (error.name === 'TimeoutError') {
      return json(response, 504, { error: '等待 Friday 回复超时，请稍后再试。' });
    }
    return json(response, 502, { error: '暂时无法获得 Friday 的回复，请稍后再试。' });
  }
}

createServer(async (request, response) => {
  response.setHeader('X-Content-Type-Options', 'nosniff');
  response.setHeader('Cache-Control', 'no-store');
  const path = request.url.split('?')[0];
  try {
    if (path === '/api/chat') {
      if (request.method === 'POST') return await chat(request, response);
      response.setHeader('Allow', 'POST');
      return json(response, 405, { error: '请使用 POST。' });
    }
    const asset = assets.get(path);
    if (asset && ['GET', 'HEAD'].includes(request.method)) {
      const [file, contentType] = asset;
      if (path === '/widget/') response.setHeader('Content-Security-Policy', framePolicy);
      const body = await readFile(new URL(`./public/${file}`, import.meta.url));
      response.writeHead(200, { 'Content-Type': contentType });
      return response.end(request.method === 'HEAD' ? undefined : body);
    }
    return json(response, 404, { error: '页面不存在。' });
  } catch (error) {
    console.error('Chat server request failed:', error.message);
    if (!response.destroyed) json(response, 500, { error: '聊天服务暂时不可用。' });
  }
}).listen(port, host, () => console.log(`Friday 网页演示 http://${host}:${port}/`));
