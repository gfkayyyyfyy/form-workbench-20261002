"""number 字段提交的回归测试。

只通过公开入口 ``python -m forms submit`` 执行：合成 JSON 写入临时目录，
检查退出码与 stdout 解析后的 JSON，并用标准库 sqlite3 核对落库结果。
不导入 forms 包内的任何内部函数，不读取或修改既有数据库；
与 test_max_length.py 相互独立，可单独运行::

    python -m unittest test_number_field -v
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


def _number_field(field_id="score", required=False):
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

    def _run_number(self, answers_text, required=False, db_path=None):
        """以单个 number 字段的表单提交给定答卷原文。"""
        form_text = json.dumps({"fields": [_number_field(required=required)]})
        return self._run_raw(form_text, answers_text, db_path=db_path)

    def _payload(self, proc):
        return json.loads(proc.stdout.decode("utf-8"))

    def _assert_ok(self, result):
        """退出码 0、valid 为 true、errors 为空，且原文按新行落库。"""
        proc, db_path, form_text, answers_text = result
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8"))
        payload = self._payload(proc)
        self.assertEqual(
            set(payload), {"valid", "errors", "submissionId"}, payload
        )
        self.assertTrue(payload["valid"], payload)
        self.assertEqual(payload["errors"], {}, payload)
        submission_id = payload["submissionId"]
        self.assertIsInstance(submission_id, int, payload)
        self.assertTrue(os.path.exists(db_path), "成功提交应创建数据库文件")
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id, form, answers FROM submissions ORDER BY id"
            ).fetchall()
        self.assertTrue(rows, "成功提交应在 submissions 中留下记录")
        self.assertEqual(rows[-1][0], submission_id)
        self.assertEqual(
            rows[-1][1], form_text, "落库表单应与提交原文逐字节一致"
        )
        self.assertEqual(
            rows[-1][2], answers_text, "落库答卷应与提交原文逐字节一致"
        )
        return payload, rows

    def _assert_validation_failed(self, result, expected_errors):
        """退出码 1、错误码精确匹配、无 submissionId，且不创建数据库。"""
        proc, db_path, _, _ = result
        self.assertEqual(proc.returncode, 1, proc.stderr.decode("utf-8"))
        payload = self._payload(proc)
        self.assertEqual(
            payload,
            {"valid": False, "errors": expected_errors},
            "校验失败的输出应与预期错误映射完全一致",
        )
        self.assertNotIn("submissionId", payload)
        self.assertFalse(os.path.exists(db_path), "校验失败不应创建数据库文件")

    def _assert_invalid_input(self, result):
        """退出码 2、invalid_input，且数据库文件不被创建。"""
        proc, db_path, _, _ = result
        self.assertEqual(proc.returncode, 2, proc.stderr.decode("utf-8"))
        self.assertEqual(self._payload(proc), {"error": "invalid_input"})
        self.assertFalse(os.path.exists(db_path), "输入不合法不应创建数据库文件")

    # -- 有限数均可提交：零、负整数、小数、指数形式、超大整数 --------------

    def test_zero_passes(self):
        for raw in ('{"score": 0}', '{"score": -0}', '{"score": 0.0}'):
            with self.subTest(raw=raw):
                self._assert_ok(self._run_number(raw))

    def test_negative_integer_passes(self):
        self._assert_ok(self._run_number('{"score": -42}'))

    def test_decimal_passes(self):
        for raw in ('{"score": 3.14}', '{"score": -0.5}'):
            with self.subTest(raw=raw):
                self._assert_ok(self._run_number(raw))

    def test_exponent_form_passes(self):
        # 原文保留指数写法，落库文本应与原文一致。
        for raw in (
            '{"score": 1e3}',
            '{"score": -2.5E-4}',
            '{"score": 6.02e+23}',
        ):
            with self.subTest(raw=raw):
                self._assert_ok(self._run_number(raw))

    def test_hundred_digit_integer_passes(self):
        # 一百个 9 组成的整数：JSON 整数任意精度，必然有限。
        big = "9" * 100
        self._assert_ok(self._run_number('{"score": %s}' % big))

    def test_whitespace_and_newlines_preserved_in_storage(self):
        # 表单与答卷原文中的空格、缩进与换行应原样落库。
        form_text = (
            '{\n'
            '  "fields": [\n'
            '    { "id": "score",\n'
            '      "type": "number" }\n'
            '  ]\n'
            '}'
        )
        answers_text = '{\n    "score":\n      12.5\n}'
        self._assert_ok(self._run_raw(form_text, answers_text))

    # -- 非数值类型一律报 type --------------------------------------------

    def test_non_number_values_report_type(self):
        cases = [
            ('{"score": true}', "布尔值 true"),
            ('{"score": false}', "布尔值 false"),
            ('{"score": "42"}', "数字字符串"),
            ('{"score": "3.14"}', "小数字符串"),
            ('{"score": [1, 2]}', "数组"),
            ('{"score": {"x": 1}}', "对象"),
        ]
        for raw, label in cases:
            with self.subTest(label=label, raw=raw):
                self._assert_validation_failed(
                    self._run_number(raw), {"score": "type"}
                )

    def test_non_finite_json_number_reports_type(self):
        # 1e400 是合法 JSON 数值，但解码为 inf，不有限，应报 type（退出码 1）。
        for raw in ('{"score": 1e400}', '{"score": -1e400}'):
            with self.subTest(raw=raw):
                self._assert_validation_failed(
                    self._run_number(raw), {"score": "type"}
                )

    def test_nonstandard_constants_are_invalid_input(self):
        # NaN / Infinity / -Infinity 不是标准 JSON，应整体拒绝（退出码 2）。
        for constant in ("NaN", "Infinity", "-Infinity"):
            raw = '{"score": %s}' % constant
            with self.subTest(raw=raw):
                self._assert_invalid_input(self._run_number(raw))

    # -- 选填与必填的缺失/null/空字符串语义 --------------------------------

    def test_optional_missing_and_null_pass(self):
        for raw in ("{}", '{"score": null}'):
            with self.subTest(raw=raw):
                self._assert_ok(self._run_number(raw))

    def test_optional_empty_string_reports_type(self):
        self._assert_validation_failed(
            self._run_number('{"score": ""}'), {"score": "type"}
        )

    def test_required_missing_null_empty_report_required(self):
        for raw in ("{}", '{"score": null}', '{"score": ""}'):
            with self.subTest(raw=raw):
                self._assert_validation_failed(
                    self._run_number(raw, required=True),
                    {"score": "required"},
                )

    def test_required_other_bad_types_report_type(self):
        # 必填字段收到非空但类型错误的值时仍报 type，而非 required。
        for raw in (
            '{"score": true}',
            '{"score": "5"}',
            '{"score": []}',
            '{"score": {}}',
        ):
            with self.subTest(raw=raw):
                self._assert_validation_failed(
                    self._run_number(raw, required=True),
                    {"score": "type"},
                )

    # -- 多字段错误同时汇总 -------------------------------------------------

    def test_required_and_type_errors_reported_together(self):
        form = {
            "fields": [
                _number_field("a", required=True),
                _number_field("b"),
            ]
        }
        result = self._run(form, {"a": "", "b": True})
        proc, db_path, _, _ = result
        self.assertEqual(proc.returncode, 1, proc.stderr.decode("utf-8"))
        payload = self._payload(proc)
        self.assertEqual(
            payload,
            {"valid": False, "errors": {"a": "required", "b": "type"}},
        )
        self.assertNotIn("submissionId", payload)
        self.assertFalse(os.path.exists(db_path))

    # -- 失败不影响既有库 -------------------------------------------------

    def test_failures_leave_existing_database_unchanged(self):
        seeded = self._run_number('{"score": 7}')
        _, db_path, _, _ = seeded
        _, seeded_rows = self._assert_ok(seeded)

        def read_rows():
            with sqlite3.connect(db_path) as conn:
                return conn.execute(
                    "SELECT id, form, answers FROM submissions ORDER BY id"
                ).fetchall()

        # 字段校验失败（退出码 1）：记录数与内容不变。
        type_fail = self._run_number('{"score": true}', db_path=db_path)
        proc, _, _, _ = type_fail
        self.assertEqual(proc.returncode, 1, proc.stderr.decode("utf-8"))
        self.assertEqual(self._payload(proc),
                         {"valid": False, "errors": {"score": "type"}})
        self.assertEqual(read_rows(), seeded_rows,
                         "校验失败不应新增或修改任何记录")

        # 非有限数值（退出码 1）：记录数与内容不变。
        inf_fail = self._run_number('{"score": 1e400}', db_path=db_path)
        proc, _, _, _ = inf_fail
        self.assertEqual(proc.returncode, 1, proc.stderr.decode("utf-8"))
        self.assertEqual(read_rows(), seeded_rows,
                         "非有限数值失败不应新增或修改任何记录")

        # 非标准常量（退出码 2）：记录数与内容同样不变。
        nan_fail = self._run_number('{"score": NaN}', db_path=db_path)
        proc, _, _, _ = nan_fail
        self.assertEqual(proc.returncode, 2, proc.stderr.decode("utf-8"))
        self.assertEqual(self._payload(proc), {"error": "invalid_input"})
        self.assertEqual(read_rows(), seeded_rows,
                         "invalid_input 不应新增或修改任何记录")


if __name__ == "__main__":
    unittest.main()
