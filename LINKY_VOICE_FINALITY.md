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

## 唯一采集与闭日事实

- `fetch_guild_day(guild, business_date)` 是 `streamer_stat` 与 `live_room_stat` 的唯一公共采集入口；
  实时、快照和 API 日账本消费者只能复用完整 `FetchBundle`，不能自行请求 Linky API。
- 小时 runner 使用 UTC 今天；API 闭日完成只读取 `state/linky-api-closure` 的
  `API_CLOSED + scanComplete=true` 事实，和 BI 三种核对状态完全独立。
- 所有 Linky 采集入口共用非阻塞采集域 flock。锁忙记录 `SKIPPED_LOCK_BUSY` 后退出，不排队或强杀。
- 2026-08-05 只读核验确认印尼2两来源在生产字典均为 active：生效日均为 2026-06-01、无失效日；
  2026-07-28 至 2026-08-03 的生产 API 日账本每天同时存在 Carote 与 Carote2 数据，且候选 BI 证据的
  `sourceGuilds` 与两来源金额之和逐日一致。因此该有效期内允许两来源共同进入灰度，后续仍按字典有效期动态读取。
