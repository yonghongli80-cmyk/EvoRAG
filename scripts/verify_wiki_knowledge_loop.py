"""End-to-End Verification: Hypertext Wiki Substrate & Track B Remediation Loop.

Simulates the complete lifecycle:
1. Online failure clustering & knowledge gap ticket generation (undetermined failure).
2. Authoring a Wiki article with Markdown AST headers, tables, and internal links.
3. Structure-aware AST chunking & hybrid Qdrant + BM25 ingestion.
4. Linking the gap ticket to the Wiki remediation asset.
5. Deterministic re-check probe closing the loop (status -> content_added).
6. Wiki revision history snapshot and instant rollback.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from qdrant_client import QdrantClient

import rag.bm25_store as bm25_store
import rag.retriever as retriever_module
import rag.vector_store as vector_store
from rag.evolution.gap_remediation import GapRemediationService
from rag.evolution.gaps import KnowledgeGapTicket
from rag.evolution.store import FailureTraceStore
from rag.wiki.chunker import MarkdownASTChunker
from rag.wiki.indexer import WikiIndexer
from rag.wiki.store import WikiStore


def run_verification() -> bool:
    print("=" * 70)
    print("🚀 [Step 0/5] 初始化测试沙箱环境 (Isolated Sandbox)...")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        wiki_db = str(tmppath / "wiki.db")
        evo_db = str(tmppath / "evolution.db")
        qdrant_dir = str(tmppath / "qdrant")
        bm25_db = str(tmppath / "bm25.db")

        # Configure vector and bm25 stores
        vector_store._client = QdrantClient(path=qdrant_dir)
        bm25_store.settings.bm25_db_path = bm25_db
        retriever_module.settings.bm25_db_path = bm25_db

        def mock_embed(texts: list[str]) -> list[list[float]]:
            return [[float(len(t) % 50) / 50.0] * 1024 for t in texts]

        retriever_module.embed = mock_embed
        retriever_module.invalidate_bm25_cache()

        wiki_store = WikiStore(db_path=wiki_db)
        trace_store = FailureTraceStore(db_path=evo_db)
        chunker = MarkdownASTChunker(max_chunk_chars=400, min_chunk_chars=50)
        indexer = WikiIndexer(wiki_store=wiki_store, chunker=chunker, embed_fn=mock_embed)

        kb_id = "kb_network"

        # -------------------------------------------------------------
        # 1. 模拟线上拒答与起草知识缺口工单
        # -------------------------------------------------------------
        print("\n📋 [Step 1/5] 模拟线上因证据不足拒答，起草 KnowledgeGapTicket...")
        cluster = trace_store.upsert_cluster(
            cluster_key=f"{kb_id}|generation|bgp_oscillation",
            kb_id=kb_id,
            failure_stage="generation",
            root_cause_feature="undetermined",
            seen_at=100.0,
        )

        ticket = KnowledgeGapTicket(
            cluster_id=cluster["id"],
            kb_id=kb_id,
            question_samples=("BGP 路由震荡怎么处理？",),
            occurrences=6,
            first_seen=50.0,
            last_seen=100.0,
            missing_terms=("bgp", "震荡", "路由"),
            probed_questions=1,
            rationale="线上提问6次因知识库缺乏 BGP 处置流程而拒答",
            created_at=100.0,
            status="open",
        )
        ticket_id = trace_store.add_gap_ticket(ticket)
        print(f"   ✓ 工单生成成功: ID={ticket_id}, 状态={ticket.status}, 缺失词={ticket.missing_terms}")

        # -------------------------------------------------------------
        # 2. 采编原子化 Wiki 词条（结构感知 AST：包含多级大纲、表格与双向链接）
        # -------------------------------------------------------------
        print("\n✍️  [Step 2/5] 采编超文本 Wiki 词条（含表格、代码与双向超链接）...")
        wiki_md = """# BGP 运维保障规范

本手册规定核心骨干网 BGP 路由协议维护流程，针对 BGP 路由震荡怎么处理给出明确 SOP 指引，参考关联条目 [[告警监控手册]]。

## 故障定义与阈值表
当检测到对等体状态频繁在 Established 与 Idle 间跳变时触发处理：

| 告警级别 | 震荡频次阈值 | 阻尼衰减半衰期 | 处置动作 |
| :--- | :--- | :--- | :--- |
| P1 紧急 | > 10次/分钟 | 15分钟 | 强制会话去激活并切备用链路 |
| P2 重要 | > 5次/分钟  | 30分钟 | 启用路由衰减抑制 (Flap Damping) |

