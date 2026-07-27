# Security Boundary Hardening — 开发日志

工作分支：`codex/security-boundary-hardening`  
工作目录：`RAGv3/.worktrees/security-boundary-hardening`

## 已完成

| 提交 | 内容 | 验证 |
| --- | --- | --- |
| `512be22` | 统一解析 JWT / API Key 身份；API Key 必须映射到真实用户 | 37 项认证与用户库测试通过 |
| `5918a37` | 数据源默认关闭、仅管理员可管理、配置响应脱敏 | 8 项数据源与认证测试通过 |
| `c4cfcee` | 上传/导入认证、同名文件保护、失败补偿、移除 `/data/upload` 静态暴露 | 19 项后端测试、2 项前端测试、前端构建通过 |
| `8cab15c` | 知识库 ACL：列表、详情、查询、删除遵循所有者/管理员/明确共享规则 | 74 项知识库相关测试通过 |
| `68a0edf` | 文档 ACL：历史无权限记录默认拒绝、保护文件不可改、共享 edit 权限持久化 | 34 项文档/权限/上传/知识库测试通过 |
| `3c0609a` | 移除按“问题文本”缓存最终答案的逻辑，避免跨用户泄漏 | 专门缓存安全测试通过 |
| `89cd0b2` | 知识库源文件隔离路径、暂存写入与原子落盘基础模块 | 3 项路径安全测试通过 |
| `c863551` | 上传、单文件删除和整库删除接入持久化源文件、状态记录与失败补偿 | 15 项生命周期/知识库/文档 ACL 测试通过 |
| `0f8a907` | 流式聊天绑定显式 `kb_id` 并在后端先做 KB ACL 验证 | 2 项后端测试、1 项前端 store 测试、前端构建通过 |

## 正在进行

- 审计、数据生命周期与最终验证。

## 回家后可执行的验证

在 PowerShell 中执行：

```powershell
$project = 'C:\Users\Administrator\Desktop\RAGv3\RAGv3\.worktrees\security-boundary-hardening'
Set-Location $project

& .\.venv\Scripts\python.exe -m pytest `
  tests\test_api_security.py `
  tests\test_data_sources_security.py `
  tests\test_upload_security.py `
  tests\test_kb_security_boundaries.py `
  tests\test_document_security_boundaries.py `
  tests\test_kb_file_lifecycle.py `
  tests\test_stream_kb_target.py `
  tests\test_pipeline_cache_security.py -v

Set-Location "$project\frontend"
& .\node_modules\.bin\vitest.cmd run src/api/__tests__/files.test.ts
& npm.cmd run build
```

## 已知环境限制

- 完整 `tests/test_pipeline.py` 会因本机没有配置模型 API 凭据而失败，错误为 `Missing credentials`。这不是上述安全修复造成的失败。
- FastAPI 的 `@app.on_event("startup")` 与 Starlette TestClient 会显示弃用警告；目前不影响测试结果。
