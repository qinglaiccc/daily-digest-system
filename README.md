# 每日早报自动化系统

每天早上 7:30（Asia/Shanghai）由外部定时器触发，自动抓取科技/消费新闻与 GitHub 热门项目，调用 DeepSeek 做客观精简总结，渲染成响应式网页发布到 GitHub Pages、导出一份带双链的 Obsidian 笔记，并推送 HTML 邮件。

```
┌─────────────┐   ┌──────────────┐   ┌──────────────────┐   ┌──────────────┐
│ 1. 数据采集  │ → │ 2. AI 提炼    │ → │ 3. 渲染          │ → │ 4. 通知/部署  │
│ 24 个 RSS   │   │ DeepSeek     │   │ 瑞士极简网页      │   │ SMTP 邮件 +   │
│ GitHub API  │   │ 五大板块      │   │ + Obsidian 双链笔记│   │ gh-pages     │
└─────────────┘   └──────────────┘   └──────────────────┘   └──────────────┘
```

## 网页功能

| 功能 | 说明 |
| --- | --- |
| **往期存档** | 每天一份存档提交回仓库，页面**左侧边栏**列出全部期次（当前期反黑高亮），底部有上/下期翻页，另有独立索引页 |
| **板块 Tab 切换** | 五个板块横向 Tab 栏，点击 / 方向键 / `#锚点` 三种方式切换，选中状态记住 |
| **GitHub 深度解析** | 开源项目由 DeepSeek 结合 README 原文做通俗解析，输出「简介 / 亮点 / 部署指南 / 一键命令」四段式 |
| **中英对照** | 顶栏三档切换：**中文 / 原文 / 双语**。选择记在 localStorage，刷新和跳转往期页都不会丢 |
| **阅读全文** | 每条新闻都有直达原文的「阅读全文」链接，标题本身也可点击 |
| **响应式** | 375 / 900 / 1280 / 1680 四档断点均已实测，无横向溢出；窄屏侧栏自动变顶部横条 |
| **降级可读** | AI 不可用时页面照常生成，并在顶部说明原因 |

**关于「原文」模式**：原文标题与摘录直接取自 RSS / GitHub 的原始素材，不经过模型改写，所以不存在被编造或翻译失真的风险。中文模式下这些字段隐藏，切到「双语」时以浅灰小字附在中文标题下方，切到「原文」时升为标题级字号。

## Obsidian 知识库导出

同一份数据除了发布网页，还会额外导出一份带 **YAML 前置区**与**实体双链**的 Markdown
到仓库根目录的 `obsidian/YYYY-MM-DD.md`，可以直接把这个目录当作 Obsidian 库打开（或放进已有库）。

### 长什么样

```markdown
---
title: 每日早报-2026-09-17
date: 2026-09-17
type: 早报
tags:
  - 早报
  - AI
  - 华为
  - 昇腾960
  - Manus
items: 30
news: 25
repos: 5
sources: 9
---

> 华为昇腾960提前登场，美联储三年多来首次加息，Manus估值翻倍至40亿美元。

# 国际科技新闻

**Meta深度伪造规则被指根本不足**

监督委员会称[[Meta]]对AI深度伪造内容的规则"一贯且根本不足"，要求优先处理相关举报。 — [Engadget](https://...)

# GitHub 热门效率与 AI 工具

**openclaw/openclaw**

★389.9k · TypeScript · 近期趋势

装在自己电脑上的 AI 助手，能在微信、Slack 等你常用的聊天软件里直接使唤它。

**核心亮点**

- 在 Discord、Telegram 等聊天里对话
- 数据、记忆和密钥都存在自己电脑

**应用与部署指南**

在 macOS、Linux 或 Windows 上跑一条安装命令即可。

- [ ] 打开终端
- [ ] 运行 curl -fsSL https://openclaw.ai/install.sh | bash
- [ ] 按提示完成配置

```bash
curl -fsSL https://openclaw.ai/install.sh | bash
```

[查看仓库](https://github.com/openclaw/openclaw)
```

### 前置区字段

`title` / `date` / `tags` 是必须的三项。另加 `type` 与四个计数，是为了 Dataview 之类的
数据库插件好用，例如：

```dataview
TABLE items, repos, sources
FROM "obsidian"
WHERE type = "早报"
SORT date DESC
```

不需要这些字段的话，删掉 `obsidian.py` 里 `build_frontmatter` 对应的几行即可，不影响正文。

### 双链是怎么来的

模型在写 summary 时，会把**有持续追踪价值的具名实体**（公司、产品、模型、技术名词）用
`[[双方括号]]` 包起来，每天的早报于是自然形成一个实体图谱 —— 点开 `[[华为]]` 就能看到
它在历史上出现过的每一期。提示词里有几条硬约束：每条最多 3 个、只标具体实体不标泛化词
（不标"人工智能""手机"）、同一实体只标首次出现、只出现在 summary 里不进标题。

**双链只存在于 Obsidian 笔记里。** 网页、邮件、`history.json` 都会把 `[[Anthropic]]`
还原成 `Anthropic`（见 `obsidian.strip_wikilinks`）—— 方括号对非 Obsidian 读者只是噪音。

### 待办清单

GitHub 板块的「应用与部署指南」会渲染成 `- [ ]` 待办清单，在 Obsidian 里可以直接打勾。

清单语法由程序生成而不是让模型写：模型写清单时常飘成 `* [ ]`、`1. [ ]`，
甚至把 `- [ ]` 塞进正文，渲染时再加一次前缀就变成 `- [ ] - [ ] xxx`。
所以提示词明确要求**不要自带前缀**，`summarizer._coerce_str_list` 里也做了一道兜底清洗。

### 几个实现上的坑

| 坑 | 后果 | 处理 |
| --- | --- | --- |
| 摘要按字符数截断 | 会把双链拦腰切断，留下 `[[Anthrop…` | `repair_wikilinks` 专门清理不闭合的 `[[` |
| 关键词含 `:` `[` `]` `#` 或换行 | 直接把 YAML 前置区解析搞崩，整篇属性全丢 | `sanitize_tag` 把这些字符全部归一成 `-` |
| 关键词含空格 | Obsidian 标签不允许空格 | 归一成连字符（`machine learning` → `machine-learning`） |
| 关键词是纯数字 | Obsidian 拒绝纯数字标签 | 直接丢弃 |
| 存档日期字段被污染 | 可能写到 `obsidian/` 目录外面去 | `markdown_path` 用正则校验日期，不合法直接拒绝 |
| 模板里几十处文本输出 | 漏掉任何一处都会让 `[[` 泄漏到网页上 | 用 Jinja 的 `finalize` 钩子统一处理，而非逐处加过滤器 |

