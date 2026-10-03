"""number 字段 minimum 下限约束的回归测试。

只通过公开入口 ``python -m forms submit`` 执行：合成 JSON 写入临时目录，
检查退出码与 stdout 解析后的 JSON，并用标准库 sqlite3 核对落库结果。
不导入 forms 包内的任何内部函数，不读取或修改既有数据库。

覆盖的公开行为：
- minimum 可为零、负数、整数或有限小数，两端按 JSON 解码后的数值比较，
  恰好等于下限通过、低于下限返回 min_value；
- 省略 minimum 时维持原有 number 校验；
- 选填缺失/null 跳过下限检查；必填缺失/null/空字符串仍报 required；
- 布尔、数字字符串、数组、对象以及 1e400 解码出的无穷值仍报 type；
- minimum 取布尔/字符串/null/数组/对象/无穷值，或出现在 text/select
  字段上时表单不合法（退出 2，即使答卷未填该字段）；
- 大整数按任意精度比较，不因舍入接受低于下限的值；
- 校验失败不创建数据库、不改变既有记录，成功时原文（含空格换行）落库。
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


def _number_field(field_id="n", minimum=None, required=False):
    field = {"id": field_id, "type": "number"}
    if minimum is not None:
        field["minimum"] = minimum
    if required:
        field["required"] = True
    return field


class MinimumValueTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    # -- 辅助 ------------------------------------------------------------

    def _run_raw(self, form_text, answers_text, db_path=None):
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

    def _assert_ok(self, label, result):
        proc, db_path, form_text, answers_text = result
        self.assertEqual(
            proc.returncode,
            0,
            "用例 %s：期望退出码 0，实际 %s，stderr=%r"
            % (label, proc.returncode, proc.stderr.decode("utf-8")),
        )
        payload = self._payload(proc, label)
        self.assertIs(payload.get("valid"), True, "用例 %s：%r" % (label, payload))
        self.assertEqual(payload.get("errors"), {})
        self.assertIsInstance(payload.get("submissionId"), int)
        self.assertEqual(
            self._rows(db_path)[-1],
            (payload["submissionId"], form_text, answers_text),
            "用例 %s：落库原文应逐字符一致" % label,
        )
        return payload

    def _assert_min_value(self, label, result):
        proc, db_path, _, _ = result
        self.assertEqual(
            proc.returncode,
            1,
            "用例 %s：期望退出码 1，实际 %s，stderr=%r"
            % (label, proc.returncode, proc.stderr.decode("utf-8")),
        )
        self.assertEqual(
            self._payload(proc, label),
            {"valid": False, "errors": {"n": "min_value"}},
        )
        self.assertFalse(
            os.path.exists(db_path),
            "用例 %s：校验失败不应创建数据库文件 %s" % (label, db_path),
        )

    def _assert_invalid_input(self, label, result):
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
        self.assertEqual(
            self._payload(proc, label), {"error": "invalid_input"}
        )
        self.assertFalse(
            os.path.exists(db_path),
            "用例 %s：输入不合法不应创建数据库文件 %s" % (label, db_path),
        )

    # -- 边界包含：零、负数、小数、指数 -----------------------------------

    def test_values_at_or_above_limit_pass(self):
        cases = [
            ("zero-boundary", 0, 0),
            ("above-zero", 0, 1),
            ("negative-limit-at", -5, -5),
            ("negative-limit-above", -5, -4),
            ("decimal-at", 2.5, 2.5),
            ("decimal-above", 2.5, 2.75),
            ("negative-decimal-at", -0.25, -0.25),
            ("exponent-limit", 1000, 1e3),
        ]
        for label, minimum, value in cases:
            with self.subTest(case=label):
                self._assert_ok(
                    label,
                    self._run(
                        {"fields": [_number_field(minimum=minimum)]},
                        {"n": value},
                    ),
                )

    def test_values_below_limit_report_min_value(self):
        cases = [
            ("zero", 0, -1),
            ("negative", -5, -6),
            ("decimal", 2.5, 2.49),
            ("negative-decimal", -0.25, -0.3),
            ("exponent", 1000, 999.99),
        ]
        for label, minimum, value in cases:
            with self.subTest(case=label):
                self._assert_min_value(
                    label,
                    self._run(
                        {"fields": [_number_field(minimum=minimum)]},
                        {"n": value},
                    ),
                )

    # -- 大整数：任意精度比较，不得舍入 -----------------------------------

    def test_big_integer_compared_exactly(self):
        big = 10 ** 100
        form = {"fields": [_number_field(minimum=big)]}
        self._assert_ok("at-big-limit", self._run(form, {"n": big}))
        self._assert_ok("above-big-limit", self._run(form, {"n": big + 1}))
        self._assert_min_value(
            "one-below-big-limit", self._run(form, {"n": big - 1})
        )

    # -- 选填缺失/null 跳过；空字符串仍报 type ----------------------------

    def test_optional_missing_and_null_skip_minimum(self):
        form = {"fields": [_number_field(minimum=0)]}
        for label, answers in (("missing", {}), ("null", {"n": None})):
            with self.subTest(case=label):
                self._assert_ok(label, self._run(form, answers))

    def test_optional_empty_string_reports_type(self):
        result = self._run(
            {"fields": [_number_field(minimum=0)]}, {"n": ""}
        )
        proc, _, _, _ = result
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(
            self._payload(proc, "empty-string"),
            {"valid": False, "errors": {"n": "type"}},
        )

    # -- 必填优先于下限 ---------------------------------------------------

    def test_required_missing_null_empty_report_required(self):
        form = {"fields": [_number_field(minimum=0, required=True)]}
        for label, answers in (
            ("missing", {}),
            ("null", {"n": None}),
            ("empty", {"n": ""}),
        ):
            with self.subTest(case=label):
                result = self._run(form, answers)
                proc, _, _, _ = result
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(
                    self._payload(proc, label),
                    {"valid": False, "errors": {"n": "required"}},
                )

    def test_required_below_limit_reports_min_value(self):
        form = {"fields": [_number_field(minimum=0, required=True)]}
        self._assert_min_value(
            "required-below", self._run(form, {"n": -1})
        )

    # -- 类型错误不得改报 min_value ---------------------------------------

    def test_wrong_types_still_report_type(self):
        form_text = json.dumps(
            {"fields": [_number_field(minimum=0)]}, ensure_ascii=False
        )
        cases = [
            ("bool", '{"n": true}'),
            ("numeric-string", '{"n": "123"}'),
            ("array", '{"n": [1]}'),
            ("object", '{"n": {"v": 1}}'),
            ("infinity", '{"n": 1e400}'),
        ]
        for label, raw_answers in cases:
            with self.subTest(case=label):
                if label == "infinity":
                    self.assertTrue(
                        math.isinf(json.loads(raw_answers)["n"])
                    )
                result = self._run_raw(form_text, raw_answers)
                proc, _, _, _ = result
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(
                    self._payload(proc, label),
                    {"valid": False, "errors": {"n": "type"}},
                )

    # -- 省略 minimum：维持原有规则 ---------------------------------------

    def test_without_minimum_keeps_old_behavior(self):
        form = {"fields": [_number_field()]}
        self._assert_ok(
            "negative-without-minimum",
            self._run(form, {"n": -10 ** 20}),
        )

    # -- 多字段一次汇总 ---------------------------------------------------

    def test_min_value_and_unknown_field_reported_together(self):
        form = {"fields": [_number_field(minimum=0)]}
        result = self._run(form, {"n": -1, "extra": 2})
        proc, db_path, _, _ = result
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(
            self._payload(proc, "aggregate")["errors"],
            {"n": "min_value", "extra": "unknown_field"},
        )
        self.assertFalse(os.path.exists(db_path))

    # -- 非法 minimum 定义：退出 2，即使答卷未填 ---------------------------

    def test_invalid_minimum_values_reject_form(self):
        for bad in (True, False, "0", None, [0], {"v": 0}):
            with self.subTest(bad=bad):
                # 直接写字典：None 会序列化为显式 null（键仍存在）。
                form = {"fields": [{"id": "n", "type": "number",
                                    "minimum": bad}]}
                # 答卷填写时拒绝。
                self._assert_invalid_input(
                    "bad-%r-present" % (bad,),
                    self._run(form, {"n": 5}),
                )
                # 答卷未填该字段时同样拒绝。
                self._assert_invalid_input(
                    "bad-%r-missing" % (bad,),
                    self._run(form, {}),
                )

    def test_infinite_minimum_rejects_form(self):
        # 1e400 超出 float 范围，静默解码为 inf；表单定义阶段必须拒绝，
        # 且优先于答卷内容（此处答卷缺失）。
        raw_form = (
            '{"fields": [{"id": "n", "type": "number", "minimum": 1e400}]}'
        )
        self.assertTrue(
            math.isinf(json.loads(raw_form)["fields"][0]["minimum"])
        )
        self._assert_invalid_input(
            "inf-minimum", self._run_raw(raw_form, "{}")
        )

    def test_minimum_on_text_or_select_rejects_form(self):
        text_form = {
            "fields": [{"id": "t", "type": "text", "minimum": 0}]
        }
        select_form = {
            "fields": [
                {
                    "id": "s",
                    "type": "select",
                    "options": ["A"],
                    "minimum": 0,
                }
            ]
        }
        self._assert_invalid_input(
            "minimum-on-text", self._run(text_form, {"t": "hi"})
        )
        self._assert_invalid_input(
            "minimum-on-select", self._run(select_form, {"s": "A"})
        )

    def test_invalid_form_wins_over_answer_errors(self):
        # 非法表单即使答卷同时触发 min_value/unknown_field，也只退出 2。
        form = {"fields": [_number_field(minimum=True)]}
        self._assert_invalid_input(
            "invalid-form-first",
            self._run(form, {"n": -1, "extra": 1}),
        )

    # -- 成功原文落库，失败不影响既有库 -----------------------------------

    def test_success_preserves_raw_text(self):
        form_text = (
            "{\n"
            '  "fields": [\n'
            '    { "id": "n", "type": "number", "minimum": 0 }\n'
            "  ]\n"
            "}\n"
        )
        answers_text = '{\n  "n": 5  \n}\n'
        result = self._run_raw(form_text, answers_text)
        proc, db_path, _, _ = result
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8"))
        self.assertEqual(
            self._rows(db_path),
            [
                (
                    self._payload(proc, "raw")["submissionId"],
                    form_text,
                    answers_text,
                )
            ],
        )

    def test_failures_leave_existing_database_unchanged(self):
        form = {"fields": [_number_field(minimum=0, required=True)]}
        seeded = self._run(form, {"n": 7})
        _, db_path, _, _ = seeded
        self._assert_ok("seed", seeded)
        seeded_rows = self._rows(db_path)
        self.assertEqual(len(seeded_rows), 1)

        failing = [
            ("below-min", form, {"n": -1}, 1),
            ("required-missing", form, {}, 1),
            ("type-bool", form, {"n": True}, 1),
            ("type-infinity", None, None, 1),
            ("bad-minimum", {"fields": [_number_field(minimum=False)]}, {}, 2),
        ]
        for label, bad_form, answers, expected_code in failing:
            with self.subTest(case=label):
                if label == "type-infinity":
                    result = self._run_raw(
                        json.dumps(form), '{"n": 1e400}', db_path=db_path
                    )
                else:
                    result = self._run(
                        bad_form, answers, db_path=db_path
                    )
                proc, _, _, _ = result
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
                self.assertEqual(
                    self._rows(db_path),
                    seeded_rows,
                    "用例 %s：失败提交后数据库记录发生变化" % label,
                )


if __name__ == "__main__":
    unittest.main()
