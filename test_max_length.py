"""text 字段 maxLength 约束的回归测试。

仅通过公开入口 `python -m forms submit` 读取合成 JSON 文件，
检查退出码与标准输出，并核对 SQLite 落库结果；不依赖 forms 包内部函数。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))


class MaxLengthTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    # ---- 辅助 -----------------------------------------------------------

    def _path(self, name):
        return os.path.join(self.tmp, name)

    def _write(self, name, content):
        path = self._path(name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    def _dump(self, name, value):
        # 与产品无关：统一不写结尾换行，成功用例可逐字比对入库原文。
        return self._write(name, json.dumps(value, ensure_ascii=False))

    def _submit(self, form, answers, db_name="data.db", raw_form=None, raw_answers=None):
        """执行一次提交，返回 (CompletedProcess, db_path)。

        form/answers 传 dict 时按 UTF-8 原文落盘；需要直接控制 JSON 转义
        形式时可用 raw_form/raw_answers 传字符串。
        """
        form_path = (
            self._write("form.json", raw_form)
            if raw_form is not None
            else self._dump("form.json", form)
        )
        answers_path = (
            self._write("answers.json", raw_answers)
            if raw_answers is not None
            else self._dump("answers.json", answers)
        )
        db_path = self._path(db_name)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "forms",
                "submit",
                "--form",
                form_path,
                "--answers",
                answers_path,
                "--db",
                db_path,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        return proc, db_path

    @staticmethod
    def _payload(proc):
        return json.loads(proc.stdout)

    def _rows(self, db_path):
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute(
                "SELECT form, answers FROM submissions ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    def _assert_success(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = self._payload(proc)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["errors"], {})
        self.assertIsInstance(payload["submissionId"], int)
        return payload

    def _assert_field_error(self, proc, field, code):
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(self._payload(proc), {"valid": False, "errors": {field: code}})

    def _form(self, max_length, required=False):
        field = {"id": "note", "type": "text"}
        if max_length is not _UNSET:
            field["maxLength"] = max_length
        if required:
            field["required"] = True
        return {"fields": [field]}

    # ---- 零上限 / 恰好达到 / 超出一个码点 --------------------------------

    def test_zero_limit_empty_string_passes(self):
        proc, db_path = self._submit(self._form(0), {"note": ""})
        self._assert_success(proc)
        self.assertEqual(len(self._rows(db_path)), 1)

    def test_zero_limit_nonempty_is_max_length(self):
        proc, db_path = self._submit(self._form(0), {"note": "a"})
        self._assert_field_error(proc, "note", "max_length")
        self.assertFalse(os.path.exists(db_path))

    def test_exactly_at_limit_counts_codepoints(self):
        # “中🙂”为 2 个 Unicode 码点（UTF-8 下共 7 字节），恰好达到上限 2。
        proc, _ = self._submit(self._form(2), {"note": "中🙂"})
        self._assert_success(proc)

    def test_one_codepoint_over_limit(self):
        proc, _ = self._submit(self._form(2), {"note": "中🙂A"})
        self._assert_field_error(proc, "note", "max_length")

    def test_space_and_newline_each_count_one_codepoint(self):
        proc_ok, _ = self._submit(self._form(2), {"note": " \n"})
        self._assert_success(proc_ok)
        proc_bad, _ = self._submit(self._form(2), {"note": " \n "})
        self._assert_field_error(proc_bad, "note", "max_length")

    def test_length_measured_after_json_decode_not_escape_text(self):
        # 同一份答卷的 JSON 转义写法：转义文本很长，但解码后只有 2 个码点。
        escaped = '{"note": "\\u4e2d\\ud83d\\ude42"}'
        proc_ok, _ = self._submit(
            self._form(2), None, raw_answers=escaped
        )
        self._assert_success(proc_ok)
        proc_bad, _ = self._submit(
            self._form(1), None, raw_answers=escaped
        )
        self._assert_field_error(proc_bad, "note", "max_length")

    # ---- 无 maxLength ---------------------------------------------------

    def test_long_text_without_max_length_passes(self):
        proc, db_path = self._submit(self._form(_UNSET), {"note": "中" * 1000})
        self._assert_success(proc)
        self.assertEqual(len(self._rows(db_path)), 1)

    # ---- 可选字段：缺失 / null / 空串 ------------------------------------

    def test_optional_missing_and_null_skip_length_check(self):
        for index, answers in enumerate(({}, {"note": None})):
            with self.subTest(answers=answers):
                # 每个子用例使用独立的库文件，避免与同目录中前一次提交累加。
                proc, db_path = self._submit(
                    self._form(0), answers, db_name="optional_%d.db" % index
                )
                self._assert_success(proc)
                self.assertEqual(len(self._rows(db_path)), 1)

    # ---- 与必填、类型判断的交互 ------------------------------------------

    def test_required_takes_precedence_under_zero_limit(self):
        for answers in ({}, {"note": None}, {"note": ""}):
            with self.subTest(answers=answers):
                proc, db_path = self._submit(
                    self._form(0, required=True), answers
                )
                self._assert_field_error(proc, "note", "required")
                self.assertFalse(os.path.exists(db_path))

    def test_non_string_value_is_type_not_max_length(self):
        for value in (0, [], True, {}):
            with self.subTest(value=value):
                proc, _ = self._submit(
                    self._form(0, required=True), {"note": value}
                )
                self._assert_field_error(proc, "note", "type")

    # ---- 非法 maxLength 定义 ---------------------------------------------

    def test_invalid_max_length_values_reject_form(self):
        for bad in (-1, True, "2", None, 1.5):
            with self.subTest(bad=bad):
                proc, db_path = self._submit(
                    self._form(bad), {"note": "abc"}
                )
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertEqual(self._payload(proc), {"error": "invalid_input"})
                self.assertFalse(os.path.exists(db_path))

    def test_max_length_on_number_and_select_rejects_form(self):
        cases = [
            (
                {"fields": [{"id": "n", "type": "number", "maxLength": 1}]},
                {"n": 1},
            ),
            (
                {
                    "fields": [
                        {
                            "id": "s",
                            "type": "select",
                            "options": ["x"],
                            "maxLength": 1,
                        }
                    ]
                },
                {"s": "x"},
            ),
        ]
        for form, answers in cases:
            with self.subTest(form=form):
                proc, db_path = self._submit(form, answers)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertEqual(self._payload(proc), {"error": "invalid_input"})
                self.assertFalse(os.path.exists(db_path))

    # ---- 存储：成功写入原文，失败保持既有库不变 -----------------------------

    def test_success_persists_raw_form_and_answers_text(self):
        form = self._form(2)
        answers = {"note": "中🙂"}
        proc, db_path = self._submit(form, answers, db_name="keep.db")
        payload = self._assert_success(proc)

        form_raw = json.dumps(form, ensure_ascii=False)
        answers_raw = json.dumps(answers, ensure_ascii=False)
        rows = self._rows(db_path)
        self.assertEqual(rows, [(form_raw, answers_raw)])

        # 再成功提交一次：新增一条，编号递增，旧记录原样保留。
        answers2 = {"note": "AB"}
        proc2, _ = self._submit(form, answers2, db_name="keep.db")
        payload2 = self._assert_success(proc2)
        self.assertGreater(payload2["submissionId"], payload["submissionId"])
        self.assertEqual(
            self._rows(db_path),
            [(form_raw, answers_raw), (form_raw, json.dumps(answers2, ensure_ascii=False))],
        )

    def test_length_failure_leaves_existing_db_unchanged(self):
        form = self._form(2)
        proc_ok, db_path = self._submit(form, {"note": "中🙂"})
        self._assert_success(proc_ok)
        before = self._rows(db_path)

        proc_bad, _ = self._submit(form, {"note": "中🙂A"}, db_name=os.path.basename(db_path))
        self._assert_field_error(proc_bad, "note", "max_length")

        self.assertEqual(self._rows(db_path), before)

    def test_invalid_definition_leaves_existing_db_unchanged(self):
        proc_ok, db_path = self._submit(self._form(2), {"note": "ok"})
        self._assert_success(proc_ok)
        before = self._rows(db_path)

        proc_bad, _ = self._submit(
            self._form(-1), {"note": "x"}, db_name=os.path.basename(db_path)
        )
        self.assertEqual(proc_bad.returncode, 2)
        self.assertEqual(self._payload(proc_bad), {"error": "invalid_input"})

        self.assertEqual(self._rows(db_path), before)


# 哨兵：表示字段不配置 maxLength（区别于显式 null，null 本身是非法定义）。
_UNSET = object()


if __name__ == "__main__":
    unittest.main()
