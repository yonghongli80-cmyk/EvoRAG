"""查询改写模块 — 将口语化问题改写为正式问法，提升检索命中率"""

from rag.prompt_manager import PromptManager
from rag.resilience import retry

_pm = PromptManager()


def _history_block(history, summary: str | None) -> str:
    """把最近几轮对话渲染成提示词前缀（只用于补全指代，不改变问题本身）。"""
    lines = ["# 最近的对话（用于补全指代；若本问题已是独立完整的问题，请原样返回）"]
    if summary:
        lines.append(f"（更早内容的摘要：{summary}）")
    for role, content in history:
        speaker = "用户" if role == "user" else "助手"
        lines.append(f"{speaker}：{str(content)[:400]}")
    return "\n".join(lines)


@retry(max_attempts=2, backoff_base=0.5, retryable_exceptions=(TimeoutError, ConnectionError, OSError))
def rewrite_query(question: str, history=(), summary: str | None = None) -> str:
    """将口语化问题改写为正式问法，失败时返回原问题"""
    from rag.generator import generate

    try:
        prompt = _pm.render("rewrite", question=question)
        if history or summary:
            # 追问"它呢？"这类指代在**当轮**里没有先行词；把最近几轮喂进来才能补全。
            prompt = _history_block(history, summary) + "\n\n" + prompt
        messages = [
            {"role": "system", "content": "你是一个查询改写助手。"},
            {"role": "user", "content": prompt},
        ]
        result = generate(messages).strip()
        return result if result else question
    except Exception:
        return question
