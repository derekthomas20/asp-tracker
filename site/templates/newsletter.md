{#- The newsletter draft for a new quarter's payment limits (Markdown, for
    Buttondown). Rendered by build_site.py into build/newsletter.json; every
    figure comes from the same queries as the site. scripts/newsletter_draft.py
    saves it to Buttondown as a draft only, for the owner to read and send. -#}
{%- macro row(c) -%}
- [{{ c.code }} {{ c.name|md }}]({{ site_url }}codes/{{ c.code }}.html): {{ c.prior|money }} → {{ c.latest|money }} per {{ c.dosage }} ({{ c.pct|percent }}){% if c.maker %}, {{ c.maker.name|md }}{% endif %}
{%- endmacro -%}
CMS posted the {{ s.latest_q|months }} Medicare Part B drug payment limits on {{ s.posted|long_date }}. They apply to services from {{ s.latest_q|effective }}.

Of the {{ s.compared|num }} codes in both this file and last quarter’s, **{{ s.up|num }} went up** and **{{ s.down|num }} went down**{% if s.median_pct is not none %} (median change {{ s.median_pct|percent }}){% endif %}. {{ s.n_new|num }} code{{ "s" if s.n_new != 1 }} {{ "are" if s.n_new != 1 else "is" }} new and {{ s.n_dropped|num }} {{ "were" if s.n_dropped != 1 else "was" }} dropped.

## Biggest increases

{% for c in movers.pct.increases[:n] %}
{{ row(c) }}
{% endfor %}

## Biggest decreases

{% for c in movers.pct.decreases[:n] %}
{{ row(c) }}
{% endfor %}
{% if movers.effect.increases or movers.effect.decreases %}

## Largest effect on Medicare spending

Change per billing unit times the code’s Medicare use in {{ effect_year }}:

{% for c in (movers.effect.increases[:3] + movers.effect.decreases[:3]) %}
- [{{ c.code }} {{ c.name|md }}]({{ site_url }}codes/{{ c.code }}.html): about {{ c.effect|short_signed_money }} a year ({{ c.pct|percent }} per {{ c.dosage }})
{% endfor %}
{% endif %}

## More on Rxcurve

- [All movers]({{ site_url }}movers.html), by percent, dollars and yearly effect
- [New and dropped codes]({{ site_url }}changes.html)
- [Biosimilars]({{ site_url }}biosimilars.html) and the [net cost recovery calculator]({{ site_url }}tools/ncr-calculator/)
- [Reduced coinsurance drugs]({{ site_url }}coinsurance.html)

Percent rankings leave out codes whose prior limit was under {{ pct_min_prior|money }}. Payment limits are generally based on sales from about two quarters earlier ({{ sales_q|months }}). Every figure is computed from the CMS files; check the [CMS ASP pricing files]({{ source_page }}) before relying on one. Not billing or investment advice.
