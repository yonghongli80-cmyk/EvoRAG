# RAGv3 部署指南

> 从零到上线的完整记录，包含所有踩过的坑和解决方案。

---

## 一、服务器准备

### 1.1 购买服务器

推荐阿里云学生机：
- 配置：1 核 CPU / 2GB 内存 / 20GB 磁盘
- 系统：Ubuntu 22.04 LTS
- 需要公网 IP

### 1.2 安全组配置

阿里云控制台 → 实例 → 安全组 → 入方向 → 手动添加：

| 协议 | 端口 | 来源 | 说明 |
|------|------|------|------|
| TCP | 8000 | 0.0.0.0/0 | RAG 应用端口 |
| TCP | 22 | 0.0.0.0/0 | SSH 连接（默认已开） |

---

## 二、安装 Docker

### 2.1 连接服务器

通过阿里云 ECS Workbench（浏览器版 SSH）或本地 SSH 客户端连接。

### 2.2 安装 Docker（使用国内镜像）

```bash
curl -fsSL https://get.docker.com | bash -s docker --mirror Aliyun
```

### 2.3 配置 Docker 镜像源（加速镜像拉取）

```bash
mkdir -p /etc/docker
cat > /etc/docker/daemon.json << 'EOF'
{
  "registry-mirrors": [
    "https://docker.1ms.run",
    "https://docker.xuanyuan.me"
  ]
}
EOF
systemctl restart docker
```

### 2.4 验证安装

```bash
docker --version && docker compose version
```

---

## 三、部署应用

### 3.1 克隆项目

```bash
cd ~
git clone https://github.com/sighistp/RAG.git
cd RAG
```

### 3.2 配置环境变量

```bash
cp .env.example .env
nano .env
```

填入你的 API Key：

```bash
RAG_DEEPSEEK_API_KEY=你的deepseek-key
RAG_BAILIAN_API_KEY=你的硅基流动key
RAG_BAILIAN_BASE_URL=https://api.siliconflow.cn/v1
RAG_BAILIAN_EMBED_MODEL=BAAI/bge-m3
```

保存：`Ctrl+O` → 回车 → `Ctrl+X`

### 3.3 构建并启动

```bash
docker compose up -d --build
```

首次构建较慢（5-10 分钟），需要下载 Python、Node、Java 镜像和安装依赖。

### 3.4 查看日志

```bash
docker compose logs -f
```

看到 `Uvicorn running on http://0.0.0.0:8000` 表示启动成功。

### 3.5 访问

浏览器打开 `http://你的服务器IP:8000`

---

## 四、日常维护

### 4.1 更新代码

```bash
cd ~/RAG
git pull
docker compose up -d --build
```

### 4.2 查看状态

```bash
docker compose ps
```

### 4.3 重启服务

```bash
docker compose restart
```

### 4.4 停止服务

```bash
docker compose down
```

### 4.5 查看日志

```bash
# 实时日志
docker compose logs -f

# 最近 50 行
docker compose logs --tail 50
```

---

## 五、踩坑记录

### 5.1 Docker Hub 被墙

**现象：** `docker compose up -d --build` 卡在拉取镜像，超时报错。

**原因：** 国内无法访问 Docker Hub（registry-1.docker.io）。

**解决：** 配置 Docker 镜像源（见 2.3 节）。

### 5.2 apt/pip 下载慢

**现象：** Dockerfile 构建时下载系统依赖和 Python 依赖极慢（2 小时+）。

**原因：** Docker 容器内使用默认的 Debian/PyPI 源，国内访问慢。

**解决：** Dockerfile 中使用阿里云镜像源：

```dockerfile
# apt 源
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources

# pip 源
RUN pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
```

### 5.3 python-multipart 缺失

**现象：** 上传文件返回 500 错误，日志显示 `RuntimeError: Form data requires "python-multipart" to be installed`。

**原因：** FastAPI 文件上传依赖 `python-multipart`，但 `requirements.txt` 遗漏。

**解决：** 添加 `python-multipart>=0.0.18,<1` 到 `requirements.txt`。

### 5.4 容器内监听 127.0.0.1

**现象：** 容器启动正常，但外部无法访问。

**原因：** `start.py` 中 uvicorn 监听 `127.0.0.1`，只接受容器内部连接。

**解决：** 根据环境变量切换监听地址：

```python
host = "0.0.0.0" if os.getenv("DOCKER_CONTAINER") else "127.0.0.1"
```

Dockerfile 中设置 `ENV DOCKER_CONTAINER=1`。

### 5.5 删除文件导致全部向量丢失

**现象：** 删除一个文件后，所有文件的向量都丢失，搜索无结果。

**原因：** `delete_file` 调用 `index_folder()`，该函数先清空整个向量库再重建。如果重建过程中出错，向量就全没了。

**解决：** 改为只删除目标文件的向量：

```python
# 之前：index_folder() → clear() + rebuild
# 现在：delete_doc(collection, doc_name) → 只删目标
```

### 5.6 删除文件后向量残留

**现象：** 文件已删除，但搜索仍能搜到该文件的内容。

**原因：** 原代码先删文件再删向量，如果向量删除失败只打日志不回滚。

