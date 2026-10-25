# 暂存与归档快照只读诊断

此工具用于检查材料上传失败、未完成版本、归档暂存与文件归属关系。它只读取显式指定的SQLite快照和版本目录元数据，输出JSON；不会读取材料正文、移动文件、改数据库或执行清理。

## 输入准备

使用已授权取得的、自包含的SQLite快照和与之对应的稳定版本目录副本。数据库旁不能存在`-wal`或`-shm`；应通过正常SQLite backup/已验证备份流程取得快照，不要为通过检查而删除live sidecar。

现有备份默认排除`staging`和`archive_work`。若诊断失败上传暂存字节，需要在维护流程中另行保存这些目录的稳定副本；单独使用普通备份的文件payload并不证明暂存文件已丢失。此工具不负责创建跨数据库/文件系统的原子快照，也没有在运行中的业务数据上自动取样的能力。

工具也不会重定位快照内的绝对路径。若数据库里的绝对路径与传入的副本根目录不匹配，会报告root外引用并停止检查该路径；不会转向原业务目录查找文件。副本内路径与目录的对应关系须由已验证的快照/恢复流程保证，不能仅复制数据库后就把这类报告解释为文件丢失。

Python需符合项目的3.12及以上要求。下面路径是操作者准备的快照示例，不是业务目录默认值：

```powershell
python scripts/audit_staging.py --database C:\Temp\snapshot\users.db --versions-root C:\Temp\snapshot\document_versions
```

`--database`和`--versions-root`都必须显式提供。脚本不加载`config`、`.env`、UserDB或FastAPI应用；即使从别的工作目录执行，也优先使用脚本所属仓库的代码。没有`--apply`、删除、rehome或重试参数。

## 输出与退出码

正常执行或输入错误返回JSON；标准`--help`显示命令行帮助。

```json
{
  "schema_version": 1,
  "action": "inspect_only",
  "entries": [],
  "findings": [],
  "counts": {
    "entries": 0,
    "findings": 0,
    "by_kind": {},
    "by_code": {}
  }
}
```

退出码0表示报告已生成，包括报告里存在findings的情况；退出码2表示参数、快照、schema或输入根目录不可用，报告中带`error.code`。它不是“文件可清理”的成功/失败门禁。所有entry/finding的action均为`inspect_only`。

## 常见报告与相关删除错误码

| code | 含义与处理边界 |
| --- | --- |
| `snapshot_sidecar_present` | 输入可能仍依赖WAL/SHM，先取得自包含一致快照。 |
| `unsupported_schema` | 核心/现存可选表缺少消费字段、关键row metadata类型不兼容，或路径metadata包含NUL；工具返回JSON错误，不猜测或重写schema。 |
| `material_job_pending`、`pending_archive_manifest`、`pending_kb_document_reference` | 有在途引用，应保留上下文；不是无主垃圾。 |
| `staging_version_missing_material_job` | 只表明该版本缺少material job，需结合其它归档/permission/目录引用判断；不是自动删除许可。 |
| `version_file_missing` | 声明路径的文件不存在；可能是补偿/崩溃断点或快照未包含暂存内容。 |
| `owned_version_candidate` | 对确认缺失的root内staging声明，在其document/version目录下发现regular文件，包含默认rag_docs的global stage双重补偿失败情况；未比对内容hash，不猜原文件名，不自动修正file_path。root外声明和链接不触发候选扫描。 |
| `unregistered_stage_file` | 暂存文件没有匹配的已知引用；文件名不足以证明原owner，保留供人工核查。 |
| `legacy_committed_stage_preserve` | 旧current/historical版本仍引用global staging；只是旧布局事实，工具不迁移或删除。 |
| `version_path_unowned`、`version_path_shared`、`version_shared_reference` | 路径的owner或共享引用存在冲突；应先核对相关KB/document/version身份。 |
| `archive_container_version_mismatch`、`archive_source_unowned` | manifest、container/version或archive源归属不一致/不确定，保留原文件和身份。 |
| `cleanup_path_invalid` | KB删除计划中的预期文件/目录类型错误；应在任何manager清理前修正元数据或恢复快照，CLI不替代执行。 |
| `unsafe_link`及相关unsafe代码 | 检查遇到symlink/junction等不安全路径，不跟随目标。 |

报告只做元数据与目录检查，不证明某个candidate就是原始上传字节；文件正文、内容hash、权限决定和业务恢复仍需独立核验。

## 与上传及KB删除的关系

新的上传交接保证请求取消不会先删除worker仍在使用的stage。完成交接后，即使HTTP等待取消，后台worker也可能继续完成提交；取消请求不等于服务器事务撤销。版本路径与job最终提交共用短事务；可补偿失败保留上传字节，补偿无法完成时保留version/permission身份。已有HTTP返回契约保持，工具也不会替用户重新上传或建立新owner记录。

KB删除遇到不明archive源、错owner、共享引用或错误文件类型时先返回503并保留身份。存在的manifest source必须仍在该KB的hashed staging；container version自己可以已在final目录。不能直接把错误码当作授权去删路径或手工改指针。旧global archive的显式rehome/重试写流程尚未由此CLI实现。

本轮所有工具验证均使用合成Temp快照；业务目录、真实归档和运行服务不参与。
