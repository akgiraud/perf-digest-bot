"""
Daily paid-media performance digest -> Telegram.

Collects the last N hours of posts from marketing-expert sources (RSS / Reddit),
asks Claude to pick and summarize the most important topics on paid-campaign
performance and measurement (in Arabic), and sends the digest to Telegram.

Usage:
  python bot.py                  # collect, summarize, send
  python bot.py --dry-run        # print the digest instead of sending
  python bot.py --check-sources  # show how many fresh items each source returned

Env vars: ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (comma-separated for several chats)
"""

import argparse
import html
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import anthropic
import feedparser
import requests
import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (compatible; PerfDigestBot/1.0; +https://github.com)"
TG_LIMIT = 3900  # Telegram max is 4096 chars per message; keep a margin

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("digest")


# ---------- collection ----------

def load_config():
    with open(os.path.join(ROOT, "sources.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def clean(text, limit):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def entry_time(entry):
    for key in ("published_parsed", "updated_parsed"):
        if entry.get(key):
            return datetime(*entry[key][:6], tzinfo=timezone.utc)
    return None


def matches_keywords(text, keywords):
    text = text.lower()
    return any(k.lower() in text for k in keywords)


def fetch_source(src, since, keywords):
    r = requests.get(src["url"], headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    items = []
    for e in feed.entries[:50]:
        t = entry_time(e)
        if t is None or t < since:
            continue
        title = clean(e.get("title", ""), 200)
        summary = clean(e.get("summary") or e.get("description", ""), 600)
        if src.get("filter") and not matches_keywords(f"{title} {summary}", keywords):
            continue
        items.append({
            "source": src["name"],
            "title": title,
            "summary": summary,
            "link": e.get("link", ""),
            "time": t,
        })
    return items


def collect(cfg, verbose=False):
    since = datetime.now(timezone.utc) - timedelta(hours=cfg.get("hours_back", 24))
    keywords = cfg.get("keywords", [])
    all_items, seen = [], set()
    ok_sources = 0
    for src in cfg["sources"]:
        try:
            items = fetch_source(src, since, keywords)
            ok_sources += 1
            if verbose:
                print(f"✔ {src['name']}: {len(items)} items")
        except Exception as ex:  # one broken feed must not stop the digest
            log.warning("source failed: %s (%s)", src["name"], ex)
            if verbose:
                print(f"✘ {src['name']}: {ex}")
            continue
        for it in items:
            key = (it["link"].split("?")[0].rstrip("/") or it["title"]).lower()
            if key in seen:
                continue
            seen.add(key)
            all_items.append(it)
    all_items.sort(key=lambda x: x["time"], reverse=True)
    return all_items[: cfg.get("max_items_total", 120)], ok_sources


# ---------- summarization ----------

NO_TOPICS = "NO_RELEVANT_TOPICS"

SYSTEM_PROMPT = """You are a senior performance-marketing analyst writing a daily briefing for the performance team at stc, Saudi Arabia's largest telecom operator. The team runs paid campaigns (Google, Meta, Snapchat, TikTok, X, YouTube, programmatic) and owns campaign performance.

stc's business lines the ideas must fit:
{business_lines}

You receive posts and articles published in the last 24 hours by marketing experts, practitioners and industry outlets. Each item has an id in square brackets.

STRICT SELECTION. Include an item only if it passes ALL three tests:
1) It is about IMPROVING the performance of paid campaigns: a new idea, a new tool or feature, or a new method/tactic that can raise conversions, lower CPA/CAC, raise ROAS, improve CTR/CVR, scale efficiently, or measure performance more accurately so it can be optimized (e.g. incrementality tests, conversion tracking that feeds bidding).
2) It is NEW and ACTIONABLE: the team could test or apply it. Test results with numbers, step-by-step tactics and new platform features count.
3) It clearly fits at least one stc business line above (e.g. subscription/lead-gen offers, app installs and activation, e-commerce device sales with catalogs and shopping ads, prepaid top-up and acquisition, travel/roaming offers around seasons).

REJECT anything else, including: general industry or business news, company earnings, acquisitions, people moves, events and conferences, opinion or trend pieces with no concrete tactic, SEO/organic-only content, B2B/agency-management topics, brand awareness with no performance angle, and anything that only fits unrelated industries.

Merge items about the same idea. Use only what the items say; never invent numbers, dates or claims. Quality over quantity: write at most {max_topics} topics, and fewer if only a few pass. If none pass, reply with exactly {no_topics} and nothing else.

Write in clear professional Arabic. Keep technical terms and platform/product names in English (ROAS, CPA, PMax, Advantage+, GA4...).

Use exactly this structure for each topic:
<b>N. عنوان الفكرة</b>
🏷️ النوع: فكرة / أداة / طريقة — and the platform if any.
📌 الفكرة: one or two sentences on what it is and the result if the items give one.
📱 تناسب: which stc line(s) and how, in one sentence (e.g. متجر الأجهزة: ...).
✅ كيف نطبقها: one concrete test or action the team can run.
🔗 [[id]] [[id]]

Separate topics with one blank line. After the last topic add:
<b>💡 أهم فكرة نجربها هالأسبوع:</b> one sentence naming the single best idea to test first.

Formatting rules: plain text with only <b> and <i> tags. No markdown, no other tags, no URLs (sources are referenced only as [[id]]). Keep the whole digest under 3000 characters."""


def build_system_prompt(cfg):
    lines = "\n".join(f"- {b}" for b in cfg.get("business_lines", []))
    return SYSTEM_PROMPT.format(
        business_lines=lines,
        max_topics=cfg.get("max_topics", 6),
        no_topics=NO_TOPICS,
    )


def build_user_prompt(items):
    lines = []
    for i, it in enumerate(items, 1):
        lines.append(f"[{i}] ({it['source']}) {it['title']}\n{it['summary']}")
    return "Items from the last 24 hours:\n\n" + "\n\n".join(lines)


def summarize(cfg, items):
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=cfg.get("model", "claude-sonnet-5-5"),
        max_tokens=4000,
        system=build_system_prompt(cfg),
        messages=[{"role": "user", "content": build_user_prompt(items)}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


# ---------- formatting & sending ----------

def to_telegram_html(text, items):
    # escape everything, then restore only the tags we allow
    safe = html.escape(text, quote=False)
    safe = re.sub(r"&lt;(/?)(b|i)&gt;", r"<\1\2>", safe)

    # replace [[id]] references with real links (URLs never come from the model)
    def link(m):
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(items) and items[idx]["link"]:
            it = items[idx]
            return f'<a href="{html.escape(it["link"], quote=True)}">{html.escape(it["source"])}</a>'
        return ""

    safe = re.sub(r"\[\[(\d+)\]\]", link, safe)
    return safe


def split_message(text, limit=TG_LIMIT):
    parts, current = [], ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        while len(block) > limit:  # very long block: hard split
            parts.append(block[:limit])
            block = block[limit:]
        current = block
    if current:
        parts.append(current)
    return parts


def send_telegram(token, chat_ids, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chat_id in chat_ids:
        for part in split_message(text):
            r = requests.post(url, json={
                "chat_id": chat_id,
                "text": part,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=30)
            if r.status_code == 200:
                continue
            log.warning("HTML send failed (%s), retrying as plain text", r.text[:200])
            plain = html.unescape(re.sub(r"<[^>]+>", "", part))
            r = requests.post(url, json={
                "chat_id": chat_id, "text": plain, "disable_web_page_preview": True,
            }, timeout=30)
            r.raise_for_status()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check-sources", action="store_true")
    ap.add_argument("--chat", help="send only to this chat id (on-demand report)")
    args = ap.parse_args()

    cfg = load_config()
    items, ok_sources = collect(cfg, verbose=args.check_sources)
    if args.check_sources:
        print(f"\nTotal fresh items: {len(items)} from {ok_sources}/{len(cfg['sources'])} working sources")
        return

    tz = timezone(timedelta(hours=cfg.get("timezone_offset_hours", 3)))
    today = datetime.now(tz).strftime("%Y-%m-%d")
    header = f"<b>🚀 أفكار تحسين أداء الحملات — stc</b>\n<i>{today}</i>"

    empty = "ما فيه أفكار أو أدوات جديدة لتحسين الأداء تناسب مجالاتنا خلال آخر 24 ساعة."
    if not items:
        body = empty
    else:
        summary = summarize(cfg, items)
        body = empty if NO_TOPICS in summary else to_telegram_html(summary, items)

    footer = f"<i>تمت مراجعة {len(items)} مقال/نقاش من {ok_sources} مصدر</i>"
    message = f"{header}\n\n{body}\n\n{footer}"

    if args.dry_run:
        print(message)
        return

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    if args.chat:
        chat_ids = [args.chat]
    else:
        chat_ids = [c.strip() for c in os.environ["TELEGRAM_CHAT_ID"].split(",") if c.strip()]
    send_telegram(token, chat_ids, message)
    log.info("digest sent to %d chat(s)", len(chat_ids))


if __name__ == "__main__":
    try:
        main()
    except KeyError as ex:
        sys.exit(f"Missing environment variable: {ex}")
