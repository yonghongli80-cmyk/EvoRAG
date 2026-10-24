import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api, { isAbortError, streamRequest } from '../utils/api'

export type ChatMode = 'file' | 'kb' | 'analysis'

interface Conversation {
  id: number
  title: string
  mode: ChatMode
  created_at: string
}

interface Message {
  id?: number
  role: 'user' | 'assistant'
  content: string
  sources?: Array<{
    doc_name: string
    chunk_index: number
    text_preview?: string
    document_id?: number
    document_version_id?: number
    section_title?: string
    is_historical?: boolean
    rank?: number
  }>
  feedback?: 'positive' | 'negative'
  assistantMessageId?: number
  error?: string
  failed?: boolean
  /** Set when the user stopped the stream; keeps the partial answer visible. */
  stopped?: boolean
  confidence_level?: 'high' | 'medium' | 'low' | 'unavailable'
  confidence_reasons?: string[]
  answerability_kind?: string
  model?: string
  arenaResponses?: Record<string, string>
  structuredResult?: {
    kind: 'structured_result' | 'hybrid_result' | 'clarification_required'
    facts: Array<{ label: string; value: string }>
    sources: Array<{ dataset_id: number; sheet_key: string; cell_range: string }>
    rowsScanned: number
    clarification?: { code: string; question: string; options: string[] }
  }
}

