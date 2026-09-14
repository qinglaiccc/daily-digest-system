# 每日早报自动化系统

每天早上 8:00（Asia/Shanghai）自动抓取科技/消费新闻与 GitHub 热门项目，调用 DeepSeek 做客观精简总结，渲染成响应式网页发布到 GitHub Pages，并推送 HTML 邮件。

```
┌─────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│ 1. 数据采集  │ → │ 2. AI 提炼    │ → │ 3. HTML 渲染  │ → │ 4. 通知/部署  │
│ 24 个 RSS   │   │ DeepSeek     │   │ 瑞士极简风格   │   │ SMTP 邮件 +   │
│ GitHub API  │   │ 五大板块      │   │ + 往期存档     │   │ gh-pages     │
└─────────────┘   └──────────────┘   └──────────────┘   └──────────────┘
```

## 网页功能

| 功能 | 说明 |
| --- | --- |
| **往期存档** | 每天一份存档提交回仓库，页面上可翻阅任意历史期次；首页有「往期存档」列表与上/下期翻页 |
| **中英对照** | 顶栏三档切换：**中文 / 原文 / 双语**。选择记在 localStorage，刷新和跳转往期页都不会丢 |
| **阅读全文** | 每条新闻都有直达原文的「阅读全文」链接，标题本身也可点击 |
| **响应式** | 375px 手机到 1600px 宽屏四档断点均已实测，无横向溢出 |
| **降级可读** | AI 不可用时页面照常生成，并在顶部说明原因 |

**关于「原文」模式**：原文标题与摘录直接取自 RSS / GitHub 的原始素材，不经过模型改写，所以不存在被编造或翻译失真的风险。中文模式下这些字段隐藏，切到「双语」时以浅灰小字附在中文标题下方，切到「原文」时升为标题级字号。

## UI 风格

页面采用 **Swiss / International Typographic Style（瑞士国际主义）**，规格来自本地 skill `ui-style-swiss-minimal`：

- 纯白底 `#FFFFFF` / 浅灰 `#F7F7F8`，唯一无衬线字族（Helvetica Neue + 思源黑体）
- **只有一个点缀色**：Swiss Red `#E4002B`，用于编号、日期数字与悬停态
- 结构与分隔一律用 1px 发丝线，**全站零圆角、零阴影、零渐变**
- 一律左对齐，用非对称栅格而非居中版式
- 日期数字（如 `13`）作为版式锚点，字号 56–112px 随断点变化
- **差异化动作**：鼠标悬停任意条目时，底层 5 条瑞士红栅格线从透明显影，同时该条序号变红

改风格只需替换 `template.html` 与 `template_index.html` 里的 CSS 变量块，或用其他 `ui-style-*` skill 重写这两个文件。

## 目录结构

```
.
├── main.py                      # 主流程（采集 → 提炼 → 渲染 → 推送）
├── config.py                    # 配置中心：信息源、板块、密钥、超时、.env 加载
├── fetcher.py                   # 数据采集：RSS（并发）+ GitHub Search API
├── summarizer.py                # DeepSeek 提炼、提示词、防幻觉校验、降级
├── renderer.py                  # 渲染今日页 + 全部往期页 + 往期索引 + history.json
├── notifier.py                  # 邮件推送（标准库 smtplib + email.mime）
├── template.html                # 早报正文模板（Swiss 风格，自包含 CSS）
├── template_index.html          # 往期索引页模板
├── selftest.py                  # 离线自检（108 项，无需密钥/联网）
├── requirements.txt
├── .env                         # 本地密钥（已 gitignore，不会提交）
├── archive/                     # 往期存档 JSON —— 必须提交进仓库，是历史记录的数据源
│   └── YYYY-MM-DD.json
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
| `GITHUB_MIN_STARS` | `50` | GitHub 项目的最低星数 |
| `GITHUB_TOPICS` | 见 `config.py` | 检索的标签 |
| `NOTIFY_ENABLED` | `true` | 设为 `false` 可全局关闭推送 |
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

## 本地自检

```bash
python selftest.py
```

108 项断言，**不需要 API Key，也不需要联网**，覆盖：

- 文本清洗、URL 归一化
- 防幻觉链接校验（编造链接必须被拒绝、标题回查、GitHub 路径归一）
- 模型 JSON 的宽松解析
- 模型输出归一化（幻觉过滤、超量限流、字段类型容错）
- **中英对照数据**（原文标题/摘录回填、中英文判定）
- **往期存档**（读写、排序、损坏存档容错、相对路径前缀、翻页导航、manifest 生成）
- 降级方案
- 邮件组装（主题长度、HTML 转义、表格布局、行内样式、纯文本兜底、未配置时安全跳过）
- 页面完整性（无 Jinja 残留、五大板块锚点、链接规范）
- **Swiss 风格 token 合规**（只允许白/灰阶+瑞士红、零圆角、零阴影、零渐变、无衬线、无 emoji、差异化动作存在）
- 配色对比度（按 WCAG AA 逐对校验所有文字/背景组合）
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

## 常见问题

**Q：定时任务没有在 8:00 准点跑？**
GitHub Actions 的 `schedule` 在高峰期会有几分钟到几十分钟的排队延迟，这是平台行为。如果需要严格准点，把 cron 往前调 10 分钟（如 `50 23 * * *`）。

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
python main.py --serve --port 8000 # 渲染后起本地预览服务（带 charset 头）
python main.py --keep-dist         # 渲染前不清空 dist
python main.py -v                  # 输出调试日志
```

退出码：`0` 正常，`1` 致命错误（连页面都没生成）。

## 免责声明

本页内容由机器自动聚合与摘要，仅供信息参考。所有内容版权归原媒体所有，请以页面中的原文链接为准。摘要由 AI 生成，可能存在偏差。
