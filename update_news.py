
#!/usr/bin/env python3
"""Автоматичний збирач новин для ТИЦЬ."""
import json
import os
import re
import html
from datetime import datetime, timezone
from urllib.parse import quote, urlparse, urljoin

import requests
import feedparser
from bs4 import BeautifulSoup

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; TYTSNewsAggregator/1.1)",
    "Accept-Language": "uk-UA,uk;q=0.9"
})

QUERIES = {
    "local": '(Жмеринка OR Жмеринський OR Браїлів OR Северинівка OR Станіславчик) (site:zhmerynka.city OR site:zhmr.gov.ua OR site:zhmerynka-rda.gov.ua)',
    "region": '(Вінниця OR Вінниччина OR Вінницька область) (site:vin.gov.ua OR site:suspilne.media OR site:vn.20minut.ua)',
    "ukraine": '(Україна OR українці) (site:ukrinform.ua OR site:suspilne.media OR site:pravda.com.ua)'
}

ALLOWED = {
    "local": {"zhmerynka.city", "zhmr.gov.ua", "zhmerynka-rda.gov.ua"},
    "region": {"vin.gov.ua", "suspilne.media", "vn.20minut.ua"},
    "ukraine": {"ukrinform.ua", "suspilne.media", "pravda.com.ua"}
}

SOURCES = {
    "zhmerynka.city": "Жмеринка.City",
    "zhmr.gov.ua": "Жмеринська міська рада",
    "zhmerynka-rda.gov.ua": "Жмеринська РВА",
    "vin.gov.ua": "Вінницька ОВА",
    "suspilne.media": "Суспільне",
    "vn.20minut.ua": "20 хвилин. Вінниця",
    "ukrinform.ua": "Укрінформ",
    "pravda.com.ua": "Українська правда"
}

BLOCK = re.compile(
    r"((початок|кінець|відбій|оголошення|скасування|лунає|пролунала).{0,65}"
    r"(повітрян\w* тривог|сирен)|повітрян\w* тривог.{0,65}"
    r"(початок|кінець|відбій|оголошен|скасуван))",
    re.I
)

def clean(value):
    soup = BeautifulSoup(html.unescape(value or ""), "html.parser")
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()

def hostname(url):
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except (ValueError, TypeError):
        return ""

def accepted_host(url, area):
    host = hostname(url)
    if urlparse(url).scheme == "https" and host in ALLOWED[area]:
        return host
    return None

def resolve(link):
    if not link:
        return ""
    if hostname(link) not in {"news.google.com", "google.com"}:
        return link
    try:
        from googlenewsdecoder import gnewsdecoder
        result = gnewsdecoder(link, interval=1)
        if isinstance(result, dict):
            decoded = result.get("decoded_url", "")
            if decoded and hostname(decoded) not in {"news.google.com", "google.com"}:
                return decoded
    except Exception as exc:
        print("DECODER ERROR:", str(exc)[:180], flush=True)
    try:
        response = SESSION.get(link, timeout=14, allow_redirects=True)
        if hostname(response.url) not in {"news.google.com", "google.com"}:
            return response.url
    except requests.RequestException as exc:
        print("REDIRECT ERROR:", str(exc)[:180], flush=True)
    return link

def metadata(url):
    try:
        response = SESSION.get(url, timeout=14)
        response.raise_for_status()
        soup = BeautifulSoup(response.text[:700000], "html.parser")

        def get_meta(*selectors):
            for key, value in selectors:
                node = soup.find("meta", attrs={key: value})
                if node and node.get("content"):
                    return node["content"].strip()
            return ""

        image = get_meta(
            ("property", "og:image"),
            ("name", "twitter:image"),
            ("property", "twitter:image")
        )
        image = urljoin(url, image) if image else ""
        if not image.startswith("https://"):
            image = ""

        description = get_meta(
            ("name", "description"),
            ("property", "og:description")
        )
        return image, clean(description)[:320]
    except requests.RequestException as exc:
        print("ARTICLE ERROR:", hostname(url), str(exc)[:140], flush=True)
        return "", ""

def collect(area, query):
    rss = (
        "https://news.google.com/rss/search?q="
        + quote(query)
        + "&hl=uk&gl=UA&ceid=UA:uk"
    )
    try:
        response = SESSION.get(rss, timeout=25)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        entries = feed.entries
        print(area, "RSS entries:", len(entries), flush=True)
    except requests.RequestException as exc:
        print(area, "RSS ERROR:", str(exc), flush=True)
        return []

    articles = []
    rejected = 0

    for entry in entries[:50]:
        title = clean(entry.get("title", ""))
        if " - " in title:
            title = title.rsplit(" - ", 1)[0]
        if not title or BLOCK.search(title):
            continue

        article_url = resolve(entry.get("link", ""))
        host = accepted_host(article_url, area)

        if not host:
            rejected += 1
            if rejected <= 8:
                print(area, "SKIPPED URL:", article_url[:180], flush=True)
            continue

        published = entry.get("published_parsed") or entry.get("updated_parsed")
        date = (
            datetime(*published[:6], tzinfo=timezone.utc).isoformat()
            if published else ""
        )
        image, description = metadata(article_url)
        if not description:
            description = clean(entry.get("summary", ""))[:320]

        articles.append({
            "area": area,
            "title": title[:220],
            "url": article_url,
            "date": date,
            "excerpt": description,
            "image": image,
            "source": SOURCES[host]
        })
        print(area, "ACCEPTED:", host, "IMAGE:", bool(image), flush=True)

    print(
        area,
        "ACCEPTED COUNT:", len(articles),
        "REJECTED LINKS:", rejected,
        flush=True
    )
    return articles

def interleave(groups, limit=60):
    pattern = [
        "local", "region", "ukraine",
        "local", "region", "ukraine",
        "local", "region", "ukraine", "local"
    ]

    for items in groups.values():
        items.sort(key=lambda x: x["date"], reverse=True)

    result = []
    seen = set()

    while len(result) < limit:
        changed = False
        for area in pattern:
            while groups[area]:
                item = groups[area].pop(0)
                key = re.sub(r"\W+", "", item["title"].lower())[:110]
                if key in seen:
                    continue
                seen.add(key)
                result.append(item)
                changed = True
                break
            if len(result) >= limit:
                break
        if not changed:
            break

    return result

def main():
    groups = {
        area: collect(area, query)
        for area, query in QUERIES.items()
    }
    output = os.environ.get("NEWS_OUTPUT", "news.json")
    news = interleave(groups)

    if not news:
        print(
            "ERROR: No articles collected. "
            "Check RSS entries, SKIPPED URL and DECODER ERROR.",
            flush=True
        )
        raise SystemExit(1)

    data = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "distribution_target": {
            "local": 40,
            "region": 30,
            "ukraine": 30
        },
        "items": news
    }

    with open(output + ".tmp", "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)

    os.replace(output + ".tmp", output)
    print("SUCCESS: Saved", len(news), "articles", flush=True)

if __name__ == "__main__":
    main()
