# coding: utf8
from collections import defaultdict
import re
from uuid import uuid4
from itertools import combinations, groupby

from fuzzywuzzy import fuzz
from bs4 import BeautifulSoup
from souplib import (
    new_tag, text, tag_and_text, update_attr, descendants, next_siblings, children,
    remove,
)

from clld.scripts.util import parsed_args
from clld.util import jsonload, slug

from apics.scripts.convert_util import (
    convert_chapter, Parser, SURVEY_SECTIONS, REFERENCE_CATEGORIES, normalize_whitespace,
    YEAR, get_bibtex,
)


class Paragraph(object):
    number_pattern = re.compile('\([0-9]+\)')
    number_language_ref_pattern = re.compile(
        '\((?P<no>[0-9]+)\)\s+(?P<lang>[^\(]+)(\((?P<ref>[^0-9]+[0-9]{4}([a-z])?(:\s*[0-9]+)?)\))?$')
    value_pattern = re.compile('[0-9]\.\s+[^\t]+\t[0-9]+$')
    header_pattern = re.compile('Chapter\s+[0-9]+$')

    def __init__(self, lines, refs=False):
        #if refs:
        #    print '\n'.join(l[1] for l in lines)
        self.lines = lines
        self.is_example = ('\t' in self.lines[0][1] and self.lines[0][2] == 'p') \
                          or self.number_language_ref_pattern.match(self.lines[0][1])
        if self.value_pattern.match(self.lines[0][1]):
            # entry in list of values for non-multiple valued feature
            self.is_example = False

        self.is_refs = refs
        self.is_header = self.header_pattern.match(self.lines[0][1])


class Atlas(Parser):
    BR = '----------'

    def get_id(self, fname):
        return fname.name.split('.')[0]

    def preprocess(self, html):
        for t, d in [
            ('<h3 ', '<p '),
            ('</h3>', '</p>'),
            ('<h1 ', '<p '),
            ('</h1>', '</p>'),
            # Special special case for 42:
            ('(2005e)).</span></p>',
             '(2005e)).</span></p>\n<p><br></p>\n<p>References</p>'),
            ('<br />', self.BR),
            ('<br>', self.BR),
        ]:
            html = html.replace(t, d)
        html = re.sub('line\-height:\s*100%', 'line-height:150%', html, flags=re.M)
        html = re.sub('line\-height:\s*100%', 'line-height:150%', html, flags=re.M)
        html = re.sub('margin\-right:\s*1\.74in;?', '', html, flags=re.M)
        return html

    def postprocess(self, html):
        return html.replace(self.BR, '<br />')

    def refactor(self, soup, md):
        d = BeautifulSoup('<body></body>')
        body = d.find('body')
        linked = 0
        notlinked = 0
        multiple = 0
        for p in self._chunks(soup):
            if not isinstance(p, list):
                p = [p]
            for pp in p:
                if pp.is_header:
                    continue
                elif pp.is_refs:
                    md['refs'] = [self.get_ref(line[0]) for line in pp.lines]
                else:
                    ex = None
                    if pp.is_example:
                        container = d.new_tag(
                            'blockquote',
                            **{
                                'class': 'example',
                                'style': 'font-size:100%;padding-left:1.8em;margin-left:0.3em'})
                        #body.append(Tag(name='hr'))
                    else:
                        container = body
                    for e, line, t in pp.lines:
                        body.append(e)
                        if pp.is_example:
                            if re.match('\([0-9]+\)', line):
                                e.attrs['style'] = 'text-indent:-2em'
                            equo = "’".decode('utf8')
                            if line.startswith("‘".decode('utf8')) and equo in line:
                                line = equo.join(line[1:].split(equo)[:-1]).strip()
                                examples = self.examples.get(slug(line))
                                if examples:
                                    if len(examples) > 1:
                                        #print '~~~', line
                                        multiple += 1
                                    else:
                                        ex = examples.values()[0]
                                        #print '+++'
                                        linked += 1
                                else:
                                    #print '---', line
                                    notlinked += 1
                        container.append(e)
                    if pp.is_example:
                        if ex:
                            container.attrs['id'] = 'ex-' + ex
                            container.append(new_tag(d, 'small', new_tag(
                                d, 'a', 'See example ' + ex, href='/sentences/' + ex)))
                        body.append(container)
        #print 'examples:', linked, 'linked,', notlinked, 'not linked,', multiple, 'multiple choices'
        for e in body.find_all('font'):
            e.unwrap()
        return d

    def _paragraphs(self, soup):
        lines = []
        refs = False

        for e in soup.find_all(['p', 'table']):
            t = text(e)

            if e.name == 'table':
                if re.match('[\-\s]+excl\s+', t) \
                        or re.match('[\-\s]*1\.[^0-9]+[0-9]+\s+2\.\s+', t):
                    e.replace_with(new_tag(soup, 'p', 'value-table'))
                    break

            if e.name == 'p':
                if re.match('1\.\s+(.+?)\s+[0-9]+$', t):
                    ex = []
                    for p in next_siblings(e):
                        tt = text(p)
                        if p.name != 'p' or not re.match('[0-9]\.\s+(.+?)\s+[0-9]+$', tt):
                            break
                        ex.append(p)
                    if ex:
                        for ee in ex:
                            ee.extract()
                        e.replace_with(new_tag(soup, 'p', 'value-table'))
                        break

        for e, t in tag_and_text(soup.find_all(['p', 'table', 'ol', 'ul'])):
            if e.parent.name in ['li', 'td']:
                continue

            #print t
            br = t == self.BR
            if t in ['References', 'Reference']:
                refs = True
                t = ''
            elif not lines and re.match('[0-9]+\.\s+[A-Za-z]+(\s+[A-Za-z]+)*$', t):
                e.name = 'h3'
            elif not lines and re.match('[0-9]+\.[0-9]+\.\s+[A-Z]', t):
                e.name = 'h4'
            elif t.endswith('and the APiCS Consortium'):
                continue

            if br and not refs:
                if lines:
                    yield Paragraph(lines)
                    lines = []
            if t and t != self.BR:
                lines.append((e, t, e.name))

        if lines:
            yield Paragraph(lines, refs=refs)

    def _chunks(self, soup):
        example_group = []
        for p in self._paragraphs(soup):
            if p.is_example:
                example_group.append(p)
            else:
                if example_group:
                    yield example_group
                    example_group = []
                yield p


