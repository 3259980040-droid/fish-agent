// Repair only Fish persona rows written before Harness's protected system head.
// Originals are backed up; user/assistant messages and all sequence IDs stay intact.
import {readFileSync, writeFileSync, mkdirSync, renameSync, copyFileSync, readdirSync} from 'node:fs'
import {resolve, relative, join} from 'node:path'
import {createHash} from 'node:crypto'
import {isDeepStrictEqual} from 'node:util'
import {zstdDecompressSync, zstdCompressSync, constants} from 'node:zlib'
import {releasedV4SessionFormatCodec as codec, restoreReleasedV4Artifact}
  from '../vendor/runtime/dsh/node_modules/@deepseek-ai/dsh-session-format-v3-to-v4/lib/index.js'
import {KNOWN_SESSION_EVENT_TYPES} from '../vendor/runtime/dsh/node_modules/@deepseek-ai/dsh-session/lib/index.js'

const root = resolve(process.argv[2] || '.')
const apply = process.argv.includes('--apply')
const base = join(root, 'homes/qq/sessions')
const backup = join(root, 'backups', 'SESSION-HEAD-' + new Date().toISOString().replace(/[:.]/g, '-'))
const digest = bytes => createHash('sha256').update(bytes).digest('hex')
function files(path) {
  return readdirSync(path, {withFileTypes: true}).flatMap(item =>
    item.isDirectory() ? files(join(path, item.name)) : item.name === 'session.v4.jsonl.zstd' ? [join(path, item.name)] : [])
}
function decode(bytes) {
  const rows = []
  for (let offset = 0; offset < bytes.length;) {
    const result = zstdDecompressSync(bytes.subarray(offset), {info: true})
    if (!result.engine.bytesWritten) throw new Error('No frame consumed')
    rows.push(...result.buffer.toString('utf8').trim().split('\n').filter(Boolean).map(JSON.parse))
    offset += result.engine.bytesWritten
  }
  const decoder = codec.createDecoder(rows[0], 'strict')
  const events = []
  const context = {emitEvent: event => events.push(event), emitRun: run => events.push(...run.expand())}
  for (const row of rows.slice(1)) decoder.decodeRow(row, context)
  return {header: decoder.header, inheritedEventCount: decoder.finish(context), events}
}
const result = {applied: apply, backup: apply ? backup : null, repaired: [], unchanged: []}
for (const path of files(base)) {
  const original = readFileSync(path)
  const artifact = decode(original)
  const system = artifact.events.find(event => event.type === 'system/message')
  const early = system ? artifact.events.filter(event => event.seq < system.seq
    && event.type === 'user/message' && event.data?.source?.kind === 'fish-persona-context') : []
  if (!early.length) { result.unchanged.push(artifact.header.id); continue }
  const otherSurface = artifact.events.some(event => event.seq < system.seq
    && event.surfaceOp !== undefined && !early.includes(event))
  if (otherSurface) throw new Error('Unexpected surface before system head; preserving original: ' + artifact.header.id)
  const originalEvents = structuredClone(artifact.events)
  for (const event of early) {
    event.type = 'fish/persona-context-retired'
    event.ignorable = true
    delete event.surfaceOp
    delete event.sourceEventSeqs
  }
  restoreReleasedV4Artifact(artifact, new Set(KNOWN_SESSION_EVENT_TYPES))
  const physical = [{type: 'session', ...codec.encodeHeader(artifact.header, artifact.inheritedEventCount)},
                    ...artifact.events.map(event => codec.encodeEvent(event))]
  const encoded = zstdCompressSync(physical.map(value => JSON.stringify(value)).join('\n') + '\n',
    {params: {[constants.ZSTD_c_checksumFlag]: 1}})
  const restored = restoreReleasedV4Artifact(decode(encoded), new Set(KNOWN_SESSION_EVENT_TYPES))
  for (const event of restored.events) {
    if (!early.some(item => item.seq === event.seq)
        && !isDeepStrictEqual(event, originalEvents[event.seq])) {
      throw new Error('Recovery changed an unrelated event; preserving original: ' + artifact.header.id)
    }
  }
  const messageCount = artifact.events.filter(event => event.type === 'assistant/message'
    || (event.type === 'user/message' && event.data?.source?.kind === 'user')).length
  if (apply) {
    const saved = join(backup, relative(root, path))
    mkdirSync(resolve(saved, '..'), {recursive: true})
    copyFileSync(path, saved)
    const staging = path + '.fish-repair.tmp'
    writeFileSync(staging, encoded, {flag: 'wx'})
    renameSync(staging, path)
  }
  result.repaired.push({session: artifact.header.id, retired_sequences: early.map(event => event.seq),
    events_preserved: artifact.events.length, user_assistant_messages_preserved: messageCount,
    original_sha256: digest(original), repaired_sha256: digest(encoded), upstream_validation: true,
    other_event_payloads_unchanged: true})
}
if (apply) {
  mkdirSync(backup, {recursive: true})
  writeFileSync(join(backup, 'repair-result.json'), JSON.stringify(result, null, 2) + '\n')
}
console.log(JSON.stringify(result))
