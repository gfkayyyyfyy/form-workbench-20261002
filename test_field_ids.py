"""字段标识（field id）合法性与唯一性的回归测试。

只通过公开入口 ``python -m forms submit`` 执行：合成 UTF-8 JSON 写入独立
临时目录，检查退出码、stdout 的 JSON 与标准库 sqlite3 中的落库结果。
不导入 forms 包内的任何内部函数，不读取或修改既有数据库。

覆盖的公开行为：
- 字段未提供 id，或 id 为空字符串、null、数字、布尔、数组、对象时，
  一律以退出码 2 输出恰为 ``{"error": "invalid_input"}`` 的单个 JSON
  对象，stderr 为空，不返回字段错误或 submissionId，也不创建数据库；
- 同一表单内两个字段使用相同 id（即使 type 不同）同样被拒绝；
- 重复标识同时遇到必填遗漏或未知答卷字段时，仍为 invalid_input，
  不被答卷校验结果覆盖；
- name、Name、" name " 是三个互不相同的合法标识，按字符串原值匹配，
  不做大小写转换或去除空格；
- 非法表单不会在尚不存在的数据库路径上创建文件，也不改变已成功保存
  一份答卷的数据库（记录数、编号与两份原文均保持不变）。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

INVALID_STDOUT = '{"error": "invalid_input"}\n'

# 各类非法字段标识：字段除 id 外的其余属性保持合法。
BAD_ID_FIELDS = [
    ("missing-id", {"type": "text"}),
    ("empty-id", {"id": "", "type": "text"}),
    ("null-id", {"id": None, "type": "text"}),
    ("integer-id", {"id": 7, "type": "text"}),
    ("float-id", {"id": 3.5, "type": "text"}),
    ("boolean-true-id", {"id": True, "type": "text"}),
    ("boolean-false-id", {"id": False, "type": "text"}),
    ("empty-array-id", {"id": [], "type": "text"}),
    ("array-id", {"id": ["name"], "type": "text"}),
    ("empty-object-id", {"id": {}, "type": "text"}),
    ("object-id", {"id": {"name": 1}, "type": "text"}),
]

# 重复标识表单：相同 id 即使 type 不同也必须拒绝。
DUPLICATE_ID_FORMS = [
    (
        "dup-different-type",
        {"fields": [
            {"id": "name", "type": "text"},
            {"id": "name", "type": "number"},
        ]},
    ),
    (
        "dup-same-type",
        {"fields": [
            {"id": "name", "type": "text"},
            {"id": "name", "type": "text"},
        ]},
    ),
    (
        "dup-nonadjacent",
        {"fields": [
            {"id": "name", "type": "text"},
            {"id": "age", "type": "number"},
            {"id": "name", "type": "select", "options": ["甲", "乙"]},
        ]},
    ),
    (
        "dup-spaced",
        {"fields": [
            {"id": " name ", "type": "text"},
            {"id": " name ", "type": "number"},
        ]},
    ),
]


class FieldIdTests(unittest.TestCase):
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

    def _rows(self, db_path):
        with sqlite3.connect(db_path) as conn:
            return conn.execute(
                "SELECT id, form, answers FROM submissions ORDER BY id"
            ).fetchall()

    def _assert_invalid_stdio(self, label, proc):
        """退出码 2、stderr 为空、stdout 恰为一个 invalid_input JSON。"""
        self.assertEqual(
            proc.returncode,
            2,
            "用例 %s：期望退出码 2，实际 %s，stderr=%r"
            % (
                label,
                proc.returncode,
                proc.stderr.decode("utf-8"),
            ),
        )
        self.assertEqual(
            proc.stderr,
            b"",
            "用例 %s：stderr 应为空，实际 %r"
            % (label, proc.stderr.decode("utf-8")),
        )
        raw = proc.stdout.decode("utf-8")
        # 恰为一个 JSON 对象：字节级精确匹配公开入口的单行输出。
        self.assertEqual(
            raw,
            INVALID_STDOUT,
            "用例 %s：stdout 应为单个 %r，实际 %r"
            % (label, INVALID_STDOUT, raw),
        )
        payload = json.loads(raw)
        self.assertEqual(
            payload,
            {"error": "invalid_input"},
            "用例 %s：期望 invalid_input，实际 %r" % (label, payload),
        )
        self.assertNotIn("errors", payload, "用例 %s：不应返回字段错误" % label)
        self.assertNotIn(
            "submissionId", payload, "用例 %s：不应返回 submissionId" % label
        )

    def _assert_rejected_without_db(self, label, form, answers):
        proc, db_path, _, _ = self._run(form, answers)
        self._assert_invalid_stdio(label, proc)
        self.assertFalse(
            os.path.exists(db_path),
            "用例 %s：输入不合法不应创建数据库文件 %s" % (label, db_path),
        )

    # -- 非法 id 类型 ----------------------------------------------------

    def test_non_string_or_empty_ids_rejected(self):
        # 答卷本身始终是合法 JSON 对象；失败只能来自字段标识。
        for label, field in BAD_ID_FIELDS:
            with self.subTest(field=label):
                self._assert_rejected_without_db(
                    label, {"fields": [field]}, {}
                )

    # -- 重复 id ---------------------------------------------------------

    def test_duplicate_ids_rejected_even_with_different_types(self):
        # 题目给定的最小示例：text 与 number 共用 "name"，答卷合法。
        form = {"fields": [
            {"id": "name", "type": "text"},
            {"id": "name", "type": "number"},
        ]}
        proc, db_path, _, _ = self._run(form, {"name": "甲"})
        self._assert_invalid_stdio("given-example", proc)
        self.assertFalse(os.path.exists(db_path))

    def test_other_duplicate_id_forms_rejected(self):
        for label, form in DUPLICATE_ID_FORMS:
            with self.subTest(form=label):
                self._assert_rejected_without_db(label, form, {"name": "甲"})

    def test_duplicate_ids_win_over_answer_validation(self):
        # 若表单通过校验，这些答卷分别会触发 required、unknown_field
        # （以及 type）；表单标识非法时一律只输出 invalid_input。
        form = {"fields": [
            {"id": "name", "type": "text"},
            {"id": "name", "type": "number", "required": True},
        ]}
        cases = [
            ("required-missing", {}),
            ("unknown-field", {"name": "甲", "extra": "乙"}),
            ("both", {"extra": "乙"}),
        ]
        for label, answers in cases:
            with self.subTest(case=label):
                self._assert_rejected_without_db(label, form, answers)

    # -- 合法标识按原值精确区分 ------------------------------------------

    def test_distinct_ids_case_and_space_sensitive_submit_persists(self):
        # 含空格与换行的 UTF-8 原文：name、Name、" name " 三个文本字段。
        form_text = (
            "{\n"
            '  "fields": [\n'
            '    { "id": "name", "type": "text" },\n'
            '    { "id": "Name", "type": "text" },\n'
            '    { "id": " name ", "type": "text" }\n'
            "  ]\n"
            "}\n"
        )
        answers_text = (
            "{\n"
            '  "name": "甲",\n'
            '  "Name": "乙 副",\n'
            '  " name ": "  丙  "\n'
            "}\n"
        )
        decoded_form = json.loads(form_text)
        decoded_answers = json.loads(answers_text)
        # 前置事实：三个标识与三个答卷键互不相同，且仅大小写/空格有别。
        self.assertEqual(
            [f["id"] for f in decoded_form["fields"]],
            ["name", "Name", " name "],
        )
        self.assertEqual(
            set(decoded_answers), {"name", "Name", " name "}
        )

        # 先在该库保存一条种子答卷，再核对“只追加一条”。
        db_path = os.path.join(self.tmp, "distinct.db")
        seeded = self._run(
            {"fields": [{"id": "seed", "type": "text"}]},
            {"seed": "种子"},
            db_path=db_path,
        )
        seed_proc, _, seed_form_text, seed_answers_text = seeded
        self.assertEqual(
            seed_proc.returncode, 0, seed_proc.stderr.decode("utf-8")
        )
        seeded_rows = self._rows(db_path)
        self.assertEqual(len(seeded_rows), 1)

        proc, _, _, _ = self._run_raw(
            form_text, answers_text, db_path=db_path
        )
        self.assertEqual(
            proc.returncode,
            0,
            "期望退出码 0，实际 %s，stderr=%r"
            % (proc.returncode, proc.stderr.decode("utf-8")),
        )
        self.assertEqual(proc.stderr, b"")
        payload = json.loads(proc.stdout.decode("utf-8"))
        self.assertIs(payload.get("valid"), True)
        self.assertEqual(payload.get("errors"), {})
        submission_id = payload.get("submissionId")
        self.assertIsInstance(submission_id, int)

        rows = self._rows(db_path)
        # 只追加一条：原有记录逐字段保持，新记录编号与返回值一致。
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], seeded_rows[0])
        new_id, row_form, row_answers = rows[1]
        self.assertEqual(new_id, submission_id)
        self.assertNotEqual(new_id, seeded_rows[0][0])
        # 表单与答卷原文逐字符保留（含中文、首尾空格与换行）。
        self.assertEqual(row_form, form_text)
        self.assertEqual(row_answers, answers_text)
        self.assertNotEqual(row_form, seed_form_text)
        self.assertNotEqual(row_answers, seed_answers_text)
        # 落库原文解码后仍是三个不同标识，未发生归一化。
        self.assertEqual(
            [f["id"] for f in json.loads(row_form)["fields"]],
            ["name", "Name", " name "],
        )
        self.assertEqual(json.loads(row_answers), decoded_answers)

    def test_answer_keys_match_ids_by_exact_string(self):
        # 标识符按原值匹配：漏填带空格的必填字段，错误键就是原串；
        # 仅大小写或空格不同的答卷键属于未知字段。
        form = {"fields": [
            {"id": "name", "type": "text"},
            {"id": "Name", "type": "text"},
            {"id": " name ", "type": "text", "required": True},
        ]}
        result = self._run(form, {"name": "甲", "Name": "乙"})
        proc, db_path, _, _ = result
        self.assertEqual(proc.returncode, 1, proc.stderr.decode("utf-8"))
        self.assertEqual(
            json.loads(proc.stdout.decode("utf-8")),
            {"valid": False, "errors": {" name ": "required"}},
        )
        self.assertFalse(os.path.exists(db_path))

        result = self._run(
            form,
            {
                "name": "甲",
                "Name": "乙",
                " name ": "丙",
                " name": "丁",
                "NAME": "戊",
            },
        )
        proc, db_path, _, _ = result
        self.assertEqual(proc.returncode, 1, proc.stderr.decode("utf-8"))
        self.assertEqual(
            json.loads(proc.stdout.decode("utf-8")),
            {
                "valid": False,
                "errors": {" name": "unknown_field", "NAME": "unknown_field"},
            },
        )
        self.assertFalse(os.path.exists(db_path))

    # -- 失败不影响既有库 -------------------------------------------------

    def test_invalid_forms_leave_existing_database_unchanged(self):
        # 先成功保存一份答卷作为基线。
        db_path = os.path.join(self.tmp, "existing.db")
        seeded = self._run(
            {"fields": [{"id": "seed", "type": "text"}]},
            {"seed": "种子\n原文"},
            db_path=db_path,
        )
        seed_proc, _, _, _ = seeded
        self.assertEqual(
            seed_proc.returncode, 0, seed_proc.stderr.decode("utf-8")
        )
        seeded_rows = self._rows(db_path)
        self.assertEqual(len(seeded_rows), 1)

        invalid_submits = []
        for label, field in BAD_ID_FIELDS:
            invalid_submits.append(
                (label, {"fields": [field]}, {})
            )
        for label, dup_form in DUPLICATE_ID_FORMS:
            invalid_submits.append((label, dup_form, {"name": "甲"}))
        # 重复标识与必填遗漏、未知字段同时出现。
        dup_form = {"fields": [
            {"id": "name", "type": "text"},
            {"id": "name", "type": "number", "required": True},
        ]}
        invalid_submits.append(("dup-required-missing", dup_form, {}))
        invalid_submits.append(
            ("dup-unknown-field", dup_form, {"name": "甲", "extra": "乙"})
        )

        for label, form, answers in invalid_submits:
            with self.subTest(case=label):
                proc, _, form_text, answers_text = self._run(
                    form, answers, db_path=db_path
                )
                self._assert_invalid_stdio(label, proc)
                rows = self._rows(db_path)
                # 记录数、编号与两份原文在每次失败后均保持不变。
                self.assertEqual(
                    rows,
                    seeded_rows,
                    "用例 %s：失败提交后数据库记录发生变化" % label,
                )
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0][0], seeded_rows[0][0])
                self.assertEqual(rows[0][1], seeded_rows[0][1])
                self.assertEqual(rows[0][2], seeded_rows[0][2])
                # 本次被拒的原文没有混入既有记录。
                self.assertNotEqual(rows[0][1], form_text)
                self.assertNotEqual(rows[0][2], answers_text)


if __name__ == "__main__":
    unittest.main()
