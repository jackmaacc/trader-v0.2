"""Frozen ETF research definitions shared by offline replay and shadow decisions."""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math

ETF_UNIVERSE = ('SPY', 'QQQ', 'IWM', 'TLT', 'GLD')
CANDIDATE_IDS = ('MR30', 'MR60', 'MOM20', 'MOM60')

@dataclass(frozen=True)
class StrategySpec:
    candidate_id: str
    universe: tuple[str, ...] = ETF_UNIVERSE
    version: str = 'etf-net-edge-v1'
    max_trade_risk: float = .001
    max_portfolio_risk: float = .005
    max_gross: float = .50
    max_name: float = .10
    max_equity_cluster: float = .20
    max_overnight: float = .25
    daily_loss_halt: float = .005
    drawdown_halt: float = .03
    commission_bps: float = 0.
    spread_bps: float = 0.
    slippage_bps: float = 7.
    daily_overhead: float | None = None

    def __post_init__(self):
        if self.candidate_id not in CANDIDATE_IDS or self.universe != ETF_UNIVERSE:
            raise ValueError('Only the four registered candidates and fixed five-ETF universe are supported')
        for name in ('max_trade_risk', 'max_portfolio_risk', 'max_gross', 'max_name', 'max_equity_cluster', 'max_overnight', 'daily_loss_halt', 'drawdown_halt'):
            if not math.isfinite(getattr(self, name)) or not 0 < getattr(self, name) <= 1:
                raise ValueError(f'Invalid {name}')
        caps={'max_trade_risk':.001,'max_portfolio_risk':.005,'max_gross':.5,'max_name':.1,
              'max_equity_cluster':.2,'max_overnight':.25,'daily_loss_halt':.005,'drawdown_halt':.03}
        if any(getattr(self,name)>cap for name,cap in caps.items()):
            raise ValueError('Strategy risk settings may tighten but cannot weaken the registered mandate')
        for name in ('commission_bps', 'spread_bps', 'slippage_bps', 'daily_overhead'):
            if name == 'daily_overhead' and self.daily_overhead is None:
                continue
            if not math.isfinite(getattr(self, name)) or getattr(self, name) < 0:
                raise ValueError(f'Invalid {name}')

        if self.one_way_impact >= 1 or self.commission_rate >= 1:
            raise ValueError('Modeled trading costs must remain below 100 percent per side')

    @property
    def family(self):
        return 'MR' if self.candidate_id.startswith('MR') else 'MOM'

    @property
    def horizon(self):
        return int(self.candidate_id[2:] if self.family == 'MR' else self.candidate_id[3:])

    @property
    def spec_hash(self):
        return sha256(json.dumps(asdict(self), sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    @property
    def one_way_impact(self):
        # Full quoted spread is charged half on each side, slippage once per side.
        return (self.spread_bps / 2 + self.slippage_bps) / 10000

    @property
    def commission_rate(self):
        return self.commission_bps / 10000
