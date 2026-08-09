from pathlib import Path


DOC = Path(__file__).parents[1] / "docs" / "permission-design.md"


def test_permission_design_documents_the_runtime_scope_model():
    design = DOC.read_text(encoding="utf-8")

    assert "private / shared / public" in design
    assert "owner" in design
    assert "is_admin" in design
    assert "无权限记录" in design and "默认拒绝" in design


def test_permission_design_does_not_claim_level_based_authorization():
    design = DOC.read_text(encoding="utf-8")

    assert "用户等级 ≥ 文档等级" not in design
    assert "角色等级定义" not in design
    assert "permission_level` 不参与授权判断" in design
