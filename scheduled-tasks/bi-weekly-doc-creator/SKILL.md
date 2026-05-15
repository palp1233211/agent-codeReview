---
name: bi-weekly-doc-creator
description: 每周二早上8点执行，复制模版创建本周四 BI 迭代上线文档套件（汇总 + 泰国 + 菲律宾）
---

# BI 迭代上线文档创建

> 实现代码：`src/agents/bi_weekly_doc.py`
> 执行方式：`python cli.py bi-weekly-doc`

## 固定配置

| 配置项 | 值 |
|--------|---|
| 目标文件夹 | `BQ4ifcdM1lUjJidpg83cIWlJnKf` |
| 汇总模版 token | `Nzoxd1LrPoCwQaxFOAdcXMm4nMh` |
| 国家模版 token | `Ou8tdObXMolH8VxQ0yzcd5K6nQh` |
| 国家列表 | 泰国、菲律宾 |
| 飞书群 chat_id | `oc_02b6d626dd23a36c13ab1e3c6d80d21f` |

## 执行步骤

1. 自动计算本周四日期（`_next_thursday()`）
2. 并行复制泰国、菲律宾国家文档
3. 复制汇总文档
4. 更新汇总文档（分支号替换、插入国家文档 mention 链接）
5. 发送汇总文档地址到飞书群

## 所需环境变量

```
LARK_APP_ID=...
LARK_APP_SECRET=...
```
