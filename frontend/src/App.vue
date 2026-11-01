<script setup>
import { computed, nextTick, reactive, ref } from 'vue'
import { useChat } from './composables/useChat'
import { insertMention, mentionQuery } from './mention'

const {
  nickname, messages, onlineCount, members, status, statusLabel, error, userId,
  agent, agentMessages, agentDraft, agentError, taskList, activeTasks,
  roomStreams, agentStreams,
  connect, send, sendAgent, stopTask, disconnect,
  followup, invite, isLocal, isAdmin, notices, dismissNotice,
  automation, automationError, automationBusy, setPolicy, summarizeNow, refreshAutomation,
} = useChat()

const panel = ref('private')
const showInvite = ref(false)
const supplements = reactive({})
const newResults = computed(() => automation.prepared.length + notices.value.length)
function submitFollowup(task) {
  if (followup(task.taskId, supplements[task.taskId] || '')) supplements[task.taskId] = ''
}
function dateLabel(iso) { return iso ? new Date(iso).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '' }
const draft = ref('')
const composerInput = ref(null)
const mentionOpen = ref(false)
const mentionRange = ref(null)
const mentionOptions = computed(() => {
  const query = (mentionRange.value?.query || '').toLowerCase()
  return [{ userId: 'agent', nickname: agent.name, isAgent: true }, ...members.value.filter(member => member.userId !== userId.value && member.nickname)]
    .filter(member => member.nickname.toLowerCase().includes(query))
})
const joined = computed(() => status.value === 'connected' || status.value === 'connecting' || status.value === 'disconnected')

function join() { connect(nickname.value) }
function submit() { if (send(draft.value)) draft.value = ''; nextTick(() => document.querySelector('.composer textarea')?.focus()) }
function onKeydown(event) { if (event.key === 'Escape' && mentionOpen.value) { mentionOpen.value = false; event.preventDefault(); return } if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { if (mentionOpen.value && mentionOptions.value.length) { event.preventDefault(); selectMention(mentionOptions.value[0]); return } event.preventDefault(); submit() } }
function updateMentionQuery(event) {
  mentionRange.value = mentionQuery(draft.value, event.target.selectionStart)
  mentionOpen.value = !!mentionRange.value
}
function openMentionMenu() {
  if (status.value !== 'connected') return
  mentionRange.value = null
  mentionOpen.value = !mentionOpen.value
}
function selectMention(member) {
  const textarea = composerInput.value
  const range = mentionRange.value
  const start = range?.start ?? textarea?.selectionStart ?? draft.value.length
  const end = range?.end ?? textarea?.selectionEnd ?? start
  const next = insertMention(draft.value, member.nickname, start, end)
  draft.value = next.text
  mentionOpen.value = false
  mentionRange.value = null
  nextTick(() => { textarea?.focus(); textarea?.setSelectionRange(next.cursor, next.cursor) })
}
function mentionPerson(name) {
  if (status.value !== 'connected' || !name) return
  const textarea = composerInput.value
  const start = textarea?.selectionStart ?? draft.value.length
  const end = textarea?.selectionEnd ?? start
  const next = insertMention(draft.value, name, start, end)
  mentionOpen.value = false
  mentionRange.value = null
  draft.value = next.text
  nextTick(() => { textarea?.focus(); textarea?.setSelectionRange(next.cursor, next.cursor) })
}
function submitAgent() {
  if (sendAgent(agentDraft.value)) agentDraft.value = ''
  nextTick(() => document.querySelector('.agent-composer textarea')?.focus())
}
function onAgentKeydown(event) { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); submitAgent() } }
function timeLabel(iso) { return iso ? new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(new Date(iso)) : '' }
function taskStatusLabel(value) {
  return ({ queued: '排队中', running: '执行中', done: '已完成', failed: '失败', stopped: '已停止' }[value] || value)
}
</script>