### 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OBSIDIAN_FIXED_TAGS` | `早报,AI` | 每篇都带的固定标签，逗号分隔 |
| `OBSIDIAN_MAX_KEYWORDS` | `5` | 模型提取的关键词最多取几个（固定标签不计入） |

`obsidian/` 与 `archive/` 一样会由 Actions 提交回仓库，历史笔记长期累积。
每期都会重新导出（不只当天），所以双链或排版规则升级后，往期笔记会跟着一起更新。

## GitHub 板块：3 + 2 混合推荐

这个板块不是"抓一堆候选让 AI 挑"，而是**每天先定好 5 个项目**，AI 只负责把这 5 个讲清楚。

| 来源 | 条数 | 查询策略 |
| --- | --- | --- |
| **本周新建** | 1 | `created:>=<7天前>` — 最近一周内创建、已经有人气的新项目 |
| **本月新秀** | 1 | `created:<30天前>..<7天前>` — 这个月冒出来的，稍成熟一些 |
| **老将新动作** | 1 | `pushed:>=<7天前>` + `created:<30天前>` + 星数区间 — 中等规模但最近活跃 |
| **历史经典** | 2 | `topic:* stars:>=8000` 按总星数排名，**游标轮播**推进 |

三个"趋势"来源各占一个名额（配额轮转），保证每天既有"刚出生的"、也有"这个月的"、还有"老牌的"。
某个来源当天没货时用其余来源补齐，全都没货才回退用近期推荐过的，并记警告。

### 游标轮播怎么持久化

`archive/github_offset.json` 记录三样东西：

```json
{
  "classic_offset": 2,                            // 经典池推到第几名了，每次 +2
  "classic_cycle": 0,                             // 已完成几圈
  "classic_pool": ["openclaw/openclaw", "..."],  // 持久化的有序池（130 个左右）
  "recent_trending": ["Chuloo/mural", "..."]     // 最近推过的趋势项目，用于次日去重
}
```

因为这个文件在 `archive/` 下，会被工作流的「保存往期存档到仓库」步骤一起提交，
**所以状态天然持久化，不需要任何外部存储 —— git 就是我们的持久层。**

**池子必须持久化排序，不能每天重排。** 这是实测踩出来的：星数每天都在变，
每次运行重新按星数排序会让排名漂移，今天排第 3 的明天可能变第 5，
游标指向的仓库前后对不上，就会出现重复推荐。所以池子在首次构建或推完一圈后重建
（最长复用 14 天），中间所有运行复用同一份顺序，推送当天再按名字取一次最新元数据。

### 选品时踩过的三个坑

写这段逻辑时踩了三个不写下来一定会重犯的坑：

**1. `created:` 限定符不能写两个。**
```javascript
created:>=2026-08-17 created:<2026-09-09   // ✗ 两个限定符被静默忽略，返回 2013 年的仓库
created:2026-08-17..2026-09-09             // ✓ 区间语法才生效
created:2026-08-17 .. 2026-09-09           // ✗ 带空格返回 0 条
```
最坑的是它**不报错**，只是悄悄返回一批完全不符合条件的仓库。

**2. `pushed:>=<7天>` 单独用会返回永久霸榜的常驻民。**
`freeCodeCamp`（45 万星）、`public-apis`（48 万星）这类仓库每天都在推送，
所以"最近 7 天有推送"对它们永远成立。第一版实测出来趋势桶里全是这些，
和"每天新鲜血液"完全相反。加星数上限（`stars:2000..40000`）后才恢复正常。

**3. 有些 topic 名根本不存在。**
配置里原本有个 `tools-for-developers`，实测在 GitHub 上**零仓库使用**，
`topic:tools-for-developers stars:>=8000` 稳定返回 0 条，一直在静默缩小候选池。
已换成确实有内容的 `developer-tools`。**新增 topic 后务必先验证**：
`topic:<名字> stars:>=8000` 能不能查出东西。

**4. 玩票/抗议型仓库会挤进趋势桶。**
实测混进来过一个 `ai-sucks-butt/ai-sucks-butt`（README 写"觉得 AI 不行就点个星"），
它甚至有 Python 语言标记，按语言拦不住；但它是**零 topic** 的，
而同期 6 个真工具（threeui / next-ai-draw-io / ECC / hermes-agent / mural / m3e-canvas）全都有 topic。
所以加了「至少一个 topic」的要求 —— 认真做的项目基本都会给自己打标签。
觉得误伤太多可以设 `GITHUB_REQUIRE_TOPICS=false` 关掉。

另外，精选清单（`awesome-*`）、教程课程（`*for-beginners`、`*-course`）、
面试题库这类仓库星数极高、topics 又常打 `ai`/`productivity`，
不拦会把整个板块占满。`config.py` 里有 29 条按仓库名的正则排除规则。

### README 深度解析

定好 5 个项目后，逐个抓 README 原文（`GET /repos/{owner}/{repo}/readme`，用 `Accept: application/vnd.github.raw` 直接拿原文），清洗掉徽章图片、内联 HTML、代码围栏、目录链接等噪声后取前 1500 字符。走的是 **core 配额（5000/小时）**，与 Search 的 30 次/分钟是两套独立配额，互不挤占。

然后**独立发起一次 DeepSeek 调用**（不与新闻混在一起），强制四段式输出：

| 字段 | 要求 |
| --- | --- |
| **项目通俗简介** | 一句话说清"它到底是个啥 + 解决什么痛点"，40 字以内，外行能懂 |
| **核心功能亮点** | 2–4 条，每条 20 字以内，说"能做什么"而不是"用了什么技术" |
| **应用与部署指南** | 80 字以内，说清前置条件、怎么跑起来、在哪访问 |
| **一键安装** | 单行命令，**必须逐字来自 README**，README 里没有就留空 |

提示词里明确要求「说人话」并给了正反例：

> 反例：「基于 WASM 的边缘计算框架」　正例：「让网页跑得跟本地软件一样快的工具」

**星数与编程语言一律取采集到的真实值**，不采用模型填写的值。

实际产出：

```
[1] Chuloo/mural      对着一个会说话的小圆球聊天来学外语的手机应用，学习记录只存在你自己手机里
[2] lnkiai/m3e-canvas 在浏览器里拖拖拽拽画出手机界面草图，再一键变成给 AI 写代码用的提示词
[3] ShareX/ShareX     Windows 上按一个键就能截屏、录屏、传文件并自动生成分享链接的免费工具
[4] openclaw/openclaw 装在自己电脑上的 AI 助手，能在微信、Slack 等你常用的聊天软件里直接使唤它
[5] obra/superpowers  给 AI 编程助手立规矩的一套方法，让它先问清需求再动手写代码
```

