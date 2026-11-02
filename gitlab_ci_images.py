#!/usr/bin/env python3
"""
GitLab CI で利用しているコンテナイメージを CI Lint API で洗い出すスクリプト

GET /projects/:id/ci/lint の merged_yaml(include / extends / !reference 展開済み)を解析し、
各ジョブの image / services を抽出して CSV に出力します。

必要パッケージ: pip install requests pyyaml

環境変数:
  GITLAB_URL    例: https://gitlab.example.com
  GITLAB_TOKEN  read_api スコープのトークン(全プロジェクト対象なら管理者のトークン)

出力 (--out-dir 配下):
  images.csv   ジョブ × イメージ単位の明細
  summary.csv  イメージ単位の集計(利用プロジェクト数・ジョブ数)
  errors.csv   取得・解析できなかったプロジェクト
"""
import argparse
import csv
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
import yaml

log = logging.getLogger("ci-images")

# ジョブではないトップレベルキー
RESERVED_KEYS = {
    "default", "include", "stages", "variables", "workflow", "image", "services",
    "cache", "before_script", "after_script", "types", "spec",
}

VAR_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


# --------------------------------------------------------------------------- #
# GitLab API クライアント
# --------------------------------------------------------------------------- #
class GitLab:
    def __init__(self, url, token, verify=True, timeout=60):
        self.base = url.rstrip("/") + "/api/v4"
        self.s = requests.Session()
        self.s.headers["PRIVATE-TOKEN"] = token
        self.s.verify = verify
        self.timeout = timeout

    def get(self, path_or_url, params=None):
        url = path_or_url if path_or_url.startswith("http") else self.base + path_or_url
        r = None
        for attempt in range(6):
            r = self.s.get(url, params=params, timeout=self.timeout)
            if r.status_code == 429 or r.status_code >= 500:
                try:
                    wait = int(r.headers.get("Retry-After", ""))
                except ValueError:
                    wait = 2 ** attempt
                log.debug("retry %s in %ss (status %s)", url, wait, r.status_code)
                time.sleep(wait)
                continue
            return r
        return r

    def paginate(self, path, params):
        """Link ヘッダーの rel=next を辿る(offset / keyset 両対応)"""
        r = self.get(path, dict(params, per_page=100))
        while True:
            r.raise_for_status()
            yield from r.json()
            nxt = r.links.get("next", {}).get("url")
            if not nxt:
                break
            r = self.get(nxt)


def list_projects(gl, args):
    base = {"archived": None if args.include_archived else "false"}
    base = {k: v for k, v in base.items() if v is not None}

    if args.project:
        for p in args.project:
            r = gl.get(f"/projects/{requests.utils.quote(p, safe='')}")
            if r.ok:
                yield r.json()
            else:
                log.warning("project %s: HTTP %s", p, r.status_code)
        return

    if args.group:
        path = f"/groups/{requests.utils.quote(args.group, safe='')}/projects"
        params = dict(base, include_subgroups="true", order_by="id", sort="asc")
    else:
        # 管理者トークンなら全プロジェクト。keyset で 50,000 件制限を回避
        path = "/projects"
        params = dict(base, pagination="keyset", order_by="id", sort="asc")
    yield from gl.paginate(path, params)


# --------------------------------------------------------------------------- #
# YAML 解析
# --------------------------------------------------------------------------- #
def norm_vars(v):
    """variables: の値を {name: str} に正規化(value/description 形式にも対応)"""
    out = {}
    if not isinstance(v, dict):
        return out
    for k, val in v.items():
        if isinstance(val, dict):
            val = val.get("value", "")
        if val is not None:
            out[str(k)] = str(val)
    return out


def image_name(v):
    if isinstance(v, dict):
        return v.get("name")
    return v


def inherits(inherit_value, key):
    """inherit:default / inherit:variables の評価(true/false/リスト)"""
    if inherit_value is None or inherit_value is True:
        return True
    if inherit_value is False:
        return False
    if isinstance(inherit_value, list):
        return key in inherit_value
    return True


def extract_images(merged_yaml):
    """
    merged_yaml から (job, kind, image_raw, source, variables) を列挙する。
    kind: image / service, source: job(ジョブで直接指定) / default(default・グローバル継承)
    """
    cfg = yaml.safe_load(merged_yaml) or {}
    if not isinstance(cfg, dict):
        return

    default = cfg.get("default") if isinstance(cfg.get("default"), dict) else {}
    g_image = default.get("image", cfg.get("image"))
    g_services = default.get("services", cfg.get("services"))
    g_vars = norm_vars(cfg.get("variables"))

    for name, job in cfg.items():
        if name in RESERVED_KEYS or str(name).startswith(".") or not isinstance(job, dict):
            continue
        if "trigger" in job:  # 子/マルチプロジェクトパイプラインのトリガージョブ
            continue
        if not any(k in job for k in ("script", "run")):
            continue

        inherit = job.get("inherit") if isinstance(job.get("inherit"), dict) else {}
        inh_default = inherit.get("default")
        inh_vars = inherit.get("variables")

        jvars = {}
        if inh_vars is None or inh_vars is True:
            jvars.update(g_vars)
        elif isinstance(inh_vars, list):
            jvars.update({k: v for k, v in g_vars.items() if k in inh_vars})
        jvars.update(norm_vars(job.get("variables")))

        # image
        if "image" in job:
            img, src = job["image"], "job"
        elif inherits(inh_default, "image"):
            img, src = g_image, "default"
        else:
            img, src = None, None
        if image_name(img):
            yield name, "image", str(image_name(img)), src, jvars

        # services
        if "services" in job:
            svcs, src = job["services"], "job"
        elif inherits(inh_default, "services"):
            svcs, src = g_services, "default"
        else:
            svcs, src = None, None
        for s in svcs or []:
            if image_name(s):
                yield name, "service", str(image_name(s)), src, jvars


