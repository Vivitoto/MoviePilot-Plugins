# 高频表与用途（第一版）

这不是完整数据字典，只是第一版高频表认知，用于后续自然语言生成 SQL 时快速定位数据源。

## 用户主表 / 用户标签

### `LESSEE.V_DM_D_ST_V_USR_AL_USER_DERIVE`
高频中的高频。常作为用户主表或用户派生标签入口。

常见用途：
- 在网用户筛选
- B2I 用户识别
- 套餐、月租、证件类型、用户状态
- 融合标记、入网时间、在网时长
- 设备号解密前的基础字段来源

常见过滤字段：
- `month_id`, `day_id`
- `is_innet`
- `tele_type`
- `is_b2i`
- `dinner_id`, `dinner_name`
- `cert_type`
- `user_status_desc`
- `innet_date`, `innet_months`, `innet_day`

### `LESSEE.V_DWA_V_D_PRD_AL_USER_TAG_MRT_CUT`
常用于用户标签、携转相关客群切片、套餐和地市/区县补充。

## 产品 / 套餐 / 剔除维表

### `MARKET.DIM_HY_PRODUCT`
常用于剔除行业卡。

### `MARKET.DIM_BIGDATA_WLW`
常用于剔除物联网产品。

### `PER_031_DIM.DIM_PRD_CERT_TYPE_ALL`
常用于识别企业证件、群组证件等。

### `LESSEE.V_DWA_S_D_PRD_AL_PRODUCT_MARKET`
常用于产品/套餐属性补充。

## 收入 / 出账 / 费用

### `PER_031_DM.DM_M_IC_QDXZ_4G_INCOME_RT_SR`
高频收入表，常用于收入、出账、群组口径分析。

### `LESSEE.DWA_V_D_CUS_CB_SING_CHARGE`
常用于单产品/单用户出账与费用分析。

### `LESSEE.V_DWA_V_D_USE_AL_SUM_MARKET`
常用于用户使用量、流量、市场侧汇总。

### `PER_031_DWD.DWD_D_ACC_CB_F_PAYLOG`
常用于充值、支付、缴费类分析。

### `PER_031_DM.DM_M_IC_RH_FEE`
常用于融合/费用相关分析。

## 携出 / 解约 / 流失

### `PER_031_DM.DM_D_MRT_NP_TURN_INFO_RT`
高频携转/携出事实表。

### `LESSEE.DWD_D_CUS_NP_TURN_QUERY_USER`
常用于携转用户明细或追踪。

### `MARKET.ZCS_WJ_M_LIUSHI_KUANBIAO_G`
流失类分析中常见。

## 状态 / 客群 / 营销结果

### `MARKET.ZCS_ZQC_M_USER_STATUS_DANGYUE_CHUZHANG`
当月用户状态与出账相关分析常用。

### `MARKET.ZCS_ZH_IMP_USER_STATUS`
用户状态类结果表。

### `MARKET.ZCS_CY_D_YCDX_LIST`
营销、异动、名单类场景常出现。

### `MARKET.DIM_USER_STATUS`
用户状态维表。

## 宽带 / 融合 / 活动

### `MARKET.DM_GUYI_RONGHE_FUKA_ALL_QUNZU`
融合相关客群、群组分析。

### `LESSEE.V_DWA_S_D_PRD_AL_ACTIVITY_MARKET`
活动、合约、受理类补充信息来源之一。

## 补充说明

- 很多脚本还大量使用 `MARKET.ZCS_*`、`TMP_*` 这类中间结果表。
- 生成 SQL 时，如果用户要“按以前的口径”，优先从高频主表开始，而不是从零设计数据源。
- 遇到业务口径不明确时，优先参考历史脚本中的高频组合，而不是擅自换表。
