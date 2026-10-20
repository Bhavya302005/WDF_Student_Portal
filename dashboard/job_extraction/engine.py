from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from hashlib import sha256
from html import unescape
import json
import os
from pathlib import Path
import re

VERSION = 'jd-v5'

_BOUNDARY = re.compile(r'[;\n]|[.!?](?=\s|$)')


def description_text(value):
    """Keep paragraph/list boundaries, which carry requirement and pay context."""
    text = str(value or '')
    # Greenhouse sends entity-escaped HTML ("&lt;p&gt;"): unescape first, or the
    # tags survive as literal text and no paragraph/list boundaries are kept.
    if re.search(r'&lt;\s*/?\s*(?:p|div|li|ul|br|h[1-6]|span|strong)\b', text, re.I):
        text = unescape(text)
    text = re.sub(r'<(script|style)\b[^>]*>.*?</\1>', '', text, flags=re.I | re.S)
    text = re.sub(r'<\s*(?:br\s*/?|/p|/div|/li|/h[1-6]|/tr)\s*>', '\n', text, flags=re.I)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = unescape(text).replace('\xa0', ' ')
    text = re.sub(r'[^\S\n]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n\s*\n+', '\n\n', text).strip()


def evidence(text, start, end, field='description', context=False):
    if context:
        # Search bounded windows instead of rebuilding all sentence boundaries
        # for every extracted field. Decimal points are not boundaries.
        left_window = text[max(0, start - 500):start]
        left_matches = list(_BOUNDARY.finditer(left_window))
        left = (max(0, start - 500) + left_matches[-1].end()) if left_matches else 0
        right_match = _BOUNDARY.search(text, end, min(len(text), end + 500))
        right = right_match.end() if right_match else len(text)
        start, end = max(left, start - 140), min(right, end + 140)
    return {'field': field, 'text': text[start:end], 'start': start, 'end': end}


def requirement(text, start, end):
    ev = evidence(text, start, end, context=True)['text'].lower()
    # A trailing preferred/required modifier applies to this item.
    if re.search(r'\b(?:not required|not necessary|optional|nice[- ]to[- ]have|preferred|a plus|desirable)\b', ev):
        return 'preferred'
    if re.search(r'\b(?:required|must|essential|mandatory|minimum)\b', ev):
        return 'required'
    # Headings may be on the preceding line.
    prefix = text[max(0, start - 300):start].lower()
    headings = list(re.finditer(r'(preferred|required|minimum|basic)\s+(?:qualifications|skills|requirements)', prefix))
    if headings:
        return 'preferred' if headings[-1].group(1) == 'preferred' else 'required'
    return 'mentioned'


_CURRENCY = r'(?:US\$|CA\$|C\$|AU\$|A\$|NZ\$|SG\$|S\$|HK\$|USD|CAD|AUD|NZD|SGD|HKD|INR|GBP|EUR|CHF|JPY|AED|SAR|SEK|NOK|DKK|PLN|BRL|ZAR|[$£€₹¥])'
_NUMBER = r'\d+(?:[,.]\d+)*'
_UNIT = r'(?:crores?|lakhs?|lacs?|LPA|Cr|k|m|l)\b'
_MONEY = re.compile(
    rf'(?<![\w.])(?:(?P<c1>{_CURRENCY})\s*)?(?P<n1>{_NUMBER})\s*(?P<u1>{_UNIT})?'
    rf'(?:\s*(?:[-–—]|\bto\b)\s*(?:(?P<c2>{_CURRENCY})\s*)?(?P<n2>{_NUMBER})\s*(?P<u2>{_UNIT})?)?'
    rf'(?:\s*(?P<suffix>{_CURRENCY})(?!\w))?', re.I)
_PAY = re.compile(r'\b(?:salary|salaries|pay|paid|compensation|wage|wages|remuneration|earnings|OTE|on[- ]target earnings|bonus|commission|stipend)\b', re.I)
_MONEY_MARKER = re.compile(rf'{_CURRENCY}|{_UNIT}', re.I)
_PERIODS = {
    'hour': r'(?:/\s*h(?:r|our)?\b|\b(?:per|an?|each)\s+(?:hour|hr)\b|\bhourly\b|\b(?:per uur|pro Stunde|de l\'heure|por hora)\b|\bstündlich)',
    'year': r'(?:/\s*(?:yr|year)\b|\b(?:per|an?|each)\s+(?:year|yr|annum)\b|\bannual(?:ly)?\b|\bLPA\b|\b(?:per jaar|pro Jahr|par an|al año|annuel(?:le)?|brut annuel)\b|\bjährlich)',
    'month': r'(?:/\s*(?:mo|month)\b|\b(?:per|an?|each)\s+(?:month|mo)\b|\bmonthly\b|\b(?:per maand|pro Monat|par mois|al mes|mensuel(?:le)?|monatlich)\b)',
    'week': r'(?:/\s*(?:wk|week)\b|\b(?:per|an?|each)\s+week\b|\bweekly\b)',
    'day': r'(?:/\s*day\b|\b(?:per|an?|each)\s+day\b|\bdaily\b)',
}


def currency_of(text):
    tokens = re.findall(_CURRENCY, text, re.I)
    mapping = {'US$': 'USD', 'CA$': 'CAD', 'C$': 'CAD', 'AU$': 'AUD', 'A$': 'AUD',
               'NZ$': 'NZD', 'SG$': 'SGD', 'S$': 'SGD', 'HK$': 'HKD', '£': 'GBP', '€': 'EUR', '₹': 'INR'}
    found = {mapping.get(t.upper(), t.upper()) for t in tokens if t not in ('$', '¥')}
    found = {t for t in found if len(t) == 3 and t.isalpha()}
    if not found and re.search(r'\b(?:LPA|lakhs?|crores?)\b', text, re.I):
        found.add('INR')
    return next(iter(found)) if len(found) == 1 else None


