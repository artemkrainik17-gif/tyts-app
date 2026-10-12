#!/usr/bin/env python3
"""ТИЦЬ: погодинний збирач прямих новин із перевіркою рубрик та фото.

Публікує лише метадані та URL оригінальних фото. Право показувати фото
у застосунку залежить від дозволу/ліцензії правовласника.
"""
import calendar
import json
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse, urlunparse

import feedparser
import requests
from bs4 import BeautifulSoup

SESSION = requests.Session()
SESSION.headers.update({
    'User-Agent': 'Mozilla/5.0 (compatible; TYTS-NewsReader/4.0; +https://github.com/artemkrainik17-gif/tyts-app)',
    'Accept-Language': 'uk-UA,uk;q=0.9,en;q=0.5',
})
TIMEOUT = 15
MAX_AGE_DAYS = 21
MAX_PER_SITE = 16
MAX_ITEMS = 40
MIN_ITEMS_TO_REPLACE = 3

# Використовуємо лише прямі сторінки та RSS, виявлені на сайті.
SITES = [
    ('local', 'Жмеринка.City', 'https://zhmerynka.city/articles', []),
    ('local', 'Жмеринська міська рада', 'https://zhmr.gov.ua/', []),
    ('local', 'Жмеринська РВА', 'https://zhmerynka-rda.gov.ua/', []),
    ('region', 'Вінницька ОВА', 'https://vin.gov.ua/news', []),
    ('region', '20 хвилин. Вінниця', 'https://vn.20minut.ua/', []),
    ('region', 'Суспільне Вінниця', 'https://suspilne.media/vinnytsia/', []),
    ('ukraine', 'Українська правда', 'https://www.pravda.com.ua/news/',
     ['https://www.pravda.com.ua/rss/view_news/']),
    ('ukraine', 'Укрінформ', 'https://www.ukrinform.ua/', []),
    ('ukraine', 'Суспільне', 'https://suspilne.media/', []),
]

# Не вважаємо кожну новину на сайті РВА суто жмеринською.
LOCAL_TERMS = re.compile(
    r'жмерин|браїл|северинів|станіславчик|шаргород|шаргоро|барськ|'
    r'копайгород|мураф|джурин|северинівськ|станіславчицьк', re.I)
REGION_TERMS = re.compile(
    r'вінниц|віннич|жмерин|гайсин|тульчин|хмільник|могилів[ -]подільськ|'
    r'козятин|калинівк|немирів|погребищ|липівець|томашпіл', re.I)
ALERTS = re.compile(
    r'(повітрян\w* тривог|сирен\w*).{0,70}(почат|відбі|заверш|оголош|скасув)|'
    r'(почат|відбі|заверш|оголош|скасув).{0,70}(повітрян\w* тривог|сирен\w*)', re.I)
UNWANTED = re.compile(r'на правах реклами|партнерськ\w* матеріал|рекламн\w* матеріал', re.I)
RUBRIC_TITLES = re.compile(
    r'^(останні новини|топ новини|усі новини|всі новини|головні новини|'
    r'новини (україни|вінниччини|вінниці)|euкраїна|війна росії проти україни\s*[—-]\s*усі новини)', re.I)
BAD_IMAGE = re.compile(r'(logo|favicon|avatar|sprite|placeholder|pixel|tracking|emoji|icon|banner|advert)', re.I)


def host(url):
    try:
        return (urlparse(url).hostname or '').lower().removeprefix('www.')
    except (TypeError, ValueError):
        return ''


def allowed(url, base):
    try:
        h, b = host(url), host(base)
        return urlparse(url).scheme == 'https' and bool(h) and (h == b or h.endswith('.' + b))
    except (TypeError, ValueError):
        return False


def canonical(url):
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path.rstrip('/') or '/', '', '', ''))


def get(url):
    response = SESSION.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return response


def clean(value):
    return re.sub(r'\s+', ' ', BeautifulSoup(str(value or ''), 'html.parser').get_text(' ', strip=True)).strip()


def iso_date(value):
    if not value:
        return ''
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        try:
            return parsedate_to_datetime(str(value)).astimezone(timezone.utc).isoformat()
        except (ValueError, TypeError, OverflowError):
            return ''


def meta(soup, *selectors):
    for attr, name in selectors:
        element = soup.find('meta', attrs={attr: name})
        if element and element.get('content'):
            return element['content'].strip()
    return ''


