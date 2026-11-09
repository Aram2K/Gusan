"""Prepare the temporary bench function "speed-bench" for a deploy. It only writes local files:
no network, no deploy, and the token is never printed.

    python bench/interpreter/server/prepare_deploy.py           the token in bench/interpreter/.bench_token
                                                                 (made once), the deploy copy of index.ts
    python bench/interpreter/server/prepare_deploy.py --new     a fresh token, even if one exists
    python bench/interpreter/server/prepare_deploy.py --stub    the 410 stub in the deploy copy's place
                                                                 (deploy it right after the run)
    python bench/interpreter/server/prepare_deploy.py --check   only report what is there

The deploy copy is server/.deploy/supabase/functions/speed-bench/index.ts (gitignored): it holds the
token, and --stub overwrites it. server/.deploy/supabase/config.toml sets verify_jwt = false, so the
Supabase CLI can deploy from there (see server/README.md).
"""
import argparse
import json
import os
import re
import secrets
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
SOURCE = HERE / "index.ts"
STUB = HERE / "stub_410.ts"
TOKEN_FILE = BENCH / ".bench_token"
DEPLOY_ROOT = HERE / ".deploy"
SLUG = "speed-bench"
OUT = DEPLOY_ROOT / "supabase" / "functions" / SLUG / "index.ts"
CONFIG = DEPLOY_ROOT / "supabase" / "config.toml"
PROJECT_REF = "hftcelxzfoubheqeoool"
PLACEHOLDER = '"__TOKEN__"'
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{24,200}$")


def read_token():
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return token if TOKEN_RE.match(token) else ""


def write_token(token):
    TOKEN_FILE.write_text(token, encoding="utf-8")       # no newline: it goes into a header as is
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass


def write_config():
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(f'project_id = "gusan-bench-deploy"\n\n[functions.{SLUG}]\nverify_jwt = false\n',
                      encoding="utf-8")


def deploy_copy(token):
    src = SOURCE.read_text(encoding="utf-8")
    if src.count(PLACEHOLDER) != 1:
        sys.exit(f"{SOURCE} must hold the token placeholder exactly once (found {src.count(PLACEHOLDER)})")
    out = src.replace(PLACEHOLDER, json.dumps(token))
    if PLACEHOLDER in out or out.count(token) != 1:
        sys.exit("the token did not land exactly once in the deploy copy")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(out, encoding="utf-8", newline="\n")
    write_config()


def report():
    token = read_token()
    print(f"token file:  {TOKEN_FILE} ({'present' if token else 'missing'})")
    env = os.environ.get("GUSAN_BENCH_TOKEN", "").strip()
    if env and token and env != token:
        print("WARNING: GUSAN_BENCH_TOKEN is set and differs from .bench_token: the client may send the wrong one")
    if OUT.exists():
        text = OUT.read_text(encoding="utf-8")
        kind = "the 410 stub" if "status: 410" in text and "BENCH_TOKEN" not in text else "the bench function"
        holds = bool(token) and token in text
        print(f"deploy copy: {OUT} ({kind}{', with the current token' if holds else ''})")
    else:
        print(f"deploy copy: {OUT} (none)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--new", action="store_true", help="make a fresh token even if one exists")
    ap.add_argument("--stub", action="store_true", help="put the 410 stub in the deploy copy's place")
    ap.add_argument("--check", action="store_true", help="only report what is there")
    a = ap.parse_args()
    if a.check:
        report()
        return
    if a.stub:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(STUB.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        write_config()
        print(f"410 stub written to {OUT}")
        print(f"Deploy it now as '{SLUG}' (verify_jwt=false) - see server/README.md. The token is dead after that.")
        return
    token = "" if a.new else read_token()
    if not token:
        token = secrets.token_urlsafe(32)
        write_token(token)
        print(f"new token written to {TOKEN_FILE}")
    else:
        print(f"using the token in {TOKEN_FILE}")
    deploy_copy(token)
    print(f"deploy copy written to {OUT}")
    print(f"Next: deploy it as '{SLUG}' on project {PROJECT_REF} with verify_jwt=false (server/README.md),")
    print("run the benchmark, then: prepare_deploy.py --stub and deploy the stub.")
    report()


if __name__ == "__main__":
    main()
