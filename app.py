# app.py  — Slackスレッド展開 + Block Kit化 + GitHub Pages出力（チャンネルID安全抽出対応）
import os, re, time, random, collections
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import yaml
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from dotenv import load_dotenv

# =======================
# 環境変数・定数
# =======================
load_dotenv()
JST = timezone(timedelta(hours=9))

TOP_K = int(os.getenv("TOP_K", "10"))
POST_LIMIT_PER_SOURCE = int(os.getenv("POST_LIMIT_PER_SOURCE", "5"))

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL_ID_RAW = os.environ.get("SLACK_CHANNEL_ID", "")  # C/Gで始まるID推奨（#nameでも可）
PAGES_BASE = os.getenv("PAGES_BASE", "https://<yourname>.github.io/trend-keywords-bot")

HEADERS = {"User-Agent": "trend-keywords-bot (+https://github.com/your/repo)"}
TIMEOUT = 15

# チャンネルID抽出（コメント混入対策）
ID_RE = re.compile(r"\b[CG][A-Z0-9]{8,}\b")

# 技術っぽいトークン抽出
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9#\+\.\-]{1,30}")
STOPWORDS = set("""
the of and for with from this that what when where how why who whose your you into out are is in on to a an by at as or not
home about login signup tags jobs help privacy terms stackexchange stackoverflow qiita github trending follow issue pull
""".split())


# =======================
# ユーティリティ
# =======================
def must_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"ENV '{name}' is required but not set")
    return v

def load_sources():
    with open("sources.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def fetch_html(url):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text

def fetch_rss(url):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text

def dedup(items):
    seen = set()
    out = []
    for src, title, link in items:
        key = (title.lower(), link)
        if key in seen:
            continue
        seen.add(key)
        out.append((src, title, link))
    return out


# =======================
# 収集・パース
# =======================
def parse_qiita_trend(html):
    soup = BeautifulSoup(html, "lxml")
    items = []
    for a in soup.select("a[href^='/articles/']"):
        title = a.get_text(strip=True)
        link = "https://qiita.com" + a.get("href")
        if title and link:
            items.append(("Qiita", title, link))
    return dedup(items)

def parse_stackoverflow_hot(html):
    soup = BeautifulSoup(html, "lxml")
    items = []
    for q in soup.select("a.question-hyperlink"):
        title = q.get_text(strip=True)
        href = q.get("href")
        if not href:
            continue
        link = href if href.startswith("http") else "https://stackexchange.com" + href
        items.append(("StackOverflow", title, link))
    return dedup(items)

def parse_github_trending(html):
    soup = BeautifulSoup(html, "lxml")
    items = []
    for repo in soup.select("article h2 a"):
        title = repo.get_text(" ", strip=True)
        href = repo.get("href")
        if not href:
            continue
        link = "https://github.com" + href
        items.append(("GitHubTrending", title, link))
    # 説明文もキーワード抽出用に拾う（リンクなし）
    for p in soup.select("article p"):
        t = p.get_text(" ", strip=True)
        if t:
            items.append(("GitHubTrending", t, ""))
    return dedup(items)

def parse_rss_generic(xml_text, source_name):
    soup = BeautifulSoup(xml_text, "xml")
    items = []
    for it in soup.find_all(["item", "entry"]):
        title_tag = it.title
        link_tag = it.link
        title = title_tag.get_text(strip=True) if title_tag else ""
        link = ""
        if link_tag:
            link = link_tag.get("href") or link_tag.get_text(strip=True)
        if title:
            items.append((source_name, title, link))
    return dedup(items)


# =======================
# 解析（キーワード抽出）
# =======================
def extract_tokens(text):
    tokens = []
    for m in TOKEN_RE.finditer(text):
        w = m.group(0)
        lw = w.lower()
        if lw in STOPWORDS or lw.isdigit() or len(w) <= 1:
            continue
        tokens.append(w)
    return tokens

def analyze(items):
    counter = collections.Counter()
    for _, title, _ in items:
        for t in extract_tokens(title):
            counter[t] += 1
    top = counter.most_common(TOP_K)
    related = {k: [] for k, _ in top}
    for src, title, link in items:
        toks = set(extract_tokens(title))
        for k, _ in top:
            if k in toks and link and len(related[k]) < POST_LIMIT_PER_SOURCE:
                related[k].append((src, title, link))
    return top, related


# =======================
# GitHub Pages 用 Markdown
# =======================
def save_markdown(top_list, related_map, date_label):
    Path("docs").mkdir(exist_ok=True)
    fname = f"docs/{date_label}.md"
    lines = [f"# 今週のトレンド技術キーワード（{date_label}）", ""]
    for i, (k, c) in enumerate(top_list, 1):
        lines.append(f"**{i}. {k}** — {c}件")
    lines.append("\n---\n")
    for k, _ in top_list:
        links = related_map.get(k, [])
        if not links:
            continue
        lines.append(f"## {k} の関連トピック")
        for (src, title, link) in links:
            if link:
                lines.append(f"- [{title}]({link}) _{src}_")
        lines.append("")
    Path(fname).write_text("\n".join(lines), encoding="utf-8")

    # index.md へ追記（先頭に最新を差し込み）
    idx = Path("docs/index.md")
    base = ["# 週次アーカイブ", ""]
    if idx.exists():
        base = idx.read_text(encoding="utf-8").splitlines()
    rel = f"- [{date_label}](./{date_label}.html)"
    if rel not in base:
        base.insert(2, rel)
    idx.write_text("\n".join(base), encoding="utf-8")
    return fname


# =======================
# Slack Block Kit（見た目強化）
# =======================
def build_parent_blocks(ranking_lines, page_url, date_label):
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "今週のトレンド技術キーワード"}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"*集計日*: {date_label}"},
            {"type": "mrkdwn", "text": f"*詳細*: <{page_url}|ページを見る>"}
        ]},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(ranking_lines[:10])}},
    ]

