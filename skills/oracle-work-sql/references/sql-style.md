# Oracle SQL 风格约定

这套工作语料有比较稳定的 Oracle 风格。生成 SQL 时优先贴近它。

## 优先使用的写法

- `to_char`, `to_date`, `add_months`, `substr`
- `nvl`
- `decode`
- `case when`
- `row_number() over (...)`
- `partition by`
- `create table as select`

## 常见风格偏好

- 使用月度/日度分区字段，如 `month_id`, `day_id`
- 喜欢先筛主客群，再 left join 补标签和策略信息
- 中间表很多，尤其适合复杂任务分步写
- 名单型 SQL 常直接输出中文别名字段
- 大量业务判断写在 `case when` 中

## 生成 SQL 时的默认策略

1. 默认写 Oracle SQL，而不是 MySQL/Postgres 风格。
2. 如果任务复杂，优先给出分步版本或 `create table as select` 版本。
3. 如果用户只是要思路，可以先给单条 SQL；如果接近生产任务，优先给可落地脚本版本。
4. 优先复用历史语料里的时间口径和字段命名风格。
5. 若缺少关键表结构，不硬编字段，明确标注待确认点。

## 不要默认做的事

- 不要擅自改成别的数据库方言。
- 不要把明显业务化的筛选条件省掉。
- 不要把名单型需求只写成简单汇总。
- 不要忽略注释里写明的口径变化。
