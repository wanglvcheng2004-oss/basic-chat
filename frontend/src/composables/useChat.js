import { computed, nextTick, onMounted, reactive, ref } from 'vue'

export function useChat() {
  const nickname = ref('')
  const messages = ref([])
  const onlineCount = ref(0)
  const members = ref([])
  const status = ref('idle')
  const error = ref('')
  const socket = ref(null)
  const userId = ref('')
  const invite = ref('')
  const isLocal = ref(false)
  const isAdmin = ref(false)
  const notices = ref([])
  const automationError = ref('')
  const automationBusy = ref(false)
  const automation = reactive({ policy: {}, prepared: [], summaries: [], budget: {}, recentIssues: [], invite: '' })

  const agent = reactive({ name: '小深', online: false, demo: false, allowWrite: true, maxWorkers: 3 })
  const agentMessages = ref([])
  const agentDraft = ref('')
  const agentError = ref('')
  const tasks = reactive({})
  const streaming = reactive({})

  function mergeMessages(current, incoming) {
    const byId = new Map(current.map(item => [item.id || item.messageId, item]))
    for (const item of incoming) byId.set(item.id || item.messageId, item)
    return [...byId.values()].sort((a, b) => (a.timestamp || '').localeCompare(b.timestamp || ''))
  }

  async function api(path, method = 'GET', body) {
    const response = await fetch(path, {
      method, credentials: 'same-origin',
      ...(body ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}),
    })
    const data = await response.json()
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求未通过校验，请检查输入。')
    return data
  }

  async function refreshAutomation() {
    try { Object.assign(automation, await api('/api/automation')); automationError.value = '' }
    catch (e) { automationError.value = e.message }
  }

  async function setPolicy(changes) {
    automationBusy.value = true
    try { Object.assign(automation, await api('/api/automation', 'PUT', changes)); automationError.value = '' }
    catch (e) { automationError.value = e.message }
    finally { automationBusy.value = false }
  }

  async function summarizeNow() {
    automationBusy.value = true
    try { Object.assign(automation, await api('/api/automation/summary', 'POST')); automationError.value = '' }
    catch (e) { automationError.value = e.message }
    finally { automationBusy.value = false }
  }

  const statusLabel = computed(() => ({
    idle: '尚未加入', connecting: '连接中…', connected: '在线', disconnected: '已断开',
  }[status.value] || status.value))

  const taskList = computed(() => Object.values(tasks).sort((a, b) => (b.createdAt || '').localeCompare(a.createdAt || '')))
  const activeTasks = computed(() => taskList.value.filter((task) => task.status === 'queued' || task.status === 'running'))
  const roomStreams = computed(() => Object.entries(streaming)
    .filter(([, item]) => item.channel === 'room' && !item.taskId)
    .map(([id, item]) => ({ id, content: item.content })))
  const agentStreams = computed(() => Object.entries(streaming)
    .filter(([, item]) => item.channel === 'dm' && !item.taskId)
    .map(([id, item]) => ({ id, content: item.content })))

  function scrollTo(selector) {
    nextTick(() => {
      const element = document.querySelector(selector)
      if (element) element.scrollTop = element.scrollHeight
    })
  }

  function resetAgentState() {
    for (const key of Object.keys(tasks)) delete tasks[key]
    for (const key of Object.keys(streaming)) delete streaming[key]
    agentMessages.value = []
    agentError.value = ''
  }

  function pushAgentMessage(message) {
    if (message.id && agentMessages.value.some((item) => item.id === message.id)) return
    agentMessages.value.push(message)
    scrollTo('.agent-messages')
  }

  function handlePacket(packet) {
    if (packet.type === 'ready') {
      userId.value = packet.userId
      isAdmin.value = !!packet.admin
      onlineCount.value = packet.onlineCount
      members.value = Array.isArray(packet.members) ? packet.members : []
      if (packet.agent) Object.assign(agent, packet.agent)
      refreshAutomation()
    } else if (packet.type === 'presence') {
      onlineCount.value = packet.onlineCount
      members.value = Array.isArray(packet.members) ? packet.members : []
    } else if (packet.type === 'chat') {
      messages.value = mergeMessages(messages.value, [packet])
      scrollTo('.messages')
    } else if (packet.type === 'chat.history') {
      messages.value = mergeMessages(messages.value, packet.messages || [])
      scrollTo('.messages')
    } else if (packet.type === 'agent.tasks') {
      for (const task of packet.tasks || []) tasks[task.taskId] = { traces: [], ...tasks[task.taskId], ...task }
    } else if (packet.type === 'agent.automation') {
      Object.assign(automation, packet)
    } else if (packet.type === 'agent.notice') {
      if (!notices.value.some(item => item.id === packet.id)) notices.value.push(packet)
    } else if (packet.type === 'agent.history') {
      agentMessages.value = mergeMessages(agentMessages.value, (packet.messages || []).map((item) => ({
        id: item.id, role: item.role, content: item.content, timestamp: item.timestamp,
      })))
      scrollTo('.agent-messages')
    } else if (packet.type === 'agent.message') {
      pushAgentMessage({
        id: packet.messageId || packet.id,
        role: packet.role || 'user',
        content: packet.content,
        timestamp: packet.timestamp,
      })
    } else if (packet.type === 'agent.delta') {
      const entry = streaming[packet.messageId] || (streaming[packet.messageId] = {
        content: '', channel: packet.channel, taskId: packet.taskId || null,
      })
      entry.content += packet.content
      if (packet.taskId && tasks[packet.taskId]) tasks[packet.taskId].output = entry.content
      if (!packet.taskId) scrollTo(packet.channel === 'room' ? '.messages' : '.agent-messages')
    } else if (packet.type === 'agent.done') {
      if (packet.channel === 'room') {
        messages.value = mergeMessages(messages.value, [{
          id: packet.messageId || packet.id,
          messageId: packet.messageId || packet.id,
          userId: packet.userId || 'agent',
          nickname: packet.nickname || agent.name,
          content: packet.content,
          timestamp: packet.timestamp,
          isAgent: true,
        }])
        scrollTo('.messages')
      } else {
        pushAgentMessage({
          id: packet.messageId || packet.id,
          role: 'agent',
          content: packet.content,
          timestamp: packet.timestamp,
          taskId: packet.taskId || null,
        })
      }
      if (packet.messageId) delete streaming[packet.messageId]
      if (packet.taskId && tasks[packet.taskId]) tasks[packet.taskId].output = packet.content
    } else if (packet.type === 'agent.state') {
      const task = tasks[packet.taskId] || (tasks[packet.taskId] = { taskId: packet.taskId, traces: [] })
      Object.assign(task, packet)
      if (!task.traces) task.traces = []
    } else if (packet.type === 'agent.trace') {
      const task = tasks[packet.taskId]
      if (task) {
        task.traces = [...(task.traces || []), { step: packet.step, detail: packet.detail, timestamp: packet.timestamp }].slice(-30)
      }
    } else if (packet.type === 'agent.error') {
      agentError.value = packet.message || '智能体处理失败'
    } else if (packet.type === 'error') {
      error.value = packet.message || '消息发送失败'
    }
  }

  async function connect(name = nickname.value) {
    const cleanName = name.trim()
    error.value = ''
    if (!cleanName) { error.value = '请输入昵称'; return false }
    nickname.value = cleanName
    status.value = 'connecting'
    try {
      const data = await api('/api/session', 'POST', { nickname: cleanName, invite: invite.value })
      if (userId.value && userId.value !== data.member.userId) { messages.value = []; notices.value = []; resetAgentState() }
      userId.value = data.member.userId
      isAdmin.value = data.member.admin
      if (data.agent) Object.assign(agent, data.agent)
      openSocket()
      return true
    } catch (e) { error.value = e.message; status.value = 'idle'; return false }
  }

  function openSocket() {
    socket.value?.close()
    status.value = 'connecting'
    const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${scheme}://${window.location.host}/ws`)
    socket.value = ws
    ws.addEventListener('open', () => { if (socket.value === ws) status.value = 'connected' })
    ws.addEventListener('message', (event) => {
      try { handlePacket(JSON.parse(event.data)) } catch { /* 忽略无法解析的包 */ }
    })
    ws.addEventListener('close', () => { if (socket.value === ws) { status.value = 'disconnected'; socket.value = null; members.value = [] } })
    ws.addEventListener('error', () => { if (socket.value === ws) { error.value = '连接失败，请检查后端服务'; status.value = 'disconnected' } })
    return true
  }

  onMounted(async () => {
    try {
      const data = await api('/api/session')
      isLocal.value = data.local
      if (data.agent) Object.assign(agent, data.agent)
      if (data.member) {
        nickname.value = data.member.nickname
        userId.value = data.member.userId
        isAdmin.value = data.member.admin
        openSocket()
      }
    } catch (e) { error.value = e.message }
  })

  function send(content) {
    const text = content.trim()
    if (!text || !socket.value || socket.value.readyState !== WebSocket.OPEN) return false
    socket.value.send(JSON.stringify({ type: 'chat', content: text }))
    return true
  }

  function sendAgent(content) {
    const text = content.trim()
    if (!text) return false
    if (!socket.value || socket.value.readyState !== WebSocket.OPEN) {
      agentError.value = '连接已断开，请先重新连接'
      return false
    }
    agentError.value = ''
    socket.value.send(JSON.stringify({ type: 'agent.dm', content: text }))
    return true
  }

  function stopTask(taskId) {
    if (!socket.value || socket.value.readyState !== WebSocket.OPEN) return false
    socket.value.send(JSON.stringify({ type: 'agent.stop', taskId }))
    return true
  }

  function disconnect() { socket.value?.close(); socket.value = null; status.value = 'disconnected' }

  function followup(taskId, content) {
    if (!content.trim() || socket.value?.readyState !== WebSocket.OPEN) return false
    socket.value.send(JSON.stringify({ type: 'agent.followup', taskId, content: content.trim() }))
    return true
  }

  function dismissNotice(id) {
    if (socket.value?.readyState !== WebSocket.OPEN) return
    socket.value.send(JSON.stringify({ type: 'notice.ack', noticeId: id }))
    notices.value = notices.value.filter(item => item.id !== id)
  }

  return {
    nickname, messages, onlineCount, members, status, statusLabel, error, userId,
    agent, agentMessages, agentDraft, agentError, tasks, taskList, activeTasks,
    roomStreams, agentStreams,
    connect, send, sendAgent, stopTask, disconnect, followup,
    invite, isLocal, isAdmin, notices, dismissNotice, automation, automationError, automationBusy,
    setPolicy, summarizeNow, refreshAutomation,
  }
}
