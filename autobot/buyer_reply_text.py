"""Conservative reply text parsing shared by browser and IMAP transports."""
import hashlib
import json
import re


def exact_identity_reason(row, quote):
    """Only a literal, complete identity can automatically confirm a quote.

    Keep decimals and token boundaries: 4x1,5 is not 4x15, and 4x15 is not
    4x150. Numbered shorthand still maps the reply but does not prove its SKU.
    """
    def canonical(value):
        text = str(value or '').casefold().replace('ё', 'е')
        text = re.sub(r'(?<=\d),(?=\d)', '.', text)
        text = text.translate(str.maketrans({'×':'х', 'x':'х', '–':'-', '—':'-', '²':'2', '³':'3'}))
        return re.sub(r'\s+', ' ', text).strip()
    name, evidence = canonical(row.get('name')), canonical(quote)
    if re.search(r'аналог|вместо|замен|не\s+соответств|отлича|нет\s+в\s+наличии', evidence):
        return 'Предложена замена или требуется уточнить соответствие'
    if not name or not re.search(r'(?<!\w)' + re.escape(name) + r'(?![\w.,])', evidence):
        return 'В ответе нет полного наименования запрошенной позиции'
    specification = row.get('specification') or {}
    requirements = specification.get('requirements', row.get('requirements')) or {}
    from autobot.market_requirements import technical_conflict, technical_specs
    observed = {(s['kind'], canonical(s['value'])) for s in technical_specs(quote)}
    for trait in requirements.get('specifications', []):
        fragment = canonical(trait.get('evidence') if isinstance(trait, dict) else trait)
        matches = fragment in evidence if fragment else (
            isinstance(trait, dict) and (trait.get('kind'), canonical(trait.get('value'))) in observed)
        if not matches:
            label = trait.get('label') or trait.get('evidence') if isinstance(trait, dict) else trait
            return 'Ответ не подтверждает характеристику: ' + str(label)
    return technical_conflict(row.get('name'), quote)


def unquoted(body):
    # Remove recognizable quoted-message boundaries, keeping original evidence
    # in the snapshot. Never derive a price from the customer's quoted request.
    cut=re.search(r'(?im)^(?:\s*>|\s*-{3,}.*(?:сообщени|message)|\s*On .+ wrote:|\s*(?:От|From):\s|.*(?:писал|писала)\s*:|'
                  r'\s*(?:понедельник|вторник|среда|четверг|пятница|суббота|воскресенье),?\s+\d{1,2}\s+[а-я]+\s+\d{4}[^\n]*\sот\s)',body)
    return body[:cut.start()].strip() if cut else body.strip()


def prices(body,positions):
    """Keep literal unit prices; map grouped replies only by explicit identity.

    Numbered replies refer to the saved request, never to the order of prices
    in the answer. Alternatives and totals remain in the raw message. Even a
    mapped price needs the existing server-side specification/unit review.
    """
    if not positions: return []
    body=unquoted(body)
    pattern=re.compile(r'(?<!\w)(\d+(?:[ \u00a0]\d{3})*(?:[.,]\d{1,2})?)\s*(?:руб(?:\.|лей|ля)?|₽|RUB)\s*(?:/|за)\s*(пог\.\s*м|пм|м[²³23]?|шт|кг|т)(?!\w)',re.I)
    vat_pattern=re.compile(r'без\s+НДС|с\s+НДС|включая\s+НДС|НДС\s+(?:включ[её]н|не\s+облагается)',re.I)
    def normalized(value):
        return re.sub(r'[^\w]+','',value.casefold().replace('ё','е').replace('×','х'))
    by_line={p.get('line',i):p for i,p in enumerate(positions,1)}
    lines=body.splitlines()
    shared='\n'.join(line for line in lines if re.fullmatch(
        r'\s*(?:(?:все\s+)?цены\s+(?:(?:указаны|приведены)\s+)?)?(?:'+vat_pattern.pattern+r')\s*[.!]?\s*',line,re.I))
    shared_vat=list(vat_pattern.finditer(shared))
    global_vat=shared_vat[0][0] if len({m[0].casefold() for m in shared_vat})==1 else ''
    delivery=re.search(r'[^\n]*доставк[^\n]*',body,re.I)
    candidates={}
    for quote in lines:
        matches=list(pattern.finditer(quote))
        if not matches or len(quote)>4000: continue
        if re.search(r'\b(?:итого|общая стоимость|за весь|за комплекс|доставк\w*)\b',quote[:matches[0].start()],re.I): continue
        marker=re.match(r'^\s*(?:(?:поз(?:иция)?\.?|№)\s*)?(\d+)\s*[.):—-](?!\d)\s*',quote,re.I)
        named=[number for number,p in by_line.items() if normalized(p.get('name','')) and normalized(p['name']) in normalized(quote[:matches[0].start()])]
        number=int(marker[1]) if marker else named[0] if len(named)==1 else next(iter(by_line)) if len(positions)==1 else None
        if number not in by_line or marker and named and number not in named: continue
        # Repeated or alternative prices for one position cannot be chosen.
        if number in candidates or len(matches)!=1:
            candidates[number]=None
            continue
        match=matches[0]
        vats=list(vat_pattern.finditer(quote))
        vat=vats[0][0] if len({m[0].casefold() for m in vats})==1 else global_vat if not vats else ''
        availability=re.search(r'[^\n]*\b(?:в наличии|под заказ|нет в наличии)[^\n]*',quote if len(positions)>1 else body,re.I)
        candidates[number]={'line':number,'price':match[1],'unit':match[2].replace(' ',''),'vat':vat,
             'availability':availability[0][:400] if availability else '',
             'delivery':delivery[0][:600] if delivery else '',
             'exact_match':not exact_identity_reason(by_line[number], quote),'quote':quote}
    return [item for item in candidates.values() if item is not None]


def web_reply_fingerprint(sender, text, received_at):
    """Old browser IDs cannot be mapped to RFC IDs; compare preserved facts."""
    facts=[sender.lower(),re.sub(r'\s+',' ',unquoted(text)).strip(),int(received_at//60)]
    return hashlib.sha256(json.dumps(facts,ensure_ascii=False).encode()).hexdigest()
