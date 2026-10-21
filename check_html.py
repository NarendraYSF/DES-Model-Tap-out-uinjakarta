import os
from html.parser import HTMLParser

class Validator(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.links = []
        self.ids = set()
        self.imgs = []
    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        d = dict(attrs)
        if 'id' in d:
            self.ids.add(d['id'])
        if tag == 'a' and 'href' in d:
            self.links.append(d['href'])
        if tag == 'img' and 'src' in d:
            self.imgs.append(d['src'])

with open('docs/index.html', 'r', encoding='utf-8') as f:
    html = f.read()

v = Validator()
v.feed(html)
print(f'Total HTML tags: {len(v.tags)}')
print('IDs found:', sorted(v.ids))

all_ok = True
for link in v.links:
    if link.startswith('#'):
        target = link[1:]
        ok = target in v.ids
        print(f'Link {link} -> {ok}')
        if not ok:
            all_ok = False

for img in v.imgs:
    p = os.path.join('docs', img)
    ok = os.path.isfile(p)
    sz = os.path.getsize(p) if ok else 0
    print(f'Image {img} -> {ok} ({sz} bytes)')
    if not ok:
        all_ok = False

print('Status:', 'SUCCESS' if all_ok else 'FAILED')