侧记：改成"先定 5 个"之后，GitHub 那次调用的提示词从 **46,469 字符降到 11,026**（-76%），更快也更省。


普通的「一句话摘要」读不出开源项目到底能干什么，所以 GitHub 板块走**完全独立的一条链路**：

1. **抓 README 原文**：对每个候选仓库调用 `GET /repos/{owner}/{repo}/readme`（`Accept: application/vnd.github.raw` 直接拿原文），清洗掉徽章图片、内联 HTML、代码围栏、目录链接等噪声后，取前 1500 字符。
   走的是 **core 配额（5000/小时）**，与 Search API 的 30 次/分钟是两套独立配额，互不挤占。取不到 README 时自动退回只依赖仓库描述，不会中断采集。
2. **独立提示词**：GitHub 素材和新闻素材形态完全不同，硬塞进一次调用会让提示词互相妥协。现在拆成**两次独立的 DeepSeek 调用**，各自有独立的上下文预算（`GITHUB_MAX_INPUT_CHARS` / 新闻侧 60% × `LLM_MAX_INPUT_CHARS`）。
3. **强制四段式输出**，页面上以带边框的字段标签逐个渲染：

| 字段 | 要求 |
| --- | --- |
| **项目通俗简介** | 一句话说清「它到底是个啥 + 解决什么痛点」，40 字以内，外行能懂 |
| **核心功能亮点** | 2–4 条，每条 20 字以内，说「能做什么」而不是「用了什么技术」 |
| **应用与部署指南** | 80 字以内，说清前置条件（要不要装 Docker/Node）、怎么跑起来、在哪访问 |
| **一键安装** | 单行命令，**必须逐字来自 README**，README 里没有就留空 |

提示词里明确要求「说人话」，并给了正反例：

> 反例：「基于 WASM 的边缘计算框架」　正例：「让网页跑得跟本地软件一样快的工具」

**命令不许编造**是硬约束 —— 普通读者会照着敲，编造的命令会浪费他们的时间。README 里没有明确命令时 `install` 字段必须留空字符串。

实际产出示例：

```
openclaw/openclaw                     389.7k stars · TypeScript
├ 项目通俗简介  装在自己电脑上的 AI 助手，能在你常用的聊天软件里直接对话
├ 核心功能亮点  · 支持 Discord、微信类、Slack 等 20 多个聊天软件
│               · 数据存在自己设备上，不上传云端
│               · 可自由更换背后的 AI 模型
├ 应用与部署指南 在 macOS、Linux 或 Windows 上运行安装脚本即可，会自动装好运行环境
└ 一键安装      $ curl -fsSL https://openclaw.ai/install.sh | bash
```

另外，**星数与编程语言一律取采集到的真实值**，不采用模型填写的值 —— 这是零成本就能拿准的字段，没必要让模型去猜。

新闻与 GitHub 的降级是**分开的**：GitHub 那条链路挂了只会让该板块退回原始描述，新闻板块不受影响，反之亦然。

## UI 风格

页面采用 **复古纸质 + 硬核边框的 Dashboard 布局**。这套视觉是从本地 skill `ui-style-brutalist` 与 `ui-style-lofi-print` 中提取元素后**刻意融合**的定制方案（两个 skill 本身都声明「混合即是品类错误」，这里是按明确的设计规格做的取舍，不是任一 skill 的纯正应用）。

**色彩**

| 变量 | 取值 | 用途 |
| --- | --- | --- |
| `--paper` | `#F4F1EA` | 纸质米色底板，**全站不出现纯白背景** |
| `--paper-alt` / `--paper-deep` | `#EBE6DA` / `#E3DDD0` | 次级区块与悬停态 |
| `--ink` | `#111111` | 正文与**所有边框** |
| `--muted` | `#5E5849` | 暖灰辅助文字 |
| `--accent` | `#8C2F1F` | 暗红主点缀 |
| `--accent-2` | `#2F5D3A` | 深绿次点缀 |

**硬核边框**：`* { border-radius: 0 !important }`、零 `box-shadow`、所有分隔与卡片边框一律 `1px/2px solid #111111`。

**排版**：大号粗体无衬线标题（30–58px 随断点变化）+ 等宽体承担数字、标签、命令（档案卡片感），正文走无衬线。

**布局**

- 左侧固定宽度 Sidebar（268px，吸顶满高），放往期存档索引，**当前期反黑高亮**（`aria-current="page"`）
- 右侧主内容：特大标题 + 日期 → 6 格 KPI 看板（科技新闻 / 消费新闻 / 开源项目 / 媒体来源 / RSS 源 / 本期收录）→ 五个板块的 Tab 栏 → 内容面板

**Tab 切换用原生 JS，零外部依赖，且做了渐进增强**

- 有 JS：只显示当前面板，Tab 带完整 `role="tablist"/"tab"/"tabpanel"` 语义，支持方向键 / Home / End 键盘导航
- 无 JS：`html.js-on` 这个类由 JS 自己加上，所以脚本没跑起来时**五个板块会顺序全部展开**，内容永远可读
- 支持 `#github` 这类锚点直达，`hashchange` 时同步切换
- 选中板块记在 localStorage

**响应式**：≥1000px 双栏；<1000px 侧栏自动变成顶部横向可滚动条（同一份 DOM，只换排布方式），往期条目横向排布。375 / 900 / 1280 / 1680 四档均已实测，无横向溢出。

改风格只需替换 `template.html` 与 `template_index.html` 里的 CSS 变量块。

## 目录结构

```
.
├── main.py                      # 主流程（采集 → 提炼 → 渲染 → 推送）
├── config.py                    # 配置中心：信息源、板块、密钥、超时、.env 加载
├── fetcher.py                   # 数据采集：RSS（并发）+ GitHub Search API
├── summarizer.py                # DeepSeek 提炼、提示词、防幻觉校验、降级
├── renderer.py                  # 渲染今日页 + 全部往期页 + 往期索引 + history.json
├── obsidian.py                  # Obsidian 导出：YAML 前置区、实体双链、待办清单
├── notifier.py                  # 邮件推送（标准库 smtplib + email.mime）
├── template.html                # 早报正文模板（Swiss 风格，自包含 CSS）
├── template_index.html          # 往期索引页模板
├── selftest.py                  # 离线自检（266 项，无需密钥/联网）
├── requirements.txt
├── .env                         # 本地密钥（已 gitignore，不会提交）
├── archive/                     # 往期存档 JSON —— 必须提交进仓库，是历史记录的数据源
│   └── YYYY-MM-DD.json
├── obsidian/                    # Obsidian 笔记 —— 同样提交进仓库，长期累积
│   └── YYYY-MM-DD.md
├── .github/workflows/daily_report.yml
└── dist/                        # 生成产物（已 gitignore，仅 gh-pages 分支发布）
    ├── index.html               # 今日
    ├── history.json             # 往期清单
    └── archive/                 # 往期页面 + 索引页
```