def amount(number, unit=None):
    number = str(number).strip().replace(' ', '')
    if ',' in number and '.' in number:
        # Last separator is decimal when both locale separators occur.
        number = number.replace('.', '').replace(',', '.') if number.rfind(',') > number.rfind('.') else number.replace(',', '')
    elif ',' in number:
        number = number.replace(',', '.') if len(number.rsplit(',', 1)[1]) in (1, 2) else number.replace(',', '')
    elif number.count('.') > 1 or ('.' in number and len(number.rsplit('.', 1)[1]) == 3):
        number = number.replace('.', '')
    multipliers = {'k': 1000, 'm': 1_000_000, 'l': 100_000, 'lpa': 100_000,
                   'lakh': 100_000, 'lakhs': 100_000, 'lac': 100_000, 'lacs': 100_000,
                   'cr': 10_000_000, 'crore': 10_000_000, 'crores': 10_000_000}
    value = Decimal(number) * multipliers.get((unit or '').lower(), 1)
    return int(value) if value == value.to_integral_value() else float(value)


def salary_candidates(text, field='description', source='jd'):
    if not _PAY.search(text) and not _MONEY_MARKER.search(text):
        return []
    results = []
    for match in _MONEY.finditer(text):
        g = match.groupdict()
        snippet = evidence(text, match.start(), match.end(), field, context=True)
        context = snippet['text']
        before = text[max(snippet['start'], match.start() - 90):match.start()]
        after = text[match.end():min(snippet['end'], match.end() + 55)]
        # "401k" / "403(b)" are retirement plans, not pay.
        if re.match(r'\s*\(?\s*[kb]\s*\)?', text[match.end('n1'):match.end('n1') + 4], re.I) and g['n1'] in ('401', '403', '457'):
            continue
        money_signal = g['c1'] or g['c2'] or g['suffix'] or g['u1'] or g['u2']
        # All bare numeric ranges require pay context, not merely plausible size.
        if not money_signal and not _PAY.search(before):
            continue
        if re.match(r'\s*(?:%|years?\b|yrs?\b|days?\s+(?:of\s+)?(?:PTO|leave)|countries\b|employees\b|people\b)', after, re.I):
            continue
        if re.search(r'\b(?:401\s*\(?k\)?|funding|revenue|valuation|budget|sales target|insurance coverage|life insurance)\b', before[-65:], re.I):
            continue
        if re.match(r'\s*(?:in\s+)?(?:funding|revenue|valuation|budget|insurance coverage)\b', after, re.I):
            continue
        if not money_signal and re.search(r'\b(?:experience|team|founded|since)\b', before[-45:], re.I):
            continue
        if not g['c1'] and not g['u1'] and re.fullmatch(r'(?:19|20)\d{2}', g['n1']):
            continue
        try:
            unit1, unit2 = g['u1'], g['u2']
            if unit2 and not unit1:
                unit1 = unit2
            if unit1 and g['n2'] and not unit2:
                unit2 = unit1
            low = amount(g['n1'], unit1)
            high = amount(g['n2'], unit2) if g['n2'] else low
            if g['n2'] and not unit1 and not unit2 and re.fullmatch(r'\d{1,3}', g['n1']) and re.search(r'[,\.]\d{3}$', g['n2']):
                low *= 1000
        except (InvalidOperation, ValueError):
            continue
        if low > high or low < 0 or high > 1_000_000_000:
            continue
        qualifiers = before[-40:].lower()
        if not g['n2']:
            if re.search(r'\b(?:up to|maximum|at most)\s*$', qualifiers):
                low = None
            elif re.search(r'\b(?:from|starting at|minimum|at least)\s*$', qualifiers) or re.match(r'\s*\+', after):
                high = None
        # Component and period are local to this amount, not another pay range.
        component = 'unspecified'
        labels = list(re.finditer(r'\b(base(?: salary| pay)?|OTE|on[- ]target earnings|total compensation|bonus|commission|equity|stock)\b', before, re.I))
        if labels:
            label = labels[-1].group().lower()
            component = 'base' if label.startswith('base') else 'ote' if label == 'ote' or label.startswith('on') else 'total' if label.startswith('total') else 'equity' if label in ('equity', 'stock') else label
        elif re.search(r'^\s*(?:annual\s+)?(?:bonus|commission)\b', after, re.I):
            component = 'bonus' if 'bonus' in after.lower() else 'commission'
        local = before[-35:] + match.group() + after
        periods = [period for period, pattern in _PERIODS.items() if re.search(pattern, local, re.I)]
        period = periods[0] if len(periods) == 1 else None
        currency = currency_of(match.group()) or currency_of(before[-20:])
        # Keep region qualification as verbatim context, never collapse distinct ranges.
        region = re.search(r'(?:^|[.;\n])\s*([^\n;:.]{2,70}):\s*$', before)
        region_name = region.group(1).strip() if region else None
        if region_name and _PAY.search(region_name):
            region_name = None
        results.append({'min': low, 'max': high, 'currency': currency, 'period': period,
                        'component': component, 'location': region_name,
                        'text': match.group().strip(), 'evidence': snippet, 'source': source,
                        'status': 'explicit' if currency and period else 'needs_context',
                        '_span': (match.start(), match.end())})
    return [{k: v for k, v in item.items() if k != '_span'} for item in _merge_between(text, results)]


# "between €2.650 and €2.800" is one range, not two salaries.
_BETWEEN = re.compile(r'\b(?:between|tussen|zwischen|entre|tra|fra)\s*$', re.I)
_AND = re.compile(r'^\s*(?:and|en|und|et|y|e|à|a|-|–|—)\s*$', re.I)


def _merge_between(text, results):
    merged, skip = [], set()
    for index, item in enumerate(results):
        if index in skip:
            continue
        following = results[index + 1] if index + 1 < len(results) else None
        if (following and item['min'] == item['max'] and following['min'] == following['max']
                and item['min'] is not None and following['min'] is not None and item['min'] <= following['min']
                and _BETWEEN.search(text[max(0, item['_span'][0] - 20):item['_span'][0]])
                and _AND.match(text[item['_span'][1]:following['_span'][0]])
                and (item['currency'] or following['currency']) in (item['currency'] or following['currency'], None)):
            combined = dict(item, max=following['min'], currency=item['currency'] or following['currency'],
                            period=item['period'] or following['period'],
                            text=text[item['_span'][0]:following['_span'][1]],
                            _span=(item['_span'][0], following['_span'][1]))
            combined['status'] = 'explicit' if combined['currency'] and combined['period'] else 'needs_context'
            merged.append(combined)
            skip.add(index + 1)
        else:
            merged.append(item)
    return merged


