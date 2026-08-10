import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api, { streamRequest } from '../utils/api'

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
  confidence_level?: 'high' | 'medium' | 'low' | 'unavailable'
  confidence_reasons?: string[]
  answerability_kind?: string
}

export const useChatStore = defineStore('chat', () => {
  const conversations = ref<Conversation[]>([])
  const currentConvId = ref<number | null>(null)
  const messages = ref<Message[]>([])
  const isStreaming = ref(false)
  const suggestedQuestions = ref<string[]>([])
  const selectedFile = ref<string | null>(null)
  const activeMode = ref<ChatMode>('file')
  const _selectedFilesByConv = new Map<number, string | null>()

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
    activeMode.value = mode
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
    try {
      const params: Record<string, string> = {}
      if (mode) params.mode = mode
      const res = await api.get('/conversations', { params })

      // Validate response is an array
      const data = Array.isArray(res.data) ? res.data : []
      conversations.value = data

      // If current conversation is no longer in the list, clear it
      if (currentConvId.value !== null) {
        const found = res.data.find((c: Conversation) => c.id === currentConvId.value)
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
    resetModeState(mode)
    // Save current selection before switching
    if (currentConvId.value !== null) {
      _selectedFilesByConv.set(currentConvId.value, selectedFile.value)
    }
    const res = await api.post('/conversations', { mode })
    conversations.value.unshift(res.data)
    _selectedFilesByConv.delete(res.data.id)
    currentConvId.value = res.data.id
    messages.value = []
    selectedFile.value = null
    return res.data
  }

  async function selectConversation(id: number) {
    const previousId = currentConvId.value
    const nextSelectedFile = id === previousId
      ? selectedFile.value
      : (_selectedFilesByConv.get(id) ?? null)
    try {
      const res = await api.get(`/conversations/${id}/messages`)
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
      // On failure, don't change currentConvId — keep showing old conversation
    }
  }

  async function deleteConversation(id: number) {
    await api.delete(`/conversations/${id}`)
    conversations.value = conversations.value.filter(c => c.id !== id)
    _selectedFilesByConv.delete(id)
    if (currentConvId.value === id) {
      currentConvId.value = null
      messages.value = []
      selectedFile.value = null
    }
  }

  function selectFile(name: string | null) {
    selectedFile.value = name
  }

  async function sendMessage(question: string, mode?: ChatMode, kbId?: string) {
    // Auto-create conversation if none exists
    if (!currentConvId.value && mode) {
      await createConversation(mode)
    }

    // Add user message
    messages.value.push({ role: 'user', content: question })
    isStreaming.value = true
    suggestedQuestions.value = []

    // Add empty assistant message for streaming
    messages.value.push({ role: 'assistant', content: '' })
    // Get the reactive proxy from the array (not the plain object)
    const assistantMsg = messages.value[messages.value.length - 1]

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

      const response = await streamRequest('/query/stream', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify(payload)
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

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''  // Keep incomplete line in buffer

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          try {
            const event = JSON.parse(line.slice(6))
            if (event.type === 'token') {
              answer += event.content
              assistantMsg.content = answer
            } else if (event.type === 'sources') {
              sources = event.sources
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
          } catch {}
        }
      }

      // Flush remaining bytes from TextDecoder (fixes CJK truncation)
      const tail = decoder.decode()
      if (tail) {
        for (const line of tail.split('\n')) {
          if (!line.startsWith('data: ')) continue
          try {
            const event = JSON.parse(line.slice(6))
            if (event.type === 'sources') sources = event.sources
          } catch {}
        }
      }

      // Process buffer residual (last event without trailing newline)
      if (buffer.startsWith('data: ')) {
        try {
          const event = JSON.parse(buffer.slice(6))
          if (event.type === 'sources') sources = event.sources
        } catch {}
      }

      assistantMsg.sources = sources

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
          suggestedQuestions.value = suggestRes.data.questions || []
        } catch {
          // Suggestions are non-critical, ignore errors
        }
      }

    } catch (err: any) {
      assistantMsg.content = err?.response?.data?.detail || '请求失败，请稍后重试。'
    } finally {
      isStreaming.value = false
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

    isStreaming.value = true
    try {
      const res = await api.post('/regenerate', {
        conversation_id: currentConvId.value,
        message_id: msg.id
      })
      msg.content = res.data.answer
    } catch {
      // Ignore
    } finally {
      isStreaming.value = false
    }
  }

  return {
    conversations,
    currentConvId,
    currentMode,
    messages,
    isStreaming,
    suggestedQuestions,
    selectedFile,
    currentConversation,
    conversationsByMode,
    resetModeState,
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
