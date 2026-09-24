"""Render descriptive cost sensitivity; never selects or promotes a strategy."""
import argparse
from pathlib import Path
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots


def render(table, output):
    model_col = next(c for c in ('family', 'candidate_id', 'model', 'candidate') if c in table)
    cost_col = next(c for c in ('cost_bps', 'one_way_cost_bps', 'impact_bps') if c in table)
    delay_col = next(c for c in ('delay', 'delay_sessions', 'delay_days') if c in table)
    models = list(table[model_col].unique())
    fig = make_subplots(rows=len(models), cols=1, subplot_titles=models, vertical_spacing=.18)
    for row, model in enumerate(models, 1):
        subset = table.loc[table[model_col] == model]
        for delay, group in subset.groupby(delay_col):
            group = group.sort_values(cost_col)
            # Bucket summaries retain the full minimum/maximum envelope. They do
            # not sample a handful of favorable individual scenarios.
            bucket = pd.cut(group[cost_col], bins=100, duplicates='drop')
            summary = group.groupby(bucket, observed=True).agg(
                cost=(cost_col, 'mean'), low=('net_pnl', 'min'),
                middle=('net_pnl', 'median'), high=('net_pnl', 'max'))
            label = f'{model}, extra delay {delay}'
            fig.add_trace(go.Scatter(x=summary.cost, y=summary.low, mode='lines',
                line=dict(width=0), showlegend=False, hoverinfo='skip'), row=row, col=1)
            fig.add_trace(go.Scatter(x=summary.cost, y=summary.high, mode='lines',
                line=dict(width=0), fill='tonexty', name=label+' range'), row=row, col=1)
            fig.add_trace(go.Scatter(x=summary.cost, y=summary.middle, mode='lines',
                name=label+' median'), row=row, col=1)
        fig.add_hline(y=0, line_dash='dot', row=row, col=1)
        fig.update_xaxes(title_text='One-way impact assumption (bps); crypto fee is additional', row=row, col=1)
        fig.update_yaxes(title_text='YTD net trading P&L ($)', row=row, col=1)
    fig.update_layout(template='plotly_white', height=450*len(models),
        title='Historical sensitivity: 100 equal-width cost buckets per delay; shared market path',
        margin=dict(t=110), legend=dict(orientation='h', y=-.15))
    output = Path(output)
    if output.exists():
        raise FileExistsError('Preserve prior chart')
    fig.write_html(output, include_plotlyjs=True, full_html=True)


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--results', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    a=p.parse_args()
    render(pd.read_parquet(a.results), a.output)