@lru_cache(maxsize=4)
def taxonomy(extra_file=''):
    data = json.loads(Path(__file__).with_name('skills.json').read_text())
    if extra_file:
        data.update(json.loads(Path(extra_file).read_text()))
    alias_map = {}
    for name, aliases in data.items():
        for alias in [name, *aliases]:
            alias_map[alias.casefold()] = name
    pattern = re.compile(r'(?<![\w+#.])(?:' + '|'.join(re.escape(a) for a in sorted(alias_map, key=len, reverse=True)) + r')(?![\w+#])', re.I)
    return alias_map, pattern


@lru_cache(maxsize=4)
def taxonomy_version(extra_file=''):
    return sha256(json.dumps(taxonomy(extra_file)[0], sort_keys=True).encode()).hexdigest()


def skill_candidates(text, field='description', source='jd'):
    aliases, pattern = taxonomy(os.getenv('JOB_SKILLS_FILE', ''))
    result, seen = [], set()
    for match in pattern.finditer(text):
        term = match.group()
        name = aliases[term.casefold()]
        if name in ('Go', 'R', 'SAS') and term != name and term.lower() != 'golang':
            continue
        if name == 'Go' and re.match(r'-to\b', text[match.end():], re.I):
            continue
        if term.casefold() == 'gcp':
            local = text[max(0, match.start()-120):match.end()+120]
            if re.search(r'\b(?:clinical|trial|patient|pharma|ich)\b', local, re.I) and not re.search(r'\b(?:cloud|google)\b', local, re.I):
                name = 'Good Clinical Practice'
        if len(term) <= 2 and term.isalpha() and term.upper() != term:
            continue
        nearby = evidence(text, match.start(), match.end(), field, context=True)
        # Do not turn a negated requirement into a positive skill requirement.
        prefix = text[max(0, match.start() - 15):match.start()]
        if re.search(r'\b(?:no|without)\s*$', prefix, re.I) and re.search(r'experience.*(?:needed|required)', text[match.end():match.end()+50], re.I):
            continue
        req = requirement(text, match.start(), match.end())
        key = name, req
        if key not in seen:
            seen.add(key)
            result.append({'name': name, 'requirement': req, 'source': source, 'evidence': nearby, 'status': 'explicit'})
    return result


_WORK_PATTERNS = [
    # "hybrid cloud/vehicle/app" is technology, not a work arrangement.
    ('Hybrid', re.compile(r'\bhybrid\b(?![- ]?(?:cloud|vehicles?|cars?|apps?|applications?|mobile|electric|energy|engines?|seeds?|infrastructure|environments?|it\b|integration|architectures?|solutions?|systems?|workflows?|integrated|approach(?:es)?|models? of care|funds?|securities|instruments?|methodolog\w*|tech\w*|storage|networks?|events?|meetings?|interviews?|learning|classes|courses?))|\b(?:one|two|three|four|five|\d+)\s+(?:remote|office|on[- ]?site)\s+days?\s+(?:a|per|each)\s+week\b|\b(?:one|two|three|four|five|\d+)(?:\s*(?:-|to|or)\s*(?:one|two|three|four|five|\d+))?\s+days?\s+(?:a|per|each|every)\s+week\s+(?:in|at|from)\s+(?:the\s+|our\s+|an?\s+)?(?:[\w&.\'’-]+\s+){0,3}(?:office|studio|hq|headquarters|campus)\b|\bin[- ](?:the[- ])?office\s+(?:one|two|three|four|five|\d+)(?:\s*(?:-|to)\s*\d+)?\s+days\b|\b(?:one|two|three|four|five|\d+)\s+days?\s+(?:in[- ]office|on[- ]?site).*?(?:one|two|three|four|five|\d+)\s+days?\s+remote(?:ly)?\b', re.I)),
    ('Onsite', re.compile(r'\b(?:on[- ]?site|in[- ]office|in[- ]person)\s+(?:role|position|job|work|schedule)\b|\b(?:work|performed|based|required to work)\s+(?:fully\s+)?(?:on[- ]?site|in[- ]office|in[- ]person)\b|\breport to (?:the )?office\b|\b(?:no|not|non)[ -]+remote\b', re.I)),
    ('Remote', re.compile(r'\b(?:fully|100%)\s+remote\b|\bremote[- ](?:first|only|friendly|eligible)\b|\bremote\s+(?:role|position|job|work|opportunity|within)\b|\b(?:work from home|WFH)\b|\bremote\s+(?:in|across|from)\s+(?:the\s+)?(?:US|U\.S\.|USA|United States|UK|Canada|Europe|EMEA|LATAM|India|country)\b|\b(?:US|U\.S\.|USA)[- ]based\s+remote\b|\b(?:this|the)\s+(?:position|role|job|opportunity)\s+(?:is|can be)\s+(?:fully\s+|100%\s+)?(?:remote|performed remotely)\b|\bwork remotely from (?:anywhere|home)\b', re.I)),
]


# Board-published labels (e.g. Indeed's "Job Type: Full-time" / "Work Location:
# In person" footer). Markdown bullets/bold around the label are tolerated.
_LABEL = r'(?:^|(?<=[\s*•.]))[*•\-\s]*\**\s*(?:{})\s*\**\s*:\s*\**\s*'
_LABELED_WORK = re.compile(_LABEL.format(r'Work Location|Workplace Type|Work Arrangement|Work Model|Workplace Model|Workplace|Work Type|Location Type|Work Setting|Work Environment|Remote Status|Job Location Type|Work Mode|Working Model|Location')
                           + r'(?P<value>hybrid(?: remote)?|fully remote|remote|in[- ]person|on[- ]?site)\b', re.I | re.M)
_TYPE_VALUE = (r'(?:(?:regular|permanent)\s+)?(?:full[- ]?time|part[- ]?time)(?:\s*[-,]?\s*(?:regular|permanent))?'
               r'|contract(?:or)?|temporary|temp|internship|seasonal|CDI|CDD|Vollzeit|Teilzeit|alternance|stage')
_LABELED_TYPE = re.compile(_LABEL.format(r'Job Types?|Employment Type|Type of Employment|Employment Status|Position Type|Job Status|'
                                         r'Schedule|Work Schedule|Contract Type|Type de contrat|Vertragsart|Anstellungsart|Beschäftigungsart')
                           + rf'(?P<value>(?:{_TYPE_VALUE})(?:\s*,\s*(?:{_TYPE_VALUE}))*)\b', re.I | re.M)
