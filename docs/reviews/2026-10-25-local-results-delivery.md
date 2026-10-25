# 本地新增成果集成与双推（2026-10-25）

状态：用户已授权合并main并按交接四命令推送，完整集成验收通过，合并/推送待实际核对。候选ef758db，基线D main/Gitee/GitHub均1d413ed；候选含31个本地成果提交及本轮交付计划。

## 集成范围

KB清理目标版本快照/目录索引、401/403会话与Compose契约、历史暂存受控恢复与v6回执、容量/长期架构设计。四专项已独立验收，Luna只读交付审计确认所有冻结点属于候选祖先，报告/闭环清单/摘要与代码对应。容量与架构只代表已注明的组件诊断和设计；业务SLO、PG/对象存储/共享fencing/多实例实现、业务恢复/迁移不在本次运行切换范围。

全量后端在C无.env工作树执行，offline/BelowNormal/BLAS1、新短Temp，因宿主可用约1.4GiB采用单worker。前端使用私有Node22与C依赖镜像，随后串行运行，不使用指向D的原frontend/node_modules、不重建C/D原static。依赖锁不变，前端git tree与此前330通过版本相等；本轮实际完整后端3341通过/5跳过、1397.38秒、JUnit3346项0失败0错误；前端41文件330项通过、172.43秒，vue-tsc/Vite构建退出0，Ruff通过，前端source hash前后保持。

## 保护与证据

20:24:55原服务PID48512/start2026-10-23T14:09:17保持，uvicorn无reload，ready200、SQLite/Qdrant local OK、Redis optional degraded；D.env哈希保持，D tracked clean、无有效hook。合并前后再核对，源码交付不等于部署新实例或执行v6业务库迁移。

四专项控制/验证原件314个、1,583,602字节已复制到D artifacts/local-results-delivery-2026-10-25/accepted-evidence，逐文件SHA256一致；未复制.env、依赖junction、业务DB或合成fixture DB。C原始环境、全部artifact与SDD保留。本轮集成/推送/回执日志在同名C artifact/verification，最终证据随交付另归档。

## 流程与接续

完整后端、私有前端测试/构建、Ruff通过后，再fetch两端，D main快进至受测源码等价候选；C同源码烟测后按origin main、github main、origin --tags、github --tags依次推，不force。实际heads/tags及服务核对是完成依据，不预填成功。

公司timer T-CD7DA0E4EFE2待实际submit。交付完成后，按用户顺序另写材料定位优化设计/计划，在新codex分支并行Luna；当前只读勘查不修改产品。拟优先减少未授权元数据载入和默认路径重复映射，保持完整model_input、语义允许集合、组织/legacy/admin权限、custom callback、FTS与结果顺序；不用跨请求缓存或截断伪造收益。
