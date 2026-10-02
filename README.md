# 表单与问卷管理台

建设用于小团队数据收集的本地表单产品，逐步覆盖字段配置、表单发布、填写校验、提交记录、版本留存、条件显示、结果筛选和 CSV 导出。

计划采用：Python 3 标准库 / http.server / sqlite3 / json。

## 命令行提交

校验 JSON 表单下的合成答卷，并把有效提交保存到 SQLite：

```
python -m forms submit --form f.json --answers a.json --db data.db
```

- 退出码 0：`{"valid": true, "errors": {}, "submissionId": N}`，提交已保存。
- 退出码 1：`{"valid": false, "errors": {...}}`，按字段 id 映射错误码
  （`required` / `type` / `choice` / `unknown_field`），不保存记录。
- 退出码 2：`{"error": "invalid_input"}`（输入文件或结构不合法）
  或 `{"error": "storage_error"}`（数据库打开或保存失败）。