def expand(s, variables, depth=5):
    for _ in range(depth):
        new = VAR_RE.sub(
            lambda m: variables[m.group(1)] if m.group(1) in variables else m.group(0), s
        )
        if new == s:
            break
        s = new
    return s


def parse_ref(ref):
    """イメージ参照を (registry, repository, tag, digest) に分解"""
    if not ref or "$" in ref:
        return "", "", "", ""
    name, digest = ref.split("@", 1) if "@" in ref else (ref, "")
    tag = ""
    if ":" in name.rsplit("/", 1)[-1]:
        name, tag = name.rsplit(":", 1)
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, repo = first, rest
    else:
        registry, repo = "docker.io", name
        if "/" not in repo:
            repo = "library/" + repo
    if not tag and not digest:
        tag = "latest"
    return registry, repo, tag, digest


def predefined_vars(project, server_url, registry_host):
    """Lint からは得られない定義済み変数のうち、プロジェクト情報から推定できるもの"""
    u = urlparse(server_url)
    host = u.hostname or ""
    port = u.port or (443 if u.scheme == "https" else 80)
    ns_full = project.get("namespace", {}).get("full_path", "")
    top_ns = ns_full.split("/")[0] if ns_full else ""
    path = project.get("path_with_namespace", "")
    dp_server = f"{host}:{port}"
    v = {
        "CI_SERVER_HOST": host,
        "CI_SERVER_PORT": str(port),
        "CI_SERVER_URL": server_url.rstrip("/"),
        "CI_PROJECT_PATH": path,
        "CI_PROJECT_NAMESPACE": ns_full,
        "CI_PROJECT_ROOT_NAMESPACE": top_ns,
        "CI_PROJECT_NAME": project.get("path", ""),
        "CI_DEFAULT_BRANCH": project.get("default_branch") or "",
        "CI_DEPENDENCY_PROXY_SERVER": dp_server,
        "CI_DEPENDENCY_PROXY_GROUP_IMAGE_PREFIX": f"{dp_server}/{top_ns}/dependency_proxy/containers",
        "CI_DEPENDENCY_PROXY_DIRECT_GROUP_IMAGE_PREFIX": f"{dp_server}/{ns_full}/dependency_proxy/containers",
    }
    if registry_host:
        v["CI_REGISTRY"] = registry_host
        v["CI_REGISTRY_IMAGE"] = f"{registry_host}/{path}".lower()
    return v


# --------------------------------------------------------------------------- #
# プロジェクト単位の処理
# --------------------------------------------------------------------------- #
def process_project(gl, project, args):
    pid = project["id"]
    ppath = project.get("path_with_namespace", str(pid))
    ref = args.ref or project.get("default_branch")
    rows, errors = [], []

    def err(reason, detail=""):
        errors.append({"project_id": pid, "project_path": ppath, "ref": ref or "",
                       "reason": reason, "detail": detail})
        return rows, errors

    if project.get("empty_repo"):
        return err("empty_repo")
    if project.get("builds_access_level") == "disabled" or project.get("jobs_enabled") is False:
        return err("ci_disabled")

    params = {}
    if ref:
        params["content_ref"] = ref
    if args.dry_run:
        params["dry_run"] = "true"
        params["include_jobs"] = "true"
        if ref:
            params["dry_run_ref"] = ref

    r = gl.get(f"/projects/{pid}/ci/lint", params)
    if r.status_code in (401, 403, 404):
        return err(f"http_{r.status_code}", r.text[:300])
    if not r.ok:
        return err(f"http_{r.status_code}", r.text[:300])

    data = r.json()
    merged = data.get("merged_yaml")
    if not merged:
        return err("lint_invalid" if not data.get("valid") else "no_merged_yaml",
                   " | ".join(data.get("errors") or [])[:500])

    dry_jobs = {j.get("name") for j in data.get("jobs") or []} if args.dry_run else None
    pvars = predefined_vars(project, args.url, args.registry_host)

    try:
        for job, kind, raw, src, jvars in extract_images(merged):
            resolved = expand(raw, {**pvars, **jvars})
            registry, repo, tag, digest = parse_ref(resolved)
            rows.append({
                "project_id": pid,
                "project_path": ppath,
                "ref": ref or "",
                "job": job,
                "kind": kind,
                "source": src,
                "image_raw": raw,
                "image_resolved": resolved,
                "unresolved": "$" in resolved,
                "registry": registry,
                "repository": repo,
                "tag": tag,
                "digest": digest,
                "in_dry_run": "" if dry_jobs is None else job in dry_jobs,
                "lint_valid": data.get("valid"),
            })
    except yaml.YAMLError as e:
        return err("yaml_parse_error", str(e)[:500])

    if not data.get("valid"):
        err("lint_invalid_partial", " | ".join(data.get("errors") or [])[:500])
    return rows, errors