class Surveys(Parser):
    fname_pattern = re.compile('(?P<vol>I+)_(?P<no>[0-9]+)?_(?P<name>[^\._]+)')

    headings = SURVEY_SECTIONS
    heading_pattern = re.compile(
        '((?P<no>[0-9]+\.(?P<sub>[0-9]+\.?)?)[\s\xa0]*)?(?P<title>%s)$' % '|'.join(h.lower() for h in headings))
    _language_lookup = None

    @property
    def language_lookup(self):
        if not self._language_lookup:
            self._language_lookup = {slug(v): k for (k, v) in self.languages.items()}
        return self._language_lookup

    def get_id(self, fname):
        match = self.fname_pattern.search(fname.name)
        assert match
        lid = self.language_lookup.get(slug(match.group('name')))
        if lid:
            return '%s.%s' % (lid, '%(vol)s-%(no)s' % match.groupdict())
        assert not match.group('no')
        return '%(vol)s-%(name)s' % match.groupdict()

    def preprocess(self, html):
        for s in ['<o:p>', '</o:p>', 'color:windowtext;']:
            html = html.replace(s, '')
        html = re.sub('line\-height:\s*200%', 'line-height:150%', html, flags=re.M)
        html = re.sub('font\-size:\s*12\.0pt;?', '', html, flags=re.M)
        return html

    def refactor(self, soup, md):
        # clean attributes:
        def update_style(current):
            style = []
            for rule in (current or '').split(';'):
                rule = rule.strip()
                # tab-stops:14.2pt  text-indent:36.0pt
                if rule in ['tab-stops:14.2pt', 'text-indent:36.0pt']:
                    rule = 'margin-top:0.4em'
                if normalize_whitespace(rule, repl='') in [
                    'font-family:Junicode',
                    'font-family:JunicodeRegular',
                ]:
                    continue
                if rule and not rule.startswith('mso-'):
                    style.append(rule)
            return ';'.join(style)

        for e in descendants(soup.find('body')):
            update_attr(e, 'style', update_style)
            update_attr(e, 'lang', None)

        for e, t in tag_and_text(
                descendants(soup.find('body'), include=['p', 'h1', 'h2']),
                non_empty=False):
            if not t:
                e.extract()

        for p in soup.find_all('p'):
            if p.attrs.get('class') == ['Zitat']:
                p.wrap(soup.new_tag('blockquote'))
                continue

            if not p.parent.name == 'td':
                # need to detect headings by text, too!
                t = text(p)
                match = self.heading_pattern.match(t.lower())
                if match:
                    p.name = 'h2' if match.group('sub') else 'h1'

        # re-classify section headings:
        for i in range(1, 3):
            for p in soup.find_all('h%s' % i):
                p.name = 'h%s' % (i + 1,)

        for p in soup.find_all('a'):
            if p.attrs.get('name', '').startswith('OLE_LINK'):
                p.unwrap()

        top_level_elements = children(soup.find('div'))[:4]
        if '.' in self.id:
            try:
                assert [e.name for e in top_level_elements] == ['p', 'p', 'table', 'h3']
            except:
                print top_level_elements[0]
                print top_level_elements[1]
                print top_level_elements[3]
                raise

            md['title'] = text(top_level_elements[0])
            md['authors'] = [s for s in re.split(',|&| and ', text(top_level_elements[1]))]
            remove(*top_level_elements[:3])

        refs = soup.find(lambda e: e.name == 'h3' and text(e).startswith('References'))
        if refs:
            ex = []
            category = None
            for e, t in tag_and_text(next_siblings(refs)):
                if e.name == 'p':
                    if t in REFERENCE_CATEGORIES:
                        category = t
                    elif len(t.split()) < 3:
                        raise ValueError(t)
                    else:
                        if 'comment' in e.attrs.get('class', []):
                            if 'refs_comments' not in md:
                                md['refs_comments'] = [t]
                            else:
                                md['refs_comments'].append(t)
                        else:
                            if not YEAR.search(t):
                                print t
                            md['refs'].append(self.get_ref(e, category=category))
                    ex.append(e)
                elif e.name in ['h3', 'h4']:
                    category = t
                    ex.append(e)
            [e.extract() for e in ex + [refs]]

        for t in soup.find_all('table'):
            t.wrap(soup.new_tag('div', **{'class': 'table'}))

        return soup


