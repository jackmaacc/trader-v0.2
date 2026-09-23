"""Concurrent advisory team; default execution is local and deterministic."""
from queue import Queue, Empty
from dataclasses import replace
from threading import Thread
from time import monotonic
from typing import Protocol
from .models import SharedContext, SpecialistReport, TeamReport, SourceReference, validate_report
from .specialists import analyze

class AnalysisProvider(Protocol):
    """Trusted application adapter; no tools, credentials or broker are supplied."""
    def analyze(self,role: str,context: SharedContext,prior_reports: tuple[SpecialistReport,...]) -> SpecialistReport: ...

def failed(role,context,status):
    return SpecialistReport(role,'insufficient_data','Analysis unavailable.',(),(),(status,),
                            'unknown',context.strategy_hash,context.input_hash,status=status)

def run_team(context: SharedContext, provider: AnalysisProvider | None=None, timeout_seconds=5.0):
    if not 0<timeout_seconds<=300:raise ValueError('Timeout must be in (0,300] seconds')
    results=Queue()
    def execute(role,prior):
        try:
            baseline=analyze(role,context,prior)
            result=baseline if provider is None else provider.analyze(role,context,prior)
            if provider is not None:
                result=replace(result,analysis_method='provider_interpretation_unverified',
                    thesis='Unverified provider interpretation: '+result.thesis,
                    risks=result.risks+('Provider interpretation is unverified; factual citation checks do not establish narrative truth.',))
            validate_report(result,role,context)
            # Provider may interpret existing facts but cannot introduce purported
            # factual claims merely by attaching a valid citation identifier.
            permitted={(f.text,f.evidence_ids) for f in baseline.facts}
            if any((f.text,f.evidence_ids) not in permitted for f in result.facts):
                raise ValueError('Provider introduced an unsupported factual claim')
        except Exception:
            result=failed(role,context,'failed')
        results.put((role,result))
    roles=('technical','fundamental','news','macro')
    deadline=monotonic()+timeout_seconds
    for role in roles:Thread(target=execute,args=(role,()),daemon=True).start()
    collected={}
    while len(collected)<4:
        try:
            role,result=results.get(timeout=max(0,deadline-monotonic()))
            collected[role]=result
        except Empty:break
    prior=tuple(collected.get(role,failed(role,context,'timeout')) for role in roles)
    # Deterministic critic always executes after finalizing the first four. This
    # makes provider outages unable to remove the required risk critique.
    critic=validate_report(analyze('bear',context,prior),'bear',context)
    return TeamReport(context.input_hash,context.strategy_hash,prior+(critic,),
                      tuple(SourceReference.from_evidence(e) for e in context.evidence))
