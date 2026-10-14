# 原创题生成 Skill（Original Problem Generator）

把经典题改写成**保持知识点与解题方法一致**的原创变形题 / 变式题。一份「识别题目 → 检索同知识点/同方法的相似题 → 生成一致性原创题 → 交付完整包」的教研 SOP，固化为可复用的 Agent 技能。

- **适用范围**：全科（语数英物化生地政史等）、全学段。
- **核心约束**：只换数据/情境/图形/设问，**绝不偷换解题方法的本质**；严防"纯数字替换"式换皮题；多道题必须各取不同母题。
- **交付物**：含原题回显、知识点/方法标签、相似题溯源、原创题（题干+答案+解析+改编说明）的 `.docx`（LaTeX 公式转 Word 原生公式 OMML）或 Markdown 卡片。

---

## 跨平台安装

本仓库遵循开放的 **Agent Skills** 格式（`SKILL.md` + `references/`），以下平台均可使用。

### 1. WorkBuddy（推荐，原生支持）
将整个仓库目录放入用户级技能目录，重启/下一轮对话即自动加载：

```bash
# macOS / Linux
cp -r original-problem-generator-skill ~/.workbuddy/skills/original-problem-generator
# Windows: 复制到 C:\Users\<用户名>\.workbuddy\skills\original-problem-generator\
```

也可在 WorkBuddy 对话中输入「导入技能」并拖入本目录，或从 SkillHub 市场安装。

### 2. 豆包（doubao）
1. 打开豆包 **PC 客户端**，进入「技能 · 连接器 · 伙伴 → 我的技能」。
2. 点击「新建 → 上传技能」，把本仓库目录（`original-problem-generator-skill/`，内含 `SKILL.md`）整个**拖入上传区**（不要只拖单个文件或拖进聊天框）。
3. 填写技能名称与触发词（建议触发词：`原创题` `改编` `变形题` `变式`），保存并启用。
4. 对话中输入触发词 + 需求即可，例如「原创题：出 3 道关于二次函数最值的变式」。

> 豆包读取的是 `SKILL.md` 的 Markdown 正文作为指令，不依赖 frontmatter；因此正文开头的「跨平台说明」与全部 SOP 步骤对豆包同样有效。

### 3. 其他支持 Agent Skills 的平台（Claude Code / Cursor / Codex / Gemini CLI / OpenCode 等）
按各平台各自的"本地技能目录"放置本目录即可。多数平台读取 `SKILL.md` 的 `description` 作为自动触发条件。

---

## 目录结构

```
original-problem-generator-skill/
├── SKILL.md                  # 技能主入口：工作流、硬约束、自检清单
├── references/
│   ├── method_tags.md        # 全科知识点三级标签 + 各学解法标签 + 难度标准
│   ├── search_templates.md   # 相似题检索词模板与三级回退（学科网/菁优网/通用/模型内置）
│   ├── output_template.md    # 完整包输出模板 + 一致性自检清单
│   └── figure_style.md       # 条件图绘制风格规范（全黑、点偏小、背后线虚线、标签贴而不叠）
├── README.md
└── LICENSE                   # MIT
```

---

## 使用方式

对话中自然表达即可触发，例如：

- 「帮我把这道导数题出 3 道原创变式」（可附带题目图片/文件）
- 「改编这道题，保持分类讨论方法不变」
- 「出 5 道不同母题来源的压敏传感器原创题」

技能会自动：识别题目 → 抽取知识点/方法标签 → 检索相似题素材 → 生成 ≥2 类结构性改动的原创题 → 校验（参数链完整、结构同构、公式 LaTeX、配图合规）→ 输出完整包。

---

## 平台特定说明（重要）

`SKILL.md` 与 `references/` 中出现的以下元素是 **以 WorkBuddy 为例** 的实现细节，迁移到其他平台时按需替换：

- `agent-browser` → 你所在平台的浏览器自动化 / 网页检索工具；
- `~/.workbuddy/jyeoo-profile`（菁优网持久化登录 profile）→ 平台对应的本地 profile 或登录态管理；
- `latex2mathml` → `mathml2omml` → `python-docx`（LaTeX 转 Word 原生公式 OMML）→ 平台可用的文档生成方案（如不想转 OMML，直接交付 `.md` 含 LaTeX 也可，但需在教研端用支持 LaTeX 的编辑器查看）。

检索部分采用「学科网/菁优网站内检索 → 通用 WebSearch → 模型内置兜底」三级回退；当站内登录失效时，**如实降级并标注来源**，绝不臆造题目。

---

## 许可

MIT License。详见 `LICENSE`。生成内容须为原创表达，引用原题仅取题干要点与知识点，遵守版权。