**解决：** 改为先删向量再删文件，向量删除失败则文件不删：

```python
try:
    delete_doc(COLLECTION_NAME, safe_name)  # 先删向量
except Exception as e:
    raise HTTPException(500, f"删除向量失败: {e}")
file_path.unlink()  # 向量删除成功后才删文件
```

### 5.7 上传文件报 UNIQUE constraint failed

**现象：** 上传文件返回 500，日志显示 `sqlite3.IntegrityError: UNIQUE constraint failed: document_permissions.doc_name, document_permissions.kb_id`。

**原因：** 重复上传同名文件时，`create_document_permission` 尝试插入重复记录。

**解决：** 上传前先检查是否已有权限记录，有则复用：

```python
existing = user_db.get_document_permission(filename, "rag_docs")
if existing:
    perm_id = existing["id"]
else:
    perm_id = user_db.create_document_permission(...)
```

### 5.8 仓库文件被当成普通文件删除

**现象：** git 仓库中的示例文件（如压测文件）被用户删除，重启后丢失。

**原因：** 仓库文件没有权限记录，被当作旧文档（公开但可删除）。

**解决：** 受保护文件机制：

- 启动时 `_sync_repo_files()` 自动同步仓库文件到服务器
- 创建权限记录 `protected=True, is_public=True, owner_id=0`
- 删除接口拒绝删除 `protected` 文件
- 前端不显示删除按钮

### 5.9 管理员初始化不执行

**现象：** 设置了 `INIT_ADMIN_USERNAME` 环境变量，但用户没有被设为管理员。

**原因：** `_init_admin()` 放在 `if not changed and not deleted:` 的 `return` 之后，无文件变化时跳过。

**解决：** 移到 `return` 之前：

```python
if not changed and not deleted:
    ...
    _init_admin()      # ← 在 return 之前
    _sync_repo_files()
    return
```

### 5.10 GitHub Actions 推送被拒

**现象：** `git push` 报错 `refusing to allow a Personal Access Token to create or update workflow .github/workflows/deploy.yml without workflow scope`。

**原因：** GitHub PAT token 没有 `workflow` 权限。

**解决：** 去 GitHub Settings → Developer settings → Personal access tokens → 编辑 token → 勾选 `workflow` 权限。或者推送时排除 workflow 文件。

---

## 六、数据持久化

Docker volume 挂载确保数据不丢失：

```yaml
volumes:
  - ./data:/app/data          # 用户数据、SQLite 数据库
  - ./qdrant_data:/app/qdrant_data  # 向量数据库
```

**即使容器重建、镜像重新构建，数据都在服务器磁盘上。**

### 6.1 备份

```bash
# 备份数据目录
tar -czf rag-backup-$(date +%Y%m%d).tar.gz ~/RAG/data ~/RAG/qdrant_data

# 恢复
tar -xzf rag-backup-20260626.tar.gz -C ~/
```

### 6.2 清理空间

```bash
# 清理未使用的 Docker 镜像
docker system prune -a

# 查看磁盘使用
df -h
```

---

## 七、环境变量说明

| 变量 | 必填 | 说明 |
|------|------|------|
| `RAG_DEEPSEEK_API_KEY` | ✅ | DeepSeek API Key |
| `RAG_BAILIAN_API_KEY` | ✅ | 硅基流动 API Key |
| `RAG_BAILIAN_BASE_URL` | ✅ | 硅基流动 API 地址 |
| `RAG_BAILIAN_EMBED_MODEL` | ✅ | 嵌入模型名称 |
| `INIT_ADMIN_USERNAME` | ❌ | 首次启动时指定管理员用户名 |
| `RAG_AUTH_ENABLED` | ❌ | 是否启用 API Key 认证（默认 false） |
| `RAG_DOCUMENT_VERSIONS_ROOT` | ❌ | 不可变文档版本存储根目录（默认 `data/document_versions`） |
| `RAG_FEEDBACK_CLUSTER_DAYS` | ❌ | 反馈聚合窗口天数（默认 30） |
| `RAG_FEEDBACK_CLUSTER_USERS` | ❌ | 触发 not_useful 任务所需的不同用户数（默认 3） |
| `RAG_KNOWLEDGE_TASK_DUE_DAYS` | ❌ | 知识任务到期天数（默认 7） |
| `RAG_KNOWLEDGE_STALE_DAYS` | ❌ | 知识库过期判定天数（默认 180） |
| `RAG_HISTORICAL_RESULT_WEIGHT` | ❌ | 历史资料检索权重（默认 0.35） |

---

## 八、常见问题

**Q: 上传文件失败？**
A: 检查文件大小（最大 10MB）和格式（支持 txt/md/pdf/docx/xlsx/csv）。

**Q: 搜索没有结果？**
A: 检查文件是否已索引（启动时自动索引 `data/upload/` 中的文件）。

**Q: 重启后数据丢失？**
A: 检查 Docker volume 是否正确挂载（`docker compose ps` 查看）。

**Q: 构建太慢？**
A: 确认已配置 Docker 镜像源和 apt/pip 镜像源。

**Q: 无法访问？**
A: 检查安全组是否放行 8000 端口。