_LABEL_VALUES = {'hybrid': 'Hybrid', 'hybrid remote': 'Hybrid', 'remote': 'Remote', 'fully remote': 'Remote',
                 'in person': 'Onsite', 'onsite': 'Onsite', 'on site': 'Onsite',
                 'full time': 'full_time', 'fulltime': 'full_time', 'part time': 'part_time', 'parttime': 'part_time',
                 'contract': 'contract', 'contractor': 'contract', 'temporary': 'temporary', 'temp': 'temporary',
                 'seasonal': 'temporary', 'internship': 'internship',
                 'cdi': 'full_time', 'cdd': 'contract', 'vollzeit': 'full_time', 'teilzeit': 'part_time',
                 'alternance': 'internship', 'stage': 'internship'}


def labeled_value(pattern, text):
    """Return an explicit labeled value, or None when absent or self-contradictory."""
    found = []
    for match in pattern.finditer(text):
        # "Full-time or Part-time" / "Full-time/Part-time" offers both: not a single value.
        if re.match(r'\s*(?:or|/|&|and)\s*\w', text[match.end('value'):match.end('value') + 12], re.I):
            return None
        for token in re.split(r'\s*,\s*', match.group('value')):
            key = re.sub(r'[\s-]+', ' ', token.lower())
            key = re.sub(r'^(?:regular|permanent) |,? ?(?:regular|permanent)$', '', key).strip()
            value = _LABEL_VALUES.get(key)
            if value:
                found.append((value, match))
    if len({value for value, _ in found}) != 1:
        return None
    value, match = found[0]
    return {'value': value, 'status': 'explicit', 'source': 'jd_label',
            'evidence': evidence(text, match.start('value'), match.end('value'), context=True)}


def work_mode(text):
    labeled = labeled_value(_LABELED_WORK, text)
    if labeled:
        return labeled
    found = []
    for value, pattern in _WORK_PATTERNS:
        for match in pattern.finditer(text):
            prefix = text[max(0, match.start() - 20):match.start()]
            if value != 'Onsite' and re.search(r'\b(?:no|not|non|without)\s*$', prefix, re.I):
                continue
            found.append({'value': value, 'status': 'explicit', 'source': 'jd',
                          'evidence': evidence(text, match.start(), match.end(), context=True), '_position': match.start()})
    values = {item['value'] for item in found}
    if len(values) == 1:
        result = min(found, key=lambda item: item['_position'])
        result.pop('_position', None)
        return result
    if found:
        for item in found: item.pop('_position', None)
        return {'value': None, 'status': 'conflict', 'source': 'jd', 'evidence': None, 'candidates': found}
    return {'value': None, 'status': 'not_stated', 'source': None, 'evidence': None}


_TITLE_TYPE_PATTERNS = [('internship', r'\b(?:internship|intern|co-op|stagiaire|Praktikum|Werkstudent(?:in)?|alternance|alternant(?:e)?)\b|\bstage\b(?=.*(?:\((?:H/F|F/H)\)|\bmois\b))|(?<=\(H/F\) - )stage\b|(?<=\(F/H\) - )stage\b'), ('part_time', r'\bpart[- ]time\b|\b(?:Teilzeit|temps partiel|media jornada|medio tiempo)\b'),
                ('full_time', r'\bfull[- ]time\b|\b(?:Vollzeit|temps plein|tiempo completo|jornada completa)\b'), ('temporary', r'\b(?:temporary|seasonal)\s+(?:role|position|job|employment)\b'),
                ('contract', r'\b(?:contract|freelance)\s+(?:role|position|job|employment)\b|\b(?:fixed[- ]term|contractor)\b')]
_DESCRIPTION_TYPE_PATTERNS = [('internship', r'\b(?:internship|co-op)\b|\bintern\s+(?:role|position|job|program)\b'),
                ('part_time', r'\bpart[- ]time\s*,?\s*(?:(?:on[- ]?site|remote|hybrid|permanent|regular|hourly)\s*,?\s*)*(?:role|position|job|employment|schedule|opportunity|employee)\b|\b(?:role|position|job|employment)\s+(?:is\s+)?(?:a\s+)?part[- ]time\b'),
                ('full_time', r'\bfull[- ]time\s*,?\s*(?:(?:on[- ]?site|remote|hybrid|permanent|regular|salaried|exempt)\s*,?\s*)*(?:role|position|job|opportunity|employee|hire)\b|\b(?:role|position|job|employment)\s+(?:is\s+)?(?:a\s+)?full[- ]time\b|(?:\b(?:en|contrat)\s+|\((?:H/F|F/H)\)[^\n]{0,40}?)\bCDI\b'),
                ('temporary', r'\b(?:temporary|seasonal)\s+(?:role|position|job|employment)\b'),
                ('contract', r'\b(?:contract|freelance|contractor|fixed[- ]term)\s+(?:role|position|job|employment)\b|(?:\b(?:en|contrat)\s+|\((?:H/F|F/H)\)[^\n]{0,40}?)\bCDD\b|\bbefristet\b'),
                ('internship', r'\b(?:stagiaire|Praktikum|Praktikant(?:in)?|Werkstudent(?:in)?|alternance|alternant(?:e)?|tirocinio)\b')]
def employment_type(text):
    title, _, description = text.partition('\n')
    labeled = labeled_value(_LABELED_TYPE, description)
    if labeled:
        labeled['evidence']['start'] += len(title) + 1
        labeled['evidence']['end'] += len(title) + 1
        return labeled
    found = []
    for haystack, candidates, offset in (
        (title, _TITLE_TYPE_PATTERNS, 0),
        (description, _DESCRIPTION_TYPE_PATTERNS, len(title)+1),
    ):
        for value, pattern in candidates:
            for match in re.finditer(pattern, haystack, re.I):
                start, end = offset + match.start(), offset + match.end()
                prefix = text[max(0, start-20):start]
                if re.search(r'\b(?:non-|not |no )$', prefix, re.I):
                    continue
                found.append({'value': value, 'status': 'explicit', 'source': 'jd', 'evidence': evidence(text, start, end, context=True)})
    values = {x['value'] for x in found}
    if len(values) == 1:
        return found[0]
    return {'value': None, 'status': 'ambiguous' if found else 'not_stated', 'source': 'jd' if found else None, 'evidence': None, 'candidates': found}


