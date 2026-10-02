# 表单与问卷管理台

建设用于小团队数据收集的本地表单产品，逐步覆盖字段配置、表单发布、填写校验、提交记录、版本留存、条件显示、结果筛选和 CSV 导出。

计划采用：Python 3 标准库 / http.server / sqlite3 / json。

当前已提供命令行提交功能：

```bash
python -m forms submit --form f.json --answers a.json --db data.db
```

- 校验表单（`fields` 非空、`id` 唯一、`type` 为 text/number/select、select 的 `options` 非空无重复）与答卷（required / type / choice / unknown_field）。
- text 字段可选配置 `maxLength`（≥0 的 JSON 整数）；长度按 JSON 解码后的 Unicode 码点数计算（中文、空格、换行、emoji 均计 1 个码点），等于上限通过、超过返回 `max_length`。`maxLength` 出现在 number/select 字段或取值为布尔/字符串/null/小数/负数时表单不合法。
- 成功：退出码 0，输出 `{"valid": true, "errors": {}, "submissionId": N}`，并把原表单与原答卷文本原样追加保存到 SQLite。
- 校验失败：退出码 1，输出 `{"valid": false, "errors": {...}}`，不写库。
- 输入不合法：退出码 2，输出 `{"error": "invalid_input"}`；存储失败：退出码 2，输出 `{"error": "storage_error"}`。

示例见 `examples/` 目录。
