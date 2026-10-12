#!/usr/bin/env python3
"""Hourly public news metadata for TYTS. Requires Internet on GitHub Actions."""
import json, os, re, time, html, hashlib
from datetime import datetime, timezone
from urllib.parse import quote, urlparse
import requests, feedparser
from bs4 import BeautifulSoup

UA = {'User-Agent':'Mozilla/5.0 (compatible; TYTSNewsAggregator/1.0; +https://github.com/)','Accept-Language':'uk-UA,uk;q=0.9'}
S = requests.Session(); S.headers.update(UA)
QUERIES = {
 'local':'(Жмеринка OR Жмеринський OR Браїлів OR Северинівка OR Станіславчик) (site:zhmerynka.city OR site:zhmr.gov.ua OR site:zhmerynka-rda.gov.ua)',
 'region':'(Вінниця OR Вінниччина OR Вінницька область) (site:vin.gov.ua OR site:suspilne.media OR site:vn.20minut.ua)',
 'ukraine':'(Україна OR українці) (site:ukrinform.ua OR site:suspilne.media OR site:pravda.com.ua)'
}
ALLOWED={
 'local':{'zhmerynka.city','zhmr.gov.ua','zhmerynka-rda.gov.ua'},
 'region':{'vin.gov.ua','suspilne.media','vn.20minut.ua'},
 'ukraine':{'ukrinform.ua','suspilne.media','pravda.com.ua'}
}
SOURCES={'zhmerynka.city':'Жмеринка.City','zhmr.gov.ua':'Жмеринська міська рада','zhmerynka-rda.gov.ua':'Жмеринська РВА','vin.gov.ua':'Вінницька ОВА','vn.20minut.ua':'20 хвилин. Вінниця','suspilne.media':'Суспільне','ukrinform.ua':'Укрінформ','pravda.com.ua':'Українська правда'}
BLOCK=re.compile(r'((початок|кінець|відбій|оголошення|скасування|лунає|пролунала).{0,65}(повітрян\w* тривог|сирен)|повітрян\w* тривог.{0,65}(початок|кінець|відбій|оголошен|скасуван))',re.I)

def is_host(url, area):
 try:
  h=urlparse(url).hostname.lower().removeprefix('www.')
  return h if h in ALLOWED[area] else None
 except Exception:return None

def clean(s):return re.sub(r'\s+',' ',BeautifulSoup(html.unescape(s or ''),'html.parser').get_text(' ',strip=True)).strip()

def resolve(link):
 try:
  r=S.get(link,timeout=13,allow_redirects=True)
  if 'news.google.com' in urlparse(r.url).hostname:
   try:
    from googlenewsdecoder import gnewsdecoder
    d=gnewsdecoder(link,interval=1)
    if d.get('status') and d.get('decoded_url'):return d['decoded_url']
   except Exception:pass
  return r.url
 except requests.RequestException:return link

def meta(url):
 try:
  r=S.get(url,timeout=12);r.raise_for_status();s=BeautifulSoup(r.text[:600000],'html.parser')
  def get(*keys):
   for attr,val in keys:
    m=s.find('meta',attrs={attr:val})
    if m and m.get('content'):return m['content'].strip()
   return ''
  img=get(('property','og:image'),('name','twitter:image'),('property','twitter:image'))
  from urllib.parse import urljoin
  if img:img=urljoin(url,img)
  if not img.startswith('https://'):img=''
  summary=get(('name','description'),('property','og:description'))
  return img,clean(summary)[:320]
 except Exception:return '', ''

def collect(area,query):
 rss='https://news.google.com/rss/search?q='+quote(query)+'&hl=uk&gl=UA&ceid=UA:uk'
 try:
  resp=S.get(rss,timeout=20);resp.raise_for_status();entries=feedparser.parse(resp.content).entries
 except Exception as e:
  print(area,'RSS ERROR',str(e));return []
 arr=[]
 for entry in entries[:35]:
  title=clean(entry.get('title',''))
  if ' - ' in title:title=title.rsplit(' - ',1)[0]
  if not title or BLOCK.search(title):continue
  url=resolve(entry.get('link',''))
  host=is_host(url,area)
  if not host:continue
  published=entry.get('published_parsed') or entry.get('updated_parsed')
  date=datetime.fromtimestamp(time.mktime(published),timezone.utc).isoformat() if published else ''
  image,description=meta(url)
  # Never generate a false claim about photographs; only an article's metadata image is used.
  arr.append({'area':area,'title':title[:220],'url':url,'date':date,'excerpt':description,'image':image,'source':SOURCES[host]})
  print(area,host,title[:45],('image' if image else 'no-image'))
 return arr

def interleave(groups,limit=60):
 # Blocks of 10: local=4 region=3 ukraine=3.
 pattern=['local','region','ukraine','local','region','ukraine','local','region','ukraine','local']
 out=[]; seen=set()
 for k in groups:
  groups[k].sort(key=lambda n:n['date'],reverse=True)
 while len(out)<limit:
  changed=False
  for area in pattern:
   while groups[area]:
    x=groups[area].pop(0)
    key=re.sub(r'\W+','',x['title'].lower())[:110]
    if key in seen:continue
    seen.add(key);out.append(x);changed=True;break
   if len(out)>=limit:break
  if not changed:break
 return out

def main():
 groups={area:collect(area,q) for area,q in QUERIES.items()}
 output=os.environ.get('NEWS_OUTPUT','news.json')
 news=interleave(groups)
 # Avoid overwriting a working stream with an empty response.
 if not news and os.path.exists(output):
  print('No updates; retained last successful file.');return
 data={'updated_at':datetime.now(timezone.utc).isoformat(),'distribution_target':{'local':40,'region':30,'ukraine':30},'items':news}
 with open(output+'.tmp','w',encoding='utf-8') as f:json.dump(data,f,ensure_ascii=False,indent=2)
 os.replace(output+'.tmp',output)
 print('Saved',len(news),'stories')
if __name__=='__main__':main()
