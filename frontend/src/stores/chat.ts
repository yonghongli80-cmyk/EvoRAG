import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '../utils/api'
import { useAuthStore } from './auth'

export type ChatMode = 'file' | 'kb' | 'analysis'

const API = import.meta.env.VITE_API_BASE ?? '/api'

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
  sources?: Array<{ doc_name: string; chunk_index: number }>
  feedback?: 'positive' | 'negative'
}

export const useChatStore = defineStore('chat', () => {
  const conversations = ref<Conversation[]>([])
  const currentConvId = ref<number | null>(null)
  const messages = ref<Message[]>([])
  const isStreaming = ref(false)
  const suggestedQuestions = ref<string[]>([])
  const selectedFile = ref<string | null>(null)
  const _selectedFilesByConv = new Map<number, string | null>()

  const currentConversation = computed(() =>
    conversations.value.find(c => c.id === currentConvId.value)
  )

  /** Backward-compatible alias used by some tests */
  const currentMode = computed(() => 'file' as ChatMode)

  /** Backward-compatible filter (kept for existing test compatibility) */
  function conversationsByMode(mode: ChatMode) {
    return computed(() => conversations.value.filter(c => c.mode === mode))
  }

  /**
   * Load conversations for a specific mode.
   * Each independent page calls this on mount with its own mode.
   * The store is effectively scoped to the last-loaded mode.
   */
  async function loadConversations(mode?: ChatMode) {
    const auth = useAuthStore()
    try {
      const params: Record<string, string> = {}
      if (mode) params.mode = mode
      const res = await api.get('/conversations', {
        headers: auth.getAuthHeaders(),
        params
      })

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
    const auth = useAuthStore()
    // Save current selection before switching
    if (currentConvId.value !== null) {
      _selectedFilesByConv.set(currentConvId.value, selectedFile.value)
    }
    const res = await api.post('/conversations', { mode }, {
      headers: auth.getAuthHeaders()
    })
    conversations.value.unshift(res.data)
    currentConvId.value = res.data.id
    messages.value = []
    selectedFile.value = null
    return res.data
  }

  async function selectConversation(id: number) {
    const auth = useAuthStore()
    // Save current selection for the previous conversation
    if (currentConvId.value !== null) {
      _selectedFilesByConv.set(currentConvId.value, selectedFile.value)
    }
    // Restore selection for this conversation
    selectedFile.value = _selectedFilesByConv.has(id) ? _selectedFilesByConv.get(id)! : null
    try {
      const res = await api.get(`/conversations/${id}/messages`, {
        headers: auth.getAuthHeaders()
      })
      currentConvId.value = id
      messages.value = res.data.map((m: any) => ({
        id: m.id,
        role: m.role,
        content: m.content,
        sources: m.sources || [],
        feedback: m.feedback || undefined
      }))
    } catch {
      // On failure, don't change currentConvId — keep showing old conversation
    }
  }

  async function deleteConversation(id: number) {
    const auth = useAuthStore()
    await api.delete(`/conversations/${id}`, {
      headers: auth.getAuthHeaders()
    })
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

  async function sendMessage(question: string, mode?: ChatMode) {
    const auth = useAuthStore()

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

      const response = await fetch(`${API}/query/stream`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...auth.getAuthHeaders()
        },
        body: JSON.stringify(payload)
      })

      if (!response.ok) {
        const err = await response.json().catch(() => ({ detail: '请求失败' }))
        assistantMsg.content = err.detail || '请求失败'
        return
      }

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
          const titleRes = await fetch(`${API}/conversations/${currentConvId.value}/generate-title`, {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              ...auth.getAuthHeaders()
            },
            body: JSON.stringify({ question })
          })
          if (titleRes.ok) {
            const titleData = await titleRes.json()
            currentConv.title = titleData.title
          }
        } catch {
          // Fallback: use first 20 chars
          currentConv.title = question.slice(0, 20)
        }
      }

      // Fetch suggested follow-up questions after streaming completes
      if (answer) {
        try {
          const suggestRes = await fetch(`${API}/suggest`, {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              ...auth.getAuthHeaders()
            },
            body: JSON.stringify({ question, answer })
          })
          if (suggestRes.ok) {
            const suggestData = await suggestRes.json()
            suggestedQuestions.value = suggestData.questions || []
          }
        } catch {
          // Suggestions are non-critical, ignore errors
        }
      }

    } catch (err) {
      assistantMsg.content = '请求失败，请稍后重试。'
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
      const auth = useAuthStore()
      try {
        await api.post('/feedback', {
          message_id: msg.id,
          value: newValue
        }, { headers: auth.getAuthHeaders() })
      } catch {
        // Ignore feedback API errors
      }
    }
  }

  async function regenerate(messageIndex: number) {
    const msg = messages.value[messageIndex]
    if (!msg || msg.role !== 'assistant' || !msg.id || !currentConvId.value) return

    const auth = useAuthStore()
    isStreaming.value = true
    try {
      const res = await api.post('/regenerate', {
        conversation_id: currentConvId.value,
        message_id: msg.id
      }, { headers: auth.getAuthHeaders() })
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