_NUMBER_WORDS = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8,
                 'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12, 'fifteen': 15, 'twenty': 20}
_YEARS_NUMBER = r'(?:\d+(?:\.\d+)?|' + '|'.join(_NUMBER_WORDS) + r')'
# "five (5) years", "5+ years", "5-7 years", "5 or more years", "five years’"
_YEARS = (rf'(?P<low>{_YEARS_NUMBER})\s*(?:\(\s*\d+\s*\)\s*)?(?:(?:[-–—]|to)\s*(?P<high>{_YEARS_NUMBER})\s*)?'
          r'(?P<plus>\+|\s*plus\b|\s*or more\b)?\s*(?:years?|yrs?)[\'’]?')
# Ages and company history are not experience requirements.
_NOT_EXPERIENCE = re.compile(r'\s*(?:of\s+age\b|old\b|or older\b|of\s+service\b|in business\b|of (?:history|operation)\b|warranty\b)', re.I)
_EXPERIENCE_PATTERNS = [
    # "5+ years of SAP experience", "3 years' experience", "2 years working with"
    re.compile(rf'\b{_YEARS}\s+(?:(?:of\s+)?(?:[\w+#.&/-]+\s+){{0,5}}experience\b|(?:working|work)\s+(?:with|in|on)\b'
               rf'|in\s+(?:[\w+#.-]+\s*){{1,5}}(?=[,.;\n]|$))', re.I),
    # "10+ years of semiconductor sourcing, ..." (the plus sign makes "of <field>" a requirement)
    re.compile(rf'\b(?P<low>{_YEARS_NUMBER})\s*(?P<plus>\+|\s*plus\b|\s*or more\b)\s*(?:years?|yrs?)[\'’]?\s+of\s+'
               r'(?!age\b)(?:[\w+#.&/-]+\s*){1,6}(?=[,.;\n]|$)', re.I),
    # "minimum of 6 years’ ...", "at least five years in ..."
    re.compile(rf'\b(?:minimum(?: of)?|at least|requires?|required|must have|with|having|bring|possess)\s+{_YEARS}\s+'
               r'(?:of\s+|in\s+)?(?!age\b)(?:[\w+#.&/-]+\s*){1,6}(?=[,.;\n]|$)', re.I),
]


def _years_value(token):
    if token is None:
        return None
    token = token.lower()
    return float(_NUMBER_WORDS[token]) if token in _NUMBER_WORDS else float(token)


def experience_candidates(text):
    out, seen = [], set()
    for pattern in _EXPERIENCE_PATTERNS:
        for match in pattern.finditer(text):
            unit = re.search(r'(?:years?|yrs?)[\'’]?', match.group(), re.I)
            if unit and _NOT_EXPERIENCE.match(match.group(), unit.end()):
                continue
            low, high = _years_value(match['low']), _years_value(match.groupdict().get('high'))
            if low is None or low > 40 or (high is not None and (high < low or high > 40)):
                continue
            start = match.start('low')
            if any(abs(start - other) < 3 for other in seen):
                continue
            seen.add(start)
            out.append({'min': low, 'max': high, 'requirement': requirement(text, match.start(), match.end()),
                        'source': 'jd', 'status': 'explicit', 'evidence': evidence(text, match.start(), match.end(), context=True)})
    return out


_APOS = r"(?:'|’)?"
_SECTION_VOCAB = {
    'preferred_qualifications': [
        r'(?:preferred|desired|desirable|additional|bonus|ideal)\s+(?:qualifications?|requirements?|skills?|experience)(?:\s+and\s+skills?)?',
        r'nice[- ]to[- ]haves?', r'bonus(?: points)?(?: if you have)?', rf'(?:it{_APOS}s )?a plus(?: if you have)?', r'pluses',
        rf'while not required,? (?:it{_APOS}s )?(?:an? )?(?:added )?plus if you (?:also )?have', r'extra credit', r'good to have',
    ],
    'key_responsibilities': [
        r'(?:key |main |primary |core |essential )?(?:responsibilities|duties)(?:\s+(?:and|&)\s+responsibilities)?',
        rf'what you{_APOS}ll (?:do|be doing)(?: at [\w .&-]{{1,30}})?', r'what you will (?:do|be doing)', r'(?:the|your) role',
        r'role responsibilities', rf'(?:the )?impact you{_APOS}ll have', r'(?:the )?impact you will (?:have|make)', r'your impact',
        r'a typical day', r'day[- ]to[- ]day', r'in this role,? you will', r'job duties',
        r'essential (?:duties|functions)(?: and responsibilities)?', r'vos missions', r'ihre aufgaben', r'deine aufgaben',
        r'responsabilidades',
    ],
    'basic_qualifications': [
        r'(?:basic|minimum|required|essential|key|core|mandatory|must[- ]have)\s+(?:qualifications?|requirements?|skills?|experience)',
        r'qualifications?', r'requirements?', r'job requirements',
        r'(?:skills|experience|education)\s*(?:&|and)\s*(?:qualifications?|experience|skills|education|requirements?|abilities)',
        rf'what you{_APOS}ll need', r'what you need(?: to succeed)?', rf'what we{_APOS}re looking for', r'what we look for',
        r'who you are', r'about you', r'you have', r'you bring', r'what you bring', rf'what you{_APOS}ll bring',
        r'your (?:expertise|experience|background|skills|profile|qualifications)(?:\s*(?:&|and)\s*(?:experience|expertise|skills))?',
        r'(?:the\s+)?ideal candidate(?: will have)?', r'must[- ]haves?', rf'we{_APOS}d love to hear from you if you have',
        r'you might be a (?:good |great )?fit if(?: you have)?', r'knowledge,? skills,? (?:and|&) abilities', r'competencies',
        r'votre profil', r'profil recherch[ée]', r'votre expertise', r'ihr profil', r'das bringst du mit', r'requisitos',
        r'requisiti',
    ],
}
# Headings that end a section without starting a field (benefits, company, legal).
_STOP_HEADERS = [
    r'benefits?(?: (?:and|&) perks)?', r'perks(?: (?:and|&) benefits)?', r'what we offer', rf'why (?:join|work)[\w .&{_APOS}-]{{0,40}}',
    r'compensation(?: (?:and|&) benefits)?', r'pay(?: range| transparency)?[\w .&-]{0,40}', r'(?:annual )?(?:base )?salary[\w .&-]{0,40}',
    r'about (?:us|the company|the team|the role|[\w .&-]{1,30})', r'who we are',
    r'our (?:company|mission|values|culture|story|commitment[\w .&-]{0,60})',
    r'equal (?:employment )?opportunity[\w .&-]{0,60}', r'eeo[\w .&-]{0,40}', r'accommodations?[\w .&-]{0,60}',
    rf'how we{_APOS}ll take care of you', r'(?:your )?location', r'(?:the )?community you will join', r'diversity[\w .&-]{0,40}',
    r'inclusion[\w .&-]{0,40}', r'privacy[\w .&-]{0,40}', r'how to apply', r'next steps', r'(?:the )?interview process',
    r'life at [\w .&-]{1,30}', r'disclaimer[\w .&-]{0,40}', r'additional information', r'other information',
]


