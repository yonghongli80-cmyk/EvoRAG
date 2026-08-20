from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UnitSeed:
    code: str; parent_code: str | None; name: str; kind: str; depth: int


@dataclass(frozen=True)
class PersonSeed:
    person_code: str; username: str; display_name: str; job_title: str; is_admin: bool


@dataclass(frozen=True)
class MembershipSeed:
    person_code: str; org_code: str; is_primary: bool


@dataclass(frozen=True)
class ManagerSeed:
    person_code: str; org_code: str


@dataclass(frozen=True)
class KnowledgeBaseSeed:
    kb_id: str; name: str; org_code: str; kb_type: str; sensitivity: str | None; documents: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class SeedManifest:
    units: tuple[UnitSeed, ...]
    people: tuple[PersonSeed, ...]
    memberships: tuple[MembershipSeed, ...]
    manager_scopes: tuple[ManagerSeed, ...]
    qualifications: tuple[tuple[str, str], ...]
    knowledge_bases: tuple[KnowledgeBaseSeed, ...]


def build_seed_manifest() -> SeedManifest:
    units = [UnitSeed("ORG-ROOT", None, "星河协作集团", "company", 1)]
    top_names = ("综合运营事业群", "数字产品事业群", "供应协同事业群", "市场发展事业群", "客户体验中心", "品质保障中心", "技术平台中心")
    department_names = ("规划部", "运营部", "支持部")
    departments: list[UnitSeed] = []
    for top_index, top_name in enumerate(top_names, 1):
        top_code = f"ORG-D{top_index:02d}"
        units.append(UnitSeed(top_code, "ORG-ROOT", top_name, "division", 2))
        for dep_index, suffix in enumerate(department_names, 1):
            dep = UnitSeed(f"{top_code}-P{dep_index:02d}", top_code, top_name[:2] + suffix, "department", 3)
            departments.append(dep)
            units.append(dep)
    team_no = 0
    for dep_index, dep in enumerate(departments):
        for local in range(2 if dep_index < 11 else 1):
            team_no += 1
            units.append(UnitSeed(f"ORG-T{team_no:02d}", dep.code, f"协作{team_no:02d}组", "team", 4))
    assert len(units) == 61

    surnames = ("林", "周", "沈", "许", "顾", "陆", "江", "叶", "唐", "苏", "温", "程", "白", "乔", "夏", "宋", "韩", "秦", "楚", "黎")
    given = ("知远", "明川", "嘉禾", "安宁", "思齐", "景行", "清和", "云舟", "书言", "予安", "星野", "怀瑾", "闻溪", "亦辰")
    people: list[PersonSeed] = []
    for i, (surname, first) in enumerate(((s, g) for s in surnames for g in given), 1):
        title = "公司管理员" if i <= 2 else ("组织负责人" if i <= 49 else ("资深专员" if i % 5 == 0 else "业务专员"))
        people.append(PersonSeed(f"P-{i:03d}", f"demo{i:03d}", surname + first, title, i <= 2))

    leaf_codes = [u.code for u in units if u.kind == "team"]
    memberships = [MembershipSeed(p.person_code, leaf_codes[(i - 1) % len(leaf_codes)], True) for i, p in enumerate(people, 1)]
    for offset, person_no in enumerate((80, 95, 110, 125, 140, 155, 170, 185), 3):
        memberships.append(MembershipSeed(f"P-{person_no:03d}", leaf_codes[(person_no + offset) % len(leaf_codes)], False))
    managers = [ManagerSeed("P-001", "ORG-ROOT"), ManagerSeed("P-002", "ORG-ROOT")]
    managers.extend(ManagerSeed(f"P-{i + 2:03d}", units[i].code) for i in range(1, 48))
    qualifications = tuple((f"P-{i:03d}", domain) for domain, start in (("finance", 20), ("hr", 30), ("customer", 40)) for i in range(start, start + 12))

    def docs(name: str, code: str) -> tuple[tuple[str, str], ...]:
        first = ("使用说明.md", f"# {name}\n\n本资料说明 {name} 的协作范围、资料维护方式和查询注意事项。\n")
        second = ("工作指引.md", f"# {name}工作指引\n\n编号：{code}\n\n请依据已发布资料开展协作，并及时维护过期内容。\n")
        return (first, second) if sum(map(ord, code)) % 2 == 0 else (first,)

    kbs = [KnowledgeBaseSeed("kb-company-public", "公司公共知识库", "ORG-ROOT", "company_public", None, docs("公司公共知识库", "PUBLIC"))]
    kbs.extend(KnowledgeBaseSeed(f"kb-{u.code.lower()}", f"{u.name}知识库", u.code, "organization", None, docs(f"{u.name}知识库", u.code)) for u in units)
    for domain, name in (("finance", "财务管理知识库"), ("hr", "人才发展知识库"), ("customer", "客户协作知识库")):
        kbs.append(KnowledgeBaseSeed(f"kb-sensitive-{domain}", name, "ORG-ROOT", "sensitive", domain, docs(name, domain)))
    return SeedManifest(tuple(units), tuple(people), tuple(memberships), tuple(managers), qualifications, tuple(kbs))
