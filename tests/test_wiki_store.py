
import pytest

from rag.wiki.models import extract_wiki_links
from rag.wiki.store import WikiStore


@pytest.fixture
def wiki_store(tmp_path):
    db_file = tmp_path / "wiki_test.db"
    store = WikiStore(db_file)
    yield store
    store.close()


def test_extract_wiki_links():
    text = """
    # Introduction
    Welcome to [[Overview]]!
    Please check [[Hardware Guide|Device Manual]] for specifications.
    Also refer to [[FAQ]].
    Invalid: [[ unclosed
    """
    links = extract_wiki_links(text)
    assert links == ["Overview", "Hardware Guide", "FAQ"]


def test_create_and_get_page(wiki_store):
    page = wiki_store.create_page(
        kb_id="kb_prod",
        title="快速上手指南",
        content="# 快速上手\n这是内容，参考 [[系统架构]].",
        tags=["guide", "onboarding"],
        author_id="user_admin",
        comment="创建快速上手指南",
    )

    assert page.id > 0
    assert page.kb_id == "kb_prod"
    assert page.title == "快速上手指南"
    assert page.slug == "快速上手指南"
    assert page.tags == ("guide", "onboarding")
    assert page.current_revision_id > 0

    fetched = wiki_store.get_page(page.id)
    assert fetched is not None
    assert fetched.id == page.id
    assert fetched.title == "快速上手指南"

    by_slug = wiki_store.get_page_by_slug("kb_prod", "快速上手指南")
    assert by_slug is not None
    assert by_slug.id == page.id

    by_title = wiki_store.get_page_by_title("kb_prod", "快速上手指南")
    assert by_title is not None
    assert by_title.id == page.id

    # 验证初始版本快照
    revisions = wiki_store.get_revisions(page.id)
    assert len(revisions) == 1
    assert revisions[0].revision_num == 1
    assert revisions[0].comment == "创建快速上手指南"
    assert revisions[0].content == page.content


def test_slug_uniqueness_and_custom_slug(wiki_store):
    wiki_store.create_page(
        kb_id="kb_prod",
        title="接口规范",
        slug="api-spec",
        content="规范内容",
    )

    # 同一个 kb_id 下相同 slug 报错
    with pytest.raises(ValueError) as exc:
        wiki_store.create_page(
            kb_id="kb_prod",
            title="接口规范第二版",
            slug="api-spec",
            content="冲突内容",
        )
    assert "slug" in str(exc.value).lower() or "unique" in str(exc.value).lower()

    # 不同 kb_id 下相同 slug 允许
    page_kb2 = wiki_store.create_page(
        kb_id="kb_test",
        title="接口规范",
        slug="api-spec",
        content="测试库规范内容",
    )
    assert page_kb2.id > 0


def test_update_creates_incremental_revisions(wiki_store):
    page = wiki_store.create_page(
        kb_id="kb_prod",
        title="配置说明",
        content="version 1",
        author_id="user_1",
    )

    updated = wiki_store.update_page(
        page.id,
        content="version 2: added new settings",
        comment="新增设置项说明",
        author_id="user_2",
    )

    assert updated.content == "version 2: added new settings"
    assert updated.current_revision_id > page.current_revision_id

    revisions = wiki_store.get_revisions(page.id)
    assert len(revisions) == 2
    assert revisions[0].revision_num == 1
    assert revisions[0].content == "version 1"
    assert revisions[1].revision_num == 2
    assert revisions[1].content == "version 2: added new settings"
    assert revisions[1].comment == "新增设置项说明"
    assert revisions[1].author_id == "user_2"
    assert revisions[1].delta is not None


def test_rollback_revision(wiki_store):
    page = wiki_store.create_page(
        kb_id="kb_prod",
        title="操作手册",
        content="初始内容 V1",
    )
    wiki_store.update_page(page.id, content="修改内容 V2", comment="更新第二版")
    wiki_store.update_page(page.id, content="错误改动 V3", comment="引入错误")

    # 回滚到 V1
    rolled = wiki_store.rollback_revision(page.id, revision_num=1, author_id="user_admin")
    assert rolled.content == "初始内容 V1"

    revisions = wiki_store.get_revisions(page.id)
    # 回滚产生第 4 个 revision，记录其回滚动作
    assert len(revisions) == 4
    assert revisions[-1].revision_num == 4
    assert "回滚" in revisions[-1].comment or "rollback" in revisions[-1].comment.lower()
    assert revisions[-1].content == "初始内容 V1"