## 快速开始

### 第一步：本地试跑

```bash
pip install -r requirements.txt

# 配置密钥：复制下面内容到项目根目录的 .env（已被 gitignore，不会提交）
#   DEEPSEEK_API_KEY=sk-xxxxxxxx
#   SMTP_USER=you@qq.com
#   SMTP_PASS=你的QQ邮箱授权码
#   RECEIVER_EMAIL=you@qq.com

# 离线自检：验证渲染、双语、往期存档、链接校验等纯逻辑
python selftest.py

# 不带密钥也能跑通全链路（跳过 AI 与推送，用原始素材渲染页面）
python main.py --dry-run

# 本地预览，手机连同一 Wi-Fi 可看响应式效果
python main.py --serve
```

`--dry-run` 会在 `dist/index.html` 生成一份用原始素材拼装的页面，用来确认采集与渲染链路正常。`python main.py` 是完整流程，会真实调用 DeepSeek。

`config.py` 内置了一个二十来行的 `.env` 解析器（不依赖 python-dotenv）。**已有的环境变量优先级高于 `.env`**，所以 GitHub Actions 上的 Secrets 会覆盖本地配置。

### 第二步：配置 GitHub Secrets

进入仓库 **Settings → Secrets and variables → Actions**，添加以下 **Repository secrets**：

| Secret 名称 | 是否必填 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 必填 | DeepSeek API Key，格式形如 `sk-xxxxxxxx` |
| `SMTP_USER` | 必填 | 发件邮箱（QQ 邮箱填完整地址） |
| `SMTP_PASS` | 必填 | **SMTP 授权码，不是登录密码** |
| `RECEIVER_EMAIL` | 必填 | 收件邮箱（可与 SMTP_USER 相同） |

`GITHUB_TOKEN` **不需要手动配置**，GitHub Actions 会自动注入。它有两个用途：提高 GitHub Search API 的调用配额（10/分钟 → 30/分钟），以及把往期存档提交回仓库。

> ⚠️ 你**无法**手动创建名为 `GITHUB_TOKEN` 的 Repository secret —— GitHub 保留了这个名字，添加时会报 `This name is reserved`。这是预期行为，不是配置错误。

**还有一步不能漏**：进入 **Settings → Actions → General → Workflow permissions**，选择 **Read and write permissions** 并保存。否则自动注入的 `GITHUB_TOKEN` 只有只读权限，会导致「保存往期存档到仓库」和「部署到 gh-pages」两个步骤失败（报 `403 Resource not accessible by integration`）。这个设置是**历史存档回写的必需前提**。

在同一个页面切到 **Variables** 标签，可选添加：

| Variable 名称 | 默认值 | 说明 |
| --- | --- | --- |
| `SITE_URL` | 自动推导 | 自定义站点地址；留空则自动推导为 `https://<用户名>.github.io/<仓库名>/` |
| `SMTP_HOST` | `smtp.qq.com` | SMTP 服务器（也可用仓库变量覆盖） |
| `SMTP_PORT` | `465` | 465 走 SSL，587 走 STARTTLS |

### 第三步：开启 GitHub Pages

1. 先把代码推到 GitHub 仓库。
2. 进入 **Actions** 页面，选择「每日早报」工作流，点 **Run workflow**，勾选 `dry_run` 先做一次空跑验证。
3. 确认空跑成功后，再点一次 **Run workflow**（不勾 dry-run）做正式运行 —— 这一步会自动创建 `gh-pages` 分支。
4. 进入 **Settings → Pages**，把 **Source** 设为 **Deploy from a branch**，**Branch** 选 **`gh-pages`** / **`/ (root)`**，保存。
5. 等 1–2 分钟，访问 `https://<用户名>.github.io/<仓库名>/` 即可看到页面。

> 顺序很重要：`gh-pages` 分支由工作流首次运行时创建，所以要先把工作流跑通一次，Pages 的下拉框里才会出现这个分支。

之后每天 UTC 00:00（北京时间 08:00）会自动运行，无需人工干预。

## 获取所需的密钥

### DeepSeek API Key

1. 打开 <https://platform.deepseek.com/>，注册并登录。
2. 进入 **API Keys** 页面，点 **创建 API key**。
3. 复制生成的 Key（形如 `sk-...`，只显示一次），存入 `DEEPSEEK_API_KEY`。

> 调用的是 OpenAI 兼容接口 `https://api.deepseek.com`，默认模型 `deepseek-chat`（即 DeepSeek-V3）。
> 如需换成推理模型，把环境变量 `DEEPSEEK_MODEL` 设为 `deepseek-reasoner` 即可。

### 邮件推送（以 QQ 邮箱为例）

推送使用 Python 标准库 `smtplib` + `email.mime`，**不需要任何第三方邮件库**。

1. 登录 QQ 邮箱网页版，进入 **设置 → 账号**。
2. 找到 **POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务** 一节。
3. 开启 **IMAP/SMTP 服务**（需要手机短信验证）。
4. 验证通过后会弹出一串 **16 位授权码**，形如 `apypxxxxxxxxxxxx`。

```
SMTP_HOST=smtp.qq.com      # SSL 端口 465，本项通常不用改
SMTP_PORT=465
SMTP_USER=你的QQ号@qq.com
SMTP_PASS=上一步拿到的授权码     ← 不是 QQ 登录密码
RECEIVER_EMAIL=你的QQ号@qq.com   ← 收件地址，与自己相同即可
```

> **最容易踩的坑**：`SMTP_PASS` 必须填**授权码**。填登录密码会在 `SMTPAuthenticationError` 处失败。程序捕获这个异常时会直接在日志里提示这一点，不会重试浪费时间。
>
> 想抄送给多人，用 `RECEIVER_EMAILS_EXTRA`（逗号分隔）而不是改 `RECEIVER_EMAIL`。

邮件效果：

