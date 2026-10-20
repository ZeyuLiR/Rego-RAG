# Rego-RAG：公司知识库与 rego clerk 问答助手

rego clerk 用中文或英文提问，助手只根据**审核过的知识库**回答，并标出处。
知识库更新时，把邮件、PDF、Word 交给模型抽取成修改提案，人工审核通过后才写入。

设计方案：[Rego 知识库设计方案](https://claude.ai/code/artifact/ab175223-ccc3-4271-8377-7bbcac76db1d)

## 现在能做什么（第一期）

| 功能 | 命令 |
| --- | --- |
| 检查知识库格式、列出到期需复核的文章 | `regorag validate` |
| 命令行提问 | `regorag ask "外州车转入需要什么材料？"` |
| 网页聊天（clerk 用）| `regorag serve`，浏览器打开 http://127.0.0.1:8000 |
| **导入与审核网页**：拖入多个文件、粘贴邮件文字或写一句说明，这一批会放在一起综合整理，整理后在同一页通过或驳回 | `regorag serve`，打开 http://127.0.0.1:8000/import |
| **附件**：空白模板、表格随资料一起导入，审核通过后存进 `kb/<知识库>/files/`；clerk 提问时助手会给出下载按钮 | 导入页自动识别，或在文章 front matter 的 `attachments` 里登记 |
| **数字核对**：回答里的费用、日期、期限、表格号逐个到引用的文章里找，找不到就黄色提醒 clerk 找人复核，并把这条回答放进导入页的“需要复核的回答” | 自动，不调用模型 |
| **反馈按钮**：每个回答下面有“有帮助 / 有错”，点“有错”可以写哪里不对，这条回答也会进“需要复核的回答” | 问答页 |
| **评测集**：几个人一起录入真实问题和必备要点，一键全部问一遍并打分 | 网页 http://127.0.0.1:8000/eval，或 `regorag eval [--judge]` |
| 命令行导入（同样的流程，多个文件一起看）| `regorag import 文件1.pdf 文件2.eml --note "都是 de-rego 流程"`（`--separate` 分开整理）|
| 审核通过提案并发布 | `regorag apply inbox/<提案文件夹> --reviewer Zeyu` |
| 直接发布手写的草稿 | `regorag publish kb/rego/.../xxx.md --reviewer Zeyu` |
| 查看本月 API 用量与预算 | `regorag usage` |

## 最简单的启动方式

装好 Python 3.11+ 和 Claude Code 后，双击：

- macOS：`start-mac.command`
- Windows：`start-windows.bat`

第一次会自动准备环境、安装组件并创建 `.env`（几分钟）；以后双击几秒就打开浏览器，并自动拉取最新版本。
详细步骤见[使用教程](https://claude.ai/code/artifact/e4d2d04d-58c0-4d4d-a187-412feef23395)。

## 手动安装（开发用）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
pytest                 # 不调用模型，不花钱
```

## 两种调用 Claude 的方式（`.env` 里的 `REGORAG_LLM_BACKEND`）

| 方式 | 需要什么 | 费用 | 适合 |
| --- | --- | --- | --- |
| `claude-code`（现在用这个）| 本机安装并登录 Claude Code（`npm install -g @anthropic-ai/claude-code`，然后运行一次 `claude` 登录）| 算在你的 Claude 套餐用量里，不另外付费 | 搭建期、你自己测试 |
| `api` | `ANTHROPIC_API_KEY` | 按 token 计费，受月度预算限制 | clerk 正式使用 |

两种方式走的是同一套代码：同样的知识库、提示词、检索、引用、审核流程，只是最后调用 Claude 的那一步不同。
切换只改 `.env` 这一行，其他什么都不用动。

人读网站（可选）：`pip install -e ".[site]" && mkdocs serve`。

## 目录

```
kb/<collection>/<category>/<article>.md   知识库（唯一事实来源），Markdown + YAML 元数据
kb/rego/_template.md                       文章模板
config/collections.yaml                    知识库列表（rego，预留 sales）
config/reviewers.yaml                      审核人名单与权限
inbox/                                     待审核的修改提案（不提交到 Git）
data/usage.jsonl, data/gaps.jsonl          API 用量记录、知识库未覆盖的问题（不提交）
data/chats.db                              聊天记录、问答日志与反馈（不提交）
eval/questions.yaml                        评测集（网页上改会自动记入 Git）
data/eval/                                 每次评测的报告（不提交）
src/regorag/                               代码
```

## 更新流程

最简单的方式是网页：打开 `/import`，把文件拖进去（可以一次多个），或者把邮件文字直接粘贴进去，
点「开始整理」。整理好的提案出现在下面的「待审核」里：每处改动都附原文依据，草稿可以直接在页面上修改，
勾选要通过的改动后点「通过所选并发布」，问答页立刻就能用到，同时自动记一次 Git 提交。
驳回的提案移到 `inbox/_rejected/`，不会删除。

> 服务器只在你自己电脑上运行（127.0.0.1）时不需要口令。让别人也能访问之前，务必在 `.env` 里设置
> `REGORAG_ADMIN_TOKEN`，否则任何能打开网页的人都能导入和发布。

命令行方式：

1. `regorag import 通知.pdf` → 生成 `inbox/<日期>-<文件名>/`，里面有 `REVIEW.md`（改什么、为什么、原文依据）和 `drafts/`（完整的文章草稿）。
2. 读 `REVIEW.md`，需要的话直接改 `drafts/` 里的草稿。不同意就删掉整个文件夹。
3. `regorag apply inbox/<文件夹> --reviewer <名字>` 把草稿写进 `kb/` 并标记发布。
4. `git commit` 并推送。Git 历史就是审核记录，可随时回滚。

上传前会自动去掉邮箱、电话、VIN、证件号等个人信息，但审核时仍请检查 `source.md`。

## 问答怎么工作

- 知识库小于约 15 万 token 时，模型每次读整个知识库（配合 prompt caching，重复提问很便宜）。
- 超过后自动切换为检索模式：先把问题改写成英文检索词，再用 BM25 找相关小节。
- 只读取 `status: published` 且已到生效日期的文章。
- 回答必须引用文章 id；知识库没有的内容会直接说不知道，并记录到 `data/gaps.jsonl`。

## 检索方式与向量检索（可选，默认不用）

`.env` 里的 `REGORAG_RETRIEVAL` 决定模型读什么，聊天页右上角的「检索方式」可以按次覆盖：

| 方式 | 说明 |
|---|---|
| `auto`（默认） | 知识库小时用 `full`，超过上限用 `bm25` |
| `full` 全文 | 模型读整个知识库 |
| `bm25` 关键词 | 问题先改写成英文检索词，再按关键词找相关小节 |
| `vector` 向量 | 按意思找相关小节；多语言模型，中文问题也能直接匹配英文资料 |
| `hybrid` 混合 | 关键词和向量两路结果合并（RRF） |

向量模型在本机运行（fastembed，CPU 即可，不花 API 费用），第一次使用时下载约 220 MB：

```bash
.venv/bin/pip install -e ".[vector]"
.venv/bin/regorag index                     # 预先建立向量索引（data/vectors/）
```

对比差别的三种办法：

- 聊天页选「对比全部四种」：同一个问题用四种方式各回答一次，并排显示来源、检索词和费用（费用是四倍）。
- `regorag compare "外州车转入要什么材料" --query "interstate transfer documents"`：只看每种方式找到哪些小节，给了 `--query` 就不调用模型；加 `--answer` 再让每种方式回答。
- `regorag eval-retrieval eval/retrieval.yaml`：用问题集给 bm25 / vector / hybrid 打分（命中率、MRR），格式见 `eval/retrieval.example.yaml`；加 `--no-rewrite` 不调用模型。

向量索引按小节内容的哈希缓存，发布新文章后只为改动的小节重新计算。现在资料少，用一个 JSON 文件就够；以后资料多了可以在 `vectors.py` 的 `VectorStore` 后面换成 LanceDB 或 pgvector，问答代码不用改。

## 回答质量：数字核对、反馈、评测集

- **数字核对**（每个回答都做，不花钱）：回答里的金额、日期、期限、百分比、表格号，会到它引用的文章里找。
  写法不同也算对上（$45 = 45 澳元，1 July 2026 = 2026年7月1日，14 天 = 14 days = fourteen days）。
  找不到的不会被删掉，而是在回答下面黄色提醒：“请先找审核人或资深 clerk 复核后再用”，同时这条回答进入导入页的
  **需要复核的回答**，维护人核对文章、更新后点“已处理”。
- **反馈**：clerk 点“有错”并写明正确内容，同样进入“需要复核的回答”。每条回答都记在问答日志里（问题、回答、
  引用的文章和版本、费用、耗时、反馈），存在聊天记录同一个数据库。
- **评测集**（`/eval`，需要管理口令）：每题写问题、必备要点、应引用的文章、是否应回答“不知道”、出题人。
  “需要复核的回答”里也可以一键“加入评测集”。点“开始评测”会把每题真的问一遍（会花额度），打分项：
  引用了对的文章、包含必备要点、该拒答时拒答、数字都有出处；勾选“模型评判”再加一项“忠于原文”（费用约翻倍）。
  改提示词、换检索方式或批量更新知识库前后各跑一次，对比通过率。

## 聊天记录存在哪

问答页的历史对话按 clerk 填的名字保存在服务器上，数据库由 `.env` 里的 `REGORAG_CHAT_DB` 决定：

| 设置 | 说明 |
|---|---|
| `sqlite:///data/chats.db`（默认） | 一个文件，Python 自带，不用安装；备份就是复制这个文件 |
| `postgresql://用户:密码@主机/库名` | 先 `.venv/bin/pip install -e ".[postgres]"`；适合共享服务器、多人同时使用 |

改完这一行重启即可，表会自动建好（换库时旧记录不会自动搬过去）。要接其他数据库，在 `src/regorag/chats.py` 里实现 `ChatStore` 的四个方法即可。

## 用量与费用

每次调用（两种方式都算）都记入 `data/usage.jsonl`（估算费用）。`REGORAG_MONTHLY_BUDGET_USD` 设定月度上限，
达到后问答接口返回 429，不会继续花钱。默认模型是 `claude-opus-5-5`，如果想降低费用，
可以在 `.env` 里把 `REGORAG_QA_MODEL` 改为 `claude-sonnet-5-5`。
