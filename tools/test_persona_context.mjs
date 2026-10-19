import assert from 'node:assert/strict'
import { ensurePersonaContext } from '../harness/runtime/fish-control/lib/index.js'

class FakeSession {
  constructor() {
    this.next = 1
    this.surface = {nodes: []}
    this.events = new Map()
  }
  add(text, revision) {
    const seq = this.next++
    this.events.set(seq, {data: {source: {kind: 'fish-persona-context', revision}, content: [{type: 'text', text}]}})
    this.surface.nodes.push(seq)
    return seq
  }
  addLegacy(text) {
    const seq = this.next++
    this.events.set(seq, {data: {source: {kind: 'user'}, content: [{type: 'text', text}]}})
    this.surface.nodes.push(seq)
    return seq
  }
  eventAt(seq) { return this.events.get(seq) }
  append(_kind, message, op) {
    if (op?.surfaceOp === 'append' || op === 'append') {
      const seq = this.next++
      this.events.set(seq, {data: {source: message.source, content: message.content}})
      this.surface.nodes.push(seq)
      return
    }
    // replacement of a specific node for this fake surface
    const seq = op.surfaceOp.startSeq
    this.events.set(seq, {data: {source: message.source, content: message.content}})
  }
}

const session = new FakeSession()
assert.equal(ensurePersonaContext(session, 'PERSONA-A', 'rev-a').inserted, true)
assert.equal(ensurePersonaContext(session, 'PERSONA-A', 'rev-a').inserted, false)
assert.equal(session.surface.nodes.length, 1)
// simulated compaction removes the node from the visible surface
session.surface.nodes.length = 0
assert.equal(ensurePersonaContext(session, 'PERSONA-A', 'rev-a').inserted, true)
assert.equal(session.surface.nodes.length, 1)
// a new session with a different revision is rejected
const other = new FakeSession()
other.add('PERSONA-B', 'rev-b')
assert.throws(() => ensurePersonaContext(other, 'PERSONA-A', 'rev-a'), /版本与会话不一致/)
const legacy = new FakeSession()
legacy.addLegacy('[当前独立会话的人格与规则快照；仅用于本会话]\nPERSONA-A')
assert.throws(() => ensurePersonaContext(legacy, 'PERSONA-A', 'rev-a'), /旧测试会话/)
console.log('persona context mock: PASS')
