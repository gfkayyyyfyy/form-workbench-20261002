"""number 字段固定类型校验的回归测试。

只通过公开入口 ``python -m forms submit`` 执行：合成 JSON 写入临时目录，
分别检查退出码、stdout 解析后的 JSON 与标准库 sqlite3 中的落库结果。
不导入 forms 包内的任何内部函数，不读取或修改既有数据库。

覆盖的公开行为：
- 零、负整数、小数、指数形式有限数、一百个 9 的任意精度整数可提交；
- 布尔、数字字符串、数组、对象报字段错误 type；
- 选填缺失/None 通过、空字符串报 type；必填缺失/None/空字符串报 required；
- 1e400 解码为 inf 报 type；NaN/Infinity/-Infinity 以 invalid_input 退出 2；
- 校验失败不创建数据库文件，也不改变已有成功记录。
"""

import json
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


def _number_field(field_id="n", required=False):
    field = {"id": field_id, "type": "number"}
    if required:
        field["required"] = True
    return field


class NumberFieldTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    # -- 辅助 ------------------------------------------------------------

    def _run_raw(self, form_text, answers_text, db_path=None):
        """以给定原文写文件并执行提交。

        每次使用全新的子目录；db_path 为 None 时数据库也位于该子目录
        （尚不存在），传入既有路径则可验证失败不影响已有库。返回内容
        附带输入原文与用例标签，便于断言信息指出具体输入。
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

    def _payload(self, proc, label):
        """解析 stdout 为 JSON，失败时附带原始输出与输入标签。"""
        raw = proc.stdout.decode("utf-8")
        try:
            return json.loads(raw)
        except ValueError:
            self.fail(
                "用例 %s：stdout 不是合法 JSON：%r（stderr=%r）"
                % (label, raw, proc.stderr.decode("utf-8"))
            )

    def _rows(self, db_path):
        with sqlite3.connect(db_path) as conn:
            return conn.execute(
                "SELECT id, form, answers FROM submissions ORDER BY id"
            ).fetchall()

    def _assert_ok(self, label, result, expected_answers=None):
        """退出码 0、valid 为 true、errors 为空、submissionId 为整数，
        且原文（含空格与换行）作为新行落库。"""
        proc, db_path, form_text, answers_text = result
        self.assertEqual(
            proc.returncode,
            0,
            "用例 %s：期望退出码 0，实际 %s，stderr=%r"
            % (label, proc.returncode, proc.stderr.decode("utf-8")),
        )
        payload = self._payload(proc, label)
        self.assertIs(
            payload.get("valid"),
            True,
            "用例 %s：期望 valid=true，实际 %r" % (label, payload),
        )
        self.assertEqual(
            payload.get("errors"),
            {},
            "用例 %s：期望 errors 为空，实际 %r"
            % (label, payload.get("errors")),
        )
        submission_id = payload.get("submissionId")
        self.assertIsInstance(
            submission_id,
            int,
            "用例 %s：submissionId 应为整数，实际 %r"
            % (label, submission_id),
        )
        self.assertTrue(
            os.path.exists(db_path),
            "用例 %s：成功提交后数据库文件应存在" % label,
        )
        rows = self._rows(db_path)
        self.assertTrue(rows, "用例 %s：submissions 中应有记录" % label)
        row_id, row_form, row_answers = rows[-1]
        self.assertEqual(
            row_id,
            submission_id,
            "用例 %s：submissionId %r 与库中主键 %r 不一致"
            % (label, submission_id, row_id),
        )
        # 存储证据：表单与答卷均保留提交原文中的空格与换行。
        self.assertIn(" ", form_text)
        self.assertIn("\n", form_text)
        self.assertIn(" ", answers_text)
        self.assertIn("\n", answers_text)
        self.assertEqual(
            row_form,
            form_text,
            "用例 %s：库中表单原文应与提交原文逐字符一致" % label,
        )
        self.assertEqual(
            row_answers,
            answers_text,
            "用例 %s：库中答卷原文应与提交原文逐字符一致" % label,
        )
        if expected_answers is not None:
            self.assertEqual(
                json.loads(row_answers),
                expected_answers,
                "用例 %s：库中答卷解码值应为 %r" % (label, expected_answers),
            )
        return payload, rows

    def _assert_validation_failed(self, label, result, expected_errors):
        """退出码 1、valid 为 false、错误映射精确匹配、无 submissionId，
        且数据库文件不被创建。"""
        proc, db_path, _, _ = result
        self.assertEqual(
            proc.returncode,
            1,
            "用例 %s：期望退出码 1，实际 %s，stderr=%r"
            % (label, proc.returncode, proc.stderr.decode("utf-8")),
        )
        payload = self._payload(proc, label)
        self.assertEqual(
            payload,
            {"valid": False, "errors": expected_errors},
            "用例 %s：期望 %r，实际 %r"
            % (
                label,
                {"valid": False, "errors": expected_errors},
                payload,
            ),
        )
        self.assertNotIn(
            "submissionId",
            payload,
            "用例 %s：校验失败不应返回 submissionId" % label,
        )
        self.assertFalse(
            os.path.exists(db_path),
            "用例 %s：校验失败不应创建数据库文件 %s" % (label, db_path),
        )

    def _assert_invalid_input(self, label, result):
        """退出码 2、输出恰为 {"error":"invalid_input"}，不创建数据库。"""
        proc, db_path, _, _ = result
        self.assertEqual(
            proc.returncode,
            2,
            "用例 %s：期望退出码 2，实际 %s，stdout=%r，stderr=%r"
            % (
                label,
                proc.returncode,
                proc.stdout.decode("utf-8"),
                proc.stderr.decode("utf-8"),
            ),
        )
        payload = self._payload(proc, label)
        self.assertEqual(
            payload,
            {"error": "invalid_input"},
            "用例 %s：期望 invalid_input，实际 %r" % (label, payload),
        )
        self.assertFalse(
            os.path.exists(db_path),
            "用例 %s：输入不合法不应创建数据库文件 %s" % (label, db_path),
        )

    def _pretty_raw(self, field_expr, literal):
        """构造刻意含空格与换行的提交原文，用于核对原样存储。"""
        form_text = (
            "{\n"
            '  "fields": [\n'
            '    { "id": "n", "type": "number"%s }\n'
            "  ]\n"
            "}\n" % (", " + field_expr if field_expr else "")
        )
        answers_text = (
            "{\n"
            '  "n": %s  \n'
            "}\n" % literal
        )
        return form_text, answers_text

    # -- 可提交的有限数字（均以含空格、换行的原文提交） ------------------

    def test_finite_numbers_submit_and_persist(self):
        # (标签, JSON 源文中的数字写法, 解码后的 Python 值)
        ninety_nines = "9" * 100
        cases = [
            ("zero", "0", 0),
            ("negative_integer", "-42", -42),
            ("positive_decimal", "3.5", 3.5),
            ("negative_decimal", "-0.25", -0.25),
            ("exponent_finite", "1e3", 1000.0),
            ("negative_exponent", "2.5E-2", 0.025),
            ("hundred_nines", ninety_nines, int(ninety_nines)),
        ]
        form = {"fields": [_number_field()]}
        for label, literal, expected in cases:
            with self.subTest(number=label):
                form_text, answers_text = self._pretty_raw("", literal)
                self.assertEqual(
                    json.loads(form_text),
                    form,
                    "用例 %s：合成表单原文应解码为标准结构" % label,
                )
                result = self._run_raw(form_text, answers_text)
                self._assert_ok(
                    label, result, expected_answers={"n": expected}
                )
                # 一百个 9 必须保持任意精度整数，不得退化为浮点。
                if label == "hundred_nines":
                    self.assertIsInstance(expected, int)
                    self.assertEqual(expected, 10 ** 100 - 1)

    # -- 非数字类型报 type -----------------------------------------------

    def test_optional_wrong_types_report_type(self):
        form = {"fields": [_number_field()]}
        # (标签, 答卷 JSON 原文)：字符串无法用 json.dumps 表达数值字面量，
        # 其余值均可序列化，二者统一用原文提交。
        cases = [
            ("boolean_true", '{\n  "n": true\n}\n'),
            ("boolean_false", '{"n": false}'),
            ("numeric_string", '{"n": "123"}'),
            ("empty_string", '{"n": ""}'),
            ("array", '{"n": [1]}'),
            ("object", '{"n": {"value": 1}}'),
        ]
        for label, raw_answers in cases:
            with self.subTest(value=label):
                result = self._run_raw(
                    json.dumps(form, ensure_ascii=False), raw_answers
                )
                self._assert_validation_failed(
                    label, result, {"n": "type"}
                )

    # -- 选填：缺失与 null 通过，空字符串报 type --------------------------

    def test_optional_missing_and_null_pass(self):
        form = {"fields": [_number_field()]}
        for label, answers in (("missing", {}), ("null", {"n": None})):
            with self.subTest(case=label):
                form_text, answers_text = self._pretty_raw(
                    "", "null" if answers else ""
                )
                if not answers:
                    # 刻意保留空格，与成功用例的原样存储断言一致。
                    answers_text = "{ }\n"
                result = self._run_raw(form_text, answers_text)
                self._assert_ok(
                    label, result, expected_answers=answers or {}
                )

    def test_optional_empty_string_reports_type(self):
        result = self._run(
            {"fields": [_number_field()]}, {"n": ""}
        )
        self._assert_validation_failed(
            "optional-empty-string", result, {"n": "type"}
        )

    # -- 必填：缺失/null/空字符串报 required，其余报 type ----------------

    def test_required_missing_null_empty_report_required(self):
        form = {"fields": [_number_field(required=True)]}
        cases = [
            ("required-missing", {}),
            ("required-null", {"n": None}),
            ("required-empty-string", {"n": ""}),
        ]
        for label, answers in cases:
            with self.subTest(case=label):
                self._assert_validation_failed(
                    label, self._run(form, answers), {"n": "required"}
                )

    def test_required_other_wrong_types_report_type(self):
        form = {"fields": [_number_field(required=True)]}
        cases = [
            ("required-bool", True),
            ("required-string", "1"),
            ("required-array", []),
            ("required-object", {}),
        ]
        for label, bad_value in cases:
            with self.subTest(case=label):
                self._assert_validation_failed(
                    label,
                    self._run(form, {"n": bad_value}),
                    {"n": "type"},
                )

    # -- 非有限数值与非标准 JSON 常量 ------------------------------------

    def test_1e400_decodes_to_infinity_and_reports_type(self):
        # 1e400 超出 float 范围，标准 json 将其静默解码为 inf；
        # 字段校验必须判定为非有限数并报 type，而非拒绝整个输入。
        form_text = '{\n  "fields": [\n    {"id": "n", "type": "number"}\n  ]\n}\n'
        answers_text = '{\n  "n": 1e400\n}\n'
        decoded = json.loads(answers_text)  # 确认前置事实：解码确为 inf
        self.assertTrue(math.isinf(decoded["n"]))
        result = self._run_raw(form_text, answers_text)
        self._assert_validation_failed(
            "1e400", result, {"n": "type"}
        )

    def test_json_constants_exit_invalid_input(self):
        # NaN/Infinity/-Infinity 不是合法 JSON，入口必须在解码阶段拒绝。
        form_text = '{"fields": [{"id": "n", "type": "number"}]}'
        for label, literal in (
            ("NaN", "NaN"),
            ("Infinity", "Infinity"),
            ("NegativeInfinity", "-Infinity"),
        ):
            with self.subTest(constant=label):
                answers_text = '{"n": %s}' % literal
                result = self._run_raw(form_text, answers_text)
                self._assert_invalid_input(label, result)

    # -- 多字段：required 与 type 错误同时给出 ---------------------------

    def test_required_and_type_errors_reported_together(self):
        # 必填 a 为空字符串 -> required；选填 b 为布尔 -> type。
        form = {
            "fields": [
                _number_field("a", required=True),
                _number_field("b"),
            ]
        }
        result = self._run(form, {"a": "", "b": True})
        self._assert_validation_failed(
            "a-empty-b-bool",
            result,
            {"a": "required", "b": "type"},
        )

    # -- 失败不影响既有库 -------------------------------------------------

    def test_failures_leave_existing_database_unchanged(self):
        # 先写入一条成功记录作为基线。
        form = {"fields": [_number_field(required=True)]}
        form_text, seeded_answers = self._pretty_raw(
            '"required": true', "123"
        )
        seeded = self._run_raw(form_text, seeded_answers)
        proc, db_path, _, _ = seeded
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8"))
        seeded_rows = self._rows(db_path)
        self.assertEqual(len(seeded_rows), 1)

        # 全部本次失败用例在同一既有库上执行：退出码各就各位，
        # 记录数与每条记录内容均不得增加或改变。
        failing = [
            ("required-missing", form_text, "{}\n", 1),
            ("required-null", form_text, '{"n": null}\n', 1),
            ("required-empty", form_text, '{"n": ""}', 1),
            ("type-bool", form_text, '{"n": true}', 1),
            ("type-string", form_text, '{"n": "1"}', 1),
            ("type-array", form_text, '{"n": [1]}', 1),
            ("type-object", form_text, '{"n": {"k": 1}}', 1),
            ("non-finite-1e400", form_text, '{"n": 1e400}', 1),
            ("constant-NaN", form_text, '{"n": NaN}', 2),
            (
                "constant-Infinity",
                form_text,
                '{"n": Infinity}',
                2,
            ),
            (
                "constant-NegativeInfinity",
                form_text,
                '{"n": -Infinity}',
                2,
            ),
        ]
        for label, raw_form, raw_answers, expected_code in failing:
            with self.subTest(case=label):
                proc, _, _, _ = self._run_raw(
                    raw_form, raw_answers, db_path=db_path
                )
                self.assertEqual(
                    proc.returncode,
                    expected_code,
                    "用例 %s：期望退出码 %s，实际 %s，stdout=%r"
                    % (
                        label,
                        expected_code,
                        proc.returncode,
                        proc.stdout.decode("utf-8"),
                    ),
                )
                rows = self._rows(db_path)
                self.assertEqual(
                    rows,
                    seeded_rows,
                    "用例 %s：失败提交后数据库记录发生变化" % label,
                )


if __name__ == "__main__":
    unittest.main()
