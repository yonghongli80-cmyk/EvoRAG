<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { ChatDotRound, Plus } from '@element-plus/icons-vue'
import { useChatStore } from '../../stores/chat'
import MessageBubble from '../../components/MessageBubble.vue'
import ChatInput from '../../components/ChatInput.vue'

const props = defineProps<{
  kbId: string
}>()

const emit = defineEmits<{
  (e: 'add-to-analysis', question: string, answer: string): void
}>()

const chatStore = useChatStore()
const messagesContainer = ref<HTMLElement>()

// Scroll handling
watch(() => chatStore.messages.length, () => {
  nextTick(() => {
    messagesContainer.value?.scrollTo({ top: messagesContainer.value.scrollHeight, behavior: 'smooth' })
  })
})

watch(() => chatStore.messages[chatStore.messages.length - 1]?.content, () => {
  nextTick(() => {
    messagesContainer.value?.scrollTo({ top: messagesContainer.value.scrollHeight, behavior: 'smooth' })
  })
})

async function newConversation() {
  await chatStore.createConversation('kb')
}

function askSuggested(q: string) {
  chatStore.sendMessage(q, 'kb', props.kbId)
}
</script>

<template>
  <div class="kb-chat-area">
    <div ref="messagesContainer" class="messages">
      <!-- No conversations at all -->
      <div v-if="!chatStore.conversations.length" class="empty-chat">
        <el-icon :size="48" style="color: var(--color-border);"><ChatDotRound /></el-icon>
        <h3>开始在知识库中提问</h3>
        <p>创建对话后，在此知识库中检索问答</p>
        <el-button type="primary" @click="newConversation" style="margin-top: 12px;">
          <el-icon style="margin-right: 6px;"><Plus /></el-icon>
          新建对话
        </el-button>
      </div>

      <!-- Conversation selected but no messages -->
      <div v-else-if="!chatStore.messages.length" class="empty-chat">
        <el-icon :size="48" style="color: var(--color-border);"><ChatDotRound /></el-icon>
        <h3>在此知识库中提问</h3>
        <p>选择左侧的文档，或直接提问</p>
      </div>

      <MessageBubble
        v-for="(msg, i) in chatStore.messages"
        :key="i"
        :message="msg"
        :index="i"
        :question="i > 0 && chatStore.messages[i - 1]?.role === 'user' ? chatStore.messages[i - 1].content : ''"
        @add-to-analysis="(q: string, a: string) => emit('add-to-analysis', q, a)"
      />

      <div v-if="chatStore.isStreaming && !chatStore.messages[chatStore.messages.length - 1]?.content" class="msg assistant">
        <div class="msg-avatar ai-avatar">R</div>
        <div class="msg-body">
          <div class="typing"><span></span><span></span><span></span></div>
        </div>
      </div>
    </div>

    <div v-if="chatStore.suggestedQuestions.length" class="suggestions">
      <button v-for="q in chatStore.suggestedQuestions" :key="q" class="suggest-btn" @click="askSuggested(q)">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="suggest-icon">
          <path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z"/>
        </svg>
        <span>{{ q }}</span>
      </button>
    </div>

    <ChatInput v-if="chatStore.currentConvId" mode="kb" :kb-id="props.kbId" />
  </div>
</template>

<style scoped>
.kb-chat-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--color-background);
}

.messages {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-6) 0;
}

.empty-chat {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--color-secondary);
  gap: var(--space-2);
}

.empty-chat h3 {
  font-family: var(--font-heading);
  font-size: var(--text-lg);
  font-weight: var(--font-semibold);
  color: var(--color-foreground);
  margin: 0;
}

.empty-chat p {
  font-size: var(--text-sm);
  color: var(--color-secondary);
}

.suggestions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  padding: 0 var(--space-6) var(--space-3);
}

.suggest-btn {
  padding: var(--space-2) var(--space-4);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-full);
  font-size: var(--text-sm);
  color: var(--color-secondary);
  cursor: pointer;
  font-family: var(--font-body);
  transition: all var(--duration-fast);
}

.suggest-btn:hover {
  border-color: var(--color-accent);
  color: var(--color-accent);
}

.typing {
  display: flex;
  gap: 6px;
  padding: var(--space-4);
}

.typing span {
  width: 8px;
  height: 8px;
  background: var(--color-border);
  border-radius: 50%;
  animation: bounce 1.4s infinite;
}

.typing span:nth-child(2) { animation-delay: 0.2s; }
.typing span:nth-child(3) { animation-delay: 0.4s; }

@keyframes bounce {
  0%, 60%, 100% { transform: translateY(0); }
  30% { transform: translateY(-8px); }
}

.msg {
  display: flex;
  gap: var(--space-4);
  max-width: var(--content-max-width);
  margin: 0 auto;
  padding: var(--space-3) var(--space-6);
  animation: slideUp 0.3s var(--ease-out);
}

.msg-avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  font-size: 13px;
  font-weight: var(--font-bold);
}

.ai-avatar {
  background: var(--color-foreground);
  color: white;
  font-family: var(--font-mono);
  font-size: 12px;
}

.msg-body {
  flex: 1;
  min-width: 0;
  max-width: 85%;
}

@keyframes slideUp {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}
</style>
