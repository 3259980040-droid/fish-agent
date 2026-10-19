/**
 * fish-control：给 Fish Core 一个最小的本地控制面。
 * 只做两件事：报告活跃 Agent；按 session_id 取消当前生成。
 * 不碰 QQ、不碰人格、不碰业务数据。
 */

import { readFileSync, realpathSync } from 'node:fs'
import { createHmac, createHash, randomUUID } from 'node:crypto'
import { resolve, isAbsolute, relative, sep, dirname, basename } from 'node:path'

export const name = 'fish-control'
export const inject = ['webServer', 'tools', 'connection']

// The snapshot is a model-input inspection aid. Never persist reasoning blocks
// or attachment bytes, and bound it before sending it to the local ToolHost.
export function snapshotRequest(options) {
  let remaining = 45000
  const omitted = {characters: 0, messages: 0, blocks: 0, reasoning: 0, media: 0}
  const text = value => {
    const raw = String(value ?? '')
    let safe = raw
    for (const [key, secret] of Object.entries(process.env)) {
      if (/API_KEY|TOKEN|SECRET|PASSWORD/i.test(key) && secret?.length >= 6) safe = safe.split(secret).join('[已隐藏]')
    }
    safe = safe.replace(/\bsk-[A-Za-z0-9_-]+/g, '[已隐藏]').replace(/Bearer\s+[^\s,;"']+/gi, 'Bearer [已隐藏]')
      .replace(/data:[^\s"']+;base64,[A-Za-z0-9+/=]+/g, '[附件数据已省略]')
      .replace(/(["']?(?:api[_-]?key|token|secret|password|authorization)["']?\s*[:=]\s*)["']?[^\s,;"'}]+/gi, '$1[已隐藏]')
      .replace(/(https?:\/\/)[^\s/@]+:[^\s/@]+@/g, '$1[已隐藏]@')
      .replace(/(https?:\/\/[^\s?"']+)\?[^\s"']*/g, '$1?[查询参数已隐藏]')
    const used = Math.min(safe.length, remaining, 12000)
    remaining -= used; omitted.characters += safe.length - used
    return safe.slice(0, used)
  }
  const blocks = content => {
    if (typeof content === 'string') return text(content)
    if (!Array.isArray(content)) return []
    omitted.blocks += Math.max(0, content.length - 80)
    return content.slice(0, 80).map(block => {
      if (/thinking|reasoning/.test(block.type ?? '')) { omitted.reasoning++; return {type: block.type, omitted: true} }
      if (block.type === 'text') return {type: 'text', text: text(block.text)}
      if (['image', 'video', 'audio', 'file', 'image_url'].includes(block.type)) { omitted.media++; return {type: block.type, omitted: '附件数据省略'} }
      if (block.type === 'tool_result') return {type: block.type, tool_use_id: text(block.tool_use_id), content: blocks(block.content)}
      if (block.type === 'tool_use') return {type: block.type, name: text(block.name), id: text(block.id), input: text(JSON.stringify(block.input ?? block.arguments ?? {}))}
      return {type: String(block.type ?? 'unknown'), omitted: true}
    })
  }
  const messages = options.messages ?? []
  omitted.messages = Math.max(0, messages.length - 120)
  return {provider: String(options.provider ?? ''), model: String(options.model ?? ''), purpose: options.purpose,
    system: text(options.system ?? ''), messages: messages.slice(0, 120).map(message => ({role: message.role,
      source: message.source?.kind, content: blocks(message.content)})),
    tools: (options.tools ?? []).slice(0, 150).map(tool => ({name: String(tool.name), description: text(tool.description ?? ''),
      schema: text(JSON.stringify(tool.parameters ?? tool.input_schema ?? {}))})), omitted}
}

export function independentMessages(decision, isolated) {
  if (!isolated || decision.kind !== 'enter') return decision
  return {...decision, messages: decision.messages.filter(message => message.source?.kind !== 'agent-instructions')}
}

// A test/desktop space owns exactly one persona context node. It is inserted
// before the current request is derived, and a later compaction may shadow it;
// the pre-step hook reinserts it when the surface no longer carries the node.
function personaContextMessage(value, revision = '', retired = false) {
  return Object.freeze({id: randomUUID(), role: 'user',
    source: Object.freeze({kind: retired ? 'fish-persona-context-retired' : 'fish-persona-context', revision: String(revision || '')}),
    content: Object.freeze([Object.freeze({type: 'text', text: value || (retired ? '' : '[人格上下文缺失]')})])})
}

function messageText(message) {
  const content = message?.content
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  return content.map(block => block?.type === 'text' ? String(block.text ?? '') : '').join('')
}

function hasLegacyPersona(session) {
  for (const seq of session.surface.nodes) {
    const event = session.eventAt(seq)
    if (event?.data?.source?.kind !== 'user') continue
    const text = messageText(event.data)
    if (text.includes('<compacted-summary>')) continue
    if (text.includes('[当前独立会话的人格与规则快照')) return true
  }
  return false
}

export function ensurePersonaContext(session, text, revision = '') {
  if (!text || !String(text).trim()) throw new Error('人格上下文为空，拒绝静默使用其他人格')
  const nodes = [...session.surface.nodes].filter(seq =>
    session.eventAt(seq)?.data?.source?.kind === 'fish-persona-context')
  if (!nodes.length && hasLegacyPersona(session)) {
    throw new Error('旧测试会话包含重复人格头，请新建测试空间，不要复用旧会话')
  }
  for (const seq of nodes) {
    const source = session.eventAt(seq)?.data?.source
    const bound = String(source?.revision || '')
    if (bound && bound !== String(revision)) {
      throw new Error('测试空间人格版本与会话不一致，请新建测试空间')
    }
  }
  if (nodes.length > 1) {
    let first = true
    for (const seq of nodes) {
      session.append('user/message', personaContextMessage(first ? text : '', revision, !first),
        {surfaceOp: {op: 'replace', startSeq: seq, endSeq: seq}, sourceEventSeqs: [seq]})
      first = false
    }
    return {inserted: false, consolidated: true}
  }
  if (nodes.length === 1) {
    const source = session.eventAt(nodes[0])?.data?.source
    const bound = String(source?.revision || '')
    if (bound && bound !== String(revision)) {
      throw new Error('测试空间人格版本与会话不一致，请新建测试空间')
    }
    return {inserted: false, consolidated: false}
  }
  // The loop commits its initial system head before accepting this batch.
  // Appending here with an empty surface breaks the durable log on cold restore.
  if (!session.surface.nodes.length) {
    return {inserted: false, consolidated: false, deferred: personaContextMessage(text, revision)}
  }
  session.append('user/message', personaContextMessage(text, revision), {surfaceOp: 'append'})
  return {inserted: true, consolidated: false}
}

export function acceptPersonaContext(decision, state) {
  return state?.deferred && decision.kind === 'enter'
    ? {...decision, messages: [state.deferred, ...decision.messages]}
    : decision
}

// Use Harness's logged surface replacement API: raw logs remain inspectable,
// but previous profiles no longer occupy the next request or its summary.
function groupContextMessage(value) {
  return Object.freeze({id: randomUUID(), role: 'user',
    source: Object.freeze({kind: 'fish-group-context'}),
    content: Object.freeze([Object.freeze({type: 'text', text: value || '[本轮临时群资料已撤下]'})])})
}
export function replaceGroupContext(session, text) {
  const previous = [...session.surface.nodes].filter(seq => session.eventAt(seq)?.data?.source?.kind === 'fish-group-context')
  for (const seq of previous) {
    session.append('user/message', groupContextMessage(text),
      {surfaceOp: {op: 'replace', startSeq: seq, endSeq: seq}, sourceEventSeqs: [seq]})
    text = ''
  }
  if (!previous.length && text) {
    session.append('user/message', groupContextMessage(text), {surfaceOp: 'append'})
  }
}

export function decodeDialogueContext(text) {
  if (!String(text).startsWith('{')) return {context: text, history: []}
  let value
  try { value = JSON.parse(text) } catch { return {context: text, history: []} }
  if (value.fish_dialogue_version !== 1) return {context: text, history: []}
  if (typeof value.context !== 'string' || !Array.isArray(value.history)) throw new Error('窗口历史格式错误')
  for (const message of value.history) {
    if (!['user', 'assistant'].includes(message.role) || !Array.isArray(message.content)
        || message.content.some(block => block.type !== 'text' || typeof block.text !== 'string')) throw new Error('窗口历史消息格式错误')
  }
  return value
}

// Restore ordinary messages into Harness's own surface. Reserve its empty
// system head first; the loop will replace it with the assembled prompt.
// This also lets native compaction see history before deriving the next call.
export function appendDialogueHistory(session, messages, turn = 1, step = 1) {
  if (!messages.length) return
  if (!session.surface.nodes.length) {
    session.append('system/message', {turn, step, message: {id: randomUUID(), role: 'system',
      content: [], source: {kind: 'system-prompt'}}}, {surfaceOp: 'append'})
  }
  for (const item of messages) {
    const message = {id: randomUUID(), role: item.role, content: item.content,
      source: item.role === 'assistant' ? {kind: 'model', provider: 'fish-history', model: 'delivered'} : {kind: 'fish-dialogue-history'}}
    if (item.role === 'assistant') session.append('assistant/message', {turn, step, stream: [], message}, {surfaceOp: 'append'})
    else session.append('user/message', message, {surfaceOp: 'append'})
  }
}

export function renderToolResult(_args, value) {
  const images = value && typeof value === 'object' && Array.isArray(value.fish_images) ? value.fish_images : []
  const textValue = value && typeof value === 'object' && !Array.isArray(value) ? {...value} : value
  if (textValue && typeof textValue === 'object' && !Array.isArray(textValue)) delete textValue.fish_images
  return [{type: 'text', text: JSON.stringify(textValue)}, ...images.map(attachment => ({type: 'image', attachment}))]
}

export function toolDenial(exec, settings, root, role) {
  if (settings.isolated && !['fish_memory_search', 'fish_memory_save', 'fish_memory_delete'].includes(exec.name)) {
    if (exec.name.startsWith('fish') || settings.nativeTools?.includes(exec.name)) return 'Fish: 独立测试窗口未授权共享资源或外部插件'
  }
  if (exec.name.startsWith('fish_') || exec.name.startsWith('fishp__')) {
    return settings.fishTools?.includes(exec.name) ? undefined : 'Fish: 工具已禁用或未向当前角色注册'
  }
  if (settings.isolated && !['read', 'read_file', 'read_image', 'list_directory', 'ls', 'glob', 'grep'].includes(exec.name)) return 'Fish: 独立测试未授权此工具'
  const defaults = ['bash', 'shell', 'terminal', 'run_code', 'write', 'edit', 'delete', 'onebot', 'send_message', 'plugin_manager']
  const forbidden = settings.blocked_tools ?? defaults
  const name = String(exec.name).toLowerCase()
  if (/(^|_)(send|post|publish|comment|reply|upload|notify|onebot)(_|$)|(^|_)bili_(like|follow)(_|$)/i.test(name)) return 'Fish: 外发工具必须接入统一发送闸门'
  if (forbidden.some(value => name.includes(String(value).toLowerCase()))) return 'Fish: 工具未获本地授权'
  const allowed = settings.allowed_tools ?? ['read', 'read_file', 'read_image', 'list_directory', 'ls', 'glob', 'grep', 'web_search', 'web_fetch', 'search', 'fetch']
  if (!allowed.includes(name) && !settings.nativeTools?.includes(exec.name)) return 'Fish: 工具不在本地允许列表'
  const text = JSON.stringify(exec.arguments ?? exec.args ?? {})
  if (/ddos|sql\s*注入|拖库|撞库|攻击.{0,12}(网站|服务器)/i.test(text)) return 'Fish: 本地规则阻止高风险工具操作'
  const canonical = value => {
    let current = resolve(value)
    const tail = []
    for (;;) {
      try { return resolve(realpathSync(current), ...tail) }
      catch (error) {
        if (error.code !== 'ENOENT') throw error
        const parent = dirname(current)
        if (parent === current) throw error
        tail.unshift(basename(current)); current = parent
      }
    }
  }
  const within = (dir, target) => { const rel = relative(dir, target); return !isAbsolute(rel) && rel !== '..' && !rel.startsWith('..' + sep) }
  const deniedPath = item => {
    if (typeof item !== 'string') return true
    const base = resolve(root, role === 'qq' ? 'qq-work' : 'knowledge')
    const target = resolve(base, item)
    const allowed = settings.isolated ? (settings.allowed_paths ?? []) : settings.isGroup ? [resolve(base, 'public')] : [base, resolve(root, 'data', role), resolve(root, 'homes', role)]
    if (/^(\.env(?:\..*)?|(?:bili_)?credentials?\.json|control\.token|auth\.json|account\.json|.*secret.*\.bin)$/i.test(target.split(/[\\/]/).pop())) return true
    return !allowed.some(dir => within(dir, target) && within(canonical(dir), canonical(target)))
  }
  let hasPath = false
  const walk = value => {
    if (!value || typeof value !== 'object') return false
    for (const [key, item] of Object.entries(value)) {
      if ((settings.isolated || settings.isGroup) && /^(pattern|glob)$/i.test(key) && typeof item === 'string'
          && /(^|[\\/])\.\.([\\/]|$)|^[a-z]:[\\/]|^[\\/]/i.test(item)) return true
      if (/^(paths?|file_paths?|filepath|files|director(?:y|ies)|cwd)$/i.test(key)) {
        hasPath = true
        if ((Array.isArray(item) ? item : [item]).some(deniedPath)) return true
      }
      if (typeof item === 'object' && walk(item)) return true
    }
    return false
  }
  if (walk(exec.arguments ?? exec.args)) return 'Fish: 工具路径超出当前角色的数据范围'
  if (settings.isolated && !hasPath) return 'Fish: 独立窗口文件工具必须指定本窗口内的路径'
  if (settings.isGroup && ['read','read_file','read_image','list_directory','ls','glob','grep'].includes(name) && !hasPath) return 'Fish: 群聊文件工具必须指定已授权公开目录中的路径'
}

function sendJson(res, code, value) {
  const body = JSON.stringify(value)
  res.statusCode = code
  res.setHeader('content-type', 'application/json; charset=utf-8')
  res.setHeader('content-length', Buffer.byteLength(body))
  res.end(body)
}

async function readBody(req) {
  const chunks = []
  let size = 0
  for await (const chunk of req) { size += chunk.length; if (size > 65536) throw new Error('body too large'); chunks.push(chunk) }
  if (chunks.length === 0) return {}
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8'))
  } catch {
    return {}
  }
}

/**
 * 多个 active 且没有指定 session_id 时，绝不随机挑一个取消。
 * 返回 {ok:true,id} 或 {ok:false,error,sessions?}。
 */
export function chooseCancelTarget(ids, requested = '') {
  if (requested) {
    return ids.includes(requested)
      ? { ok: true, id: requested }
      : { ok: false, error: 'not_found' }
  }
  if (ids.length === 0) return { ok: false, error: 'none' }
  if (ids.length > 1) return { ok: false, error: 'multiple', sessions: [...ids] }
  return { ok: true, id: ids[0] }
}

export function apply(ctx, config = {}) {
  const agents = new Map()
  const root = process.env.FISH_HARNESS_HOME
  const role = process.env.FISH_AGENT_ID
  if (!root || !['qq', 'research'].includes(role)) throw new Error('Fish governance requires an explicit root and role')
  const counts = new WeakMap()
  const historyBatches = new WeakMap()
  const token = process.env[config.tokenEnv || 'FISH_CONTROL_TOKEN'] || ''
  const toolToken = createHmac('sha256', token).update('fish-tools:' + role).digest('hex')
  const rawConfig = JSON.parse(readFileSync(resolve(root, 'harness/agents.json'), 'utf8'))
  const toolOrigin = 'http://127.0.0.1:' + (rawConfig.runtime?.tool_host_port ?? 3103)
  const rootId = createHash('sha256').update(root.toLowerCase().replaceAll('\\', '/')).digest('hex')
  const definitions = new Map()
  let fishTools = []
  let nativeTools = []
  async function host(path, body = {}, signal) {
    const response = await fetch(toolOrigin + path, {
      method: 'POST', headers: {'content-type': 'application/json', 'x-fish-token': toolToken},
      body: JSON.stringify(body), signal: signal ?? AbortSignal.timeout(10000),
    })
    const data = await response.json()
    if (!response.ok || !data.ok) throw new Error(data.error || 'Fish 本地服务拒绝调用')
    return data.result
  }
  let refreshing
  async function refreshTools() {
    if (refreshing) return refreshing
    refreshing = (async () => {
      const catalog = await host('/catalog')
      const current = new Set(catalog.tools.map(t => t.name))
      for (const [name, saved] of definitions) {
        const fresh = catalog.tools.find(t => t.name === name)
        if (!current.has(name) || JSON.stringify(fresh) !== saved.json) {
          saved.dispose(); definitions.delete(name)
        }
      }
      for (const definition of catalog.tools) {
        if (definitions.has(definition.name)) continue
        const dispose = ctx.tools.register({
          name: definition.name, description: definition.description, parameters: definition.parameters,
          output: {schema: {}, render: renderToolResult},
          async execute(args, exec) {
            const cancel = () => { host('/cancel', {call_id: String(exec.callId)}).catch(() => {}) }
            exec.signal?.addEventListener('abort', cancel, {once: true})
            try {
              const value = await host('/execute', {session: String(exec.agent?.id ?? ''), name: definition.name,
                arguments: args, call_id: String(exec.callId)}, exec.signal ?? AbortSignal.timeout(250000))
              const images = []
              if (value && typeof value === 'object' && !Array.isArray(value)) {
                let media = {}
                try { media = JSON.parse(readFileSync(resolve(root, 'harness/multimodal.json'), 'utf8')) }
                catch (error) { if (error.code !== 'ENOENT') throw error }
                const enabled = media[role + '_enabled'] !== false && media.vision?.enabled !== false
                for (const block of Array.isArray(value.content) ? value.content : []) {
                  if (block.type !== 'image_file') continue
                  if (!enabled) { block.type = 'text'; block.text = '本角色图片输入已关闭，插件图片没有传入模型'; continue }
                  const target = realpathSync(block.path)
                  const allowed = resolve(root, 'data', role, 'plugin-results')
                  const rel = relative(allowed, target)
                  if (isAbsolute(rel) || rel === '..' || rel.startsWith('..' + sep)) throw new Error('插件图片超出角色结果目录')
                  const attachments = ctx.get('attachments')
                  if (!attachments) throw new Error('当前运行时没有图片附件存储服务')
                  images.push(await attachments.saveImage({data: readFileSync(target), mediaType: block.mediaType, name: block.name}))
                }
                return {...value, fish_images: images}
              }
              return value
            } finally { exec.signal?.removeEventListener('abort', cancel) }
          },
        })
        definitions.set(definition.name, {dispose, json: JSON.stringify(definition)})
      }
      fishTools = [...current]
      nativeTools = catalog.native_tools ?? []
    })()
    try { await refreshing } finally { refreshing = undefined }
  }
  ctx.on('agent/request', async (_payload, next) => {
    await refreshTools()
    return next()
  })
  ctx.on('agent/pre-step', async ({agent, turn, step}, next) => {
    let identities = {}
    try { identities = JSON.parse(readFileSync(resolve(root, 'harness/runtime/' + role + '.identities.json'), 'utf8')) }
    catch (error) { if (error.code !== 'ENOENT') throw error }
    const identity = identities[String(agent.id)]
    const group = role === 'qq' && identity?.scope?.startsWith('chat:group:')
    const isolated = role === 'qq' && identity?.scope?.startsWith('space:')
    let personaState
    // Clear stale profiles BEFORE native compaction reads history.
    let groupContext
    if (group) {
      replaceGroupContext(agent.session, '')
      const context = await host('/social/context', {session: String(agent.id)})
      groupContext = decodeDialogueContext(context.text)
      const batch = createHash('sha256').update(context.text).digest('hex')
      if (historyBatches.get(agent) !== batch) {
        appendDialogueHistory(agent.session, groupContext.history, turn, step)
        historyBatches.set(agent, batch)
      }
    }
    // Isolated sessions get one durable persona context node before request
    // derivation. It is deliberately not prepended to every user turn.
    if (isolated) {
      let persona
      try {
        persona = await host('/social/persona', {session: String(agent.id)})
      } catch (error) {
        throw new Error('人格上下文加载失败：' + String(error?.message ?? error))
      }
      personaState = ensurePersonaContext(agent.session, String(persona.text ?? ''), String(persona.revision ?? ''))
    }
    let decision = acceptPersonaContext(await next(), personaState)
    if (group && decision.kind === 'enter') {
      const exists = [...agent.session.surface.nodes].some(seq => agent.session.eventAt(seq)?.data?.source?.kind === 'fish-group-context')
      if (exists) replaceGroupContext(agent.session, groupContext.context)
      // The initial system message is committed by the loop before the accepted
      // batch. Entering first context through that batch preserves its node 0.
      else decision = {...decision, messages: [groupContextMessage(groupContext.context), ...decision.messages]}
    }
    return independentMessages(decision, identity?.isolated || !!identity?.persona_revision)
  }, {prepend: true})
  ctx.effect(() => {
    refreshTools().catch(() => {})
    const interval = setInterval(() => refreshTools().catch(() => {}), 3000)
    return () => { clearInterval(interval); for (const item of definitions.values()) item.dispose() }
  }, 'Fish tool catalog')
  ctx.on('llm/stream', async function* (options, next) {
    // Session-title and compaction calls can have no bound session; capture
    // only actual bound conversations and leave those auxiliary calls alone.
    if (options.sessionId && !options.purpose) {
      let identity
      try { identity = JSON.parse(readFileSync(resolve(root, 'harness/runtime/' + role + '.identities.json'), 'utf8'))[String(options.sessionId)] }
      catch (error) { if (error.code !== 'ENOENT') throw error }
      if (identity) await host('/requests/capture', {session: String(options.sessionId), payload: snapshotRequest(options)})
    }
    const reservation = await host('/usage/reserve')
    let usage, status = 'unknown'
    try {
      for await (const chunk of next()) {
        if (chunk.type === 'usage') usage = chunk.usage
        yield chunk
      }
      status = usage ? 'done' : 'unknown'
    } finally {
      await host('/usage/finish', {id: reservation.id, usage, status}).catch(() => {})
    }
  })
  ctx.on('agent/inbox/claimed', ({ agent, turn }) => {
    if (counts.get(agent)?.turn !== turn) counts.set(agent, { turn, count: 0 })
  })
  ctx.effect(() => ctx.tools.guard(exec => {
    try {
      let settings = {}
      try { settings = JSON.parse(readFileSync(resolve(root, 'harness/control.json'), 'utf8')) }
      catch (error) { if (error.code !== 'ENOENT') throw error }
      if (settings.paused_roles?.[role] ?? settings.paused) return 'Fish: 主人已暂停任务'
      if (exec.name === 'read_image') {
        let media = {}
        try { media = JSON.parse(readFileSync(resolve(root, 'harness/multimodal.json'), 'utf8')) }
        catch (error) { if (error.code !== 'ENOENT') throw error }
        if (media.vision?.enabled === false || media[role + '_enabled'] === false) return 'Fish: 本角色图片输入已关闭'
      }
      let identity
      if (exec.agent) {
        const identities = JSON.parse(readFileSync(resolve(root, 'harness/runtime/' + role + '.identities.json'), 'utf8'))
        identity = identities[String(exec.agent.id)]
        if (!identity) return 'Fish: 当前会话尚未通过程序身份核验'
        if (identity.owner && !identity.desktop) {
          const channel = JSON.parse(readFileSync(resolve(root, 'channels/fish_qq/config.json'), 'utf8'))
          if (String(channel.allowlist?.owner_id ?? '') !== identity.sender) return 'Fish: 主人账号已变更，请重新核验会话身份'
        }
      }
      if (exec.agent) {
        const prior = counts.get(exec.agent)
        const value = (prior?.count ?? 0) + 1
        counts.set(exec.agent, { turn: prior?.turn, count: value })
        if (value > (settings.max_tool_calls_per_turn ?? 12)) {
          exec.agent.cancel({ kind: 'user' })
          return 'Fish: 本轮工具次数达到上限'
        }
      }
      let registry = {plugins: []}
      try { registry = JSON.parse(readFileSync(resolve(root, 'harness/plugins/registry.json'), 'utf8')) }
      catch (error) { if (error.code !== 'ENOENT') throw error }
      const nativeAllowed = nativeTools.filter(item => {
        const plugin = registry.plugins.find(p => p.id === item.plugin)
        return plugin?.enabled && plugin.roles.includes(role) && plugin.installed_roles?.includes(role)
          && (!plugin.owner_only || identity?.owner) && (!plugin.allowed_tools || plugin.allowed_tools.includes(item.name))
      }).map(item => item.name)
      return toolDenial(exec, {...settings, fishTools, nativeTools: nativeAllowed, isGroup: !!identity?.scope?.startsWith('chat:group:'),
        isolated: identity?.isolated, allowed_paths: identity?.allowed_paths}, root, role)
    } catch { return 'Fish: 本地治理规则不可读，工具已阻止' }
  }), 'Fish mandatory tool guard')

  ctx.on('agent/created', (payload) => {
    const agent = payload?.agent
    if (agent?.id !== undefined) {
      agents.set(String(agent.id), agent)
    }
  })
  ctx.on('agent/disposed', (payload) => {
    const agent = payload?.agent
    if (agent?.id !== undefined) agents.delete(String(agent.id))
  })

  ctx.effect(() => ctx.webServer.register({
    kind: 'prefix',
    path: '/fish',
    handler: async (req, res) => {
      try {
        if (!token || req.headers['x-fish-token'] !== token) {
          sendJson(res, 401, { ok: false, error: 'unauthorized' })
          return
        }
        const url = new URL(req.url, 'http://127.0.0.1')
        if (url.pathname === '/fish/connect') {
          sendJson(res, 200, { ok: true, role, root_id: rootId, guard_active: true, control_version: 3, social_context_version: 1, social_roster_version: 1, group_history_version: 1, url: ctx.connection.authenticatedUrl(ctx.webServer.getOrigin() + '/') })
          return
        }
        if (url.pathname === '/fish/status') {
          sendJson(res, 200, { ok: true, active: agents.size, sessions: [...agents.keys()], guard_active: true, role, root_id: rootId })
          return
        }
        if (url.pathname === '/fish/tools') {
          sendJson(res, 200, {ok: true, role, root_id: rootId, tools: ctx.tools.schemas().filter(t => !t.name.startsWith('fish'))})
          return
        }
        if (url.pathname === '/fish/shutdown' && req.method === 'POST') {
          sendJson(res, 200, {ok: true, role, root_id: rootId})
          setTimeout(() => process.emit('SIGTERM'), 50)
          setTimeout(() => process.exit(0), 5000).unref()
          return
        }
        if (url.pathname === '/fish/cancel') {
          if (req.method !== 'POST') {
            sendJson(res, 405, { ok: false, error: 'method not allowed' })
            return
          }
          const body = await readBody(req)
          const requested = url.searchParams.get('session_id') || body?.session_id || ''
          const target = chooseCancelTarget([...agents.keys()], String(requested))
          if (!target.ok) {
            const code = target.error === 'multiple' ? 409 : target.error === 'none' ? 404 : 404
            sendJson(res, code, {
              ok: false,
              error: target.error,
              ...(target.sessions ? { sessions: target.sessions } : {}),
            })
            return
          }
          const agent = agents.get(target.id)
          if (agent === undefined) {
            sendJson(res, 404, { ok: false, error: 'not_found' })
            return
          }
          agent.cancel({ kind: 'user' })
          sendJson(res, 200, { ok: true, session_id: target.id })
          return
        }
        sendJson(res, 404, { ok: false, error: 'not found' })
      } catch (error) {
        sendJson(res, 500, { ok: false, error: String(error?.message ?? error) })
      }
    },
  }), 'fish-control: /fish routes')
}
