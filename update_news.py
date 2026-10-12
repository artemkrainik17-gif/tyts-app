#!/usr/bin/env python3
"""ТИЦЬ: агрегатор прямих новинних сторінок/RSS без Google News.
Не завантажує зображення на сервер: image — лише адреса з метаданих статті.
Право відображати фотографії в застосунку треба узгодити з правовласниками.
"""
import calendar
import json
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse, urlunparse

import feedparser
import requests
from bs4 import BeautifulSoup

S = requests.Session()
S.headers.update({'User-Agent': 'TYTS-NewsReader/2.0 (+https://github.com/artemkrainik17-gif/tyts-app)',
                  'Accept-Language': 'uk-UA,uk;q=0.9,en;q=0.5'})
TIMEOUT = 15
MAX_AGE_DAYS = 21
MAX_PER_SITE = 15
MAX_ITEMS = 40

# Лише підтверджені сайти. RSS вказуємо там, де адресу знайдено на офіційній сторінці.
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
LOCAL_TERMS = re.compile(r'жмерин|браїл|северинів|станіславчик|шаргород|барськ|копайгород|мураф|джурин', re.I)
REGION_TERMS = re.compile(r'вінниц|віннич|жмерин|гайсин|тульчин|хмільник|могилів.подільськ', re.I)
ALERTS = re.compile(r'(повітрян\w* тривог|сирен\w*).{0,75}(почат|відбі|заверш|оголош|скасув)|'
                    r'(почат|відбі|заверш|оголош|скасув).{0,75}(повітрян\w* тривог|сирен\w*)', re.I)
EXCLUDE = re.compile(r'реклама|партнерськ\w* матеріал|на правах реклами', re.I)

def host(u):
    try: return (urlparse(u).hostname or '').lower().removeprefix('www.')
    except ValueError: return ''

def allowed(u, base):
    h, b = host(u), host(base)
    return urlparse(u).scheme == 'https' and (h == b or h.endswith('.' + b))

def canonical(u):
    p = urlparse(u)
    return urlunparse((p.scheme, p.netloc, p.path.rstrip('/') or '/', '', '', ''))

def get(u):
    r = S.get(u, timeout=TIMEOUT)
    r.raise_for_status()
    return r

def text(v):
    return re.sub(r'\s+', ' ', BeautifulSoup(str(v or ''), 'html.parser').get_text(' ', strip=True)).strip()

def iso_date(raw):
    if not raw: return ''
    try:
        d = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
        return d.replace(tzinfo=timezone.utc).isoformat() if d.tzinfo is None else d.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        try: return parsedate_to_datetime(str(raw)).astimezone(timezone.utc).isoformat()
        except (ValueError, TypeError, OverflowError): return ''

def meta(soup, *selectors):
    for attr, name in selectors:
        el = soup.find('meta', attrs={attr: name})
        if el and el.get('content'): return el['content'].strip()
    return ''

def extract_article(url, area, source, base, feed_title='', feed_date='', feed_description='', feed_image=''):
    if not allowed(url, base): return None
    try:
        r = get(url)
        if not allowed(r.url, base): return None
        soup = BeautifulSoup(r.text[:850000], 'html.parser')
        kind = meta(soup, ('property','og:type'))
        if kind and kind not in ('article', 'news', 'website'): return None
        title = text(meta(soup, ('property','og:title'), ('name','twitter:title')) or
                     (soup.title.get_text(' ',strip=True) if soup.title else feed_title))
        if not title or len(title) < 18: return None
        description = text(meta(soup, ('name','description'), ('property','og:description')) or feed_description)
        if EXCLUDE.search(title) or ALERTS.search(title + ' ' + description): return None
        if area == 'local' and not (LOCAL_TERMS.search(title + ' ' + description) or host(base) in ('zhmerynka.city','zhmr.gov.ua','zhmerynka-rda.gov.ua')):
            return None
        if area == 'region' and not (REGION_TERMS.search(title + ' ' + description) or host(base) == 'vn.20minut.ua' or 'vinnytsia' in url):
            return None
        raw_date = (meta(soup, ('property','article:published_time'), ('itemprop','datePublished'),
                         ('name','date'), ('name','pubdate')) or feed_date)
        dt = iso_date(raw_date)
        if not dt:
            for tag in soup.find_all('time', limit=3):
                dt = iso_date(tag.get('datetime'))
                if dt: break
        # Не вигадуємо дату новини; матеріали без дати допускаються лише з RSS.
        if not dt: return None
        try:
            days = (datetime.now(timezone.utc) - datetime.fromisoformat(dt)).total_seconds()/86400
            if days > MAX_AGE_DAYS or days < -1: return None
        except ValueError: return None
        image = meta(soup, ('property','og:image'), ('name','twitter:image'), ('property','twitter:image')) or feed_image
        image = urljoin(r.url,image) if image else ''
        if urlparse(image).scheme != 'https': image = ''
        final = canonical(r.url)
        return {'area': area, 'title': title[:200], 'url': final, 'date': dt,
                'excerpt': description[:320], 'image': image, 'source': source}
    except requests.RequestException as exc:
        print('ARTICLE ERROR', source, str(exc)[:120], flush=True)
        return None

