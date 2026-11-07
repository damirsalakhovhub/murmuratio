"""Subset Geist 1.9.2 from the OFL-licensed npm geist package (build input only).
Usage: python3 site/build-fonts.py /path/to/unpacked/package
Requires fonttools[woff]. The site makes no external font requests.
Source: https://github.com/vercel/geist-font ; https://www.npmjs.com/package/geist
"""
from pathlib import Path
from html.parser import HTMLParser
import sys
import re
from fontTools.ttLib import TTFont
from fontTools import subset
from fontTools.varLib.instancer import instantiateVariableFont

root=Path(__file__).parent
class Text(HTMLParser):
    def __init__(self): super().__init__(); self.skip=0; self.text=[]
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','svg'): self.skip+=1
    def handle_endtag(self,tag):
        if tag in ('script','style','svg'): self.skip-=1
    def handle_data(self,data):
        if not self.skip: self.text.append(data)
parser=Text(); parser.feed((root/'index.html').read_text())
import string
text=''.join(parser.text)+'$ Copy Copied Select 0123456789'+string.ascii_letters+string.punctuation
package=Path(sys.argv[1])
for family,source,axes in [
 ('geist-sans','geist-sans/Geist-Regular.woff2',None),
 ('geist-mono','geist-mono/GeistMono-Regular.woff2',None),
 ('geist-mono-medium','geist-mono/GeistMono-Medium.woff2',None),
 ('geist-serif','geist-serif/GeistSerif-Regular.woff2',None),
]:
    font=TTFont(package/'dist/fonts'/source)
    if 'gvar' in font:
        for glyph in font.getGlyphOrder():
            if glyph not in font['gvar'].variations: font['gvar'].variations[glyph]=[]
    if 'fvar' in font:
        font=instantiateVariableFont(font,{a.axisTag:a.defaultValue for a in font['fvar'].axes},inplace=True)
    options=subset.Options(); options.flavor='woff2'; options.recalc_timestamp=False
    options.layout_features=['kern','liga'] if not family.startswith('geist-mono') else []
    used=text
    if family=='geist-serif':
        page=(root/'index.html').read_text()
        used=''.join(re.findall(r'<h[12][^>]*>(.*?)</h[12]>',page,re.S))
        used+=''.join(re.findall(r'<p class="lede">(.*?)</p>',page,re.S))
        used=re.sub(r'<[^>]+>','',used)
    worker=subset.Subsetter(options=options); worker.populate(text=used); worker.subset(font)
    font.flavor='woff2'; font.save(root/'assets'/f'{family}-latin.woff2')
(root/'assets/Geist-OFL.txt').write_text((package/'LICENSE.txt').read_text())
