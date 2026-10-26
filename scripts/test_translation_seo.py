"""Static international SEO and conversion entry contracts."""
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import unittest
import generate_translation_pages as pages

class Tags(HTMLParser):
    def __init__(self,text):
        super().__init__();self.tags=[];self.feed(text)
    def handle_starttag(self,tag,attrs):self.tags.append((tag,dict(attrs)))

class TranslationSeoTests(unittest.TestCase):
    def test_languages_have_self_canonical_reciprocal_alternates_and_sitemap(self):
        sitemap=(pages.ROOT/'frontend/sitemap.xml').read_text()
        for lang,name in pages.PAGES.items():
            text=(pages.ROOT/'frontend'/name).read_text();tags=Tags(text).tags
            self.assertIn(('html',{'lang':lang}),tags)
            self.assertEqual([a['href'] for t,a in tags if t=='link' and a.get('rel')=='canonical'],['https://fixepub.com/'+name])
            alternate={a['hreflang']:a['href'] for t,a in tags if t=='link' and a.get('rel')=='alternate'}
            for other,target in pages.PAGES.items():self.assertEqual(alternate[other],'https://fixepub.com/'+target)
            self.assertIn('https://fixepub.com/'+name,sitemap)
            schema=json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>',text,re.S)[1]);self.assertEqual(schema['inLanguage'],lang)
            self.assertEqual(len(re.findall(r'<h1\b',text)),1)
            self.assertNotIn('noindex',text)
            self.assertFalse(any(t in ('form','input') for t,a in tags))
    def test_localized_cta_and_honest_price(self):
        for lang in pages.CONTENT:
            text=pages.render(lang)
            self.assertIn('tool=translate&amp;lang='+lang,text)
            self.assertIn('US$1.49',text)
            self.assertIn('data-growth-service="translation"',text)
            self.assertNotIn('/api/',text)
            self.assertEqual(text,(pages.ROOT/'frontend'/pages.PAGES[lang]).read_text())

if __name__=='__main__':unittest.main()