def article_url(url, base):
    """Відсікаємо сторінки-рубрики та пошуку до завантаження."""
    if not allowed(url, base):
        return False
    p = urlparse(url)
    path = p.path.rstrip('/').lower()
    if not path or path == '/':
        return False
    if re.search(r'/(tag|tags|category|categories|author|search|page|topics|event)/', path):
        return False
    if host(base) == 'vin.gov.ua':
        return bool(path.startswith('/news/') and path not in (
            '/news/ostanni-novyny', '/news/top-novyny', '/news/eukraina'))
    if host(base) == 'suspilne.media':
        return not ('/news/event/' in path or path in ('/vinnytsia', '/news'))
    if host(base) == 'pravda.com.ua':
        return bool(re.search(r'/news/20\d\d/\d\d/\d\d/\d+', path))
    if host(base) == 'zhmerynka.city':
        return bool(re.search(r'/articles/\d+', path))
    if host(base) == 'zhmerynka-rda.gov.ua':
        return bool(re.search(r'/20\d\d/\d\d/\d\d/[^/]+', path))
    return True


def photo_url(raw, page_url, published):
    if not isinstance(raw, str) or not raw.strip():
        return ''
    url = urljoin(page_url, raw.strip())
    p = urlparse(url)
    if p.scheme != 'https' or not p.hostname or BAD_IMAGE.search(p.path):
        return ''
    if p.path.lower().endswith('.svg'):
        return ''
    # Прибираємо Joomla-суфікс #joomlaImage://..., який не є частиною фото.
    url = urlunparse((p.scheme, p.netloc, p.path, p.params, p.query, ''))
    # Дуже старі медіафайли часто є шаблонними ілюстраціями, а не фото події.
    m = re.search(r'/uploads/(20\d\d)/(0[1-9]|1[0-2])/', p.path, re.I)
    if m and published:
        media_year, media_month = int(m.group(1)), int(m.group(2))
        date = datetime.fromisoformat(published)
        if (date.year - media_year) * 12 + date.month - media_month > 18:
            return ''
    return url


def jsonld_images(soup):
    result = []
    for script in soup.select('script[type="application/ld+json"]')[:12]:
        try:
            obj = json.loads(script.string or script.get_text() or '{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        stack = [obj]
        while stack:
            value = stack.pop()
            if isinstance(value, list):
                stack.extend(value[:35])
            elif isinstance(value, dict):
                typ = value.get('@type', '')
                types = ' '.join(map(str, typ)) if isinstance(typ, list) else str(typ)
                if any(t in types.lower() for t in ('newsarticle', 'article', 'reportage')):
                    pic = value.get('image') or value.get('thumbnailUrl')
                    if isinstance(pic, dict):
                        pic = pic.get('url') or pic.get('contentUrl')
                    if isinstance(pic, list):
                        pic = next((v.get('url', '') if isinstance(v, dict) else v
                                    for v in pic if v), '')
                    if isinstance(pic, str):
                        result.append(pic)
                for k in ('@graph', 'mainEntity'):
                    if k in value:
                        stack.append(value[k])
    return result


def image_from_article(soup, page_url, date, feed_image=''):
    candidates = [
        meta(soup, ('property', 'og:image'), ('name', 'twitter:image')),
        *jsonld_images(soup),
        feed_image,
    ]
    # Не збираємо довільні картинки з тіла сторінки. Лише hero/figure статті.
    for selector in ('article figure img', 'article img.wp-post-image',
                     '.article-header img', '.post-thumbnail img',
                     '.entry-header img', 'main article figure img'):
        for img in soup.select(selector)[:2]:
            for attribute in ('data-src', 'data-lazy-src', 'src'):
                if img.get(attribute):
                    candidates.append(img[attribute])
    for candidate in candidates:
        result = photo_url(candidate, page_url, date)
        if result:
            return result
    return ''


def extract(url, area, source, base, feed_title='', feed_date='', feed_description='', feed_image=''):
    if not article_url(url, base):
        return None
    try:
        response = get(url)
        if not article_url(response.url, base):
            return None
        soup = BeautifulSoup(response.text[:850000], 'html.parser')
        kind = meta(soup, ('property', 'og:type')).lower()
        if kind and kind not in ('article', 'news', 'newsarticle'):
            return None
        title = clean(meta(soup, ('property', 'og:title'), ('name', 'twitter:title')) or
                      (soup.title.get_text(' ', strip=True) if soup.title else feed_title))
        if not title or len(title) < 18 or RUBRIC_TITLES.search(title):
            return None
        description = clean(meta(soup, ('name', 'description'), ('property', 'og:description'))
                            or feed_description)
        if UNWANTED.search(title + ' ' + description) or ALERTS.search(title + ' ' + description):
            return None
        searchable = title + ' ' + description
        if area == 'local' and not LOCAL_TERMS.search(searchable):
            return None
        if area == 'region' and not REGION_TERMS.search(searchable):
            return None
        date_raw = meta(soup, ('property', 'article:published_time'),
                        ('itemprop', 'datePublished'), ('name', 'date'), ('name', 'pubdate')) or feed_date
        published = iso_date(date_raw)
        if not published:
            for node in soup.find_all('time', limit=3):
                published = iso_date(node.get('datetime'))
                if published:
                    break
        if not published:
            return None
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(published)).total_seconds() / 86400
        if age > MAX_AGE_DAYS or age < -1:
            return None
        image = image_from_article(soup, response.url, published, feed_image)
        return {'area': area, 'title': title[:200], 'url': canonical(response.url),
                'date': published, 'excerpt': description[:320],
                'image': image, 'source': source}
    except (requests.RequestException, ValueError) as exc:
        print('ARTICLE ERROR:', source, str(exc)[:130], flush=True)
        return None


