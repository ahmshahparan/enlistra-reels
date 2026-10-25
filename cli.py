"""Enlistra Reels CLI.

  python cli.py run                      # find 1 new free film, make reels, write manifests / drafts
  python cli.py run --films 3            # process 3 new films
  python cli.py run --id silent-the-count   # process one specific Internet Archive item
  python cli.py run --no-post            # render only
  python cli.py check silent-the-count   # is this item free to use?
"""
import argparse
import logging

from enlistra import env, pipeline, source

env.load()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s", datefmt="%H:%M:%S")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--films", type=int, default=1)
    r.add_argument("--id")
    r.add_argument("--no-post", action="store_true")
    r.add_argument("--config", default="config.yaml")
    pp = sub.add_parser("post", help="publish an already rendered reel from its manifest")
    pp.add_argument("manifest")
    pp.add_argument("--force", action="store_true", help="post even if the critic flagged it")
    pp.add_argument("--config", default="config.yaml")
    c = sub.add_parser("check")
    c.add_argument("identifier")
    c.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = pipeline.load_cfg(args.config)
    if args.cmd == "run":
        outs = pipeline.run(cfg, films=args.films, identifier=args.id, post_now=not args.no_post)
        for o in outs:
            print(o)
    elif args.cmd == "post":
        from pathlib import Path
        from enlistra import post as poster
        print(poster.publish(Path(args.manifest), cfg["post"], force=args.force))
    elif args.cmd == "check":
        f = source.resolve(args.identifier, cfg["source"])
        print(f.to_json() if f else "NOT free to use (or no usable file)")


if __name__ == "__main__":
    main()