def _heading_regex(phrases, inline):
    body = '|'.join(phrases)
    if inline:
        # Flattened text: a known heading followed by a colon.
        return re.compile(rf'(?:^|(?<=[.!?*\n])\s*|(?<=\s))\**\s*(?P<h>{body})\s*\**\s*[:：]', re.I)
    return re.compile(rf'(?im)^[ \t]*(?:[#>*•\-]+[ \t]*)?\**[ \t]*(?P<h>{body})[ \t]*\**[ \t]*[:：]?[ \t]*\**[ \t]*$')


_FIELD_HEADINGS = {field: (_heading_regex(p, False), _heading_regex(p, True)) for field, p in _SECTION_VOCAB.items()}
_STOP_HEADINGS = (_heading_regex(_STOP_HEADERS, False), _heading_regex(_STOP_HEADERS, True))
# Any short line ending in a colon is a heading too ("Physical Requirements:" ends "Qualifications").
_GENERIC_HEADING = re.compile(r'(?m)^[ \t]*\**[ \t]*[A-Z][^\n.!?]{2,60}:[ \t]*\**[ \t]*$')
_PREFERRED_LINE = re.compile(r'\b(?:preferred|a plus|nice[- ]to[- ]have|bonus|desirable|ideally|advantageous|an advantage)\b', re.I)
_QUALIFICATION_WORDS = re.compile(r'\b(?:experience|degree|knowledge|familiar(?:ity)?|skills?|certifi\w+|proficien\w+|background|'
                                  r'understanding|license|MBA|bachelor|master)\b', re.I)


def section_fields(text):
    """Headed qualification/responsibility blocks; each runs until the next heading of any kind."""
    lined = text.count('\n') >= 3
    starts, stops = [], []
    for field, (line_pattern, inline_pattern) in _FIELD_HEADINGS.items():
        for pattern in ((line_pattern, inline_pattern) if lined else (inline_pattern,)):
            starts.extend((m.start('h'), m.end(), field) for m in pattern.finditer(text))
    for pattern in (_STOP_HEADINGS if lined else _STOP_HEADINGS[1:]):
        stops.extend(m.start('h') for m in pattern.finditer(text))
    if lined:
        stops.extend(m.start() for m in _GENERIC_HEADING.finditer(text))
    # Longest heading first at each position; a heading inside another one ("Qualifications"
    # within "Preferred Qualifications:") is part of it, not a heading of its own.
    unique, covered_until = [], -1
    for begin, content_start, field in sorted(starts, key=lambda item: (item[0], -item[1])):
        if begin >= covered_until:
            unique.append((begin, content_start, field))
            covered_until = content_start
    boundaries = sorted({begin for begin, _, _ in unique} | set(stops))
    fields = {}
    for begin, content_start, field in unique:
        following = [b for b in boundaries if b > begin + 1]
        content_end = following[0] if following else len(text)
        value = text[content_start:content_end].strip(' \t\r\n:-*')[:6000]
        if len(value) >= 3 and re.search(r'\w{2}', value) and field not in fields:
            fields[field] = value
    if 'preferred_qualifications' not in fields:
        # No preferred heading: lines saying "... is a plus" / "... preferred" are the preferred qualifications.
        pool = fields.get('basic_qualifications') or text
        lines = [line.strip(' -*•') for line in re.split(r'\n|(?<=[.;])\s+', pool)]
        preferred = [line for line in lines if 10 <= len(line) <= 400 and _PREFERRED_LINE.search(line)
                     and _QUALIFICATION_WORDS.search(line)]
        if preferred:
            fields['preferred_qualifications'] = '\n'.join(dict.fromkeys(preferred))[:2000]
    return fields


SOURCE_KEYS = ('source_board', 'salary', 'salary_min', 'salary_max', 'salary_currency', 'salary_period', 'skills',
               'work_mode', 'job_type', 'experience', 'compensation', 'baseSalary', 'salaryRange', 'structured_fields')


def _structured_salary(source):
    # Only explicit board adapters mark structured_fields; legacy guesses are not facts.
    sf = source.get('structured_fields') or {}
    values = sf.get('salary') or source.get('baseSalary') or source.get('salaryRange')
    if values is None and not source.get('source_board') and (source.get('salary_min') is not None or source.get('salary_max') is not None):
        values = {'min': source.get('salary_min'), 'max': source.get('salary_max'), 'currency': source.get('salary_currency'), 'period': source.get('salary_period')}
    if not isinstance(values, dict):
        return []
    value = values.get('value') if isinstance(values.get('value'), dict) else values
    low, high = value.get('minValue', value.get('min')), value.get('maxValue', value.get('max'))
    if low is None and high is None and isinstance(value.get('value'), (int, float)):
        low = high = value['value']
    if low is None and high is None:
        return []
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 for v in (low, high) if v is not None):
        return []
    unit = str(value.get('unitText') or values.get('period') or value.get('interval') or '').lower()
    period = {'hourly': 'hour', 'annually': 'year', 'annual': 'year', 'yearly': 'year',
              'monthly': 'month', 'weekly': 'week', 'daily': 'day'}.get(unit, unit)
    if period not in _PERIODS:
        # Lever/Ashby style intervals: "per-year-salary", "per_hour_wage", "PER_YEAR".
        token = re.search(r'\b(hour|year|annum|month|week|day)\b', period.replace('-', ' ').replace('_', ' '))
        period = {'annum': 'year'}.get(token.group(1), token.group(1)) if token else period
    resolved_currency = values.get('currency') or source.get('salary_currency')
    resolved_period = period if period in _PERIODS else None
    return [{'min': low, 'max': high, 'currency': resolved_currency,
             'period': resolved_period, 'component': values.get('component', 'base'),
             'location': values.get('location'),
             'text': format_salary(low, high, resolved_currency, resolved_period),
             'source': 'board', 'status': 'explicit',
             'evidence': {'field': 'structured_fields.salary', 'value': deepcopy(values)}}]


