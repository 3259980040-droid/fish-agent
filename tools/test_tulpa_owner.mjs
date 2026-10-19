import assert from 'node:assert/strict'
import {chatRequest, toolDenial} from '../harness/runtime/fish-control/lib/index.js'

const names = ['fish_memory_search', 'web_search', 'web_fetch', 'schedule_create',
  'fishp__tulpa_owner__get_chat_session', 'fishp__tulpa_owner__send_chat_reply',
  'fish_chat_reply', 'fish_emoji_search', 'bash', 'fishp__other__send']
const request = Object.freeze({system: 'coding header', messages: [], tools: names.map(name => ({name}))})
const projected = chatRequest(request, {scope: 'space:example', chat_mode: 'tulpa'})
assert.deepEqual(projected.tools.map(t => t.name), names.slice(0, 6))
assert.equal(request.system, 'coding header')
assert.deepEqual(chatRequest(request, {scope:'chat:group:123'}).tools.map(t => t.name), names.slice(0,3))
for (const name of names.slice(0,6)) {
  assert.equal(toolDenial({name, arguments:{}}, {isolated:true, chat_mode:'tulpa', fishTools:names}, '.', 'qq'), undefined)
}
assert.ok(toolDenial({name:'schedule_create'}, {isolated:true,chat_mode:'owner'}, '.', 'qq'))
console.log('Tulpa tools and scheduler remain private; group projection preserved: PASS')
