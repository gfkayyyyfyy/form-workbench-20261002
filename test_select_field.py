"""select 字段选项校验的回归测试。

只通过公开入口 ``python -m forms submit`` 执行：合成表单与答卷 JSON 写入
临时目录，核对退出码、stdout 解析后的 JSON，并用标准库 sqlite3 核对落库
结果。不导入 forms 包内的任何内部函数，不读取或修改既有数据库。

覆盖的公开行为：
- 单选值按字符串原值匹配：列出的选项通过；未列出的字符串（含大小写与
  空白差异）报 choice；数字、布尔、数组、对象报 type；
- 选填缺失/None 通过、空字符串报 choice；必填缺失/None/空字符串报
  required；options 含空字符串时选填的空字符串通过、必填仍报 required；
- 多个字段的 required、choice 与 unknown_field 同时出现在 errors 中，
  每个字段仅有一个错误码；
- options 缺失、为空数组、不是数组、含非字符串元素或重复字符串时退出码
  为 2，输出恰为 {"error": "invalid_input"}，即使答卷同时有字段错误；
- 成功提交把含中文、空格与换行的表单与答卷原文按原样落库；
- 校验失败不创建数据库，也不改变已有库中的记录数、主键与原文。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


def _select_field(field_id="kind", options=("A", "B"), required=False):
    field = {"id": field_id, "type": "select", "options": list(options)}
    if required:
        field["required"] = True
    return field


class SelectFieldTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    # -- 辅助 ------------------------------------------------------------

    def _run_raw(self, form_text, answers_text, db_path=None):
        """以给定原文写文件并执行提交。

        每次使用全新的子目录；db_path 为 None 时数据库也位于该子目录
        （尚不存在），传入既有路径则可验证失败不影响已有库。
        """
        run_dir = tempfile.mkdtemp(dir=self.tmp)
        form_path = os.path.join(run_dir, "form.json")
        answers_path = os.path.join(run_dir, "answers.json")
        if db_path is None:
            db_path = os.path.join(run_dir, "data.db")
        with open(form_path, "w", encoding="utf-8") as fh:
            fh.write(form_text)
        with open(answers_path, "w", encoding="utf-8") as fh:
            fh.write(answers_text)
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
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
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        return proc, db_path, form_text, answers_text

    def _run(self, form, answers, db_path=None):
        return self._run_raw(
            json.dumps(form, ensure_ascii=False),
            json.dumps(answers, ensure_ascii=False),
            db_path=db_path,
        )

    def _payload(self, proc):
        return json.loads(proc.stdout.decode("utf-8"))

    def _rows(self, db_path):
        with sqlite3.connect(db_path) as conn:
            return conn.execute(
                "SELECT id, form, answers FROM submissions ORDER BY id"
            ).fetchall()

    def _assert_ok(self, result):
        """退出码 0、valid 为 true、errors 为空、submissionId 为整数，
        且表单与答卷原文作为新行逐字符落库。"""
        proc, db_path, form_text, answers_text = result
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8"))
        payload = self._payload(proc)
        self.assertEqual(
            set(payload), {"valid", "errors", "submissionId"}
        )
        self.assertIs(payload["valid"], True)
        self.assertEqual(payload["errors"], {})
        submission_id = payload["submissionId"]
        self.assertIsInstance(submission_id, int)
        self.assertTrue(os.path.exists(db_path))
        rows = self._rows(db_path)
        self.assertTrue(rows)
        row_id, row_form, row_answers = rows[-1]
        self.assertEqual(row_id, submission_id)
        self.assertEqual(row_form, form_text)
        self.assertEqual(row_answers, answers_text)
        return payload, rows

    def _assert_validation_failed(self, result, expected_errors):
        """退出码 1、valid 为 false、errors 精确匹配、无 submissionId，
        且数据库文件不被创建。"""
        proc, db_path, _, _ = result
        self.assertEqual(proc.returncode, 1, proc.stderr.decode("utf-8"))
        payload = self._payload(proc)
        self.assertEqual(
            payload, {"valid": False, "errors": expected_errors}
        )
        self.assertNotIn("submissionId", payload)
        self.assertFalse(os.path.exists(db_path))

    def _assert_invalid_input(self, result):
        """退出码 2、输出恰为 invalid_input，且数据库文件不被创建。"""
        proc, db_path, _, _ = result
        self.assertEqual(proc.returncode, 2, proc.stderr.decode("utf-8"))
        self.assertEqual(self._payload(proc), {"error": "invalid_input"})
        self.assertFalse(os.path.exists(db_path))

    # -- 原值匹配：列出的选项通过，其余字符串报 choice --------------------

    def test_listed_options_submit(self):
        form = {"fields": [_select_field()]}
        for value in ("A", "B"):
            with self.subTest(value=value):
                self._assert_ok(self._run(form, {"kind": value}))

    def test_unlisted_strings_report_choice(self):
        # 大小写不同、含前导空格、未列出的字符串均按原值不匹配处理。
        form = {"fields": [_select_field()]}
        for value in ("a", " A", "C"):
            with self.subTest(value=value):
                self._assert_validation_failed(
                    self._run(form, {"kind": value}), {"kind": "choice"}
                )

    def test_non_string_values_report_type(self):
        form = {"fields": [_select_field()]}
        for value in (1, True, ["A"], {"v": "A"}):
            with self.subTest(value=repr(value)):
                self._assert_validation_failed(
                    self._run(form, {"kind": value}), {"kind": "type"}
                )

    # -- 选填与必填、空字符串的交互 --------------------------------------

    def test_optional_missing_and_null_pass(self):
        form = {"fields": [_select_field()]}
        for label, answers in (("missing", {}), ("null", {"kind": None})):
            with self.subTest(case=label):
                self._assert_ok(self._run(form, answers))

    def test_optional_empty_string_reports_choice(self):
        # 空字符串是字符串值，不在 ["A", "B"] 中，报 choice 而非通过。
        form = {"fields": [_select_field()]}
        self._assert_validation_failed(
            self._run(form, {"kind": ""}), {"kind": "choice"}
        )

    def test_required_missing_null_empty_report_required(self):
        form = {"fields": [_select_field(required=True)]}
        cases = (
            ("missing", {}),
            ("null", {"kind": None}),
            ("empty", {"kind": ""}),
        )
        for label, answers in cases:
            with self.subTest(case=label):
                self._assert_validation_failed(
                    self._run(form, answers), {"kind": "required"}
                )

    def test_required_listed_option_passes(self):
        form = {"fields": [_select_field(required=True)]}
        self._assert_ok(self._run(form, {"kind": "B"}))

    def test_empty_string_option(self):
        # options 含空字符串时，选填的空字符串是合法选项。
        optional_form = {"fields": [_select_field(options=("", "A"))]}
        self._assert_ok(self._run(optional_form, {"kind": ""}))
        # 必填仍把空字符串视为未填写，报 required。
        required_form = {
            "fields": [_select_field(options=("", "A"), required=True)]
        }
        self._assert_validation_failed(
            self._run(required_form, {"kind": ""}), {"kind": "required"}
        )

    # -- 多字段：required、choice 与 unknown_field 同时给出 ----------------

    def test_multiple_errors_reported_together(self):
        # 必填 first 缺失 -> required；选填 second 值未列出 -> choice；
        # 答卷键 ghost 未在表单中定义 -> unknown_field。
        form = {
            "fields": [
                _select_field("first", options=("X",), required=True),
                _select_field("second", options=("Y",)),
            ]
        }
        answers = {"second": "nope", "ghost": 1}
        result = self._run(form, answers)
        proc, db_path, _, _ = result
        self.assertEqual(proc.returncode, 1, proc.stderr.decode("utf-8"))
        payload = self._payload(proc)
        self.assertEqual(
            payload,
            {
                "valid": False,
                "errors": {
                    "first": "required",
                    "second": "choice",
                    "ghost": "unknown_field",
                },
            },
        )
        # 每个字段仅有一个错误码，不因先遇到一个错误而遗漏其余错误。
        for key, code in payload["errors"].items():
            self.assertIsInstance(
                code, str, "字段 %r 的错误码应为单个字符串" % key
            )
        self.assertFalse(os.path.exists(db_path))

    # -- 非法 options 定义 -------------------------------------------------

    def test_invalid_options_reject_form(self):
        cases = [
            ("missing", {"id": "kind", "type": "select"}),
            ("empty_array",
             {"id": "kind", "type": "select", "options": []}),
            ("not_array_string",
             {"id": "kind", "type": "select", "options": "AB"}),
            ("not_array_object",
             {"id": "kind", "type": "select", "options": {"a": 1}}),
            ("int_element",
             {"id": "kind", "type": "select", "options": ["A", 1]}),
            ("null_element",
             {"id": "kind", "type": "select", "options": ["A", None]}),
            ("bool_element",
             {"id": "kind", "type": "select", "options": ["A", True]}),
            ("duplicate",
             {"id": "kind", "type": "select", "options": ["A", "A"]}),
            ("duplicate_empty",
             {"id": "kind", "type": "select", "options": ["", ""]}),
        ]
        for label, field in cases:
            with self.subTest(case=label):
                form = {"fields": [field]}
                # 答卷本身合法：失败原因只能来自 options 定义。
                self._assert_invalid_input(self._run(form, {"kind": "A"}))
                # 答卷同时有字段错误（choice 与 unknown_field）时，
                # 结果仍是输入不合法，退出码 2。
                self._assert_invalid_input(
                    self._run(form, {"kind": "ZZZ", "extra": 1})
                )

    # -- 原文落库：含中文、空格与换行 --------------------------------------

    def test_unicode_space_newline_text_stored_verbatim(self):
        # 表单与答卷原文刻意含中文、空格与换行；选项值本身也含空格，
        # 另一选项含换行（JSON 源文本中为转义写法）。
        form_text = (
            '{\n'
            '  "fields": [\n'
            '    {"id": "kind", "type": "select",\n'
            '     "options": ["选 项 甲", "乙\\n丙"]}\n'
            '  ]\n'
            '}\n'
        )
        for raw_answers, expected_value in (
            ('{\n  "kind": "选 项 甲"\n}\n', "选 项 甲"),
            ('{\n  "kind": "乙\\n丙"\n}\n', "乙\n丙"),
        ):
            with self.subTest(expected=expected_value):
                result = self._run_raw(form_text, raw_answers)
                _, rows = self._assert_ok(result)
                # 库中答卷解码后的值与预期一致（含空格与换行）。
                self.assertEqual(
                    json.loads(rows[-1][2]), {"kind": expected_value}
                )

    # -- 失败不影响既有库 -------------------------------------------------

    def test_failures_leave_existing_database_unchanged(self):
        # 先写入一条成功记录作为基线。
        form = {"fields": [_select_field(required=True)]}
        seeded = self._run(form, {"kind": "A"})
        _, db_path, _, _ = seeded
        _, seeded_rows = self._assert_ok(seeded)

        # 各类失败在同一既有库上执行：退出码各就各位，
        # 记录数、主键与原文均不得改变。
        invalid_form = {"fields": [{"id": "kind", "type": "select"}]}
        failing = [
            ("choice", form, {"kind": "C"}, 1),
            ("type", form, {"kind": 1}, 1),
            ("required", form, {}, 1),
            ("unknown_field", form, {"kind": "A", "ghost": 1}, 1),
            ("invalid_form", invalid_form, {"kind": "A"}, 2),
        ]
        for label, bad_form, bad_answers, expected_code in failing:
            with self.subTest(case=label):
                proc, _, _, _ = self._run(
                    bad_form, bad_answers, db_path=db_path
                )
                self.assertEqual(
                    proc.returncode,
                    expected_code,
                    proc.stderr.decode("utf-8"),
                )
                self.assertEqual(self._rows(db_path), seeded_rows)


if __name__ == "__main__":
    unittest.main()