export const useChatStore = defineStore('chat', () => {
  const conversations = ref<Conversation[]>([])
  const currentConvId = ref<number | null>(null)
  const messages = ref<Message[]>([])
  /**
   * The streaming flag is derived from the request that owns the stream instead
   * of being a bare boolean (E-05). A late `finally` from a stopped or
   * superseded request therefore cannot clear the flag belonging to a newer
   * one, which used to leave the input locked forever.
   */
  const activeRequestId = ref<number | null>(null)
  const isStreaming = computed(() => activeRequestId.value !== null)
  const suggestedQuestions = ref<string[]>([])
  const selectedFile = ref<string | null>(null)
  const activeMode = ref<ChatMode>('file')
  const availableModels = ref<Array<{ id: string; name: string; model: string; healthy: boolean }>>([])
  const selectedModel = ref<string>('deepseek')
  const arenaEnabled = ref<boolean>(false)
  const arenaModels = ref<string[]>(['deepseek'])
  const _selectedFilesByConv = new Map<number, string | null>()

  let _requestSeq = 0
  let _activeAbort: AbortController | null = null
  let _sendReservationSeq = 0
  let _sendReservationId: number | null = null
  /** Invalidates in-flight conversation loads so only the newest may land. */
  let _convRequestSeq = 0
  let _convLoadAbort: AbortController | null = null

  function beginStreamingRequest() {
    _requestSeq += 1
    const controller = new AbortController()
    activeRequestId.value = _requestSeq
    _activeAbort = controller
    return { requestId: _requestSeq, controller }
  }

  function finishStreamingRequest(requestId: number) {
    // A superseded request must not clear the flag of the request that owns it.
    if (activeRequestId.value !== requestId) return
    activeRequestId.value = null
    _activeAbort = null
  }

  /**
   * Cancel the stream currently rendered in the chat area (E-05). Partial
   * output stays on screen and is marked with `stopped`.
   */
  function stopStreaming() {
    const controller = _activeAbort
    _activeAbort = null
    activeRequestId.value = null
    if (controller) controller.abort()
  }

  /**
   * Drop every account-scoped state. Called on login/logout and when the API
   * answers 401/403 so the next account cannot see the previous one's data.
   */
  function reset() {
    stopStreaming()
    _sendReservationId = null
    invalidateConversationRequests()
    conversations.value = []
    currentConvId.value = null
    messages.value = []
    selectedFile.value = null
    suggestedQuestions.value = []
    _selectedFilesByConv.clear()
  }

  /**
   * Cancel pending conversation loads and return the new sequence number.
   * Any response whose sequence is no longer current is discarded (E-06).
   */
  function invalidateConversationRequests(): number {
    _convRequestSeq += 1
    const controller = _convLoadAbort
    _convLoadAbort = null
    if (controller) controller.abort()
    return _convRequestSeq
  }

  /** Forget per-conversation file selections for conversations that vanished. */
  function pruneSelectedFiles(list: Conversation[]) {
    const live = new Set(list.map(c => c.id))
    for (const id of Array.from(_selectedFilesByConv.keys())) {
      if (!live.has(id)) _selectedFilesByConv.delete(id)
    }
  }

  const currentConversation = computed(() =>
    conversations.value.find(c => c.id === currentConvId.value)
  )

  const currentMode = computed(() => activeMode.value)

  /** Backward-compatible filter (kept for existing test compatibility) */
  function conversationsByMode(mode: ChatMode) {
    return computed(() => conversations.value.filter(c => c.mode === mode))
  }

  function resetModeState(mode: ChatMode) {
    if (activeMode.value === mode) return
    stopStreaming()
    activeMode.value = mode
    invalidateConversationRequests()
    conversations.value = []
    currentConvId.value = null
    messages.value = []
    selectedFile.value = null
  }

  /**
   * Load conversations for a specific mode.
   * Each independent page calls this on mount with its own mode.
   * The store is effectively scoped to the last-loaded mode.
   */
  async function loadConversations(mode?: ChatMode) {
    if (mode) resetModeState(mode)
    const seq = invalidateConversationRequests()
    try {
      const params: Record<string, string> = {}
      if (mode) params.mode = mode
      const res = await api.get('/conversations', { params })

      // A newer load (mode switch, KB switch, logout) owns the list now.
      if (seq !== _convRequestSeq) return

      // Validate response is an array
      const data = Array.isArray(res.data) ? res.data : []
      conversations.value = data
      pruneSelectedFiles(data)

      // If current conversation is no longer in the list, clear it
      if (currentConvId.value !== null) {
        const found = data.find((c: Conversation) => c.id === currentConvId.value)
        if (!found) {
          currentConvId.value = null
          messages.value = []
        }
      }
    } catch {
      // Silently fail — user sees empty conversation list
    }
  }

  async function createConversation(mode: ChatMode = 'file') {
    stopStreaming()
    resetModeState(mode)
    // Save current selection before switching
    if (currentConvId.value !== null) {
      _selectedFilesByConv.set(currentConvId.value, selectedFile.value)
    }
    const seq = invalidateConversationRequests()
    const res = await api.post('/conversations', { mode })
    if (seq !== _convRequestSeq || activeMode.value !== mode) return undefined
    conversations.value.unshift(res.data)
    _selectedFilesByConv.delete(res.data.id)
    currentConvId.value = res.data.id
    messages.value = []
    selectedFile.value = null
    return res.data
  }

  async function selectConversation(id: number) {
    const previousId = currentConvId.value
    // Switching away from a conversation abandons its running stream instead of
    // letting late tokens (and a late title update) land on the new one.
    if (id !== previousId) stopStreaming()
    const nextSelectedFile = id === previousId
      ? selectedFile.value
      : (_selectedFilesByConv.get(id) ?? null)
    // Rapid clicks must not render an older conversation's messages (E-06):
    // cancel the previous load and only let the newest sequence land.
    const seq = invalidateConversationRequests()
    const controller = new AbortController()
    _convLoadAbort = controller
    try {
      const res = await api.get(`/conversations/${id}/messages`, { signal: controller.signal })
      // Drop superseded responses and any response that arrives after the user
      // (or a KB switch / logout) already moved to a different conversation.
      if (seq !== _convRequestSeq) return
      if (currentConvId.value !== previousId) return
      if (previousId !== null && previousId !== id) {
        _selectedFilesByConv.set(previousId, selectedFile.value)
      }
      selectedFile.value = nextSelectedFile
      currentConvId.value = id
      messages.value = res.data.map((m: any) => ({
        id: m.id,
        role: m.role,
        content: m.content,
        sources: m.sources || [],
        feedback: m.feedback || undefined,
        confidence_level: m.confidence_level,
        confidence_reasons: m.confidence_reasons,
        answerability_kind: m.answerability_kind
      }))
    } catch {
      // On failure (or abort), don't change currentConvId — keep old conversation
    } finally {
      if (_convLoadAbort === controller) _convLoadAbort = null
    }
  }

  async function deleteConversation(id: number) {
    await api.delete(`/conversations/${id}`)
    conversations.value = conversations.value.filter(c => c.id !== id)
    _selectedFilesByConv.delete(id)
    if (currentConvId.value === id) {
      stopStreaming()
      currentConvId.value = null
      messages.value = []
      selectedFile.value = null
      suggestedQuestions.value = []
    }
  }

  function selectFile(name: string | null) {
    selectedFile.value = name
  }

  async function sendMessage(question: string, mode?: ChatMode, kbId?: string) {
    // One stream at a time: the stop button (E-05) is the escape hatch, not a
    // second concurrent generation.
    if (isStreaming.value || _sendReservationId !== null) return

    // Auto-create conversation if none exists
    const reservationId = ++_sendReservationSeq
    _sendReservationId = reservationId
    try {
      if (!currentConvId.value && mode) {
        const created = await createConversation(mode)
        if (_sendReservationId !== reservationId || !created || currentConvId.value !== created.id) return
      }
    } finally {
      if (_sendReservationId === reservationId) _sendReservationId = null
    }
    const conversationId = currentConvId.value

    // Add user message
    messages.value.push({ role: 'user', content: question })
    suggestedQuestions.value = []

    // Add empty assistant message for streaming
    messages.value.push({
      role: 'assistant',
      content: '',
      arenaResponses: arenaEnabled.value && arenaModels.value.length > 1 ? {} : undefined
    })
    // Get the reactive proxy from the array (not the plain object)
    const assistantMsg = messages.value[messages.value.length - 1]

    const { requestId, controller } = beginStreamingRequest()
    const isOwner = () => activeRequestId.value === requestId

    try {
      const payload: Record<string, any> = {
        question,
        conversation_id: currentConvId.value,
        session_id: currentConvId.value ? `conv_${currentConvId.value}` : undefined
      }
      if (selectedFile.value) {
        payload.doc_name = selectedFile.value
      }
      if (mode === 'kb' && kbId) {
        payload.kb_id = kbId
      }
      if (arenaEnabled.value && arenaModels.value.length > 1) {
        payload.models = arenaModels.value
      } else if (selectedModel.value) {
        payload.model = selectedModel.value
      }

      const response = await streamRequest('/query/stream', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify(payload),
        signal: controller.signal
      })

      if (!response.body) {
        assistantMsg.content = '响应为空，请重试'
        return
      }
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let answer = ''
      let sources: any[] = []
      let buffer = ''

      const handleEvent = (event: any) => {
        if (event.type === 'token') {
          if (event.model && assistantMsg.arenaResponses) {
            assistantMsg.arenaResponses[event.model] =
              (assistantMsg.arenaResponses[event.model] || '') + (event.content || '')
            assistantMsg.content = Object.entries(assistantMsg.arenaResponses)
              .map(([m, c]) => `### [${m}]\n${c}`)
              .join('\n\n')
          } else {
            answer += event.content || ''
            assistantMsg.content = answer
          }
        } else if (event.type === 'sources') {
          sources = event.sources
        } else if (event.type === 'structured_result') {
          assistantMsg.structuredResult = {
            kind: event.kind,
            facts: event.facts || [],
            sources: event.sources || [],
            rowsScanned: event.rows_scanned || 0,
            clarification: event.clarification
          }
        } else if (event.type === 'error') {
          // The backend reports mid-stream failures as `{"type":"error","reason":...}`
          // and then closes the stream. Without this branch the assistant bubble stayed
          // blank forever and no failure state was surfaced.
          let errorText = '生成失败，请稍后重试'
          if (typeof event.reason === 'string' && event.reason) {
            errorText = event.reason
          } else if (typeof event.error === 'string' && event.error) {
            errorText = event.error
          } else if (typeof event.message === 'string' && event.message) {
            errorText = event.message
          }
          assistantMsg.error = errorText
          assistantMsg.failed = true
          if (!assistantMsg.content) assistantMsg.content = errorText

          const messageId = typeof event.assistant_message_id === 'number'
            ? event.assistant_message_id
            : (typeof event.message_id === 'number' ? event.message_id : null)
          if (messageId !== null) {
            assistantMsg.id = messageId
            assistantMsg.assistantMessageId = messageId
          }
        } else if (event.type === 'done') {
          if (typeof event.assistant_message_id === 'number') {
            assistantMsg.id = event.assistant_message_id
            assistantMsg.assistantMessageId = event.assistant_message_id
          }
          if (event.reliability) {
            assistantMsg.confidence_level = event.reliability.confidence_level
            assistantMsg.confidence_reasons = event.reliability.confidence_reasons
            assistantMsg.answerability_kind = event.reliability.answerability_kind
          }
        }
      }

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        // The user pressed stop (or a newer request took over): freeze output.
        if (!isOwner()) {
          assistantMsg.stopped = true
          return
        }

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''  // Keep incomplete line in buffer

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          try {
            handleEvent(JSON.parse(line.slice(6)))
          } catch {}
        }
      }

      if (!isOwner()) {
        assistantMsg.stopped = true
        return
      }

      // Flush remaining bytes from TextDecoder (fixes CJK truncation)
      const tail = decoder.decode()
      if (tail) {
        for (const line of tail.split('\n')) {
          if (!line.startsWith('data: ')) continue
          try {
            handleEvent(JSON.parse(line.slice(6)))
          } catch {}
        }
      }

      // Process buffer residual (last event without trailing newline)
      if (buffer.startsWith('data: ')) {
        try {
          handleEvent(JSON.parse(buffer.slice(6)))
        } catch {}
      }

      assistantMsg.sources = sources

      // The user moved on to another conversation while this answer finished:
      // never rename or suggest against the newly selected one.
      if (currentConvId.value !== conversationId) {
        assistantMsg.stopped = true
        return
      }

      // Generate conversation title from first user message
      const currentConv = conversations.value.find(c => c.id === currentConvId.value)
      if (currentConv && (!currentConv.title || currentConv.title === '新对话')) {
        try {
          const titleRes = await api.post(`/conversations/${currentConvId.value}/generate-title`, {
            question
          })
          currentConv.title = titleRes.data.title
        } catch {
          // Fallback: use first 20 chars
          currentConv.title = question.slice(0, 20)
        }
      }

      // Fetch suggested follow-up questions after streaming completes
      if (answer) {
        try {
          const suggestRes = await api.post('/suggest', { question, answer })
          if (isOwner() && currentConvId.value === conversationId) {
            suggestedQuestions.value = suggestRes.data.questions || []
          }
        } catch {
          // Suggestions are non-critical, ignore errors
        }
      }

    } catch (err: any) {
      // Deliberate stop (E-05) or a superseded request: keep partial output and
      // never replace it with a transport error.
      if (isAbortError(err)) {
        assistantMsg.stopped = true
        if (!assistantMsg.content) assistantMsg.content = '已停止生成'
        return
      }
      if (!isOwner()) return

      // Error bodies are either FastAPI's `detail` or the hand-written `error`
      // shape used by the streaming endpoint.
      const data = err?.response?.data
      const detail = data?.detail
      const errorText = data?.error
      assistantMsg.content =
        (typeof detail === 'string' && detail) ||
        (typeof errorText === 'string' && errorText) ||
        '请求失败，请稍后重试。'
      assistantMsg.failed = true
    } finally {
      finishStreamingRequest(requestId)
    }
  }

  async function sendFeedback(messageIndex: number, value: 'positive' | 'negative') {
    const msg = messages.value[messageIndex]
    if (!msg) return

    // Toggle: clicking the same feedback again cancels it
    const newValue = msg.feedback === value ? undefined : value
    msg.feedback = newValue

    // Only send to API if giving feedback (not cancelling)
    if (newValue && msg.id) {
      try {
        await api.post('/feedback', {
          message_id: msg.id,
          value: newValue
        })
      } catch {
        // Ignore feedback API errors
      }
    }
  }

  async function regenerate(messageIndex: number) {
    const msg = messages.value[messageIndex]
    if (!msg || msg.role !== 'assistant' || !msg.id || !currentConvId.value) return
    if (isStreaming.value) return

    const { requestId, controller } = beginStreamingRequest()
    try {
      const res = await api.post('/regenerate', {
        conversation_id: currentConvId.value,
        message_id: msg.id
      }, {
        signal: controller.signal
      })
      if (activeRequestId.value !== requestId) return
      msg.content = res.data.answer
      msg.sources = Array.isArray(res.data.sources) ? res.data.sources : []
      const reliability = res.data.reliability
      msg.confidence_level = reliability?.confidence_level || 'unavailable'
      msg.confidence_reasons = Array.isArray(reliability?.confidence_reasons)
        ? reliability.confidence_reasons
        : []
      msg.answerability_kind = reliability?.answerability_kind || 'answerable'
    } catch {
      // Ignore
    } finally {
      finishStreamingRequest(requestId)
    }
  }

  async function loadAvailableModels() {
    try {
      const res = await api.get('/models')
      if (res.data && Array.isArray(res.data.models)) {
        availableModels.value = res.data.models
        if (res.data.default_model) {
          selectedModel.value = res.data.default_model
        }
      }
    } catch {
      // Fallback: keep defaults
    }
  }

  return {
    conversations,
    currentConvId,
    currentMode,
    messages,
    activeRequestId,
    isStreaming,
    stopStreaming,
    suggestedQuestions,
    selectedFile,
    currentConversation,
    availableModels,
    selectedModel,
    arenaEnabled,
    arenaModels,
    loadAvailableModels,
    conversationsByMode,
    resetModeState,
    reset,
    loadConversations,
    createConversation,
    selectConversation,
    deleteConversation,
    selectFile,
    sendMessage,
    sendFeedback,
    regenerate
  }
})
