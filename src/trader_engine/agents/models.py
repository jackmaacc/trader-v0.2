"""Immutable, point-in-time inputs and advisory outputs; no order capabilities."""
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from json import dumps
from math import isfinite
import re
from types import MappingProxyType
from typing import Mapping, Any
from urllib.parse import urlparse

ROLES = ('technical', 'fundamental', 'news', 'macro', 'bear')

def freeze(value):
    if isinstance(value, Mapping):
        if any(not isinstance(k, str) for k in value): raise ValueError('String data keys required')
        return MappingProxyType({k:freeze(v) for k,v in value.items()})
    if isinstance(value, (tuple,list)): return tuple(freeze(v) for v in value)
    if value is None or isinstance(value,(str,bool,int)): return value
    if isinstance(value,float) and isfinite(value): return value
    raise ValueError('Evidence must contain finite JSON data')

def plain(value):
    if isinstance(value,Mapping): return {k:plain(v) for k,v in value.items()}
    if isinstance(value,tuple): return [plain(v) for v in value]
    return value

def aware(value):
    if not isinstance(value,datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Timezone-aware timestamp required')

@dataclass(frozen=True)
class Evidence:
    source_id: str
    source_url: str
    kind: str
    observed_at: datetime
    published_at: datetime
    available_at: datetime
    data: Mapping[str,Any]
    vintage_at: datetime | None = None
    def __post_init__(self):
        if not self.source_id or not self.kind: raise ValueError('Source identity and kind required')
        if urlparse(self.source_url).scheme not in ('https','http') or not urlparse(self.source_url).netloc:
            raise ValueError('Source URL required')
        for t in (self.observed_at,self.published_at,self.available_at): aware(t)
        if self.vintage_at is not None:
            aware(self.vintage_at)
            if self.vintage_at > self.published_at: raise ValueError('Vintage cannot follow publication')
        if self.available_at < self.published_at: raise ValueError('Availability precedes publication')
        object.__setattr__(self,'data',freeze(self.data))

@dataclass(frozen=True)
class SharedContext:
    as_of: datetime
    strategy_hash: str
    symbols: tuple[str,...]
    evidence: tuple[Evidence,...]
    max_age_seconds: float = 86400
    # Monthly macro / quarterly fundamentals need explicitly selected tolerances.
    max_age_by_kind: Mapping[str,float] = field(default_factory=dict)
    def __post_init__(self):
        aware(self.as_of)
        if not re.fullmatch(r'[0-9a-f]{64}',self.strategy_hash) or not isfinite(self.max_age_seconds) or self.max_age_seconds<=0:
            raise ValueError('Strategy hash and positive freshness limit required')
        limits=dict(self.max_age_by_kind)
        if any(not isinstance(k,str) or not isinstance(v,(int,float)) or isinstance(v,bool) or not isfinite(v) or v<=0 for k,v in limits.items()):
            raise ValueError('Positive kind-specific freshness limits required')
        object.__setattr__(self,'max_age_by_kind',MappingProxyType(limits))
        object.__setattr__(self,'symbols',tuple(self.symbols))
        object.__setattr__(self,'evidence',tuple(self.evidence))
        ids=[e.source_id for e in self.evidence]
        if len(set(ids))!=len(ids): raise ValueError('Duplicate evidence source IDs')
        for e in self.evidence:
            if any(t>self.as_of for t in (e.observed_at,e.published_at,e.available_at)):
                raise ValueError('Future evidence rejected')
            limit=self.max_age_by_kind.get(e.kind,self.max_age_seconds)
            timestamps=(e.observed_at,e.published_at,e.available_at) + ((e.vintage_at,) if e.vintage_at is not None else ())
            if any((self.as_of-t).total_seconds()>limit for t in timestamps):
                raise ValueError('Stale publication, observation, availability or vintage rejected')
    @property
    def input_hash(self):
        body={'as_of':self.as_of.isoformat(),'strategy_hash':self.strategy_hash,'symbols':self.symbols,
              'max_age_seconds':self.max_age_seconds,'max_age_by_kind':dict(self.max_age_by_kind),'evidence':[
                  {'source_id':e.source_id,'source_url':e.source_url,'kind':e.kind,
                   'observed_at':e.observed_at.isoformat(),'published_at':e.published_at.isoformat(),
                   'available_at':e.available_at.isoformat(),'vintage_at':e.vintage_at.isoformat() if e.vintage_at else None,'data':plain(e.data)} for e in self.evidence]}
        return sha256(dumps(body,sort_keys=True,separators=(',',':')).encode()).hexdigest()

@dataclass(frozen=True)
class Fact:
    text: str
    evidence_ids: tuple[str,...]
    def __post_init__(self):object.__setattr__(self,'evidence_ids',tuple(self.evidence_ids))

@dataclass(frozen=True)
class SpecialistReport:
    role: str
    stance: str
    thesis: str
    facts: tuple[Fact,...]
    risks: tuple[str,...]
    missing_data: tuple[str,...]
    confidence: str
    strategy_hash: str
    input_hash: str
    status: str = 'ok'
    analysis_method: str = 'deterministic_evidence'
    order_authority: bool = False
    def __post_init__(self):
        for field in ('facts','risks','missing_data'):object.__setattr__(self,field,tuple(getattr(self,field)))
        if self.order_authority is not False:raise ValueError('Advisory agents never have order authority')

@dataclass(frozen=True)
class TeamReport:
    input_hash: str
    strategy_hash: str
    reports: tuple[SpecialistReport,...]
    evidence_sources: tuple = ()
    order_authority: bool = False
    def __post_init__(self):
        object.__setattr__(self,'reports',tuple(self.reports))
        object.__setattr__(self,'evidence_sources',tuple(self.evidence_sources))
        if self.order_authority is not False:raise ValueError('No order authority')

def validate_report(report,role,context):
    if not isinstance(report,SpecialistReport) or report.role!=role or report.input_hash!=context.input_hash or report.strategy_hash!=context.strategy_hash:
        raise ValueError('Unbound specialist report')
    if report.order_authority is not False or report.stance not in ('positive','negative','neutral','insufficient_data'):
        raise ValueError('Invalid advisory stance/authority')
    if report.confidence not in ('unknown','low','medium') or report.status not in ('ok','failed','timeout'):
        raise ValueError('Invalid confidence/status')
    if report.stance not in ('positive','negative','neutral','insufficient_data'):
        raise ValueError('Invalid stance')
    if not isinstance(report.thesis,str) or not report.thesis or any(not isinstance(v,str) for v in (*report.risks,*report.missing_data)):
        raise ValueError('Invalid report text schema')
    if report.analysis_method not in ('deterministic_evidence','provider_interpretation_unverified'):
        raise ValueError('Invalid analysis provenance')
    # Conservative phrase screen, not complete semantic hallucination detection.
    narrative=' '.join((report.thesis,*report.risks,*report.missing_data))
    if re.search(r'guarantee(?:d|s)?|assured|risk[\s-]*free',narrative,re.I):
        raise ValueError('Unsupported certainty language in advisory narrative')
    allowed={e.source_id for e in context.evidence}
    for fact in report.facts:
        if not isinstance(fact,Fact) or not fact.text or not fact.evidence_ids or not set(fact.evidence_ids)<=allowed:
            raise ValueError('Unsupported fact evidence')
    if not report.facts and (report.confidence!='unknown' or report.stance!='insufficient_data'):
        raise ValueError('Confidence or directional stance without facts')
    return report


@dataclass(frozen=True)
class SourceReference:
    source_id: str
    source_url: str
    kind: str
    observed_at: str
    published_at: str
    available_at: str
    vintage_at: str | None

    @classmethod
    def from_evidence(cls,e):
        return cls(e.source_id,e.source_url,e.kind,e.observed_at.isoformat(),
                   e.published_at.isoformat(),e.available_at.isoformat(),
                   e.vintage_at.isoformat() if e.vintage_at else None)
