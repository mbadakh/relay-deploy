"""Offline review page using the existing Relay pricing-page visual language."""
import html
import json
from pathlib import Path

def write_report(path, config, plan, pricing, digest):
    esc=lambda x:html.escape(str(x),quote=True)
    css=(Path(__file__).parent.parent/'docs/review.css').read_text()
    summary=pricing['summary']
    cards=''.join(f'<div class="summary-card accent"><span class="summary-label">{esc(label)}</span><span class="summary-value">{esc(value)}</span></div>' for label,value in [
        ('Monthly estimate',f"${summary['monthlyTotal']} USD"),('Fixed monthly',f"${summary['monthlyFixed']}"),('Usage estimate',f"${summary['monthlyUsage']}"),('Annual estimate',f"${summary['annualTotal']}")])
    costs=''.join('<tr>'+''.join(f'<td>{esc(value)}</td>' for value in [c['name'],c['quantity'],c['unit'],'$'+c['monthly_cost'],c['assumption']])+'</tr>' for c in pricing['components'])
    changes=''.join(f'<tr><td class="resource-address">{esc(r["address"])}</td><td>{esc(", ".join(r["change"]["actions"]))}</td></tr>' for r in plan.get('resource_changes',[]) if r.get('mode')=='managed' and r['change']['actions']!=['no-op'])
    assumptions=''.join(f'<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>' for k,v in pricing['usageModel'].items())
    exclusions=''.join(f'<li>{esc(v)}</li>' for v in pricing['exclusions'])
    metadata=''.join(f'<dt>{esc(k)}</dt><dd>{esc(config[k])}</dd>' for k in ['name','account_id','region','users','home_lab','public_origin'])
    path.write_text(f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><title>Relay deployment review</title><style>{css}</style></head><body><header><strong>relay · Deployment review</strong></header><main style="max-width:1100px;margin:auto;padding:28px"><h1>Your server, your approval</h1><p class="sub">No resources from this plan have been applied. Review this report, then approve in your terminal.</p><section class="card"><div class="summary-grid">{cards}</div><p class="notice warning">Estimates use live AWS public prices and modeled usage. They are not a guaranteed bill. Taxes, unexpected traffic and the exclusions below may add charges.</p><dl class="result">{metadata}</dl><p>Plan SHA-256: <code>{esc(digest)}</code></p><p>Prices retrieved: {esc(pricing['priceSource']['queriedAt'])}</p></section><section class="card"><h2>Monthly cost breakdown</h2><div class="table-wrap"><table class="review-table"><thead><tr><th>Service</th><th>Quantity</th><th>Unit</th><th>Monthly</th><th>Assumption</th></tr></thead><tbody>{costs}</tbody></table></div></section><section class="card"><h2>Infrastructure changes</h2><div class="table-wrap"><table class="review-table"><thead><tr><th>Resource</th><th>Action</th></tr></thead><tbody>{changes}</tbody></table></div></section><section class="card"><h2>Usage assumptions</h2><div class="table-wrap"><table class="review-table"><tbody>{assumptions}</tbody></table></div><h3>Exclusions</h3><ul>{exclusions}</ul></section><p>This self-contained report makes no network requests and contains no raw Terraform values or application secrets. Return to the terminal to approve the exact saved plan.</p></main></body></html>''')
