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
| 从邮件 / PDF / Word 生成修改提案 | `regorag import 文件1.pdf 文件2.eml` |
| 审核通过提案并发布 | `regorag apply inbox/<提案文件夹> --reviewer Zeyu` |
| 直接发布手写的草稿 | `regorag publish kb/rego/.../xxx.md --reviewer Zeyu` |
| 查看本月 API 用量与预算 | `regorag usage` |

## 安装

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # 填入 ANTHROPIC_API_KEY，按需调整模型和月度预算
pytest                 # 不调用 API，不花钱
```

人读网站（可选）：`pip install -e ".[site]" && mkdocs serve`。

## 目录

```
kb/<collection>/<category>/<article>.md   知识库（唯一事实来源），Markdown + YAML 元数据
kb/rego/_template.md                       文章模板
config/collections.yaml                    知识库列表（rego，预留 sales）
config/reviewers.yaml                      审核人名单与权限
inbox/                                     待审核的修改提案（不提交到 Git）
data/usage.jsonl, data/gaps.jsonl          API 用量记录、知识库未覆盖的问题（不提交）
src/regorag/                               代码
```

## 更新流程

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

## 用量与费用

每次 API 调用都记入 `data/usage.jsonl`（估算费用）。`REGORAG_MONTHLY_BUDGET_USD` 设定月度上限，
达到后问答接口返回 429，不会继续花钱。默认模型是 `claude-opus-5-5`，如果想降低费用，
可以在 `.env` 里把 `REGORAG_QA_MODEL` 改为 `claude-sonnet-5-5`。