# --------------------------------------------------------------------------- #
# 出力
# --------------------------------------------------------------------------- #
def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def summarize(rows):
    agg = {}
    for r in rows:
        key = r["image_resolved"]
        a = agg.setdefault(key, {
            "image_resolved": key, "registry": r["registry"], "repository": r["repository"],
            "tag": r["tag"], "digest": r["digest"], "unresolved": r["unresolved"],
            "kinds": set(), "projects": set(), "job_count": 0,
        })
        a["kinds"].add(r["kind"])
        a["projects"].add(r["project_path"])
        a["job_count"] += 1
    out = []
    for a in agg.values():
        projects = sorted(a.pop("projects"))
        a["kinds"] = ",".join(sorted(a["kinds"]))
        a["project_count"] = len(projects)
        a["projects"] = " ".join(projects)
        out.append(a)
    out.sort(key=lambda x: (-x["project_count"], x["image_resolved"]))
    return out


def main():
    ap = argparse.ArgumentParser(description="GitLab CI の利用コンテナイメージを CI Lint API で洗い出す")
    ap.add_argument("--url", default=os.environ.get("GITLAB_URL"), help="GitLab URL (既定: $GITLAB_URL)")
    ap.add_argument("--group", help="対象グループ (ID またはフルパス。サブグループ含む)")
    ap.add_argument("--project", action="append", help="対象プロジェクト (ID またはフルパス。複数指定可)")
    ap.add_argument("--ref", help="解析するブランチ/タグ (既定: 各プロジェクトのデフォルトブランチ)")
    ap.add_argument("--dry-run", action="store_true",
                    help="パイプライン作成をシミュレートし、rules 評価後に実行されるジョブを in_dry_run 列に出す(重い)")
    ap.add_argument("--registry-host", help="Container Registry のホスト名 ($CI_REGISTRY_IMAGE の展開用)")
    ap.add_argument("--include-archived", action="store_true", help="アーカイブ済みプロジェクトも対象にする")
    ap.add_argument("--workers", type=int, default=4, help="並列数 (既定: 4)")
    ap.add_argument("--limit", type=int, help="処理するプロジェクト数の上限 (試行用)")
    ap.add_argument("--out-dir", default="out", help="出力ディレクトリ (既定: ./out)")
    ap.add_argument("--insecure", action="store_true", help="TLS 証明書検証を無効化")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("GITLAB_TOKEN")
    if not args.url or not token:
        sys.exit("GITLAB_URL (--url) と GITLAB_TOKEN を設定してください")
    if args.insecure:
        requests.packages.urllib3.disable_warnings()

    gl = GitLab(args.url, token, verify=not args.insecure)
    os.makedirs(args.out_dir, exist_ok=True)

    projects = []
    for p in list_projects(gl, args):
        projects.append(p)
        if args.limit and len(projects) >= args.limit:
            break
    log.info("対象プロジェクト: %d 件", len(projects))

    all_rows, all_errors = [], []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process_project, gl, p, args): p for p in projects}
        for i, fut in enumerate(as_completed(futs), 1):
            p = futs[fut]
            try:
                rows, errors = fut.result()
            except Exception as e:  # noqa: BLE001
                rows, errors = [], [{"project_id": p["id"], "project_path": p.get("path_with_namespace"),
                                     "ref": "", "reason": "exception", "detail": repr(e)[:500]}]
            all_rows.extend(rows)
            all_errors.extend(errors)
            if i % 50 == 0 or i == len(projects):
                log.info("%d/%d 完了 (images=%d, errors=%d)", i, len(projects), len(all_rows), len(all_errors))

    all_rows.sort(key=lambda r: (r["project_path"], r["job"], r["kind"]))
    write_csv(os.path.join(args.out_dir, "images.csv"), all_rows, [
        "project_id", "project_path", "ref", "job", "kind", "source", "image_raw", "image_resolved",
        "unresolved", "registry", "repository", "tag", "digest", "in_dry_run", "lint_valid",
    ])
    write_csv(os.path.join(args.out_dir, "summary.csv"), summarize(all_rows), [
        "image_resolved", "registry", "repository", "tag", "digest", "unresolved", "kinds",
        "project_count", "job_count", "projects",
    ])
    write_csv(os.path.join(args.out_dir, "errors.csv"), all_errors,
              ["project_id", "project_path", "ref", "reason", "detail"])
    log.info("出力先: %s", os.path.abspath(args.out_dir))


if __name__ == "__main__":
    main()