def discover(base, homepage, configured):
    feeds = list(configured)
    links = []
    try:
        page = get(homepage)
        soup = BeautifulSoup(page.text[:1200000], 'html.parser')
        for a in soup.select('link[rel="alternate"]'):
            typ = (a.get('type') or '').lower()
            if ('rss' in typ or 'atom' in typ) and a.get('href'):
                feed = urljoin(page.url, a['href'])
                if allowed(feed, base): feeds.append(feed)
        for a in soup.find_all('a', href=True, limit=500):
            u = urljoin(page.url,a['href'])
            if not allowed(u,base): continue
            path = urlparse(u).path
            if any(x in path for x in ('/articles/', '/news/', '/article/', '/2026/', '/novyny/')):
                if u not in links: links.append(u)
    except requests.RequestException as exc:
        print('LISTING ERROR', homepage, str(exc)[:150], flush=True)
    return list(dict.fromkeys(feeds)), links[:MAX_PER_SITE*3]

def from_feed(url, area, source, base):
    out=[]
    try:
        response = get(url)
        feed = feedparser.parse(response.content)
        if not feed.entries:
            print('EMPTY RSS', source, url, flush=True)
        for item in feed.entries[:MAX_PER_SITE*2]:
            direct = item.get('link', '')
            if not allowed(direct,base): continue
            date = iso_date(item.get('published','') or item.get('updated',''))
            if not date and item.get('published_parsed'):
                date = datetime.fromtimestamp(calendar.timegm(item.published_parsed),timezone.utc).isoformat()
            image = ''
            if item.get('media_content'):
                image = item.media_content[0].get('url','')
            article = extract_article(direct,area,source,base,item.get('title',''),date,
                                      item.get('summary',''),image)
            if article: out.append(article)
            if len(out)>=MAX_PER_SITE: break
    except requests.RequestException as exc:
        print('RSS ERROR', source, str(exc)[:150], flush=True)
    return out

def collect(area, source, page, configured):
    base = 'https://' + host(page)
    feeds, links = discover(base,page,configured)
    found=[]; seen=set()
    for f in feeds[:4]:
        for item in from_feed(f,area,source,base):
            if item['url'] not in seen: seen.add(item['url']); found.append(item)
    if len(found)<MAX_PER_SITE:
        for u in links:
            if canonical(u) in seen: continue
            item=extract_article(u,area,source,base)
            if item and item['url'] not in seen:
                seen.add(item['url']);found.append(item)
            if len(found)>=MAX_PER_SITE: break
    print('SOURCE',source,'feeds',len(feeds),'article_candidates',len(links),'accepted',len(found),flush=True)
    return found

def interleave(groups, limit=MAX_ITEMS):
    # 10 позицій: 4 місцеві, 3 обласні, 3 загальноукраїнські.
    pattern=('local','region','ukraine','local','region','ukraine','local','region','ukraine','local')
    for items in groups.values(): items.sort(key=lambda x:x['date'],reverse=True)
    output=[]; seen=set()
    while len(output)<limit:
        added=False
        for category in pattern:
            while groups[category]:
                item=groups[category].pop(0)
                key=re.sub(r'\W+','',item['title'].casefold())[:120]
                if key in seen:continue
                seen.add(key);output.append(item);added=True;break
            if len(output)>=limit:break
        if not added:break
    return output

def main():
    groups={'local':[], 'region':[], 'ukraine':[]}
    for area,source,page,configured in SITES:
        groups[area].extend(collect(area,source,page,configured))
    print('CATEGORY COUNTS', {k:len(v) for k,v in groups.items()},flush=True)
    news=interleave(groups)
    if not news:
        print('ERROR: No verified direct articles. Existing news.json preserved.',flush=True)
        raise SystemExit(1)
    output=os.getenv('NEWS_OUTPUT','news.json')
    payload={'updated_at':datetime.now(timezone.utc).isoformat(),
             'distribution_target':{'local':40,'region':30,'ukraine':30},'items':news}
    with open(output+'.tmp','w',encoding='utf-8') as f:json.dump(payload,f,ensure_ascii=False,indent=2)
    os.replace(output+'.tmp',output)
    print('SUCCESS',len(news),'items; with images',sum(bool(n['image']) for n in news),flush=True)

if __name__=='__main__':main()
