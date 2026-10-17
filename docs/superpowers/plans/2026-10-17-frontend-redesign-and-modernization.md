# 前端视觉美化与交互重构实施计划 (Superpowers Execution Plan)

**目标：** 对 RAGv3 前端进行全方位的视觉升级与交互功能重构，打造现代 AI 工作台（Linear/Claude 极简优雅风格），解决多栏挤压问题，支持侧栏折叠沉浸、模型对比胶囊控制、卡片呼吸动效与组件化复用。

**规范文件：** `docs/superpowers/specs/2026-10-17-frontend-redesign-and-modernization-design.md`

## 质量要求与原则
- **零回归保证**：前端现有 230 个 Vitest 单元与组件测试必须全部持续通过。
- **构建保证**：TypeScript 编译 0 报错，`npm run build` 一次性干净通过。
- **响应式支持**：宽屏保持多栏高效展示，窄屏或专注模式支持一键折叠展开。
- **语义与可访问性**：保留所有核心 `data-test` 属性以确保测试桩稳定。

---

## 实施任务清单

### Phase 1: 视觉设计系统升级 (Design Tokens)
1. **[TOKENS-UPGRADE] 现代化设计变量与动效系统** [x]
   - 升级 `frontend/src/styles/design-tokens.css`。
   - 引入 Indigo/Slate 现代品牌色阶、微质感卡片阴影、流式生成呼吸光（Pulse Glow）与磨砂毛玻璃类名。
   - 统一滚动条、Focus Ring 及 Element Plus 全局组件圆角与阴影。

### Phase 2: 应用框架与主导航重塑 (MainLayout)
2. **[LAYOUT-MODERNIZE] 主导航栏与自适应布局重构** [x]
   - 改造 `frontend/src/views/MainLayout.vue`。
   - 替换原生系统 Emoji（📄、📚、🌐、🛠️、♙）为现代清晰的轻量 SVG 矢量图标。
   - 优化用户身份卡片展示与下拉菜单交互，增加侧边栏精致品牌标识与收折支撑。

### Phase 3: 对话侧边栏组件化抽离 (ConversationSidebar)
3. **[CONV-SIDEBAR-IMPL] 封装通用会话侧栏组件** [x]
   - 新建 `frontend/src/components/ConversationSidebar.vue`，集中封装 `FileModeView` 与 `KnowledgeDetailView` 中重复的会话管理代码。
   - 实现对话列表、快速新建、删除确认、创建时间智能格式化。
   - 增加**侧边栏一键收起/展开功能**（折叠后释放 240px 空间，显著扩大阅读和对比视野）。

### Phase 4: 输入控制台与模型选择胶囊重构 (ChatInput)
4. **[INPUT-REDESIGN] 现代化输入悬浮胶囊与模型控制台** [x]
   - 改造 `frontend/src/components/ChatInput.vue`。
   - 重构大模型选择与 Arena 模式切换控件：打造精致的「模型状态胶囊（Model Pill）」，清晰展示当前模型与多选竞技状态。
   - 优化输入框焦点动效（渐变边框光晕）、发送按钮图标微动效与快捷键提示。
   - 增加文件范围（File Scope）清除标签的高级微胶囊样式。

### Phase 5: 消息气泡与 Arena 对比卡片重构 (MessageBubble)
5. **[BUBBLE-REDESIGN] 对话气泡、代码块与 Arena 双栏重塑** [x]
   - 改造 `frontend/src/components/MessageBubble.vue`。
   - 引入精致的 AI 头像呼吸指示灯（生成时具备微弱光晕呼吸动效）。
   - 美化 Arena 多模型对比网格：为不同模型卡片增加品牌主题色彩标签、独立标题栏与边框高亮。
   - 优化操作工具栏（点赞、点踩、重新生成、添加到分析）的 Hover 反馈与微交互。

### Phase 6: 核心工作区与页面视图现代化
6. **[FILE-MODE-REFINE] 文件对话与资源面板重构** [x]
   - 改造 `frontend/src/views/FileModeView.vue`：引入 `ConversationSidebar` 组件，消除冗余代码；
   - 优化文件上传拖拽区（Drop Zone）视觉与文件类型徽章（Pills）。
   - 增加文件列表面板折叠能力，提供真正的双栏/三栏随心切换。
7. **[KB-MODE-REFINE] 知识库与 Wiki 视觉卡片重构** [x]
   - 改造 `frontend/src/views/KBModeView.vue` 与 `frontend/src/views/KnowledgeDetailView.vue`；
   - 优化知识库网格卡片：现代浮动阴影（Hover Lift）、权限角色标签、文档计数微徽章；
   - 引入统一的 `ConversationSidebar`，大幅简化代码体积。
8. **[LOGIN-VIEW-POLISH] 登录页面现代化** [x]
   - 优化 `frontend/src/views/LoginView.vue` 的背景渐变、微光圆球与输入表单卡片质感。

### Phase 7: 质量门禁与端到端验证
9. **[VERIFY-TESTS] 前端测试与生产构建验证** [x]
   - 运行 `npm test -- --run`：确保所有 230 个 Vitest 单元与组件测试 100% 通过。
   - 运行 `npm run build`：验证 Vue SFC 与 TypeScript 类型检查 100% clean。
   - 检查打包后的静态资源并验证与 FastAPI 后端集成。