```
主题：每日早报 · 09月14日 | 长飞空芯光纤创全球最低损耗纪录，英伟达预计明年营收增70%

──────────────────────────────────────
每日早报 / DAILY DIGEST
14
2026.09 · 2026年09月14日 星期一

▎长飞空芯光纤创全球最低损耗纪录，英伟达预计明年营收增70%…

01 国际科技新闻                            8 条
──────────────────────────────────────
01  英伟达预计2027年营收增70%
    黄仁勋称明年营收有望同比增长70%至约6800亿美元…
    阅读全文 ›         199IT
02  …
──────────────────────────────────────
[ 查看网页版与往期存档 › ]   ← 直达 GitHub Pages
```

邮件正文用表格布局 + 行内样式单独渲染了一份，因为邮件客户端不支持 flex/grid；同时附带纯文本版本供不支持 HTML 的客户端兜底。

## 配置项速查

所有配置集中在 `config.py`，也可以通过环境变量覆盖（适合在 Actions 里临时调整）：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 空 | DeepSeek 密钥 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | 接口地址，可换成中转站 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 模型名 |
| `RECEIVER_EMAILS_EXTRA` | 空 | 额外收件人，逗号分隔（用于抄送） |
| `SMTP_FROM_NAME` | `每日早报` | 发件人显示名 |
| `SITE_URL` | 自动推导 | 站点地址 |
| `LOOKBACK_HOURS` | `24` | 只取过去 N 小时的内容 |
| `ITEMS_PER_SECTION` | `8` | 每个板块的条数上限 |
| `CANDIDATES_PER_SECTION` | `22` | 每个板块送进模型的候选条数 |
| `HISTORY_MAX_DAYS` | `30` | 保留并渲染多少期往期存档 |
| `GITHUB_TREND_COUNT` | `3` | GitHub 板块「近期趋势」条数 |
| `GITHUB_CLASSIC_COUNT` | `2` | GitHub 板块「历史经典」条数（轮播步长） |
| `GITHUB_TREND_DAYS` | `7` | 「本周新建」的时间窗 |
| `GITHUB_CLASSIC_STAR_FLOOR` | `8000` | 经典池的星数门槛 |
| `GITHUB_CLASSIC_POOL_MAX_AGE_DAYS` | `14` | 经典池最长复用天数，到期重建 |
| `GITHUB_RECENT_MEMORY` | `40` | 记住最近多少个趋势项目用于次日去重 |
| `GITHUB_REVIVED_TOPIC` | `productivity` | 「老将新动作」用的 topic |
| `README_CHAR_LIMIT` | `1500` | 每个仓库抓取 README 的字符上限 |
| `README_MAX_REPOS` | `30` | 最多为多少个仓库抓 README |
| `GITHUB_MAX_INPUT_CHARS` | `45000` | GitHub 独立调用的字符预算 |
| `GITHUB_MIN_STARS` | `50` | GitHub 项目的最低星数 |
| `GITHUB_TOPICS` | 见 `config.py` | 检索的标签 |
| `NOTIFY_ENABLED` | `true` | 设为 `false` 可全局关闭推送 |
| `OBSIDIAN_FIXED_TAGS` | `早报,AI` | Obsidian 笔记的固定标签，逗号分隔 |
| `OBSIDIAN_MAX_KEYWORDS` | `5` | 模型提取的关键词最多取几个 |
| `HTTP_TIMEOUT` | `20` | 单个请求超时（秒） |
| `LLM_MAX_INPUT_CHARS` | `45000` | 送进模型的素材字符上限 |

## 五大板块与分类口径

| JSON 键名 | 板块 | 覆盖范围 |
| --- | --- | --- |
| `intl_tech` | 国际科技新闻 | 全球科技产业动态 |
| `cn_tech` | 国内科技新闻 | 中国科技产业动态 |
| `intl_consumer` | 国际新消费新闻 | 全球零售与消费品牌 |
| `cn_consumer` | 国内新消费新闻 | 中国零售与新消费品牌 |
| `github` | GitHub 热门效率与 AI 工具 | 近 24 小时活跃的开源项目 |

**不设大公司专属板块。** 大厂动态（AI、芯片、云服务、航天、自动驾驶、具身智能、加密货币、算力基础设施等）按事件发生地与主体所在地，直接归入对应的国际/国内科技新闻。这条口径写在 `summarizer.py` 的 `SYSTEM_PROMPT` 与 `build_user_prompt()` 里，由模型执行。

其他硬性约束（均在提示词中强制）：

- 只依据原始素材陈述，禁止编造事实、数字、链接。
- 禁止主观评论、价值判断、预测和投资建议。
- 每条总结不超过 60 个汉字，附可点击的来源链接。
- 素材不足时给多少写多少，禁止凑数；无关内容直接丢弃。

## 可靠性设计

这个系统有若干道防线，保证「某一步挂了也不会开天窗」：

