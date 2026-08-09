import { ref } from 'vue'
import api from '../utils/api'
import { ElMessage } from 'element-plus'

/**
 * Composable for adding messages to analysis card groups.
 *
 * Provides:
 * - addToAnalysis(question, answer): opens the dialog to add a question to a card
 * - dialog state (visible, question, answer, loading)
 * - handleConfirm(cardId, newCardName): callback for dialog confirm
 */
export function useAnalysis() {
  const loading = ref(false)
  const dialogVisible = ref(false)
  const dialogQuestion = ref('')
  const dialogAnswer = ref('')

  function addToAnalysis(question: string, answer: string) {
    dialogQuestion.value = question
    dialogAnswer.value = answer
    dialogVisible.value = true
  }

  async function handleConfirm(cardId: number | null, newCardName?: string) {
    loading.value = true

    try {
      let targetCardId = cardId

      if (targetCardId === null && newCardName) {
        // Create a new card
        const createRes = await api.post('/analysis/cards', {
          name: newCardName,
        })
        targetCardId = createRes.data.id
      }

      if (targetCardId === null) return

      // Add the question and answer to the card
      await api.post(`/analysis/cards/${targetCardId}/questions`, {
        question: dialogQuestion.value,
        answer: dialogAnswer.value,
      })

      ElMessage.success('已添加到分析卡片')
      dialogVisible.value = false
    } catch {
      ElMessage.error('添加到分析失败')
    } finally {
      loading.value = false
    }
  }

  return {
    loading,
    dialogVisible,
    dialogQuestion,
    dialogAnswer,
    addToAnalysis,
    handleConfirm,
  }
}