_CURRENCY_SYMBOLS = {'USD': '$', 'CAD': 'CA$', 'AUD': 'AU$', 'NZD': 'NZ$', 'SGD': 'SG$',
                     'HKD': 'HK$', 'GBP': '£', 'EUR': '€', 'INR': '₹', 'JPY': '¥'}


def format_salary(low, high, currency=None, period=None):
    """Render a structured board salary as display text.

    The raw structured payload stays on the candidate's `evidence`, so nothing
    is lost -- but `salary` is surfaced directly in the dashboard and must be
    human readable rather than a serialized dict.
    """
    def money(value):
        if value is None:
            return ''
        number = f'{value:,.0f}' if float(value) == int(float(value)) else f'{value:,.2f}'
        if not currency:
            return number
        symbol = _CURRENCY_SYMBOLS.get(str(currency).upper())
        return f'{symbol}{number}' if symbol else f'{currency} {number}'

    if low is not None and high is not None and low != high:
        amount_text = f'{money(low)} - {money(high)}'
    else:
        amount_text = money(low if low is not None else high)
    if not amount_text:
        return ''
    return f'{amount_text} per {period}' if period else amount_text


# "Remote Sensing Scientist" / "Remote Patient Monitoring" name the work, not where it happens.
_PLACE_MODE = [('Remote', re.compile(r'\bremote\b(?![- ]?(?:sensing|control|patient|monitoring|operations?|operator|pilot|'
                                     r'access|support center|site manager|surgery|camera))', re.I)), ('Hybrid', re.compile(r'\bhybrid\b', re.I)),
               ('Onsite', re.compile(r'\b(?:on[- ]?site|in[- ]office)\b', re.I))]
_WORK_MODES = ('Remote', 'Hybrid', 'Onsite')
_JOB_TYPES = ('full_time', 'part_time', 'contract', 'temporary', 'internship')


def _place_mode(title, location):
    """Board location/title such as "Remote - US" or "Analyst (Hybrid)"; None when absent or mixed."""
    for field, value in (('location', location), ('title', title)):
        found = {mode for mode, pattern in _PLACE_MODE if pattern.search(value or '')}
        if len(found) == 1:
            return {'value': found.pop(), 'status': 'explicit', 'source': 'board_' + field,
                    'evidence': {'field': field, 'value': value}}
    return None


def _canonical(key, val):
    """A board's own work mode / job type value in this module's vocabulary, or None."""
    mapping = {'remote': 'Remote', 'hybrid': 'Hybrid', 'on_site': 'Onsite', 'onsite': 'Onsite', 'on site': 'Onsite',
               'in person': 'Onsite',
               'full-time': 'full_time', 'full time': 'full_time', 'part-time': 'part_time', 'part time': 'part_time',
               'full_time': 'full_time', 'part_time': 'part_time', 'contract': 'contract', 'temporary': 'temporary',
               'internship': 'internship'}
    raw = str(val or '').lower().strip()
    normalized = re.sub(r'[_-]+', ' ', raw).strip()
    canonical = mapping.get(raw) or mapping.get(normalized)
    if key == 'work_mode':
        return canonical if canonical in _WORK_MODES else None
    if canonical in _JOB_TYPES:
        return canonical
    if re.search(r'\bfull\s*time\b', normalized): return 'full_time'
    if re.search(r'\bpart\s*time\b', normalized): return 'part_time'
    if re.search(r'\b(?:internship|intern|co op)\b', normalized): return 'internship'
    if re.search(r'\b(?:contract|contractor|fixed term|freelance)\b', normalized): return 'contract'
    if re.search(r'\b(?:temporary|seasonal)\b', normalized): return 'temporary'
    return None


def _rules(text, title, source, location=''):
    salaries = _structured_salary(source)
    board_salary = source.get('salary')
    if isinstance(board_salary, str) and board_salary.strip():
        salaries.extend(salary_candidates('Salary: ' + description_text(board_salary), 'salary', 'board'))
    compensation = source.get('compensation')
    if isinstance(compensation, dict):
        for key in ('compensationTierSummary', 'scrapeableCompensationSalarySummary', 'salary', 'salaryRange', 'payRange'):
            value = compensation.get(key)
            if isinstance(value, str):
                salaries.extend(salary_candidates(description_text(value), 'compensation.' + key, 'board'))
    salaries.extend(salary_candidates(text))
    skills = skill_candidates(text)
    # Merge additional source skills only when the JD supports the term.
    existing = source.get('skills') or []
    if isinstance(existing, str):
        existing = [s.strip() for s in existing.split(',') if s.strip()]
    names = {s['name'].casefold() for s in skills}
    for name in existing if isinstance(existing, list) else []:
        if not isinstance(name, str) or name.casefold() in names:
            continue
        match = re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', text, re.I)
        if match:
            skills.append({'name': name, 'requirement': requirement(text, match.start(), match.end()), 'source': 'jd',
                           'status': 'explicit', 'evidence': evidence(text, match.start(), match.end(), context=True)})
            names.add(name.casefold())
    modes = work_mode(text)
    if modes.get('source') != 'jd_label':
        # The board's own location/title ("Remote - US") beats free-text guesses.
        placed = _place_mode(title, location)
        if placed:
            modes = placed
    types = employment_type(title + '\n' + text)
    if types.get('evidence'):
        types['evidence']['field'] = 'title_and_description'
    sf = source.get('structured_fields') or {}
    for key, obj in (('work_mode', modes), ('job_type', types)):
        val = sf.get(key)
        canonical = _canonical(key, val)
        if canonical:
            if obj['value'] and canonical != obj['value']:
                obj.update(value=None, status='conflict', candidates=[obj.copy(), {'value': canonical, 'source': 'board'}])
            else:
                obj.update(value=canonical, source='board', status='explicit', evidence={'field': 'structured_fields.' + key, 'value': val})
        elif not obj['value'] and obj.get('status') == 'not_stated':
            # Last resort: the scraper's own top-level value (e.g. board metadata).
            fallback = _canonical(key, source.get(key))
            if fallback:
                obj.update(value=fallback, source='board_field', status='explicit',
                           evidence={'field': key, 'value': source.get(key)})
    education = []
    for match in re.finditer(r"\b(?:bachelor'?s?|master'?s?|Ph\.?D\.?|doctorate|associate'?s?|high school diploma|GED)(?:\s+degree)?\b", text, re.I):
        education.append({'value': match.group(), 'requirement': requirement(text, match.start(), match.end()),
                          'evidence': evidence(text, match.start(), match.end(), context=True), 'source': 'jd'})
    return {'version': VERSION, 'salary_candidates': salaries, 'skills': skills,
            'work_mode': modes, 'job_type': types, 'experience': experience_candidates(text),
            'education': education, 'model': {'status': 'disabled'}}


