# Scanner backtest (2026-08-21 to 2026-11-20)

65 trading days · full-market data · costs 0.2% round trip · rules from config.yaml `scanner:` (gap ≥5%, volume ≥3x, $2-$50)

**How to read this:** each pick is 'bought' at the open of the next 5-minute bar after the scan. 'avg' is the average % move to that point; 'up' is how often it was higher; 't (days)' above 2 means the result held up day after day rather than coming from a few lucky days. The control group gapped up just as much but failed the volume test.

## Scan at 09:25 ET

Picks: 773 (11.9 per day) · control: 1880

| Hold until | Group | n | avg % | median % | up % | avg after cost % | t | t (days) |
|---|---|---|---|---|---|---|---|---|
| 30 min | picks | 773 | -0.68 | -0.60 | 42 | -0.88 | -2.4 | -1.7 |
| 30 min | control | 1880 | -0.06 | -0.14 | 48 | -0.26 | -0.5 | 0.1 |
| 60 min | picks | 773 | -1.01 | -0.68 | 43 | -1.21 | -3.2 | -2.0 |
| 60 min | control | 1880 | +0.06 | -0.08 | 49 | -0.14 | 0.5 | -0.0 |
| 11:30 | picks | 773 | -1.21 | -0.89 | 44 | -1.41 | -3.7 | -2.1 |
| 11:30 | control | 1880 | +0.14 | -0.13 | 48 | -0.06 | 1.1 | -0.3 |
| close | picks | 773 | -1.86 | -1.69 | 40 | -2.06 | -4.0 | -3.6 |
| close | control | 1880 | -0.14 | -0.21 | 48 | -0.34 | -0.8 | -0.4 |
| next open | picks | 769 | -3.10 | -2.77 | 39 | -3.30 | -5.7 | -3.3 |
| next open | control | 1862 | -1.29 | -1.17 | 43 | -1.49 | -6.3 | -1.0 |

**Verdict (hold until close):** 🔴 No edge: picks did not beat similar gappers that failed the volume test.

Best/worst point reached before the close (avg): up +8.0% / down -8.3%. A big gap between these means exits (Step 2) decide the result more than the picks do.

**Does the ranking help?** (hold until close)

| Bucket | n | avg % | up % |
|---|---|---|---|
| top 5 by score | 319 | -3.87 | 32 |
| rank 6-20 | 454 | -0.45 | 47 |
| with news | 124 | -2.86 | 35 |
| no news | 649 | -1.67 | 41 |
| gap 5-10% | 373 | -0.62 | 47 |
| gap 10-20% | 246 | -1.00 | 37 |
| gap 20%+ | 154 | -6.24 | 30 |
| volume 3-6x | 412 | -0.61 | 46 |
| volume 6-15x | 211 | -1.14 | 38 |
| volume 15x+ | 150 | -6.30 | 29 |
| price under $5 | 135 | -1.69 | 37 |
| price $5+ | 638 | -1.90 | 41 |

## Scan at 09:35 ET

Picks: 866 (13.3 per day) · control: 2358

| Hold until | Group | n | avg % | median % | up % | avg after cost % | t | t (days) |
|---|---|---|---|---|---|---|---|---|
| 30 min | picks | 866 | -0.20 | -0.34 | 44 | -0.40 | -1.0 | -1.0 |
| 30 min | control | 2358 | +0.03 | -0.22 | 46 | -0.17 | 0.4 | 0.2 |
| 60 min | picks | 866 | -0.74 | -0.73 | 42 | -0.94 | -3.2 | -1.8 |
| 60 min | control | 2358 | -0.11 | -0.30 | 46 | -0.31 | -0.9 | -0.2 |
| 11:30 | picks | 866 | -0.71 | -0.47 | 46 | -0.91 | -2.6 | -1.6 |
| 11:30 | control | 2358 | +0.07 | -0.06 | 49 | -0.13 | 0.6 | -0.3 |
| close | picks | 866 | -1.42 | -1.24 | 43 | -1.62 | -3.8 | -3.3 |
| close | control | 2358 | -0.07 | +0.00 | 50 | -0.27 | -0.5 | 0.3 |
| next open | picks | 861 | -2.62 | -2.09 | 39 | -2.82 | -5.8 | -3.6 |
| next open | control | 2331 | -0.87 | -0.81 | 44 | -1.07 | -4.4 | -0.3 |

