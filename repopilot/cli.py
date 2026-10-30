import argparse
import json
import uuid

from .config import Settings
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="RepoPilot repository localization")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    load = commands.add_parser("import")
    load.add_argument("source", help="demo, public GitHub URL, or local path with ALLOW_LOCAL_IMPORT=true")
    load.add_argument("--ref", default="HEAD")
    ask = commands.add_parser("ask")
    ask.add_argument("repo_id")
    ask.add_argument("question")
    ask.add_argument("--strategy", default="hybrid", choices=["bm25", "vector", "hybrid", "hybrid_rerank"])
    ask.add_argument("--mode", default="agent", choices=["agent", "single"])
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("dataset", nargs="?", default="eval/datasets/demo.json")
    evaluate.add_argument("--split", choices=["dev", "test", "all"], default="test")
    evaluate.add_argument("--end-to-end", action="store_true", help="Run single and agent reports; real mode incurs API usage")
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.command == "serve":
        import uvicorn
        from .api import create_app
        uvicorn.run(create_app(settings), host=args.host, port=args.port)
        return
    store = Store(settings)
    if args.command == "import":
        from .importer import Importer
        row = store.create_repo(args.source, args.ref)
        result = row if row["status"] == "ready" else Importer(settings, store).import_repo(
            row["id"], args.source, row.get("revision") or args.ref)
        print(json.dumps({key: value for key, value in result.items() if key != "snapshot_path"}, ensure_ascii=False, indent=2))
    elif args.command == "ask":
        from .graph import AgentRunner
        result = AgentRunner(settings, store).run(uuid.uuid4().hex, args.repo_id, args.question,
                    strategy=args.strategy, mode=args.mode)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "evaluate":
        from .evaluation import evaluate_dataset
        report = evaluate_dataset(settings, store, args.dataset, args.split, args.end_to_end)
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