1. **单源隔离**：任何一个 RSS 源失败只记日志，不影响其他源。GitHub 与 RSS 采集互相独立容错。
2. **AI 降级**：DeepSeek 未配置 / 调用失败 / 重试耗尽时，自动改用原始素材渲染一份朴素早报，页面照常发布，并在页首显示黄色提示条说明原因。
3. **防幻觉链接**：模型返回的每条链接都要在原始素材里对得上（支持忽略协议、`www.`、查询串、GitHub 路径的归一化比对，也支持按标题回查）。对不上的条目直接丢弃，确保页面上没有死链和编造来源。
4. **JSON 宽松解析**：兼容模型把 JSON 包在 ` ```json ` 围栏里或前后夹带说明文字的情况。
5. **邮件兼容性**：邮件正文用「表格布局 + 行内样式」单独生成一份，因为 Gmail / QQ 邮箱 / Outlook 普遍不支持 flex、grid 与 `<style>` 标签；同时附带纯文本版本作为兜底。
6. **失败告警**：工作流失败时会发一封告警邮件，附 Actions 日志直达链接与常见原因排查清单。
7. **产物留档**：`dist/_raw.json` 保存当次的结构化结果与采集诊断，随 Artifact 上传保留 7 天。
8. **重复触发幂等**：定时责任交给外部定时器之后，同一天被触发多次是常态（Test Run / 重试 / Re-run）。
   当天已出过正式早报时直接跳过，不重复发邮件、不重复调 AI，但仍会把整站重新渲染出来以免部署步骤因
   `dist/` 缺失而失败。需要强制重出时用 `--force`。详见「外部定时器（cron-job.org）配置白皮书」。
9. **Obsidian 导出不阻断主流程**：Markdown 导出失败只记 ERROR 日志，不会让已经发出的页面与邮件回滚；
   但导出用的是同一份内存数据，正常运行时不存在"网页成功、笔记失败"的中间态。

## 本地自检

```bash
python selftest.py
```

207 项断言，**不需要 API Key，也不需要联网**，覆盖：

- 文本清洗、URL 归一化
- 防幻觉链接校验（编造链接必须被拒绝、标题回查、GitHub 路径归一）
- 模型 JSON 的宽松解析
- 模型输出归一化（幻觉过滤、超量限流、字段类型容错）
- **GitHub 深度解析结构**（四件套字段、星数取真实值而非模型值、编造仓库丢弃、亮点字段容错、空结构丢弃）
- **GitHub 3+2 选品**（清单/教程类仓库的 29 条名称正则排除规则、轮播游标逐日推进、推完一圈绕回并计圈、池子持久化保证排名稳定）
- **轮播状态容错**（文件缺失/损坏/字段类型异常时安全重置，不影响整期生成）
- **存档扫描隔离**（`archive/github_offset.json` 这类状态文件不被误判为某一期存档）
- **中英对照数据**（原文标题/摘录回填、中英文判定）
- **往期存档**（读写、排序、损坏存档容错、相对路径前缀、翻页导航、manifest 生成）
- 降级方案
- 邮件组装（主题长度、HTML 转义、表格布局、行内样式、纯文本兜底、未配置时安全跳过）
- 页面完整性（无 Jinja 残留、五大板块锚点、链接规范）
- **视觉规范合规**（色板白名单、背景禁用纯白、零圆角零阴影、纯黑实线边框、双栏 Grid、侧栏反黑、Tab 语义与渐进增强、键盘导航、锚点直达）
- 配色对比度（21 组文字/底色组合全部按 WCAG AA 校验，含反黑高亮与反白按钮）
- **DeepSeek 完整调用链**：脚本会起一个本地 mock 服务模拟 DeepSeek 接口，真实走一遍 OpenAI SDK → HTTP → JSON 解析 → 链接校验的全流程

## 往期存档是怎么保存的

这是唯一需要特别理解的设计点。GitHub Actions 每次运行都是**全新的干净检出**，仓库里没有上次的产物。所以：

1. 每次运行把当天报告写成 `archive/YYYY-MM-DD.json`（结构化数据，不是 HTML）。
2. 工作流用一个独立步骤把 `archive/` **提交回当前分支**，commit message 带 `[skip ci]` 防止触发死循环。
3. 下一次运行时，`archive/` 里已经有历史数据，渲染阶段会**由这些 JSON 重新渲染出全部往期页面**。

用 JSON 而不是直接保存 HTML 的好处：以后改了模板，所有往期页面都会跟着更新成新样式，不会留下一堆旧样式的死页面。

**不要清空或忽略 `archive/` 目录**，删掉它就等于删掉了全部历史。`.gitignore` 里已明确注明。

## 信息源维护

`config.py` 里的 `RSS_SOURCES` 是唯一需要维护的地方。每条配置：

```python
{
    "name": "IT之家",              # 展示名（也会作为 source 字段）
    "url": "https://www.ithome.com/rss/",
    "region": "cn",                # intl / cn，给模型的线索
    "category": "tech",            # tech / consumer，给模型的线索
    "enabled": True,
}
```

`region` 和 `category` **只是线索**，最终板块归属由模型按内容判断。所以一个配置为 `cn/tech` 的源里出现的消费类新闻，同样会被正确归入「国内新消费新闻」。

几个已知情况：

- **36氪、机器之心、品玩、联商网、亿邦动力**的官方 RSS 已下线（返回 HTML 页面或 404），配置里已替换为同类可用源，或保留为 `enabled: False` 并附 RSSHub 路由。
- **国内消费垂类的 RSS 生态很差**，几乎所有消费媒体的官方 Feed 都已停止维护。目前可用的只有「刀法研究所」和「中新网财经」。因为分类由内容决定，国内科技源里的消费新闻会补上这个板块。
- **Industry Dive 系列**（Retail/Grocery/Fashion/Food/Marketing Dive）周末不更新，所以周末跑出来的「国际新消费新闻」可能条目很少，这是正常的。
- 部分源在特定网络环境下会被重置连接（配置里已用 `enabled: False` 标注）。如果你在 Actions 里看到它们可用，把开关打开即可。

新增源后，建议先验证一下连通性：

```bash
python -c "
import feedparser, requests
r = requests.get('你的feed地址', timeout=20, headers={'User-Agent':'Mozilla/5.0'})
d = feedparser.parse(r.content)
print('条目数:', len(d.entries), '| 格式:', d.version)
print('最新:', d.entries[0].get('published') if d.entries else '无')
"
```

`条目数 > 0 且格式为 rss20/atom10` 且最新时间在一天内，才算可用。

## 外部定时器（cron-job.org）配置白皮书

### 为什么必须换掉 GitHub 自己的 schedule

本仓库**已经删掉了 `schedule` 触发器**，只保留 `workflow_dispatch`。原因是实测数据：

| 运行 | cron 表达式 | 期望触发（北京） | 实际触发（北京） | 调度延迟 | 排队延迟 |
| --- | --- | --- | --- | --- | --- |
| #3 | `0 0 * * *` | 08:00 | 09-15 **12:15** | 4 小时 15 分 | 0 分钟 |
| #4 | `0 0 * * *` | 08:00 | 09-16 **12:11** | 4 小时 11 分 | 0 分钟 |
| #7 | `37 23 * * *` | 07:37 | 09-17 **09:39** | 2 小时 02 分 | 0 分钟 |

两个关键结论：

1. **排队延迟全部是 0 分钟**（每次运行的 `run_started_at` 都等于 `created_at`）。
   也就是说运行实例一旦被创建就立刻开始跑了，这段延迟完全发生在 GitHub 调度器内部 ——
   连运行实例都还没创建，所以调 `concurrency`、换 runner、加 `timeout` 都救不了。
2. 把 cron 从整点挪到非整点（`0 0` → `37 23`）只是把 4 小时压缩到 2 小时，**仍然不可用**。

对照组：同一仓库的 `workflow_dispatch` 运行，`created → started` 全部是 **0.0 分钟**。
所以"外部打接口"这条路是秒级响应的。

顺带的好处：删掉 `schedule` 之后，也绕开了 GitHub「仓库 60 天无提交则自动停用定时工作流」的限制。

### 一、复制粘贴清单

#### 1. URL

```
https://api.github.com/repos/qinglaiccc/daily-digest-system/actions/workflows/daily_report.yml/dispatches
```

URL 里的 `daily_report.yml` 是**工作流文件名**（不是 `name:` 字段里的中文名「每日早报」）。
写错文件名会返回 `404 Not Found`，看起来像"仓库或权限有问题"，其实只是文件名不对。

#### 2. HTTP Method

**POST**。

⚠️ 这是最容易踩的坑：cron-job.org 默认是 **GET**，而 GET 打这个地址会返回 **404**
（GitHub 只为该地址定义了 POST）。实测：

| 请求 | 返回 |
| --- | --- |
| `POST` 正确地址 | **204 No Content** |
| `GET` 同一地址 | 404 Not Found |
| `POST` 但文件名写错 | 404 Not Found |

所以必须去 **Advanced settings → Request method** 手动切成 POST。

#### 3. Headers

四条，逐条加（cron-job.org 用 `Key: Value` 的键值对形式，一行一条）：

| Key | Value |
| --- | --- |
| `Accept` | `application/vnd.github+json` |
| `Authorization` | `Bearer 你的PAT` |
| `X-GitHub-Api-Version` | `2022-11-28` |
| `Content-Type` | `application/json` |

注意 `Authorization` 的值里 `Bearer` 和 token 之间是**一个空格**，`Bearer` 首字母大写。
用 `token xxx` 的旧写法在部分接口上会返回 401。

cron-job.org **不允许**自定义 `User-Agent` 和 `Connection` 两个头，但这不影响 ——
GitHub 只需要请求里存在 User-Agent，cron-job.org 会发自己的默认值。

#### 4. Body

正式运行（会真实采集、调 AI、发邮件）：

```json
{"ref":"main"}
```

只想验证链路通不通、**不想收到多余邮件**时：

```json
{"ref":"main","inputs":{"dry_run":"true"}}
```

注意 `inputs` 里的值写成字符串 `"true"` 或布尔 `true` **实测都能生效**（都返回 204，
且都确实走了 dry-run 分支）。上面的字符串写法是 GitHub 文档里的形式，优先用它。

`ref` 必须是真实存在的分支名，这里就是 `main`。

#### 5. 时间表与时区

- **Schedule**：每天 `07:30`
- **Time zone**：**必须显式确认为 `Asia/Shanghai`**

这是第二个"时区坑"：cron-job.org 的时间是按**账户时区**解释的。如果账户时区是 UTC，
那 `07:30` 就变成了北京时间 15:30，你会以完全相同的方式再踩一次坑。

整条链路的耗时可参考：采集 RSS + GitHub 约 1–2 分钟，两次 DeepSeek 调用约 1–2 分钟，
渲染与邮件几秒。**所以 07:30 触发，邮件大约 07:33–07:35 到**。

#### 6. 通知设置（默认是关的，必须手动打开）

cron-job.org 的通知开关**默认全部关闭**：

| 设置 | 默认值 | 建议 |
| --- | --- | --- |
| `onFailure`（失败时通知） | `false` | **打开** |
| `onFailureCount`（连续失败几次才通知） | `1` | 保持 `1` |
| `onSuccess`（恢复成功时通知） | `false` | **打开** |
| `onDisable`（被自动停用时通知） | `false` | **打开**，这条最重要 |

`onDisable` 是保命开关：cron-job.org 在任务持续失败后**会自动把它停用**。
（官方文档确认了"自动停用"这个行为的存在——`onDisable` 选项的说明就是"任务被自动停用时是否通知"——
但**没有公布触发阈值**；第三方整理的数据说是连续失败 25 次，这个数字未经官方证实。）
如果不打开这个通知，任务被停用你不会收到任何提示，早报会静默消失。

### 二、Token 怎么给

cron-job.org 是第三方，token 会存在它的服务器上。所以**不要**用你现有的全量权限 token。

去 GitHub → Settings → Developer settings → **Personal access tokens → Fine-grained tokens**
新建一个：

| 项 | 值 |
| --- | --- |
| Token name | `cron-job-org-daily-digest` |
| Expiration | 90 天（配个日历提醒） |
| Repository access | Only select repositories → `daily-digest-system` |
| Permissions → **Actions** | **Read and write**（这是唯一必需的权限） |
| Permissions → Metadata | Read（自动带上，删不掉） |
| Permissions → Contents | Read |

**只需要 `Actions: Read and write`。** 不要给它 `Contents: write` —— 触发工作流不需要写代码的权限。

如果这个 token 泄露，最坏情况是别人能触发你的工作流，而不是能改你的代码。

验证 token 是否有权限，可以用这条命令（把 `<PAT>` 换成真值）：

```bash
curl -i -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer <PAT>" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  -H "Content-Type: application/json" \
  https://api.github.com/repos/qinglaiccc/daily-digest-system/actions/workflows/daily_report.yml/dispatches \
  -d '{"ref":"main","inputs":{"dry_run":"true"}}'