def test_bidirectional_links_and_backlinks(wiki_store):
    # 先创建页面 B
    page_b = wiki_store.create_page(
        kb_id="kb_prod",
        title="核心架构",
        content="系统架构说明",
    )

    # 创建页面 A，内容链接到 [[核心架构]] 和未创建的 [[安全策略]]
    page_a = wiki_store.create_page(
        kb_id="kb_prod",
        title="项目概览",
        content="请参阅 [[核心架构]] 和 [[安全策略|安全准则]].",
    )

    # 查页面 A 的外链
    outgoing = wiki_store.get_outgoing_links(page_a.id)
    assert len(outgoing) == 2
    targets = {link["target_title"]: link["target_page_id"] for link in outgoing}
    assert targets["核心架构"] == page_b.id
    assert targets["安全策略"] is None

    # 查页面 B 的反向引用（被谁引用）
    backlinks = wiki_store.get_backlinks(page_b.id)
    assert len(backlinks) == 1
    assert backlinks[0].id == page_a.id
    assert backlinks[0].title == "项目概览"

    # 现在创建页面 C（即此前未创建的“安全策略”），验证关联解析
    page_c = wiki_store.create_page(
        kb_id="kb_prod",
        title="安全策略",
        content="安全规范细节",
    )
    # 重新获取 A 的外链或触发展开
    outgoing_updated = wiki_store.get_outgoing_links(page_a.id)
    targets_updated = {link["target_title"]: link["target_page_id"] for link in outgoing_updated}
    assert targets_updated["安全策略"] == page_c.id

    backlinks_c = wiki_store.get_backlinks(page_c.id)
    assert len(backlinks_c) == 1
    assert backlinks_c[0].id == page_a.id


def test_delete_page_and_cascades(wiki_store):
    page_a = wiki_store.create_page(
        kb_id="kb_prod",
        title="页面甲",
        content="内容甲",
    )
    page_id = page_a.id

    assert wiki_store.get_page(page_id) is not None
    assert len(wiki_store.get_revisions(page_id)) == 1

    deleted = wiki_store.delete_page(page_id)
    assert deleted is True
    assert wiki_store.get_page(page_id) is None
    assert len(wiki_store.get_revisions(page_id)) == 0


def test_list_pages_and_tag_filter(wiki_store):
    wiki_store.create_page(kb_id="kb_prod", title="文档1", content="c1", tags=["guide"])
    wiki_store.create_page(kb_id="kb_prod", title="文档2", content="c2", tags=["faq"])
    wiki_store.create_page(kb_id="kb_prod", title="文档3", content="c3", tags=["guide", "faq"])
    wiki_store.create_page(kb_id="kb_other", title="其他库文档", content="c4", tags=["guide"])

    prod_pages = wiki_store.list_pages(kb_id="kb_prod")
    assert len(prod_pages) == 3

    guide_pages = wiki_store.list_pages(kb_id="kb_prod", tag="guide")
    assert len(guide_pages) == 2
    titles = {p.title for p in guide_pages}
    assert titles == {"文档1", "文档3"}

    other_pages = wiki_store.list_pages(kb_id="kb_other")
    assert len(other_pages) == 1
    assert other_pages[0].title == "其他库文档"


def test_title_collision_prevention(wiki_store):
    """WIKI-01: Disallow duplicate page titles in the same knowledge base."""
    # 1. Create first page
    p1 = wiki_store.create_page(
        kb_id="kb_prod",
        title="系统架构",
        content="架构说明",
        slug="arch-v1",
    )
    assert p1.id > 0

    # 2. Attempt to create second page with different slug but duplicate title in same KB -> must fail
    with pytest.raises(ValueError, match="already exists"):
        wiki_store.create_page(
            kb_id="kb_prod",
            title="系统架构",
            content="重复架构",
            slug="arch-v2",
        )

    # 3. Same title in different KB -> allowed
    p_other = wiki_store.create_page(
        kb_id="kb_dev",
        title="系统架构",
        content="Dev架构",
        slug="arch-dev",
    )
    assert p_other.id > 0

    # 4. Create another page in kb_prod
    p2 = wiki_store.create_page(
        kb_id="kb_prod",
        title="数据库规范",
        content="规范说明",
        slug="db-rules",
    )

    # 5. Attempt to rename p2 to p1's title -> must fail
    with pytest.raises(ValueError, match="already exists"):
        wiki_store.update_page(p2.id, title="系统架构")

    # 6. Update p2 with its own title or content -> succeeds
    updated = wiki_store.update_page(p2.id, title="数据库规范", content="更新规范")
    assert updated.content == "更新规范"

