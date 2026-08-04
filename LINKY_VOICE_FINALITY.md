# Linky 语音房日终核对

- 当天实时作战继续读取 Linky 实时 API；实时日账本不是最终结算信号。
- 已结束 UTC 业务日只使用阿里云 BI 的 `语音房主播行为数据` 核对，字段为
  `active_date(day)`、`guild_name`、`sid`、`diamond_amount`，金额单位为钻石。
- `印尼语音房主播薪资奖励` 是周报，不能进入每日核对。
- 映射只读取 `guild_source_dictionary` 中 `VOICE:` 开头的当前有效 Linky 来源：
  - 印尼1语音房：`Nova-Indonesia` ↔ BI `Nova`
  - 印尼2语音房：`Carote-Indonesia` + `Carote2-Indonesia` ↔ BI `Carote` + `Carote2`
  - 印尼3语音房：`Permata-Indonesia` ↔ BI `Permata`
- `daily-sync.sh` 在现有下载和写锁内调用 `linky_voice_bi_batch.py`，只原子生成0600证据，
  不写 `linke_streamer_daily`、历史收益或 publication。
- 状态只有 `WAITING_BI`、`BI_VERIFIED`、`BI_MISMATCH`。后端只校验证据并返回，前端不推导状态。