```

看到 `HTTP/2 204` 就是通了。常见错误码对照：

| 返回码 | 含义 |
| --- | --- |
| `204` | 成功，工作流已开始排队 |
| `404` | 文件名写错 / ref 不存在 / token 权限不足（GitHub 对无权访问的资源统一返回 404 而不是 403） |
| `401` | `Authorization` 头的格式错了 |
| `422` | body 格式错误（比如 `ref` 指向了不存在的分支） |

### 三、幂等闸门：为什么重复触发不会重复发邮件

改成外部触发之后，同一天被触发多次是**常态**：点一次 cron-job.org 的 "Test Run"、
任务失败后重试、在 Actions 页面点 "Re-run"，都会再打一次 dispatch 接口。
没有防护的话每次都会再发一封一模一样的邮件、再烧一次 DeepSeek 额度。

`main.py` 里有一道幂等闸门：**当天已经出过正式早报就跳过**，只把整站按既有存档重新渲染一遍
（保证 `dist/` 存在，否则 gh-pages 部署步骤会因为目录缺失而失败），不发邮件、不重新采集、不重新调 AI。

几个边界情况都处理了：

| 情况 | 行为 |
| --- | --- |
| 当天没有存档 | 正常执行 |
| 当天有正式存档 | 跳过，打印存档生成时间 |
| 当天存档是 `--dry-run` 产物 | **不算数**，正常执行（否则本地预览一次就会挡住当天的正式出刊） |
| 存档文件损坏 | 按"不存在"处理（宁可重跑一次，也不能把当天卡死） |
| 需要强制重出一期 | 加 `--force`，或在 Actions 页面勾选 **force** 输入项 |

所以：**在 cron-job.org 上点 "Test Run" 是安全的** —— 如果当天早报已经出过，它就是一次空跑。
但在"当天还没出过早报"的时候点 Test Run，会真的发一封邮件。第一次配置时想安全测试，
请把 body 临时换成 `{"ref":"main","inputs":{"dry_run":"true"}}`，确认返回 204 且运行变绿之后再换回正式 body。

### 四、第一次配置后的验收清单

配置完当天就能确认这几件事：

- [ ] 到点后 GitHub Actions 出现一条 `workflow_dispatch` 的新运行，**触发时间与 07:30 相差在 1 分钟内**
- [ ] 该运行的日志里「记录触发信息」这一步打印的是 `事件类型: workflow_dispatch`
- [ ] cron-job.org 的任务历史里，这次执行被标成**成功**（预期返回码 204）
- [ ] 邮件在 07:35 前后到达
- [ ] cron-job.org 的通知设置里 `onFailure` / `onDisable` 都打开了

第一周请重点看第 3 条。cron-job.org 把 2xx 视为成功还是只认 200，官方文档没有明确写；
GitHub 这个接口实测返回的是 204。如果它把 204 判成失败，你会每天收到一封"任务失败"通知 ——
**这是好事，说明通知是通的**，此时回来把 `onFailure` 的判定问题反馈给我，改用
Cloudflare Workers（能自己控制判定逻辑）即可。

### 五、备选方案

| 方案 | 优点 | 缺点 |
| --- | --- | --- |
| **cron-job.org** | 零代码，5 分钟配完 | token 存在第三方；成功判定规则不透明 |
| **Cloudflare Workers Cron Triggers** | 免费，判定逻辑自己写，secret 存在 Cloudflare | 需要写几行代码并 `wrangler deploy` |
| **本机 Windows 任务计划程序** | 完全不碰第三方，token 不出本机 | 电脑必须开机；关机就漏一次 |
| 自己的服务器 crontab | 最可控 | 前提是你有常开的服务器 |

## 常见问题

**Q：定时任务没有准点跑？**

已经不用 GitHub 的 `schedule` 了 —— 它在实测中会被推迟 2～4 小时，且排队延迟为 0 分钟，
说明延迟完全发生在 GitHub 调度器内部，无法调优。现在改为由外部定时器
（cron-job.org）在每天 07:30 调用 `workflow_dispatch` 接口触发。

完整的原因分析、实测数据、以及在 cron-job.org 上的全部配置参数（URL / Method / Headers /
Body / 时区 / 通知开关 / 最小权限 Token），见上一节
**「外部定时器（cron-job.org）配置白皮书」**。

**Q：页面 404？**
依次检查：仓库是 Public（私有仓库的 Pages 需要 Pro）；Settings → Pages 的 Source 选了 `gh-pages` 分支；`gh-pages` 分支确实存在（跑过一次正式工作流后才有）。

**Q：页面生成了，但没收到邮件？**
看 Actions 日志里 `notifier` 那几行，程序会明确打印失败原因：

| 日志关键字 | 原因与处理 |
| --- | --- |
| `邮件配置不完整，缺少 ...` | 对应的 Secret 没配或为空 |
| `SMTP 认证失败` | `SMTP_PASS` 填成了登录密码，应填 **16 位授权码**；或邮箱未开启 SMTP 服务 |
| `邮件发送失败（第 N/3 次）` | 网络问题或 465 端口被屏蔽，可改用 `SMTP_PORT=587`（自动走 STARTTLS） |
| `以下收件人被服务器拒绝` | 收件地址拼写错误，或对方服务器拒收 |

也可以本地直接验证一次：

```bash
python -c "import notifier, summarizer, fetcher; r=summarizer.build_fallback_result([], [], summarizer.today_local(), 'x'); print(notifier.send_email(r))"
```

**Q：邮件里排版乱了？**
邮件客户端对 CSS 支持差别很大，本项目的邮件正文刻意只用了表格布局 + 行内样式（不用 flex/grid/`<style>` 标签）来兼容。如果你用的客户端仍显示异常，请告知具体客户端，可以针对性再简化。网页版不受影响。

**Q：某个板块经常是空的？**
说明该时段对应的信息源没有发布新内容（周末尤其明显）。在 `config.py` 里给该板块加 1–2 个源即可，参考上面的「信息源维护」。

**Q：想换 AI 服务商？**
任何兼容 OpenAI 接口的服务都可以：把 `DEEPSEEK_BASE_URL` 指向对方的地址，`DEEPSEEK_MODEL` 改成对应模型名。注意对方需要支持 `response_format={"type": "json_object"}`；如果不支持，去掉 `summarizer.py` 中 `call_deepseek()` 里的这一行即可（JSON 宽松解析会兜住）。

**Q：不想推送通知，只要网页？**
把仓库变量 `NOTIFY_ENABLED` 或运行时环境变量设为 `false`，或本地执行 `python main.py --no-notify`。

**Q：日志里出现 `GitHub 认证失败` 或 `GitHub 全部检索均失败`？**
GitHub Search API 的配额：匿名 10 次/分钟，带 Token 30 次/分钟。本项目要发 8 次检索，匿名配额几乎必然被打满，所以 `fetcher.py` **强制要求携带 Token**，缺失时直接抛出可读的错误而不是静默返回空列表。

工作流里映射了 `secrets.GITHUB_TOKEN`（GitHub 自动注入，无需手动配置）。如果你在**本地**运行，需要在 `.env` 里提供 `GITHUB_TOKEN`——建议单独生成一个 Personal Access Token（classic，勾选 `public_repo` 即可），不要复用部署用的那个。

**Q：`GITHUB_TOKEN` 这个名字能建成 Repository secret 吗？**
不能。GitHub 保留了这个名字，在 Secrets 页面添加时会报 `This name is reserved`。这是正常的——该名字由 Actions 运行时自动注入，权限由仓库的 Workflow permissions 设置决定，不需要也不应该手动创建。

## 命令行参数

```
python main.py                     # 完整流程（真实调用 DeepSeek 并渲染整站）
python main.py --dry-run           # 跳过 AI 与推送，用原始素材渲染
python main.py --no-notify         # 跑完整流程但不推送
python main.py --force             # 忽略「今天已出过」的幂等闸门，强制重出一期
python main.py --serve --port 8000 # 渲染后起本地预览服务（带 charset 头）
python main.py --keep-dist         # 渲染前不清空 dist
python main.py -v                  # 输出调试日志
```

退出码：`0` 正常（含命中幂等闸门而跳过的情况），`1` 致命错误（连页面都没生成）。

## 免责声明

本页内容由机器自动聚合与摘要，仅供信息参考。所有内容版权归原媒体所有，请以页面中的原文链接为准。摘要由 AI 生成，可能存在偏差。