def build_thread_blocks(keyword, links):
    bullets = "\n".join([f"• <{u}|{t}>  _{s}_" for (s, t, u) in links if u])
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{keyword}* の関連トピック"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": bullets or "_関連リンクが見つかりませんでした_"}},
    ]


# =======================
# Slack チャンネル解決 & 投稿
# =======================
def resolve_channel_id(raw: str) -> str:
    """C/Gで始まるIDならコメント付きでも正しく抽出。名前ならそのまま返す（権限なくてもID直指定ならOK）。"""
    if not raw:
        raise RuntimeError("SLACK_CHANNEL_ID is empty")
    s = raw.strip()
    m = ID_RE.search(s.upper())
    if m:
        return m.group(0)
    return s.lstrip("#").split()[0]  # ここから先は名前扱い（conversations.readが無いと失敗あり）

def post_to_slack(top_list, related_map, date_label, page_url):
    client = WebClient(token=SLACK_BOT_TOKEN)
    channel_id = resolve_channel_id(SLACK_CHANNEL_ID_RAW)

    # 親メッセージ（Block Kit）
    ranking_lines = [f"{i}. *{k}* — {c}件" for i, (k, c) in enumerate(top_list, 1)]
    parent_blocks = build_parent_blocks(ranking_lines, page_url, date_label)

    try:
        resp = client.chat_postMessage(
            channel=channel_id,
            blocks=parent_blocks,
            text="今週のトレンド技術キーワード"  # フォールバック
        )
        thread_ts = resp["ts"]
    except SlackApiError as e:
        raise RuntimeError(f"chat.postMessage failed: {e.response.get('error')}")

    # スレッド（上位3件だけ詳細、Block Kit）
    for k, _ in top_list[:3]:
        links = related_map.get(k, [])[:5]
        if not links:
            continue
        thread_blocks = build_thread_blocks(k, links)
        try:
            client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                blocks=thread_blocks,
                text=f"{k} の関連トピック"
            )
        except SlackApiError as e:
            print(f"[warn] thread post failed for {k}: {e.response.get('error')}")


# =======================
# メイン
# =======================
def main():
    must_env("SLACK_BOT_TOKEN")
    must_env("SLACK_CHANNEL_ID")

    sources = load_sources()
    items = []

    # HTML系
    for s in sources.get("html", []):
        try:
            html = fetch_html(s["url"])
            if s["name"] == "qiita_trend":
                items.extend(parse_qiita_trend(html))
            elif s["name"] == "stackoverflow_hot":
                items.extend(parse_stackoverflow_hot(html))
            elif s["name"] == "github_trending":
                items.extend(parse_github_trending(html))
        except Exception as e:
            print(f"[warn] HTML fetch failed {s['name']}: {e}")
            continue
        time.sleep(random.uniform(1.0, 2.0))

    # RSS系
    for s in sources.get("rss", []):
        try:
            xml = fetch_rss(s["url"])
            items.extend(parse_rss_generic(xml, s["name"]))
        except Exception as e:
            print(f"[warn] RSS fetch failed {s['name']}: {e}")
            continue
        time.sleep(random.uniform(0.5, 1.0))

    if not items:
        print("No items fetched. Skipping Slack post and Pages update.")
        return

    # 解析
    top_list, related_map = analyze(items)
    if not top_list:
        print("No keywords extracted. Skipping post.")
        return

    date_label = datetime.now(JST).strftime("%Y-%m-%d")

    # Markdown保存（Pages）
    save_markdown(top_list, related_map, date_label)
    page_url = f"{PAGES_BASE}/{date_label}.html"  # テーマにより .md のまま表示可能

    # Slack投稿（親＋スレッド、Block Kit）
    post_to_slack(top_list, related_map, date_label, page_url)
    print("Done.")


if __name__ == "__main__":
    main()