## 快速恢复操作指南
运维值班工程师必须按以下规范执行：
首先排查本地物理光路质量与 MTU 配置；
其次登录路由器执行 `bgp dampening` 命令限制震荡路由扩散；
最后通过回程路由核验恢复正常转发。
"""
        page = wiki_store.create_page(
            kb_id=kb_id,
            title="BGP路由维护SOP",
            content=wiki_md,
            tags=["network", "sop", "bgp"],
            author_id="net_ops_lead",
        )
        print(f"   ✓ Wiki 页面创建成功: ID={page.id}, Title='{page.title}', Revision={page.current_revision_id}")

        # -------------------------------------------------------------
        # 3. 执行结构感知分块与双路混合索引
        # -------------------------------------------------------------
        print("\n🧩 [Step 3/5] 执行 Markdown AST 切块与 Qdrant/BM25 双路索引...")
        chunks_indexed = indexer.sync_page(page.id)
        print(f"   ✓ 同步完成，生成结构化切块数: {chunks_indexed}")

        chunks = chunker.chunk_page(page)
        for i, c in enumerate(chunks):
            print(f"     - Chunk #{i} 路径: '{c.section_path}' (前缀: '{c.text.splitlines()[0]}')")
            if "| P1 紧急 |" in c.text:
                print("       ✓ 验证通过: 表格行结构保持原子化，未发生中途截断")

        # -------------------------------------------------------------
        # 4. 工单关联 Wiki 资产与自动复测闭环
        # -------------------------------------------------------------
        print("\n🔄 [Step 4/5] 关联工单与 Wiki 资产，触发 Worker 确定性重检...")
        # Mock a retrieval reader that returns the indexed page content
        class MockReader:
            def retrieve(self, question: str, *, kb_id: str | None = None) -> list:
                class ChunkWrapper:
                    def __init__(self, text: str):
                        self.text = text

                return [ChunkWrapper(c.text) for c in chunks]

        service = GapRemediationService(
            trace_store=trace_store,
            wiki_store=wiki_store,
            retrieval_reader=MockReader(),
        )

        link_res = service.link_ticket_to_wiki(
            ticket_id=ticket_id,
            page_id=page.id,
            author_id="admin_reviewer",
        )
        print(f"   ✓ 工单成功关联至 Wiki ID={link_res['remediation']['asset_id']}")

        recheck_res = service.recheck_ticket(ticket_id)
        print(f"   ✓ 重检判定结论: {recheck_res['verdict']}")
        print(f"   ✓ 覆盖词项: {recheck_res['covered_terms']}")
        print(f"   ✓ 剩余缺失: {recheck_res['still_missing']}")
        print(f"   ✓ 工单最终状态: {recheck_res['status']}")

        assert recheck_res["status"] == "content_added", "工单应自动流转为 content_added"
        assert recheck_res["verdict"] == "covered", "所有缺失词应被完全覆盖"

        # -------------------------------------------------------------
        # 5. 版本快照与秒级原子回滚验证
        # -------------------------------------------------------------
        print("\n⏪ [Step 5/5] 测试版本修订历史与秒级原子回滚...")
        wiki_store.update_page(
            page.id,
            content="# 临时修改\n临时测试草稿内容。",
            comment="临时修改",
            author_id="test_user",
        )
        revs_after_edit = wiki_store.get_revisions(page.id)
        print(f"   ✓ 编辑后修订历史版本数: {len(revs_after_edit)}")
        assert len(revs_after_edit) == 2

        # Rollback to revision 1
        rolled_back = wiki_store.rollback_revision(page.id, revision_num=1, author_id="admin_user")
        print(f"   ✓ 秒级回滚完成，当前修订版本 ID={rolled_back.current_revision_id}")
        assert "临时测试草稿内容" not in rolled_back.content
        assert "BGP 运维保障规范" in rolled_back.content
        print("   ✓ 内容已完整还原至版本 #1")

        vector_store.close_client()
        wiki_store.close()
        trace_store.close()
        import gc
        gc.collect()

    print("\n" + "=" * 70)
    print("🎉 恭喜！超文本 Wiki 子系统与轨道 B 自演进闭环全流程实测演练全部通过！")
    print("=" * 70)
    return True


if __name__ == "__main__":
    success = run_verification()
    exit(0 if success else 1)