<template>
  <main class="page-shell">
    <section v-if="!joined || status === 'idle'" class="welcome-card">
      <div class="welcome-mark" aria-hidden="true">✳</div>
      <div class="eyebrow">A LITTLE SPACE FOR EVERYONE</div>
      <h1>相遇，从聊天开始<span>。</span></h1>
      <p>给自己起个名字，和朋友们聊聊此刻的想法。<br>智能体「{{ agent.name }}」也在房间里，试试 @它。</p>
      <form class="join-form" @submit.prevent="join">
        <label for="nickname">你的昵称</label>
        <input id="nickname" v-model="nickname" maxlength="20" autofocus placeholder="例如：小雨" />
        <template v-if="!isLocal">
          <label for="invite">房间邀请码</label>
          <input id="invite" v-model="invite" type="password" autocomplete="off" placeholder="向主机管理员获取" />
        </template>
        <small v-else class="join-hint">从主机本地加入后，可管理主动规则并查看队友邀请码。</small>
        <button type="submit">进入聊天室 <span>→</span></button>
      </form>
      <p v-if="error" class="error-text">{{ error }}</p>
    </section>

    <div v-else class="workspace">
      <section class="chat-card">
        <header class="chat-header">
          <div class="room-heading"><div class="room-icon" aria-hidden="true">✳</div><div><h1>小小聊天室</h1><div class="room-caption">一个分享想法的小空间</div></div></div>
          <div class="presence"><span :class="['status-dot', status]"></span><strong>{{ onlineCount }}</strong> 人在线</div>
        </header>
        <div class="sub-header">
          <span>{{ statusLabel }}</span>
          <span class="agent-hint">输入 @ 或点击按钮选择在线成员</span>
          <button v-if="status === 'disconnected'" class="link-button" @click="connect()">重新连接</button>
          <button class="link-button leave" @click="disconnect">离开</button>
        </div>
        <div class="messages" aria-live="polite">
          <div v-if="!messages.length && !roomStreams.length" class="empty-state"><span>✦</span><p>还没有消息，先打个招呼吧。</p></div>
          <article v-for="message in messages" :key="message.id" :class="['message', { mine: message.userId === userId, agent: message.isAgent }]">
            <div class="message-meta"><button v-if="message.userId !== userId && !message.isAgent" type="button" class="mention-name" :disabled="status !== 'connected'" :aria-label="`提及 ${message.nickname}`" :title="`@${message.nickname}`" @click="mentionPerson(message.nickname)">{{ message.nickname }}</button><strong v-else>{{ message.nickname }}</strong><time>{{ timeLabel(message.timestamp) }}</time></div>
            <p>{{ message.content }}</p>
          </article>
          <article v-for="stream in roomStreams" :key="stream.id" class="message agent streaming">
            <div class="message-meta"><strong>{{ agent.name }}</strong><span class="typing">正在输入</span></div>
            <p>{{ stream.content }}<span class="cursor">▌</span></p>
          </article>
        </div>
        <form class="composer" @submit.prevent="submit">
          <div v-if="mentionOpen" class="mention-menu" role="group" aria-label="选择要提及的成员">
            <div class="mention-menu-title">提及成员</div>
            <button v-for="member in mentionOptions" :key="member.userId" type="button" class="mention-option" @mousedown.prevent @click="selectMention(member)">
              <span class="mention-avatar">{{ member.nickname.slice(0, 1) }}</span><span>{{ member.nickname }}</span><small>{{ member.isAgent ? '智能体' : '在线 · ' + member.userId.slice(0, 4) }}</small>
            </button>
            <div v-if="!mentionOptions.length" class="mention-empty">没有匹配的成员</div>
          </div>
          <button type="button" class="mention-button" title="提及成员" aria-label="提及成员" :aria-expanded="mentionOpen" @click="openMentionMenu">@</button>
          <textarea ref="composerInput" v-model="draft" maxlength="2000" rows="2" placeholder="说点什么…（Enter 发送，Shift + Enter 换行）" :disabled="status !== 'connected'" @keydown="onKeydown" @input="updateMentionQuery" @click="updateMentionQuery"></textarea>
          <button type="submit" :disabled="status !== 'connected' || !draft.trim()" aria-label="发送">↑</button>
        </form>
        <p v-if="error" class="error-text composer-error">{{ error }}</p>
      </section>

      <aside class="agent-card">
        <header class="agent-header">
          <div class="agent-avatar">深</div>
          <div class="agent-title">
            <strong>{{ agent.name }}</strong>
            <span>{{ agent.allowWrite ? '项目助手 · 可编辑文件' : '项目助手 · 只读运行' }}</span>
          </div>
          <span :class="['status-dot', agent.online ? 'connected' : '']"></span>
        </header>

        <p v-if="agent.demo" class="agent-banner">演示模式：当前结果由固定规则生成，未调用真实模型。</p>
        <p v-else-if="activeTasks.length" class="agent-banner busy">{{ activeTasks.length }} 个任务正在排队或执行。</p>
        <nav class="assistant-tabs" aria-label="助手面板">
          <button :class="{ selected: panel === 'private' }" @click="panel = 'private'">私聊</button>
          <button :class="{ selected: panel === 'tasks' }" @click="panel = 'tasks'">任务 <small>{{ taskList.length }}</small></button>
          <button :class="{ selected: panel === 'automation' }" @click="panel = 'automation'; refreshAutomation()">主动能力 <small v-if="newResults">{{ newResults }}</small></button>
        </nav>

        <section v-if="panel === 'tasks'" class="task-board task-panel">
          <p v-if="!taskList.length" class="panel-empty">任务开始后，进度和结果会保存在这里。</p>
          <article v-for="task in taskList" :key="task.taskId" :class="['task-card', task.status]">
            <div class="task-row">
              <span class="task-title">{{ task.title }}</span>
              <span class="task-status">{{ task.retryPending ? '等待重试' : taskStatusLabel(task.status) }}</span>
              <button v-if="task.ownerId === userId && (task.status === 'queued' || task.status === 'running' || task.retryPending)" class="task-stop" @click="stopTask(task.taskId)">停止</button>
            </div>
            <p v-if="task.health === 'suspected_stall'" class="error-text">疑似停滞：暂未收到新的执行信号。</p>
            <p v-if="task.error" class="error-text">{{ task.error }}</p>
            <details v-if="task.output || task.result" class="result-detail">
              <summary>查看结果</summary><p>{{ task.result || task.output }}</p>
            </details>
            <details v-if="task.ownerId === userId && (!task.kind || task.kind === 'interactive')" class="result-detail">
              <summary>补充这个任务</summary>
              <form class="followup-form" @submit.prevent="submitFollowup(task)">
                <textarea v-model="supplements[task.taskId]" rows="2" maxlength="2000" placeholder="补充要求；已结束的任务会作为新请求处理"></textarea>
                <button type="submit" :disabled="status !== 'connected' || !(supplements[task.taskId] || '').trim()">发送补充</button>
              </form>
            </details>
            <details v-if="task.traces && task.traces.length" class="task-traces">
              <summary>{{ task.traces.length }} 步执行记录</summary>
              <ul>
                <li v-for="(trace, index) in task.traces" :key="index">
                  <code>{{ trace.step }}</code><span>{{ trace.detail }}</span>
                </li>
              </ul>
            </details>
          </article>
        </section>

        <section v-if="panel === 'automation'" class="automation-panel">
          <div class="panel-heading"><h2>主动能力</h2><span>{{ isAdmin ? '主机管理员' : '共享房间' }}</span></div>
          <p class="panel-caption">提前准备线索，跟踪异常，在固定时间整理讨论。</p>
          <div class="rule-controls">
            <label><span>静默预处理<small>报错出现后，准备只读定位线索</small></span><input type="checkbox" :checked="automation.policy.preprocess" :disabled="!isAdmin || automationBusy" @change="setPolicy({ preprocess: $event.target.checked })" /></label>
            <label><span>任务异常监控<small>仅在失败、停滞或恢复时通知</small></span><input type="checkbox" :checked="automation.policy.watchdog" :disabled="!isAdmin || automationBusy" @change="setPolicy({ watchdog: $event.target.checked })" /></label>
            <label><span>定时摘要<small>按北京时间整理共享房间记录</small></span><input type="checkbox" :checked="automation.policy.summary" :disabled="!isAdmin || automationBusy" @change="setPolicy({ summary: $event.target.checked })" /></label>
          </div>
          <div class="summary-time">
            <label for="summary-hour">每日</label>
            <input id="summary-hour" type="number" min="0" max="23" :value="automation.policy.hour ?? 19" :disabled="!isAdmin || automationBusy" @change="setPolicy({ hour: Number($event.target.value) })" aria-label="摘要小时" />
            <span>:</span><input type="number" min="0" max="59" :value="automation.policy.minute ?? 0" :disabled="!isAdmin || automationBusy" @change="setPolicy({ minute: Number($event.target.value) })" aria-label="摘要分钟" />
            <button v-if="isAdmin" :disabled="automationBusy || !automation.policy.summary" @click="summarizeNow">生成当前摘要</button>
          </div>
          <p class="quota-label">今日后台调用 {{ automation.budget.used || 0 }} / {{ automation.budget.limit || 30 }} · 为摘要保留 3 次</p>
          <p v-if="!isAdmin" class="panel-caption">规则由主机管理员调整。</p>
          <details v-if="isAdmin" class="invite-detail">
            <summary>队友邀请码</summary>
            <div class="invite-row"><input :type="showInvite ? 'text' : 'password'" readonly :value="automation.invite" aria-label="队友邀请码" /><button @click="showInvite = !showInvite">{{ showInvite ? '隐藏' : '显示' }}</button></div>
            <small>邀请码允许加入共享房间。私聊和任务归属仍按各自会话保存。</small>
          </details>
          <p v-if="automationError" class="error-text">{{ automationError }}</p>
          <section v-if="notices.length" class="notification-list">
            <h3>通知 <small>{{ notices.length }}</small></h3>
            <article v-for="notice in notices" :key="notice.id"><p>{{ notice.message }}</p><button @click="dismissNotice(notice.id)">已读</button></article>
          </section>
          <h3>已准备 <small>{{ automation.prepared.length }}</small></h3>
          <p v-if="!automation.prepared.length" class="panel-empty">暂无有效线索。启用后，明确报错会自动触发；结果保留 30 分钟。</p>
          <details v-for="item in automation.prepared" :key="item.id" class="automation-result" open>
            <summary>报错定位线索 <time>{{ dateLabel(item.createdAt) }}</time></summary>
            <p>{{ item.content }}</p>
            <small>来源 {{ item.sources.length }} 条消息 · 引用文件 {{ Object.keys(item.files).join('、') || '无' }}</small>
            <small>有效至 {{ dateLabel(item.expiresAt) }}；文件变化后自动失效。</small>
          </details>
          <h3>房间摘要 <small>{{ automation.summaries.length }}</small></h3>
          <p v-if="!automation.summaries.length" class="panel-empty">暂无摘要。启用后从当前时间开始记录；空窗口会自动跳过。</p>
          <details v-for="item in automation.summaries" :key="item.id" class="automation-result">
            <summary>{{ item.catchup ? '补摘要' : '讨论摘要' }} <time>{{ dateLabel(item.end) }}</time></summary>
            <small>{{ dateLabel(item.start) }} — {{ dateLabel(item.end) }} · {{ item.coveredCount }} / {{ item.sourceCount }} 条</small>
            <p>{{ item.content }}</p>
          </details>
          <details v-if="automation.recentIssues.length" class="result-detail">
            <summary>最近未完成的后台事项</summary>
            <p v-for="issue in automation.recentIssues" :key="issue.id">{{ issue.kind === 'summary' ? '摘要' : '预处理' }}：{{ issue.error || taskStatusLabel(issue.status) }}</p>
          </details>
        </section>

        <div v-if="panel === 'private'" class="agent-messages" aria-live="polite">
          <div v-if="!agentMessages.length && !agentStreams.length" class="agent-empty">
            <span>✦</span>
            <p>这里是你与 {{ agent.name }} 的私聊。共享讨论留在左侧，任务进度与主动结果在上方查看。</p>
          </div>
          <article v-for="message in agentMessages" :key="message.id" :class="['agent-message', message.role]">
            <div class="agent-message-meta">
              <strong>{{ message.role === 'agent' ? agent.name : '你' }}</strong>
              <time>{{ timeLabel(message.timestamp) }}</time>
            </div>
            <p>{{ message.content }}</p>
          </article>
          <article v-for="stream in agentStreams" :key="stream.id" class="agent-message agent streaming">
            <div class="agent-message-meta"><strong>{{ agent.name }}</strong><span class="typing">思考中</span></div>
            <p>{{ stream.content }}<span class="cursor">▌</span></p>
          </article>
        </div>

        <form v-if="panel === 'private'" class="agent-composer" @submit.prevent="submitAgent">
          <textarea v-model="agentDraft" maxlength="2000" rows="2" :disabled="status !== 'connected'" placeholder="把问题或任务告诉智能体…" @keydown="onAgentKeydown"></textarea>
          <button type="submit" :disabled="status !== 'connected' || !agentDraft.trim()" aria-label="发送给智能体">↑</button>
        </form>
        <p v-if="agentError" class="error-text agent-error">{{ agentError }}</p>
      </aside>
    </div>
  </main>
</template>