**Verdict (hold until close):** 🔴 No edge: picks did not beat similar gappers that failed the volume test.

Best/worst point reached before the close (avg): up +7.1% / down -7.2%. A big gap between these means exits (Step 2) decide the result more than the picks do.

**Does the ranking help?** (hold until close)

| Bucket | n | avg % | up % |
|---|---|---|---|
| top 5 by score | 323 | -2.19 | 37 |
| rank 6-20 | 543 | -0.95 | 46 |
| with news | 209 | -2.04 | 37 |
| no news | 657 | -1.22 | 45 |
| gap 5-10% | 391 | -0.63 | 47 |
| gap 10-20% | 326 | -2.00 | 40 |
| gap 20%+ | 149 | -2.21 | 39 |
| volume 3-6x | 488 | -0.74 | 49 |
| volume 6-15x | 236 | -0.89 | 41 |
| volume 15x+ | 142 | -4.62 | 27 |
| price under $5 | 145 | -2.06 | 37 |
| price $5+ | 721 | -1.29 | 44 |

## Scan at 10:00 ET

Picks: 755 (11.6 per day) · control: 3665

| Hold until | Group | n | avg % | median % | up % | avg after cost % | t | t (days) |
|---|---|---|---|---|---|---|---|---|
| 30 min | picks | 755 | -0.42 | -0.29 | 45 | -0.62 | -2.7 | -2.1 |
| 30 min | control | 3665 | +0.12 | +0.13 | 53 | -0.08 | 2.7 | 0.3 |
| 60 min | picks | 755 | -0.48 | -0.27 | 46 | -0.68 | -2.5 | -2.1 |
| 60 min | control | 3665 | +0.24 | +0.18 | 53 | +0.04 | 4.2 | -0.1 |
| 11:30 | picks | 755 | -0.65 | -0.34 | 45 | -0.85 | -3.1 | -2.6 |
| 11:30 | control | 3665 | +0.17 | +0.11 | 51 | -0.03 | 2.8 | 0.0 |
| close | picks | 755 | -1.39 | -0.91 | 43 | -1.59 | -3.8 | -4.0 |
| close | control | 3665 | +0.12 | +0.22 | 52 | -0.08 | 1.4 | 0.0 |
| next open | picks | 752 | -2.23 | -1.61 | 39 | -2.43 | -5.1 | -3.9 |
| next open | control | 3627 | -0.24 | -0.10 | 49 | -0.44 | -2.0 | -0.2 |

**Verdict (hold until close):** 🔴 No edge: picks did not beat similar gappers that failed the volume test.

Best/worst point reached before the close (avg): up +6.1% / down -6.4%. A big gap between these means exits (Step 2) decide the result more than the picks do.

**Does the ranking help?** (hold until close)

| Bucket | n | avg % | up % |
|---|---|---|---|
| top 5 by score | 319 | -2.09 | 38 |
| rank 6-20 | 436 | -0.88 | 46 |
| with news | 190 | -1.67 | 38 |
| no news | 565 | -1.29 | 44 |
| gap 5-10% | 300 | -0.68 | 46 |
| gap 10-20% | 277 | -1.24 | 40 |
| gap 20%+ | 178 | -2.81 | 40 |
| volume 3-6x | 475 | -0.70 | 48 |
| volume 6-15x | 175 | -1.44 | 37 |
| volume 15x+ | 105 | -4.42 | 28 |
| price under $5 | 130 | -1.99 | 41 |
| price $5+ | 625 | -1.26 | 43 |

SPY over the same days, 9:35 to close: avg -0.18%/day (the market's own drift, for context).

## Limits
- Smart-money picks are not included (no history yet).
- The live scanner only sees Alpaca's most-active and top-gainer lists and the smaller IEX feed; this test sees every stock with full-market data, so it shows the screen's best case.
- No stop-loss or target is applied here: this tests the PICKS. Step 2 adds and tests the exits.