import assert from 'node:assert/strict'
import { chatRequest, ownerWork } from '../harness/runtime/fish-control/lib/index.js'

const base = {
  owner: true,
  owner_coding: true,
  scope: 'private:200000001',
  chat_mode: 'owner',
  owner_mode: 'chat',
}

const options = {
  system: 'base system',
  messages: [
    { role: 'system', source: { kind: 'system-prompt' }, content: [{ type: 'text', text: 'head' }] },
    { role: 'user', source: { kind: 'agent-instructions' }, content: [{ type: 'text', text: 'project rules' }] },
    { role: 'user', content: [{ type: 'text', text: 'hello' }] },
  ],
  tools: [
    { name: 'read' }, { name: 'bash' }, { name: 'fish_memory_search' }, { name: 'fish_chat_reply' },
  ],
}

assert.equal(ownerWork(base), false, 'chat mode must not be treated as work')
const chat = chatRequest(structuredClone(options), base)
assert.deepEqual(chat.tools.map(tool => tool.name), ['fish_memory_search', 'fish_chat_reply'])
assert.equal(chat.messages.some(message => message.source?.kind === 'agent-instructions'), false)
assert.equal(chat.messages.at(-1).content[0].text, 'hello')

const work = { ...base, owner_mode: 'work' }
assert.equal(ownerWork(work), true, 'work mode must enable native tools')
const workRequest = chatRequest(structuredClone(options), work)
assert.deepEqual(workRequest.tools.map(tool => tool.name), ['read', 'bash', 'fish_memory_search'])
assert.ok(workRequest.system.includes('远程编程权限'))
console.log('prompt mode checks passed')

const finalHigh = chatRequest(structuredClone(options), {...base, owner_reply_mode:'final'}, {chat_reasoning_effort:'high',reply_display:'final'})
const shownOff = chatRequest(structuredClone(options), {...base, owner_reply_mode:'thinking_excerpt'}, {chat_reasoning_effort:'off',reply_display:'thinking_excerpt'})
assert.equal(finalHigh.reasoningEffort,'high')
assert.equal(shownOff.reasoningEffort,'off')
assert(finalHigh.system.includes('fish_capabilities'))
assert(finalHigh.system.includes('fish_chat_reply'))
assert(!finalHigh.system.includes('"bash"'))
console.log('thinking and display are independent; actual tools match chat mode')