def project(raw, result, text, original, source, key):
    """Project evidence-rich results to the existing flat job schema."""
    result = deepcopy(result)
    # Deduplicate equivalent candidates without merging distinct currencies/regions/components.
    candidates = []
    seen = set()
    for candidate in result['salary_candidates']:
        signature = tuple(candidate.get(k) for k in ('min', 'max', 'currency', 'period', 'component', 'location'))
        if signature not in seen:
            seen.add(signature)
            candidates.append(candidate)
    result['salary_candidates'] = candidates
    base = [c for c in candidates if c['component'] == 'base']
    primary = base or [c for c in candidates if c['component'] == 'unspecified']
    selected = primary[0] if len(primary) == 1 else None
    if len(primary) > 1:
        # Real pay is a range; stray single amounts nearby (funding, stipends, "1.5x") are not.
        ranges = [c for c in primary if c['min'] is not None and c['max'] is not None and c['min'] < c['max']]
        ranges = [c for c in ranges if c['currency']] or ranges
        currencies = {c['currency'] for c in ranges}
        periods = {c['period'] for c in ranges if c['period']}
        # Several ranges of one scale and currency are pay zones: one overall range.
        if ranges and len(currencies) == 1 and len(periods) <= 1 and \
                max(c['max'] for c in ranges) <= 4 * min(c['min'] for c in ranges):
            if len(ranges) == 1:
                selected = ranges[0]
            else:
                low, high = min(c['min'] for c in ranges), max(c['max'] for c in ranges)
                currency, period = next(iter(currencies)), next(iter(periods), None)
                selected = {'min': low, 'max': high, 'currency': currency, 'period': period, 'status': 'combined',
                            'text': format_salary(low, high, currency, period)}
    result['salary_status'] = (selected['status'] if selected else 'ambiguous' if len(primary) > 1 else 'not_stated')
    result['source_fields'] = deepcopy(source)
    result['input_hash'] = key
    result['description_sha256'] = sha256(text.encode()).hexdigest()
    names = list(dict.fromkeys(s['name'] for s in result['skills']))
    experiences = [e for e in result['experience'] if e.get('requirement') == 'required'] or result['experience']
    exp = max(experiences, key=lambda item: item.get('min') or 0) if experiences else None
    out = dict(raw)
    sections = section_fields(text)
    out.update(description=text, description_raw=original, extraction=result,
               salary=selected['text'] if selected else (raw.get('salary') or ''),
               salary_min=selected['min'] if (selected and selected.get('min') is not None) else raw.get('salary_min'),
               salary_max=selected['max'] if (selected and selected.get('max') is not None) else raw.get('salary_max'),
               salary_currency=(selected['currency'] or '') if selected else (raw.get('salary_currency') or ''),
               salary_period=selected['period'] if (selected and selected.get('period')) else raw.get('salary_period'),
               skills=', '.join(names), skills_required=list(dict.fromkeys(s['name'] for s in result['skills'] if s['requirement'] == 'required')),
               skills_preferred=list(dict.fromkeys(s['name'] for s in result['skills'] if s['requirement'] == 'preferred')),
               work_mode=result['work_mode']['value'] or '', job_type=result['job_type']['value'] or '',
               is_remote=(result['work_mode']['value'] == 'Remote') if result['work_mode']['value'] else None,
               experience=exp['evidence']['text'] if exp else '',
               experience_min=exp['min'] if exp else None, experience_max=exp['max'] if exp else None,
               education_requirements=list(dict.fromkeys(item['value'] for item in result['education'])),
               basic_qualifications=raw.get('basic_qualifications') or sections.get('basic_qualifications'),
               preferred_qualifications=raw.get('preferred_qualifications') or sections.get('preferred_qualifications'),
               key_responsibilities=raw.get('key_responsibilities') or sections.get('key_responsibilities'))
    return out


def extract_job(raw):
    from .storage import cached_rules
    previous = raw.get('extraction') or {}
    # Preserve the original board fields across extractor-version upgrades.
    # Reconstructing them from already projected flat fields would turn an old
    # derived salary/skill into falsely authoritative board evidence.
    source = previous.get('source_fields') if isinstance(previous.get('source_fields'), dict) else None
    if source is None:
        source = {k: deepcopy(raw[k]) for k in SOURCE_KEYS if k in raw}
    elif raw.get('structured_fields'):
        source = dict(source, structured_fields=deepcopy(raw['structured_fields']))
    description = raw.get('description') or raw.get('content') or raw.get('snippet') or ''
    original = raw.get('description_raw') or description
    # A later detail stage can replace a list summary; do not reuse its old raw text.
    if previous and previous.get('description_sha256') != sha256(description_text(description).encode()).hexdigest():
        original = description
    text = description_text(original)
    title = str(raw.get('job_title') or raw.get('title') or raw.get('jobTitle') or '')
    location = str(raw.get('location') or raw.get('location_str') or raw.get('jobLocation') or '')
    key = sha256(json.dumps([VERSION, text, title, location, source, taxonomy_version(os.getenv('JOB_SKILLS_FILE', ''))], sort_keys=True, default=str).encode()).hexdigest()
    result = cached_rules(key, lambda: _rules(text, title, source, location))
    if previous.get('input_hash') == key and previous.get('model', {}).get('status') == 'completed':
        result = previous
    return project(raw, result, text, original, source, key)
