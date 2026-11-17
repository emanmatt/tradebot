# New strategies backtest: 2026-05-18 to 2026-11-13

126 trading days. $100 start, long only, fractional shares, no borrowing (max 20% of the account per position). Costs: 1 cent per share on every buy and sell. Full-market (SIP) minute data. SPY over the period: +2.0%.

Stocks in play per day (avg): 20.0; with overnight news: 45%.

## Opening-range breakout on stocks in play (results in R = multiples of the amount risked)

| Version | Trades | /day | Win % | Avg win R | Avg loss R | Avg R after costs | Avg R before costs | Profit factor | Return | Max DD | 1st half R | 2nd half R | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ORB stocks in play (all) | 928 | 7.37 | 14.5 | 8.488 | -1.178 | +0.228 | +0.380 | 1.22 | +25.5% | 9.6% | +0.236 | +0.220 | PROMISING (positive in both halves) |
| ORB with overnight news | 434 | 3.44 | 14.5 | 8.066 | -1.139 | +0.197 | +0.330 | 1.1 | +4.3% | 4.4% | +0.096 | +0.298 | marginal (not robust) |
| ORB without news | 557 | 4.42 | 15.1 | 8.322 | -1.21 | +0.227 | +0.395 | 1.29 | +21.2% | 11.9% | +0.330 | +0.125 | PROMISING (positive in both halves) |

## Last-half-hour market momentum (results in % per trade)

| Version | Trades | Win % | Avg trade | Avg win | Avg loss | Profit factor | Return | Max DD | 1st half | 2nd half | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| MOM SPY last half-hour | 67 | 35.8 | -0.021% | +0.125% | -0.103% | 0.68 | -1.4% | 2.8% | +0.017% | -0.058% | LOSES MONEY |
| MOM QQQ last half-hour | 67 | 44.8 | -0.004% | +0.188% | -0.160% | 0.95 | -0.3% | 2.8% | +0.037% | -0.044% | LOSES MONEY |

### ORB stocks in play (all)
By month: 2026-05: 68 trades, avg +0.848, $+11.84; 2026-06: 165 trades, avg -0.119, $+0.71; 2026-07: 179 trades, avg -0.070, $-0.16; 2026-08: 156 trades, avg +1.398, $+16.47; 2026-09: 127 trades, avg -0.067, $-2.06; 2026-10: 163 trades, avg -0.075, $-4.11; 2026-11: 70 trades, avg -0.156, $+2.76
Exits: stop 789, close 139

### ORB with overnight news
By month: 2026-05: 39 trades, avg -0.453, $-0.85; 2026-06: 58 trades, avg -0.216, $-2.66; 2026-07: 80 trades, avg +0.528, $+3.36; 2026-08: 81 trades, avg +0.827, $+4.25; 2026-09: 56 trades, avg +0.178, $+0.61; 2026-10: 75 trades, avg +0.033, $+0.28; 2026-11: 45 trades, avg -0.132, $-0.63
Exits: stop 368, close 66

### ORB without news
By month: 2026-05: 34 trades, avg +2.547, $+15.31; 2026-06: 117 trades, avg -0.177, $+1.97; 2026-07: 109 trades, avg -0.385, $-3.18; 2026-08: 83 trades, avg +1.952, $+12.64; 2026-09: 79 trades, avg -0.346, $-3.59; 2026-10: 100 trades, avg -0.263, $-4.70; 2026-11: 35 trades, avg -0.166, $+2.74
Exits: stop 471, close 86

### MOM SPY last half-hour
By month: 2026-05: 5 trades, avg +0.107, $+0.54; 2026-06: 9 trades, avg +0.060, $+0.54; 2026-07: 12 trades, avg -0.000, $-0.00; 2026-08: 16 trades, avg -0.040, $-0.65; 2026-09: 9 trades, avg -0.046, $-0.41; 2026-10: 10 trades, avg -0.024, $-0.24; 2026-11: 6 trades, avg -0.200, $-1.19

### MOM QQQ last half-hour
By month: 2026-05: 5 trades, avg +0.182, $+0.91; 2026-06: 11 trades, avg +0.113, $+1.26; 2026-07: 10 trades, avg -0.035, $-0.36; 2026-08: 12 trades, avg -0.024, $-0.29; 2026-09: 12 trades, avg +0.028, $+0.34; 2026-10: 11 trades, avg -0.051, $-0.57; 2026-11: 6 trades, avg -0.262, $-1.58
