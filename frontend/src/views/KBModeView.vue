<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../utils/api'

interface KnowledgeBase { kb_id:string; name:string; doc_count:number; kb_type:'company_public'|'organization'|'sensitive'|'unassigned'; org_unit:{code:string;name:string}|null; effective_role:'read'|'manage'|'administer'; sensitivity:string|null }
interface OrgUnit { code:string; name:string; kind:string }
const router=useRouter(), knowledgeBases=ref<KnowledgeBase[]>([]), manageableUnits=ref<OrgUnit[]>([])
const loading=ref(false), showCreate=ref(false), newName=ref(''), newOrgCode=ref('')
const sections=computed(()=>[
  {key:'company_public',title:'公司公共库',items:knowledgeBases.value.filter(k=>k.kb_type==='company_public')},
  {key:'organization',title:'组织知识库',items:knowledgeBases.value.filter(k=>k.kb_type==='organization')},
  {key:'sensitive',title:'敏感知识库',items:knowledgeBases.value.filter(k=>k.kb_type==='sensitive')},
].filter(s=>s.items.length))
async function load(){loading.value=true;try{const [k,o]=await Promise.all([api.get('/knowledge-bases'),api.get('/organization/tree').catch(()=>({data:[]}))]);knowledgeBases.value=k.data;manageableUnits.value=o.data}catch{ElMessage.error('加载知识库列表失败')}finally{loading.value=false}}
async function createKB(){if(!newName.value.trim()||!newOrgCode.value)return ElMessage.warning('请填写名称并选择所属组织');await api.post('/knowledge-bases',{name:newName.value.trim(),org_unit_code:newOrgCode.value});showCreate.value=false;newName.value='';newOrgCode.value='';await load()}
async function deleteKB(kb:KnowledgeBase){await ElMessageBox.confirm(`确定删除知识库「${kb.name}」吗？`,'删除知识库',{type:'warning'});await api.delete(`/knowledge-bases/${kb.kb_id}`);await load()}
onMounted(load)
</script>

<template>
  <div class="kb-page">
    <header class="kb-header"><div><h1>知识库</h1><p>按组织权限浏览、维护和检索资料</p></div><el-button v-if="manageableUnits.length" type="primary" data-test="create-kb" @click="showCreate=true">＋ 创建知识库</el-button></header>
    <div v-if="loading" class="empty">正在加载…</div>
    <template v-else-if="sections.length"><section v-for="section in sections" :key="section.key" class="kb-section"><h2>{{section.title}}</h2><div class="kb-grid">
      <article v-for="kb in section.items" :key="kb.kb_id" class="kb-card" @click="router.push(`/kb/${kb.kb_id}`)"><div class="kb-card-title"><span>▣</span><strong>{{kb.name}}</strong></div><p>{{kb.org_unit?.name||'待归属'}}</p><div class="kb-card-meta"><span>{{kb.doc_count}} 个文档</span><span class="role">{{kb.effective_role==='read'?'可查看':kb.effective_role==='manage'?'可管理':'可管理全部'}}</span></div><button v-if="kb.effective_role==='administer'" class="delete" type="button" @click.stop="deleteKB(kb)">删除</button></article>
    </div></section></template>
    <div v-else class="empty"><span>▣</span><h2>暂无可访问的知识库</h2><p>请联系组织负责人分配权限</p></div>
    <el-dialog v-model="showCreate" title="创建组织知识库" width="460px"><el-input v-model="newName" placeholder="知识库名称"/><el-select v-model="newOrgCode" placeholder="选择所属组织" style="width:100%;margin-top:12px"><el-option v-for="unit in manageableUnits" :key="unit.code" :label="unit.name" :value="unit.code"/></el-select><template #footer><el-button @click="showCreate=false">取消</el-button><el-button type="primary" @click="createKB">创建</el-button></template></el-dialog>
  </div>
</template>

<style scoped>
.kb-page{padding:24px}.kb-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:24px}.kb-header h1{margin:0;font-size:28px}.kb-header p{color:var(--color-secondary);margin:6px 0}.kb-section{margin-bottom:28px}.kb-section h2{font-size:17px;margin:0 0 12px}.kb-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px}.kb-card{position:relative;padding:18px;border:1px solid var(--color-border);border-radius:14px;background:var(--color-surface);cursor:pointer;box-shadow:var(--shadow-xs)}.kb-card:hover{border-color:var(--color-primary)}.kb-card-title{display:flex;gap:10px;font-size:17px}.kb-card p{color:var(--color-secondary);font-size:13px}.kb-card-meta{display:flex;justify-content:space-between;font-size:13px}.role{color:var(--color-primary)}.delete{position:absolute;right:12px;top:12px;border:0;background:none;color:var(--color-danger);cursor:pointer}.empty{text-align:center;padding:100px 20px;color:var(--color-secondary)}
</style>
