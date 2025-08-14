GE Track – OSRS Flipping Assistant

A CLI that suggests actionable OSRS Grand Exchange flips using:
- OSRS Wiki Prices API (market prices, volumes, buy limits)
- Official OSRS GE guide price ("GE Price")

It outputs exactly what to do for each suggested item: which items to buy, the quantity to buy, buy price, and sell price (sell all), with profit after the 2% GE tax (capped at 5m per item).

Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Usage

```bash
# Example: budget 50m, show top 10 flips, require min ROI 0.5% and min profit 200 gp/unit
python flip_cli.py --budget 50000000 --top 10 --min-roi 0.005 --min-profit 200
```

Options
- `--budget` (required): available gp
- `--top`: number of suggestions to show (default 10)
- `--min-roi`: minimum ROI per unit, e.g. 0.01 = 1% (default 0.005)
- `--min-profit`: minimum profit per unit in gp (default 100)
- `--aggressiveness`: 0..1; higher raises buy price slightly and lowers sell price slightly to fill faster (default 0.3)
- `--liquidity-frac`: fraction of hourly volume to consider safe to attempt within ~1 hour (default 0.25)
- `--min-hourly-volume`: filter out items below this 1h volume (default 500)
- `--max-fill-hours`: cap quantity to fill within this many hours (default 1.5)
- `--with-ge`: also fetch and display GE guide price for suggested items (slower; default off)

Notes
- Applies OSRS GE tax of 2% on the sell price with a 5m cap per item when computing profit.
- Buy limits are sourced from the wiki mapping where available.
- "GE Price" is shown for comparison only and not used for fills; market prices/volumes come from the wiki live prices API.
- Set environment variable `WIKI_USER_AGENT` to a descriptive UA to comply with wiki API policy.

Data sources
- OSRS Wiki Prices API: `https://prices.runescape.wiki/api/v1/osrs/`
  - `mapping`, `latest`, `1h`
- Official GE guide price: `https://services.runescape.com/m=itemdb_oldschool/api/catalogue/detail.json?item=<id>`

Disclaimer
This tool only provides suggestions and does not automate in-game actions. Respect Jagex rules and avoid any automation or unfair advantages.
