# 双端 GitHub 开发与发布设计

日期：2026-08-14

## 目标

将新建的私有仓库 `https://github.com/hhg00uaecv/rag.git` 作为公司电脑和家里电脑之间唯一的代码真相源。任意一端完成并推送的代码，另一端首次克隆或拉取后得到相同的最新 `main`。

## 同步边界

GitHub 同步以下内容：

- 后端与前端源码；
- 数据库迁移、自动化测试和构建资源；
- 依赖清单与锁定文件；
- 设计、计划、部署和使用文档；
- `.env.example` 配置模板。

以下内容不进入 GitHub：

- 包含真实 API Key 的 `.env`；
- SQLite 数据库、向量索引和缓存；
- 用户上传文档及文档版本文件；
- Python/Node 依赖目录和测试临时数据。

真实 `.env` 由用户手动复制到两台电脑。

## 远程与分支

- 删除已失效的旧 `origin`，不保留旧地址；
- 新仓库设置为唯一的 `origin`；
- 本地 `main` 快进到已完整验证的最新稳定提交；
- 新仓库只发布稳定 `main`，不上传临时功能分支和本地 stash；
- 禁止对 `main` 使用强制推送。

## 代理托管的双端日常流程

正常开发由代理完成 Git 同步闭环，用户不需要输入 Git 命令。

首次在另一台电脑工作：

```bash
git clone https://github.com/hhg00uaecv/rag.git
cd rag
copy .env.example .env
```

代理开始修改代码前：

```bash
git switch main
git pull --rebase origin main
```

代理完成一个可独立验收的任务后：

```bash
git status
python -m pytest -q
cd frontend && npm test -- --maxWorkers=1 && npm run build
git add -A
git commit -m "说明本次修改"
git pull --rebase origin main
git push origin main
```

代理在推送后核对远端 `main` 的提交哈希，并向用户报告测试结果、提交哈希和推送状态。公司或家里另一端启动编码任务时，代理会先同步 `origin/main`，因此能够取得上一端已经推送的最新代码。

只读分析、答疑和没有产生文件修改的任务不创建空提交，也不执行无意义推送。尚未通过验证的代码、进行到一半的工作和本地运行数据不会推送。

另一端只有在上一端已经提交并推送后才能取得新代码。未提交的工作区内容不会通过 GitHub 自动同步。

## 冲突与失败处理

- `git pull --rebase` 发现本地未提交修改时，先提交当前工作或停止拉取，不丢弃文件；
- 两端同时修改同一位置时由 Git 报告冲突，人工选择正确内容后继续，不使用 `reset --hard` 或强制推送处理；
- 推送被拒绝时先拉取远端并解决差异，再重新推送；
- GitHub 登录或浏览器授权缺失时，由代理发起登录流程，用户只负责在本机浏览器确认，不向代理发送密码或 Token；
- 冲突无法安全自动判断、测试失败或远端出现未知提交时，代理停止推送并向用户说明，不猜测覆盖；
- 每次发布前要求工作区干净，并运行后端测试、前端测试与生产构建。

## 首次发布验收

1. 新远程 `origin` 指向 `https://github.com/hhg00uaecv/rag.git`；
2. 远端 `main` 指向本地最新稳定提交；
3. 从远端读取到的 `main` 提交哈希与本地一致；
4. GitHub 中不存在 `.env`、运行数据库和上传文档；
5. README 明确记录首次安装、代理托管同步、测试和启动命令；
6. 仓库级代理说明明确要求代码任务完成后自动测试、提交、同步并推送。
