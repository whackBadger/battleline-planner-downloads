"""Extract published price rows from one English Munitorum Field Manual faction page."""
import json
import re
import unicodedata
from html.parser import HTMLParser


class Node:
    def __init__(self, tag, attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []

    def elements(self, tag=None):
        return [n for n in self.children if isinstance(n, Node) and (not tag or n.tag == tag)]

    def walk(self):
        yield self
        for child in self.elements():
            yield from child.walk()

    def text(self):
        return ' '.join(' '.join(n.text() if isinstance(n, Node) else n for n in self.children).split())


class ManualPage(HTMLParser):
    """Read visible HTML including React's streamed placeholders, without executing any script."""
    void = set('area base br col embed hr img input link meta param source track wbr'.split())

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node('document')
        self.stack = [self.root]
        self.slots = {}
        self.moves = {}
        self.complete = False
        self.element_count = 0

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.element_count += 1
        if len(self.stack) > 100 or self.element_count > 75000:
            raise ValueError('Field Manual page exceeds layout limits')
        self.stack[-1].children.append(node)
        slot = node.attrs.get('id', '')
        if slot.startswith('S:') and 'hidden' in node.attrs:
            if slot in self.slots:
                raise ValueError('Duplicate Field Manual section')
            self.slots[slot] = node
        if tag not in self.void:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.void:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == 'html':
            self.complete = True
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        node = self.stack[-1]
        if node.tag == 'script':
            # Only accept explicit slot moves. All other JS/data is discarded.
            if node.attrs.get('src') is None and not data.lstrip().startswith('self.__next_f') and ('$RS(' in data or '$RC(' in data):
                for op, a, b in re.findall(r'\$(RS|RC)\("([SPB]:[\da-f]+)","([SPB]:[\da-f]+)"\)', data):
                    slot, destination = (a, b) if op == 'RS' else (b, a)
                    self.moves[destination] = slot
        elif node.tag != 'style' and data.strip():
            node.children.append(data.strip())

    def resolve(self):
        if not self.complete:
            raise ValueError('Incomplete Field Manual page')
        used = set()

        def visit(node, depth):
            if depth > 100:
                raise ValueError('Invalid Field Manual layout')
            result = []
            for child in node.children:
                if isinstance(child, str):
                    result.append(child)
                elif child.tag == 'template':
                    destination = child.attrs.get('id', '')
                    if destination.startswith(('P:', 'B:')):
                        slot = self.moves.get(destination)
                        if slot not in self.slots or slot in used:
                            raise ValueError('Incomplete Field Manual section')
                        used.add(slot)
                        result.extend(visit(self.slots[slot], depth + 1))
                elif child.tag not in ('script', 'style') and child.attrs.get('id', '') not in self.slots:
                    child.children = visit(child, depth + 1)
                    result.append(child)
            return result
        self.root.children = visit(self.root, 0)


def copy_range(label):
    if label == 'YOUR UNIT COSTS':
        return 1, None
    match = re.fullmatch(r'YOUR (\d+)(?:ST|ND|RD|TH)(?: TO (\d+)(?:ST|ND|RD|TH)|(\s*\+))? UNITS? COSTS?', label)
    if not match:
        raise ValueError('Unknown copy price in Field Manual')
    low, high, above = match.groups()
    start = int(low)
    end = None if above else int(high or low)
    if start < 1 or (end is not None and end < start):
        raise ValueError('Invalid copy range')
    return start, end


def price_rows(group, enhancements=False):
    lists = [n for n in group.walk() if n.tag == 'ul']
    if len(lists) != 1 or not lists[0].elements('li'):
        raise ValueError('Missing price rows')
    rows = []
    for row in lists[0].elements('li'):
        parts = [n.text() for n in row.walk() if n.tag == 'span' and not n.elements('span')]
        # A printed leader/support eligibility note follows the enhancement it qualifies.
        if enhancements and parts and parts[0].upper() in ('LEADER:', 'SUPPORT:'):
            continue
        price = re.fullmatch(r'(?:(?:▲|▼)\s*\([+−–-]\d+\)\s*)?(\d{1,3}(?:,\d{3})+|\d+)\s+pts?', parts[-1], re.I) if parts else None
        if len(parts) != 2 or not parts[0] or not price:
            raise ValueError('Unrecognized Field Manual price row')
        rows.append((parts[0], int(price[1].replace(',', ''))))
    return rows


def unit_entry(name, groups):
    unit = {'name': name, 'tiers': [], 'wargear': []}
    try:
        for group in groups:
            contents = group.elements()
            if not contents:
                continue
            label = contents[0].text().upper()
            if label == 'WARGEAR OPTIONS':
                unit['wargear'].extend({'name': key, 'points': cost} for key, cost in price_rows(group))
            elif label.startswith('YOUR ') or any(n.tag == 'ul' for n in group.walk()):
                start, end = copy_range(label)
                by_size = {}
                ambiguous = set()
                named_roles = False
                for key, cost in price_rows(group):
                    if (name.upper() == 'TIDEWALL SHIELDLINE' and key == '+ 1 Tidewall Defence Platform'
                            and start == 1 and end is None):
                        unit['wargear'].append({'name': 'Tidewall Defence Platform', 'points': cost})
                        continue
                    # Some squads publish fixed groups such as "1 Captain, 4 Scouts".
                    # Counts identify a price only if every composition of that size agrees.
                    roles = [re.fullmatch(r'(\d+) ([\w’\x27-]+(?: [\w’\x27-]+)*)', part.strip()) for part in key.split(',')]
                    if not all(roles) or any(int(role[1]) < 1 or re.search(r'\d', role[2]) for role in roles):
                        raise ValueError('This unit uses a special model-size or configuration price')
                    named_roles |= any(role[2].lower() not in ('model', 'models') for role in roles)
                    size = sum(int(role[1]) for role in roles)
                    if size in by_size and cost != by_size[size]:
                        ambiguous.add(size)
                    by_size[size] = cost
                sizes = [{'models': size, 'points': price} for size, price in by_size.items() if size not in ambiguous]
                tier = {'fromCopy': start, 'toCopy': end, 'sizes': sizes}
                if named_roles or ambiguous:
                    tier['exactSizes'] = True
                if ambiguous:
                    tier['ambiguousSizes'] = sorted(ambiguous)
                unit['tiers'].append(tier)
            elif label == 'WARGEAR COSTS REMOVED':
                unit['wargearCostsRemoved'] = True
            elif label not in ('LEADER', 'SUPPORT') and ('COST' in label or 'WARGEAR' in label or 'POINT' in label):
                raise ValueError('Unknown price condition')
        if not unit['tiers']:
            raise ValueError('Missing unit price tiers')
    except ValueError as error:
        unit['issue'] = str(error)
    return unit


def detachment_entry(name, groups):
    upgrades = []
    for group in groups:
        contents = group.elements()
        if contents and contents[0].text() == 'ENHANCEMENTS':
            try:
                upgrades.extend({'name': key, 'points': cost} for key, cost in price_rows(group, enhancements=True))
            except ValueError:
                return {'name': name, 'upgrades': [], 'issue': 'Unrecognized enhancement pricing'}
    return {'name': name, 'upgrades': upgrades}


def page_prices(html, slug):
    doc = ManualPage()
    doc.feed(html)
    doc.close()
    doc.resolve()
    nodes = list(doc.root.walk())
    titles = [n.text() for n in nodes if n.tag == 'h1']
    title_slug = re.sub(r'[^a-z0-9]+', '-', unicodedata.normalize('NFKD', titles[0]).lower()
                        .replace("'", '').replace('’', '')).strip('-') if len(titles) == 1 else ''
    if slug != title_slug:
        raise ValueError('Field Manual returned a different faction')
    version = next((n.text() for n in nodes if n.tag == 'h2' and re.fullmatch(r'v[\d.]+', n.text(), re.I)), '')
    def cards(node, section=''):
        children = node.elements()
        if children and children[0].tag == 'h3':
            section = children[0].text()
        if 'print:break-inside-avoid-page' in node.attrs.get('class', '').split():
            yield node, section
        else:
            for child in children:
                yield from cards(child, section)

    units, detachments = [], []
    for card, section in cards(doc.root):
        groups = card.elements()
        if not groups or not groups[0].text():
            raise ValueError('Missing Field Manual card name')
        header, *sections = groups
        header_fields = header.elements('span')
        if section == 'DETACHMENTS':
            if not header_fields:
                raise ValueError('Invalid detachment header')
            detachments.append(detachment_entry(header_fields[0].text(), sections))
        else:
            # Changed prices have an indicator beside the name, in a separate span.
            name = header_fields[0].text() if header_fields else header.text()
            unit = unit_entry(name, sections)
            unit['section'] = section
            units.append(unit)
    if not units or not any(row['tiers'] for row in units):
        raise ValueError('Field Manual has no readable unit prices')
    return {'faction': slug, 'version': version, 'url': 'https://mfm.warhammer-community.com/en/' + slug,
            'units': units, 'detachments': detachments}


def parse_faction(html, slug):
    return json.dumps(page_prices(html, slug), separators=(',', ':'))


def parse_game_data(html, slug):
    from faction_pack import build_faction_pack
    return build_faction_pack(page_prices(html, slug)).decode('utf-8')