def discover(homepage, base, configured):
    feeds = list(configured)
    links = []
    try:
        response = get(homepage)
        soup = BeautifulSoup(response.text[:1200000], 'html.parser')
        for node in soup.select('link[rel="alternate"]'):
            t = (node.get('type') or '').lower()
            if ('rss' in t or 'atom' in t) and node.get('href'):
                candidate = urljoin(response.url, node['href'])
                if allowed(candidate, base):
                    feeds.append(candidate)
        for anchor in soup.find_all('a', href=True, limit=700):
            candidate = urljoin(response.url, anchor['href'])
            if article_url(candidate, base) and candidate not in links:
                links.append(candidate)
    except requests.RequestException as exc:
        print('LISTING ERROR:', homepage, str(exc)[:130], flush=True)
    return list(dict.fromkeys(feeds))[:4], links[:MAX_PER_SITE * 4]


def from_feed(url, area, source, base):
    found = []
    try:
        response = get(url)
        feed = feedparser.parse(response.content)
        print('RSS', source, 'entries', len(feed.entries), flush=True)
        for entry in feed.entries[:MAX_PER_SITE * 3]:
            direct = entry.get('link', '')
            if not article_url(direct, base):
                continue
            published = iso_date(entry.get('published') or entry.get('updated'))
            if not published and entry.get('published_parsed'):
                published = datetime.fromtimestamp(
                    calendar.timegm(entry.published_parsed), timezone.utc).isoformat()
            pic = ''
            if entry.get('media_content'):
                pic = entry.media_content[0].get('url', '')
            article = extract(direct, area, source, base, entry.get('title', ''),
                              published, entry.get('summary', ''), pic)
            if article:
                found.append(article)
            if len(found) >= MAX_PER_SITE:
                break
    except requests.RequestException as exc:
        print('RSS ERROR:', source, str(exc)[:130], flush=True)
    return found


def collect(area, source, homepage, configured):
    base = 'https://' + host(homepage)
    feeds, links = discover(homepage, base, configured)
    result, seen = [], set()
    for feed in feeds:
        for item in from_feed(feed, area, source, base):
            if item['url'] not in seen:
                seen.add(item['url'])
                result.append(item)
    for link in links:
        if len(result) >= MAX_PER_SITE:
            break
        if canonical(link) in seen:
            continue
        article = extract(link, area, source, base)
        if article and article['url'] not in seen:
            seen.add(article['url'])
            result.append(article)
    print('SOURCE', source, 'feeds', len(feeds), 'candidates', len(links),
          'accepted', len(result), 'photos', sum(bool(x['image']) for x in result), flush=True)
    return result[:MAX_PER_SITE]


def interleave(groups, limit=MAX_ITEMS):
    pattern = ('local', 'region', 'ukraine', 'local', 'region', 'ukraine',
               'local', 'region', 'ukraine', 'local')
    for items in groups.values():
        items.sort(key=lambda x: x['date'], reverse=True)
    output, seen = [], set()
    while len(output) < limit:
        changed = False
        for area in pattern:
            while groups[area]:
                article = groups[area].pop(0)
                key = re.sub(r'\W+', '', article['title'].casefold())[:120]
                if key in seen:
                    continue
                seen.add(key)
                output.append(article)
                changed = True
                break
            if len(output) >= limit:
                break
        if not changed:
            break
    return output


def main():
    categories = {'local': [], 'region': [], 'ukraine': []}
    for area, source, homepage, configured in SITES:
        categories[area].extend(collect(area, source, homepage, configured))
    print('CANDIDATES', {k: len(v) for k, v in categories.items()}, flush=True)
    items = interleave(categories)
    if len(items) < MIN_ITEMS_TO_REPLACE:
        print('ERROR: Too few accepted articles; preserving existing news.json.', flush=True)
        raise SystemExit(1)
    counts = {area: sum(x['area'] == area for x in items) for area in categories}
    print('ACCEPTED', len(items), 'BY AREA', counts,
          'WITH PHOTOS', sum(bool(x['image']) for x in items), flush=True)
    output = os.getenv('NEWS_OUTPUT', 'news.json')
    data = {'updated_at': datetime.now(timezone.utc).isoformat(),
            'distribution_target': {'local': 40, 'region': 30, 'ukraine': 30},
            'items': items}
    with open(output + '.tmp', 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(output + '.tmp', output)
    print('SUCCESS: news.json updated', flush=True)


if __name__ == '__main__':
    main()
