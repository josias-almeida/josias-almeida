#!/usr/bin/env python3
"""Fetch GitHub profile statistics and write them into light_mode.svg / dark_mode.svg.

Configuration is env-var only:
  ACCESS_TOKEN     (required) - a GitHub token with `repo` (classic) or
                    Contents: Read + Metadata: Read (fine-grained) scopes.
  USER_NAME        (required) - the GitHub login to report on.
  BIRTHDAY         (optional) - YYYY-MM-DD. Falls back to the account's
                    createdAt date when unset.
  CURRENT_PROJECT  (optional) - text shown on the "Current project" row.
                    Defaults to "Mapstation" when unset.
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dateutil.parser import isoparse
from dateutil.relativedelta import relativedelta
from lxml import etree

GITHUB_API = "https://api.github.com/graphql"
SVG_NS = "http://www.w3.org/2000/svg"

# Total characters (label + dots + value) every stat row must add up to.
# The label text is read back from the SVG itself -- see update_svg() --
# so this is the only width knob that lives in Python.
ROW_WIDTH = 52

MAX_RETRIES = 6
INITIAL_BACKOFF_SECONDS = 2.0

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "cache"
SVG_FILES = (ROOT / "light_mode.svg", ROOT / "dark_mode.svg")

# stat key -> the `id` of its <text> row in the SVGs. These rows follow the
# generic label/dots/value contract (one or more label <tspan>s, one
# "row-dots" <tspan>, one value <tspan> -- see split_row_tspans()).
# "Lines of code" is handled separately by update_loc_row() below because
# it colors additions and deletions independently -- see the README for
# that row's own contract.
ROW_IDS = {
    "current_project": "row-project",
    "uptime": "row-uptime",
    "repositories": "row-repos",
    "contributed_to": "row-contributed",
    "stars": "row-stars",
    "commits": "row-commits",
}

LOC_ROW_ID = "row-loc"

VIEWER_QUERY = """
query {
  viewer {
    id
    login
    createdAt
  }
}
"""

# Single pass over every repo the user owns, collaborates on, or belongs to
# via an org. owner{login} lets us split "own" vs "contributed to" without a
# second query. history(first: 1) { totalCount } is a cheap way to get each
# repo's total commit count, used only to detect whether the repo changed
# since the last run.
REPOS_QUERY = """
query ($after: String) {
  viewer {
    repositories(first: 50, after: $after, ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]) {
      pageInfo { hasNextPage endCursor }
      nodes {
        nameWithOwner
        stargazerCount
        owner { login }
        defaultBranchRef {
          target {
            ... on Commit {
              history(first: 1) { totalCount }
            }
          }
        }
      }
    }
  }
}
"""

# Only run for repos whose total commit count changed. author: {id: $authorId}
# makes GitHub filter server-side, so an org repo with 20k commits and 12 of
# them mine comes back as 12 nodes instead of 20k.
REPO_HISTORY_QUERY = """
query ($owner: String!, $name: String!, $authorId: ID!, $after: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef {
      target {
        ... on Commit {
          history(first: 50, after: $after, author: { id: $authorId }) {
            totalCount
            pageInfo { hasNextPage endCursor }
            nodes { additions deletions }
          }
        }
      }
    }
  }
}
"""


class GitHubApiError(RuntimeError):
    pass


class RateLimitedError(GitHubApiError):
    pass


class ApiStats:
    def __init__(self):
        self.calls = 0


def env_required(name):
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def graphql(session, token, query, variables, stats):
    backoff = INITIAL_BACKOFF_SECONDS
    last_error = None
    for _ in range(MAX_RETRIES):
        stats.calls += 1
        try:
            resp = session.post(
                GITHUB_API,
                json={"query": query, "variables": variables},
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(backoff)
            backoff *= 2
            continue

        if resp.status_code == 403:
            raise RateLimitedError(
                "GitHub returned 403. This is almost always the undocumented "
                "secondary rate limit GitHub applies to large/expensive GraphQL "
                "queries, not the normal hourly quota. Whatever was already "
                "counted has been saved to the cache -- just run the script "
                "again (right away, or after a short wait) and it will pick up "
                "only the repositories that still need recounting."
            )

        if resp.status_code in (500, 502, 503):
            last_error = GitHubApiError(f"HTTP {resp.status_code} from GitHub")
            time.sleep(backoff)
            backoff *= 2
            continue

        resp.raise_for_status()
        payload = resp.json()
        if "errors" in payload and not payload.get("data"):
            raise GitHubApiError(str(payload["errors"]))
        return payload["data"]

    raise GitHubApiError(f"Exceeded {MAX_RETRIES} retries talking to GitHub: {last_error}")


def get_viewer(session, token, stats):
    return graphql(session, token, VIEWER_QUERY, {}, stats)["viewer"]


def list_repositories(session, token, stats):
    repos = []
    after = None
    while True:
        data = graphql(session, token, REPOS_QUERY, {"after": after}, stats)
        connection = data["viewer"]["repositories"]
        for node in connection["nodes"]:
            branch = node.get("defaultBranchRef") or {}
            target = branch.get("target") or {}
            history = target.get("history") or {}
            repos.append(
                {
                    "name_with_owner": node["nameWithOwner"],
                    "owner_login": node["owner"]["login"],
                    "stars": node["stargazerCount"],
                    "total_commits": history.get("totalCount", 0),
                }
            )
        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        after = page_info["endCursor"]
    return repos


def fetch_author_commit_stats(session, token, owner, name, author_id, stats):
    my_commits = 0
    additions = 0
    deletions = 0
    after = None
    while True:
        data = graphql(
            session,
            token,
            REPO_HISTORY_QUERY,
            {"owner": owner, "name": name, "authorId": author_id, "after": after},
            stats,
        )
        repo = data.get("repository")
        branch = (repo or {}).get("defaultBranchRef") or {}
        target = branch.get("target") or {}
        history = target.get("history")
        if history is None:
            # Empty repo, or default branch has no commits at all.
            break
        my_commits = history["totalCount"]
        for node in history["nodes"]:
            additions += node["additions"]
            deletions += node["deletions"]
        page_info = history["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        after = page_info["endCursor"]
    return my_commits, additions, deletions


def cache_path(user_name):
    digest = hashlib.sha256(user_name.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.json"


def repo_cache_key(name_with_owner):
    # Hashed, not the plain "owner/repo" name: this file is committed to a
    # public repository and must not leak the names of private repos.
    return hashlib.sha256(name_with_owner.encode("utf-8")).hexdigest()


def load_cache(path):
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


def save_cache(path, cache):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(cache, fh, indent=2, sort_keys=True)
    tmp_path.replace(path)


def collect_repo_stats(session, token, user_name, repos, author_id, stats, force_cache):
    path = cache_path(user_name)
    cache = {} if force_cache else load_cache(path)

    try:
        for repo in repos:
            key = repo_cache_key(repo["name_with_owner"])
            cached = cache.get(key)
            if cached is not None and cached.get("total_commits") == repo["total_commits"]:
                continue
            owner, name = repo["name_with_owner"].split("/", 1)
            my_commits, additions, deletions = fetch_author_commit_stats(
                session, token, owner, name, author_id, stats
            )
            cache[key] = {
                "total_commits": repo["total_commits"],
                "my_commits": my_commits,
                "additions": additions,
                "deletions": deletions,
            }
    finally:
        # Always persist what we have, even if a request above raised --
        # a re-run should resume, not start over.
        save_cache(path, cache)

    return cache


def compute_uptime(start_date):
    now = datetime.now(timezone.utc)
    delta = relativedelta(now, start_date)
    parts = []
    if delta.years:
        parts.append(f"{delta.years} year{'s' if delta.years != 1 else ''}")
    if delta.months:
        parts.append(f"{delta.months} month{'s' if delta.months != 1 else ''}")
    if delta.days or not parts:
        parts.append(f"{delta.days} day{'s' if delta.days != 1 else ''}")
    return ", ".join(parts)


def resolve_start_date(birthday_raw, created_at_raw):
    if birthday_raw:
        try:
            return datetime.strptime(birthday_raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            raise SystemExit(f"BIRTHDAY must be in YYYY-MM-DD format, got {birthday_raw!r}")
    return isoparse(created_at_raw)


def compute_dots(width, label, value):
    """Fill characters between a label and a value so every row is `width`
    characters wide (label + dots + value). Only works because the SVG uses
    a monospace font -- alignment here is pure character counting, not px.
    """
    remaining = width - len(label) - len(value)
    if remaining <= 0:
        # Value too long to leave room for a single dot -- just butt them
        # together instead of producing a negative-length string.
        return ""
    if remaining == 1:
        return " "
    return " " + ("." * (remaining - 2)) + " "


def split_row_tspans(path, row_id, text_el):
    """Split a row's <tspan> children into (label_tspans, dots_span,
    value_tspans), using the "row-dots" class as the pivot. A label can be
    made of several <tspan>s (e.g. orange text plus a differently-colored
    "." separator like "Contributed.to:") -- only the dots <tspan> and the
    value/label split are structural, so this is what every row-parsing
    function shares.
    """
    tspans = text_el.findall(f"{{{SVG_NS}}}tspan")
    dots_positions = [i for i, t in enumerate(tspans) if "row-dots" in (t.get("class") or "").split()]
    if len(dots_positions) != 1:
        raise SystemExit(
            f"{path}: row {row_id!r} must have exactly one <tspan class=\"row-dots\">, "
            f"found {len(dots_positions)}"
        )
    idx = dots_positions[0]
    label_tspans, dots_span, value_tspans = tspans[:idx], tspans[idx], tspans[idx + 1:]
    if not label_tspans:
        raise SystemExit(f"{path}: row {row_id!r} has no label <tspan> before its dots")
    return label_tspans, dots_span, value_tspans


def update_loc_row(path, root, net, additions, deletions):
    """Fill the "Lines of code" row, which breaks from the generic
    single-value contract: additions and deletions are colored
    independently, so they each need their own <tspan>. After the label and
    dots, the row has exactly 5 value <tspan>s: a "net (" prefix, additions,
    ", ", deletions, and a closing ")".
    """
    text_el = root.find(f".//{{{SVG_NS}}}text[@id='{LOC_ROW_ID}']")
    if text_el is None:
        raise SystemExit(f"{path}: no <text id=\"{LOC_ROW_ID}\"> row -- SVG/script contract broken")
    label_tspans, dots_span, value_tspans = split_row_tspans(path, LOC_ROW_ID, text_el)
    if len(value_tspans) != 5:
        raise SystemExit(
            f"{path}: row {LOC_ROW_ID!r} has {len(value_tspans)} value <tspan>s, expected exactly 5 "
            "(net-prefix, additions, separator, deletions, close-paren)"
        )
    net_span, add_span, sep_span, del_span, close_span = value_tspans
    label = "".join(t.text or "" for t in label_tspans)

    net_prefix = f"{net:,} ("
    add_text = f"{additions:,}++"
    sep_text = ", "
    del_text = f"{deletions:,}--"
    close_text = ")"

    # compute_dots only needs the *length* of the full value, so the pieces
    # are joined the same way they'll actually be laid out on screen.
    full_value = net_prefix + add_text + sep_text + del_text + close_text
    dots_span.text = compute_dots(ROW_WIDTH, label, full_value)
    net_span.text = net_prefix
    add_span.text = add_text
    sep_span.text = sep_text
    del_span.text = del_text
    close_span.text = close_text


def update_svg(path, values):
    parser = etree.XMLParser(remove_blank_text=False)
    tree = etree.parse(str(path), parser)
    root = tree.getroot()

    for key, row_id in ROW_IDS.items():
        text_el = root.find(f".//{{{SVG_NS}}}text[@id='{row_id}']")
        if text_el is None:
            raise SystemExit(f"{path}: no <text id=\"{row_id}\"> row -- SVG/script contract broken")
        label_tspans, dots_span, value_tspans = split_row_tspans(path, row_id, text_el)
        if len(value_tspans) != 1:
            raise SystemExit(
                f"{path}: row {row_id!r} has {len(value_tspans)} value <tspan>s, expected exactly 1"
            )
        label = "".join(t.text or "" for t in label_tspans)
        value = values[key]
        dots_span.text = compute_dots(ROW_WIDTH, label, value)
        value_tspans[0].text = value

    update_loc_row(
        path,
        root,
        values["lines_of_code_net"],
        values["lines_of_code_additions"],
        values["lines_of_code_deletions"],
    )

    tree.write(str(path), encoding="UTF-8", xml_declaration=False)


def format_values(viewer, repos, cache, start_date, current_project):
    own_repos = [r for r in repos if r["owner_login"] == viewer["login"]]
    contributed_repos = [
        r
        for r in repos
        if r["owner_login"] != viewer["login"]
        and cache.get(repo_cache_key(r["name_with_owner"]), {}).get("my_commits", 0) > 0
    ]

    stars = sum(r["stars"] for r in own_repos)

    commits_total = 0
    additions_total = 0
    deletions_total = 0
    for repo in repos:
        entry = cache.get(repo_cache_key(repo["name_with_owner"]))
        if not entry:
            continue
        commits_total += entry["my_commits"]
        additions_total += entry["additions"]
        deletions_total += entry["deletions"]
    net_loc = additions_total - deletions_total

    return {
        "current_project": current_project,
        "uptime": compute_uptime(start_date),
        "repositories": f"{len(own_repos):,}",
        "contributed_to": f"{len(contributed_repos):,}",
        "stars": f"{stars:,}",
        "commits": f"{commits_total:,}",
        "lines_of_code_net": net_loc,
        "lines_of_code_additions": additions_total,
        "lines_of_code_deletions": deletions_total,
    }


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force-cache",
        action="store_true",
        help="Ignore the on-disk cache and recount every repository from scratch",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    token = env_required("ACCESS_TOKEN")
    user_name = env_required("USER_NAME")
    birthday_raw = os.environ.get("BIRTHDAY")
    # `or` (not .get(..., default)) so an unset GitHub Actions `vars.*`
    # value -- which arrives as an empty string, not a missing env var --
    # still falls back to the default.
    current_project = os.environ.get("CURRENT_PROJECT") or "Mapstation"

    stats = ApiStats()
    start = time.monotonic()
    session = requests.Session()

    try:
        viewer = get_viewer(session, token, stats)
        repos = list_repositories(session, token, stats)
        cache = collect_repo_stats(
            session, token, user_name, repos, viewer["id"], stats, args.force_cache
        )
    except GitHubApiError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    start_date = resolve_start_date(birthday_raw, viewer["createdAt"])
    values = format_values(viewer, repos, cache, start_date, current_project)

    for svg_path in SVG_FILES:
        update_svg(svg_path, values)

    elapsed = time.monotonic() - start
    print(f"Updated {', '.join(p.name for p in SVG_FILES)}")
    print(f"Repositories seen: {len(repos)}  API calls: {stats.calls}  Elapsed: {elapsed:.1f}s")
    for key, row_id in ROW_IDS.items():
        print(f"  {row_id}: {values[key]}")
    print(
        f"  {LOC_ROW_ID}: {values['lines_of_code_net']:,} "
        f"(+{values['lines_of_code_additions']:,}, -{values['lines_of_code_deletions']:,})"
    )


if __name__ == "__main__":
    main()
