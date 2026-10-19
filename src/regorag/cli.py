"""Command line: `regorag <command>`. Run `regorag -h` for the list."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from .app import build_app
from .config import Settings
from .kb import KBError, load_kb
from .retrieval import MODES
from .vectors import VectorSearchUnavailable


def cmd_validate(args, settings: Settings) -> int:
    try:
        kb = load_kb(settings.kb_dir)
    except KBError as exc:
        print(f"知识库有错误：\n{exc}", file=sys.stderr)
        return 1
    today = dt.date.today()
    for name in kb.collections:
        arts = kb.collection(name)
        live = [a for a in arts if a.is_live(today)]
        print(f"{name}: {len(arts)} 篇文章，{len(live)} 篇已发布且生效")
    overdue = [a for a in kb.articles if a.meta.status == "published" and a.is_overdue_for_review(today)]
    for a in overdue:
        print(f"  到期需复核：{a.id}（review_by {a.meta.review_by}，负责人 {a.meta.owner or '未指定'}）")
    return 0


def cmd_ask(args, settings: Settings) -> int:
    app = build_app(settings)
    ans = app.qa.ask(args.question, args.collection, mode=args.retrieval)
    print(ans.text)
    print(f"\n[来源: {', '.join(ans.sources) or '无'} | 模式: {ans.mode} | 费用约 ${ans.cost_usd:.4f}]")
    return 0


SECTION_MODES = ["bm25", "vector", "hybrid"]


def cmd_compare(args, settings: Settings) -> int:
    """Show what each retrieval mode finds for one question (and optionally its answer)."""
    app = build_app(settings)
    query = args.query or app.qa._rewrite(args.question, args.collection, [])
    print(f"检索词：{query}\n")
    for mode in args.modes:
        ctx, _ = app.qa.search(args.question, args.collection, mode, query=query)
        print(f"== {mode} ==")
        if mode == "full":
            print(f"  （全文模式：模型读全部 {len(ctx.cite_ids)} 篇文章）")
        for i, (cid, score) in enumerate(zip(ctx.cite_ids, ctx.scores), 1):
            print(f"  {i}. {cid}  ({score})")
        if not ctx.cite_ids:
            print("  （没有找到）")
        if args.answer:
            ans = app.qa.ask(args.question, args.collection, mode=mode)
            print(f"\n{ans.text}\n  [来源: {', '.join(ans.sources) or '无'} | 约 ${ans.cost_usd:.4f}]")
        print()
    return 0


def cmd_eval_retrieval(args, settings: Settings) -> int:
    """Score each retrieval mode on a question set: did the expected article come back?"""
    import yaml

    items = yaml.safe_load(Path(args.file).read_text(encoding="utf-8")) or []
    app = build_app(settings)
    queries = []
    for it in items:
        q = it.get("query") or (it["question"] if args.no_rewrite
                                else app.qa._rewrite(it["question"], args.collection, []))
        queries.append(q)
    print(f"{len(items)} 个问题，取前 {args.k} 个片段\n")
    print(f"{'模式':<8}{'命中率':>8}{'MRR':>8}")
    misses: dict[str, list[str]] = {}
    for mode in args.modes:
        hits, rr = 0, 0.0
        for it, q in zip(items, queries):
            expect = {e.split("#")[0] for e in it["expect"]}
            ctx, _ = app.qa.search(it["question"], args.collection, mode, query=q)
            found = [c.split("#")[0] for c in ctx.cite_ids[: args.k]]
            rank = next((i for i, c in enumerate(found, 1) if c in expect), None)
            if rank:
                hits += 1
                rr += 1 / rank
            else:
                misses.setdefault(mode, []).append(it["question"])
        n = len(items) or 1
        print(f"{mode:<8}{hits / n:>8.0%}{rr / n:>8.2f}")
    for mode, qs in misses.items():
        print(f"\n{mode} 没找到：")
        for q in qs:
            print(f"  - {q}")
    return 0


def cmd_index(args, settings: Settings) -> int:
    """Embed every live section now, so the first vector question is not slow."""
    app = build_app(settings)
    r = app.qa.retriever(args.collection, "vector")
    print(f"{args.collection}: {len(r.sections)} 个片段已建立向量索引（{settings.vector_dir}）")
    return 0


def cmd_import(args, settings: Settings) -> int:
    from .ingest import propose

    app = build_app(settings)
    coll = app.collections[args.collection]
    for f in args.files:
        folder = propose(Path(f), app.kb, args.collection, app.llm, settings.extract_model,
                         coll.template(), list(coll.categories), settings.inbox_dir)
        print(f"{f} → 待审核：{folder / 'REVIEW.md'}")
    return 0


def cmd_apply(args, settings: Settings) -> int:
    from .ingest import apply_proposal

    app = build_app(settings)
    written = apply_proposal(Path(args.folder), app.kb, settings.kb_dir, app.policy,
                             args.reviewer, args.only)
    for p in written:
        print(f"已发布：{p}")
    load_kb(settings.kb_dir)  # fail loudly if the result does not validate
    print("下一步：检查改动后 git commit 并推送。")
    return 0


def cmd_publish(args, settings: Settings) -> int:
    from .review import ReviewPolicy, publish

    policy = ReviewPolicy.load(settings.config_dir / "reviewers.yaml")
    for f in args.files:
        print(f"已发布：{publish(Path(f), settings.kb_dir, policy, args.reviewer)}")
    return 0


def cmd_usage(args, settings: Settings) -> int:
    from .usage import UsageLog

    summary = UsageLog(settings.usage_log, settings.monthly_budget_usd).summary(args.month)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_test_page(args, settings: Settings) -> int:
    from .testpage import build_test_page

    out = build_test_page(load_kb(settings.kb_dir), args.collection, Path(args.out))
    print(f"已生成：{out}")
    return 0


def _free_port(host: str, start: int) -> int:
    import socket

    for port in range(start, start + 20):
        with socket.socket() as s:
            if s.connect_ex((host, port)) != 0:
                return port
    return start


def cmd_serve(args, settings: Settings) -> int:
    import threading
    import uvicorn
    import webbrowser

    port = _free_port(args.host, args.port)
    url = f"http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{port}"
    print(f"知识库已启动：问答 {url}  ·  导入与审核 {url}/import")
    if args.open:
        threading.Timer(1.5, webbrowser.open, [url]).start()
    uvicorn.run("regorag.api:app", host=args.host, port=port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="regorag", description="Rego 知识库工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("validate", help="检查知识库格式，列出到期需复核的文章").set_defaults(fn=cmd_validate)

    s = sub.add_parser("ask", help="在命令行提问")
    s.add_argument("question")
    s.add_argument("--collection", default="rego")
    s.add_argument("--retrieval", choices=MODES, help="这次提问用的检索方式")
    s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("compare", help="对比各检索方式为一个问题找到的内容")
    s.add_argument("question")
    s.add_argument("--collection", default="rego")
    s.add_argument("--query", help="直接给英文检索词，跳过改写（不调用模型）")
    s.add_argument("--modes", nargs="+", choices=MODES[1:], default=SECTION_MODES)
    s.add_argument("--answer", action="store_true", help="每种方式都让模型回答一次（会产生费用）")
    s.set_defaults(fn=cmd_compare)

    s = sub.add_parser("eval-retrieval", help="用问题集给各检索方式打分（命中率、MRR）")
    s.add_argument("file", help="YAML：每项有 question 和 expect（应找到的文章 id）")
    s.add_argument("--collection", default="rego")
    s.add_argument("--k", type=int, default=5)
    s.add_argument("--modes", nargs="+", choices=SECTION_MODES, default=SECTION_MODES)
    s.add_argument("--no-rewrite", action="store_true", help="直接用原问题检索，不调用模型")
    s.set_defaults(fn=cmd_eval_retrieval)

    s = sub.add_parser("index", help="预先建立向量索引（首次会下载向量模型）")
    s.add_argument("--collection", default="rego")
    s.set_defaults(fn=cmd_index)

    s = sub.add_parser("import", help="从邮件/PDF/Word 生成待审核的修改提案")
    s.add_argument("files", nargs="+")
    s.add_argument("--collection", default="rego")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("apply", help="审核通过一个提案文件夹，写入知识库并发布")
    s.add_argument("folder")
    s.add_argument("--reviewer", required=True)
    s.add_argument("--only", nargs="*", help="只应用这些文章 id")
    s.set_defaults(fn=cmd_apply)

    s = sub.add_parser("publish", help="把草稿文章标记为已发布")
    s.add_argument("files", nargs="+")
    s.add_argument("--reviewer", required=True)
    s.set_defaults(fn=cmd_publish)

    s = sub.add_parser("usage", help="查看本月 API 用量与预算")
    s.add_argument("--month", help="YYYY-MM，默认本月")
    s.set_defaults(fn=cmd_usage)

    s = sub.add_parser("test-page", help="生成测试模式网页（不用 API，在 claude.ai 里提问）")
    s.add_argument("out", nargs="?", default="site/test-chat.html")
    s.add_argument("--collection", default="rego")
    s.set_defaults(fn=cmd_test_page)

    s = sub.add_parser("serve", help="启动网页聊天和 API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000, help="被占用时自动换下一个")
    s.add_argument("--open", action="store_true", help="启动后打开浏览器")
    s.set_defaults(fn=cmd_serve)

    args = p.parse_args(argv)
    try:
        return args.fn(args, Settings.from_env())
    except (KBError, PermissionError, VectorSearchUnavailable) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
