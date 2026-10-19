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
    ans = app.qa.ask(args.question, args.collection)
    print(ans.text)
    print(f"\n[来源: {', '.join(ans.sources) or '无'} | 模式: {ans.mode} | 费用约 ${ans.cost_usd:.4f}]")
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


def cmd_serve(args, settings: Settings) -> int:
    import uvicorn

    uvicorn.run("regorag.api:app", host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="regorag", description="Rego 知识库工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("validate", help="检查知识库格式，列出到期需复核的文章").set_defaults(fn=cmd_validate)

    s = sub.add_parser("ask", help="在命令行提问")
    s.add_argument("question")
    s.add_argument("--collection", default="rego")
    s.set_defaults(fn=cmd_ask)

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
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve)

    args = p.parse_args(argv)
    try:
        return args.fn(args, Settings.from_env())
    except (KBError, PermissionError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
