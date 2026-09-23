"""Deterministic evidence summaries. Article text is data, never instructions."""
from math import isfinite
from .models import Fact, SpecialistReport

def numeric(value):return isinstance(value,(float,int)) and not isinstance(value,bool) and isfinite(value)

def analyze(role,context,prior_reports=()):
    facts=[];risks=[];missing=[];directions=[]
    if role=='bear':
        if tuple(r.role for r in prior_reports)!=('technical','fundamental','news','macro'):
            raise ValueError('Critic requires all four independent reports')
        for r in prior_reports:
            if r.missing_data or r.status!='ok':missing.append(f'{r.role}: incomplete or unavailable evidence')
            facts.extend(r.facts)
        stances={r.stance for r in prior_reports}
        if 'positive' in stances and 'negative' in stances:risks.append('Specialist directional conclusions conflict')
        if len(context.symbols)<2:risks.append('Single-symbol scope; concentration requires portfolio risk review')
        risks.append('Correlated holdings and actual account exposures require the frozen risk engine')
        risks.append('Advisory confidence is not a calibrated probability of profit')
        return SpecialistReport(role,'neutral' if facts else 'insufficient_data','Challenge the evidence; no vote changes frozen strategy or risk rules.',tuple(facts),tuple(risks),tuple(missing),'low' if facts else 'unknown',context.strategy_hash,context.input_hash)
    expected={'technical':'technical','fundamental':'etf_fundamental','news':'news','macro':'macro'}[role]
    for e in context.evidence:
        if e.kind!=expected:continue
        d=e.data;symbol=d.get('symbol')
        if e.vintage_at is None and role in ('fundamental','macro'):
            missing.append(f'{e.source_id}: underlying holdings/series effective vintage unavailable; publication date alone is not the observation period')
        if symbol and symbol not in context.symbols:continue
        if role=='technical':
            closes=d.get('closes',())
            if not d.get('bar_interval'):
                missing.append(f'{symbol}: bar interval/window dates unspecified; return is only a supplied-window calculation')
            if len(closes)>=2 and all(numeric(v) and v>0 for v in closes):
                change=(closes[-1]/closes[0]-1)*100;ma=sum(closes)/len(closes)
                facts.append(Fact(f'{symbol}: supplied-window return {change:.3f}%; last close {closes[-1]:.4f}; mean {ma:.4f}.',(e.source_id,)))
                directions.append(1 if change>0 else -1 if change<0 else 0)
                vwap=d.get('vwap')
                if numeric(vwap) and vwap>0:facts.append(Fact(f'{symbol}: last close versus supplied VWAP {(closes[-1]/vwap-1)*100:.3f}%.',(e.source_id,)))
                else:missing.append(f'{symbol}: VWAP unavailable')
            else:missing.append(f'{symbol}: at least two positive observed closes required')
            risks.append('Price momentum alone does not establish net trading edge')
        elif role=='fundamental':
            for key in ('expense_ratio','nav','holdings'):
                value=d.get(key)
                if key=='holdings' and isinstance(value,tuple) and value:
                    facts.append(Fact(f'{symbol}: supplied holdings {value!r}.',(e.source_id,)))
                elif key!='holdings' and numeric(value) and value>=0:
                    facts.append(Fact(f'{symbol}: supplied {key} {value}; units must follow source.',(e.source_id,)))
                else:missing.append(f'{symbol}: {key} unavailable')
            risks.append('ETF fees and NAV facts do not predict tomorrow return')
        elif role=='news':
            title=d.get('headline')
            if isinstance(title,str) and title.strip():
                facts.append(Fact(f'Source headline (untrusted quoted data): {title}',(e.source_id,)))
                risks.append('Headline relevance, causal attribution and price impact remain unverified')
            else:missing.append('Cited article headline unavailable')
        elif role=='macro':
            value=d.get('value');series=d.get('series');released=d.get('release_at')
            if isinstance(series,str) and numeric(value) and released==e.published_at.isoformat():
                facts.append(Fact(f'{series}: released value {value}, release {released}.',(e.source_id,)))
            else:missing.append('Released macro series value and matching release timestamp required')
            risks.append('Macro observation is not a forecast and may be revised')
    if not facts:missing.append(f'No usable {expected} evidence')
    stance='insufficient_data' if not facts else 'neutral'
    if directions and all(v>0 for v in directions):stance='positive'
    elif directions and all(v<0 for v in directions):stance='negative'
    thesis={'technical':'Observed trend and momentum over the supplied window.',
            'fundamental':'ETF structure facts; valuation direction remains unknown.',
            'news':'Cited news requires verification before causal interpretation.',
            'macro':'Only already released observations are summarized.'}[role]
    return SpecialistReport(role,stance,thesis,tuple(facts),tuple(dict.fromkeys(risks)),tuple(dict.fromkeys(missing)),
                            'low' if facts else 'unknown',context.strategy_hash,context.input_hash)