def main(args):
    if args.cmd == 'convert':
        outdir = args.data_file('texts', args.what).joinpath('lo')
        if args.what == 'Atlas':
            for p in args.data_file('texts', args.what).joinpath('in').files():
                if p.ext in ['.doc', '.docx']:
                    convert_chapter(p, outdir)
        elif args.what == 'Surveys':
            pass
    if args.cmd == 'parse':
        outdir = args.data_file('texts', args.what).joinpath('processed')
        for p in args.data_file('texts', args.what).joinpath('lo').files():
            if args.in_name in p.namebase:
                globals()[args.what](p)(outdir)
    if args.cmd == 'refs':
        refs = []
        for p in args.data_file('texts', args.what).joinpath('processed').files('*.json'):
            if args.in_name in p.namebase:
                md = jsonload(p)
                refs.extend(md['refs'])
        db = get_bibtex(refs)
        unmatched = 0
        distinct = defaultdict(list)
        for i, rec in enumerate(db):
            if 'all' in rec:
                unmatched += 1
            distinct[(
                slug(rec.get('key', unicode(uuid4().hex))),
                slug(unicode(rec.get('title', uuid4().hex)), remove_whitespace=False)
            )] = 1
        print unmatched, 'of', i, 'distinct', len(distinct)

        c = 0
        for key, refs in groupby(sorted(distinct.keys()), key=lambda t: t[0]):
            refs = list(refs)
            if len(refs) > 1:
                for t1, t2 in combinations([t[1] for t in refs], 2):
                    if fuzz.partial_ratio(t1, t2) > 80:
                        print t1
                        print t2
                        print
                        c += 1
        print c
        return


if __name__ == '__main__':
    main(parsed_args(
        (("what",), dict()),
        (("cmd",), dict()),
        (("--in-name",), dict(default='')),
    ))
