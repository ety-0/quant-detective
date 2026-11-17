# Third-party notices

No third-party source file is vendored and neither project below is a runtime
dependency. `ashare_limitup/pools.py` was written for this repository, with the
following parts adapted from or informed by MIT-licensed projects. Reviewed
commits are pinned in `connectors/upstream_review_pins.json`.

## ZhuLinsen/daily_stock_analysis

- Source: https://github.com/ZhuLinsen/daily_stock_analysis
- Reviewed revision: `aa7228986f0ac8fc78e9147be81dddb277ef342d`
- File consulted: `data_provider/akshare_fetcher.py`, method `get_limit_up_pool`
- Adapted: the output field names of a limit-up row (`code`, `name`, `change_pct`,
  `price`, `amount`, `turnover_rate`, `seal_amount`, `first_limit_time`,
  `last_limit_time`, `break_count`, `limit_stat`, `consecutive_boards`,
  `industry`), zero-padded HHMMSS seal times, and ordering by consecutive boards
  then first seal time.
- Licence: MIT, Copyright (c) 2026 ZhuLinsen

## akfamily/akshare

- Source: https://github.com/akfamily/akshare
- Reviewed revision: `25acdfac7ddf3a0721a0ebdcc6a1d7ef3e76abc9`
- File consulted: `akshare/stock/quote/stock_ztb_em.py`
- Used as reference: the Eastmoney topic-pool endpoint paths, their query
  parameters, the meaning of each column, and the page's universe note.
- Licence: MIT, Copyright (c) 2019-2026 AKFamily

## MIT License (applies to both projects above)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
