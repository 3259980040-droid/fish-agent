import assert from 'node:assert/strict'
import { chooseCancelTarget, toolDenial, renderToolResult } from './lib/index.js'

assert.deepEqual(chooseCancelTarget([], ''), { ok: false, error: 'none' })
assert.deepEqual(chooseCancelTarget(['a'], ''), { ok: true, id: 'a' })
assert.deepEqual(chooseCancelTarget(['a', 'b'], ''), {
  ok: false, error: 'multiple', sessions: ['a', 'b'],
})
assert.deepEqual(chooseCancelTarget(['a', 'b'], 'b'), { ok: true, id: 'b' })
assert.deepEqual(chooseCancelTarget(['a'], 'z'), { ok: false, error: 'not_found' })
console.log('fish-control selection tests: PASS')

assert.match(toolDenial({name:'bash',arguments:{command:'echo hi'}},{},'C:/fish','qq'), /未获本地授权/)
assert.match(toolDenial({name:'read',arguments:{path:'../../outside.txt'}},{},'C:/fish','qq'), /超出/)
assert.match(toolDenial({name:'web_fetch',arguments:{text:'DDOS website'}},{},'C:/fish','qq'), /高风险/)
assert.equal(toolDenial({name:'read',arguments:{path:'notes.txt'}},{},'C:/fish','qq'), undefined)
assert.equal(toolDenial({name:'web_search',arguments:{query:'猫咪视频'}},{},'C:/fish','qq'), undefined)
assert.match(toolDenial({name:'unknown_external_tool',arguments:{}},{},'C:/fish','qq'), /不在本地允许列表/)
console.log('fish-control mandatory tool policy tests: PASS')
assert.match(toolDenial({name:'read',arguments:{path:'C:/fish/data/qq/memory.db'}},{isGroup:true},'C:/fish','qq'), /超出/)
assert.equal(toolDenial({name:'read',arguments:{path:'public/notes.md'}},{isGroup:true},'C:/fish','qq'), undefined)
assert.match(toolDenial({name:'read',arguments:{path:'C:/fish/data/social/people.db'}},{isGroup:true},'C:/fish','qq'), /超出/)
assert.match(toolDenial({name:'glob',arguments:{pattern:'**/*'}},{isGroup:true},'C:/fish','qq'), /指定/)
assert.match(toolDenial({name:'glob',arguments:{path:'public',pattern:'../../data/social/*'}},{isGroup:true},'C:/fish','qq'), /超出/)
assert.match(toolDenial({name:'bili_reply',arguments:{text:'hi'}},{nativeTools:['bili_reply']},'C:/fish','research'), /统一发送/)
assert.equal(renderToolResult({}, {content:[],fish_images:[{attachmentId:'image-fixture'}]})[1].type, 'image')
console.log('fish-control role privacy and multimodal rendering tests: PASS')
