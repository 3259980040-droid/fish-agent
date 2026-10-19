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
const codingRequest={...request,messages:[{source:{kind:'system-prompt'},content:'native coding instructions'},{source:{kind:'runtime-context'},content:[]},{source:{kind:'skill-catalog'},content:[]}],tools:[...request.tools,{name:'write'},{name:'edit'},{name:'read'}]}
const coding=chatRequest(codingRequest,{scope:'space:owner',chat_mode:'tulpa',owner:true,desktop:false,owner_coding:true})
for (const name of ['bash','write','edit','read','schedule_create','fishp__tulpa_owner__send_chat_reply']) assert.ok(coding.tools.some(t=>t.name===name),name)
assert.equal(coding.messages[0].content[0].text,'native coding instructions')
assert.ok(coding.messages.some(m=>m.source.kind==='runtime-context'))
assert.ok(coding.system.startsWith('coding header'))
for (const name of ['bash','write','edit']) assert.equal(toolDenial({name,arguments:{}},{owner:true,owner_coding:true,desktop:false,isolated:true,isGroup:false},'.','qq'),undefined)
const daily=chatRequest(codingRequest,{scope:'owner',owner:true,owner_coding:true,desktop:false})
assert.ok(!daily.system.includes('fish_chat_reply'))
assert.ok(!daily.tools.some(t=>t.name==='schedule_create'))
console.log('Tulpa tools and scheduler remain private; group projection preserved: PASS')
