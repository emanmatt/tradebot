# New strategies backtest: 2026-05-15 to 2026-11-13

127 trading days. $100 start, long only, fractional shares, no borrowing (max 20% of the account per position). Costs: 1 cent per share on every buy and sell. Full-market (SIP) minute data. SPY over the period: +1.7%.

Stocks in play per day (avg): 20.0; with overnight news: 59%.

## Opening-range breakout on stocks in play (results in R = multiples of the amount risked)

| Version | Trades | /day | Win % | Avg win R | Avg loss R | Avg R after costs | Avg R before costs | Profit factor | Return | Max DD | 1st half R | 2nd half R | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ORB stocks in play (all) | 933 | 7.35 | 14.5 | 8.488 | -1.178 | +0.221 | +0.373 | 1.21 | +24.4% | 9.7% | +0.230 | +0.211 | PROMISING (positive in both halves) |
| ORB with overnight news | 581 | 4.57 | 14.1 | 10.149 | -1.149 | +0.445 | +0.584 | 1.18 | +11.8% | 6.3% | +0.250 | +0.640 | marginal (not robust) |
| ORB without news | 417 | 3.28 | 15.3 | 5.749 | -1.219 | -0.149 | +0.023 | 1.2 | +9.6% | 10.1% | +0.007 | -0.305 | LOSES MONEY |

## Last-half-hour market momentum (results in % per trade)

| Version | Trades | Win % | Avg trade | Avg win | Avg loss | Profit factor | Return | Max DD | 1st half | 2nd half | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| MOM SPY last half-hour | 67 | 35.8 | -0.021% | +0.125% | -0.103% | 0.68 | -1.4% | 2.8% | +0.017% | -0.058% | LOSES MONEY |
| MOM QQQ last half-hour | 67 | 44.8 | -0.004% | +0.188% | -0.160% | 0.95 | -0.3% | 2.8% | +0.037% | -0.044% | LOSES MONEY |

### ORB stocks in play (all)
By month: 2026-05: 73 trades, avg +0.716, $+11.19; 2026-06: 165 trades, avg -0.119, $+0.71; 2026-07: 178 trades, avg -0.065, $-0.30; 2026-08: 156 trades, avg +1.398, $+16.36; 2026-09: 127 trades, avg -0.070, $-2.16; 2026-10: 164 trades, avg -0.082, $-4.14; 2026-11: 70 trades, avg -0.156, $+2.74
Exits: stop 794, close 139

### ORB with overnight news
By month: 2026-05: 55 trades, avg +0.280, $+2.23; 2026-06: 76 trades, avg -0.306, $-3.17; 2026-07: 109 trades, avg +0.207, $-0.01; 2026-08: 114 trades, avg +2.105, $+16.57; 2026-09: 69 trades, avg -0.022, $-1.93; 2026-10: 105 trades, avg +0.205, $+0.32; 2026-11: 53 trades, avg -0.300, $-2.21
Exits: stop 496, close 85

### ORB without news
By month: 2026-05: 21 trades, avg +1.596, $+8.65; 2026-06: 100 trades, avg -0.018, $+2.76; 2026-07: 84 trades, avg -0.321, $+0.03; 2026-08: 50 trades, avg -0.220, $-0.81; 2026-09: 66 trades, avg -0.243, $-0.71; 2026-10: 71 trades, avg -0.654, $-4.41; 2026-11: 25 trades, avg +0.258, $+4.05
Exits: stop 351, close 66

### MOM SPY last half-hour
By month: 2026-05: 5 trades, avg +0.107, $+0.54; 2026-06: 9 trades, avg +0.060, $+0.54; 2026-07: 12 trades, avg -0.000, $-0.00; 2026-08: 16 trades, avg -0.040, $-0.65; 2026-09: 9 trades, avg -0.046, $-0.41; 2026-10: 10 trades, avg -0.024, $-0.24; 2026-11: 6 trades, avg -0.200, $-1.19

### MOM QQQ last half-hour
By month: 2026-05: 5 trades, avg +0.182, $+0.91; 2026-06: 11 trades, avg +0.113, $+1.26; 2026-07: 10 trades, avg -0.035, $-0.36; 2026-08: 12 trades, avg -0.024, $-0.29; 2026-09: 12 trades, avg +0.028, $+0.34; 2026-10: 11 trades, avg -0.051, $-0.57; 2026-11: 6 trades, avg -0.262, $-1.58
