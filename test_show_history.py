"""show 按编号读取历史答卷的回归测试。

只通过公开命令 ``python -m forms submit`` / ``python -m forms show`` 构造
场景：合成 JSON 与独立临时 SQLite 数据库均位于临时目录，断言退出码与
stdout 解码后的 JSON，并用标准库 sqlite3 直接核对库中原文。不导入 forms
包内的任何内部函数，不读取或修改既有数据库，仅依赖 Python 标准库。

覆盖的公开行为：
- 表单文件修订后旧答卷仍按提交时的表单与答卷内容读取：同一表单文件先以
  note 文本字段提交含换行的中文文本，再把 note 改为数字字段提交数字 7，
  两次 show 各自返回对应提交解码出的对象；删除输入文件后旧答卷仍可读取；
  读取前后库中全部编号、表单原文与答卷原文逐字符一致；
- 成功输出只含 submissionId、form、answers 三个键，stdout 只有一份 JSON，
  不混入说明文字，比较基于解码对象而不依赖键顺序或原文排版；
- 已有 submissions 表中查询未保存的合法编号：退出码 1 且
  {"error":"not_found"}；编号为 0、负数或含字母时退出码 2 且
  {"error":"invalid_input"}；带前导零的合法编号读到同一记录；
- 数据库路径不存在或缺少 submissions 表时退出码 2 且
  {"error":"storage_error"}，不创建文件或表；
- 所有失败读取前后，已有库中的记录数量与每条原文保持一致。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


class ShowHistoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        self.env = env

    def tearDown(self):
        self._tmp.cleanup()

    # -- 辅助 ------------------------------------------------------------

    def _write_json(self, path, value):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(value, ensure_ascii=False))

    def _run_submit(self, form_path, answers_path, db_path):
        return subprocess.run(
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
            env=self.env,
        )

    def _run_show(self, db_path, raw_id):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "forms",
                "show",
                "--db",
                db_path,
                "--id",
                raw_id,
            ],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
        )

    def _submit_ok(self, form_path, answers_path, db_path, label):
        proc = self._run_submit(form_path, answers_path, db_path)
        self.assertEqual(
            proc.returncode,
            0,
            "用例 %s：提交期望退出码 0，实际 %s，stderr=%r"
            % (label, proc.returncode, proc.stderr.decode("utf-8")),
        )
        payload = self._single_json_doc(proc, "submit:" + label)
        self.assertIs(payload.get("valid"), True)
        self.assertEqual(payload.get("errors"), {})
        submission_id = payload.get("submissionId")
        self.assertIsInstance(submission_id, int)
        self.assertNotIsInstance(submission_id, bool)
        return submission_id

    def _single_json_doc(self, proc, label):
        """stdout 必须恰好是一份 JSON（允许首尾空白），无任何说明文字。"""
        raw = proc.stdout.decode("utf-8")
        self.assertEqual(
            proc.stderr,
            b"",
            "用例 %s：stderr 应为空，实际 %r"
            % (label, proc.stderr.decode("utf-8")),
        )
        body = raw.lstrip()
        try:
            payload, end = json.JSONDecoder().raw_decode(body)
        except ValueError:
            self.fail(
                "用例 %s：stdout 不是合法 JSON：%r" % (label, raw)
            )
        self.assertEqual(
            body[end:].strip(),
            "",
            "用例 %s：stdout 在 JSON 之外混入了其他内容：%r"
            % (label, raw),
        )
        return payload

    def _seed_submission(self, form, answers):
        """在全新临时目录写文件并完成一次成功提交，返回目录、库路径与编号。"""
        run_dir = tempfile.mkdtemp(dir=self.tmp)
        db_path = os.path.join(run_dir, "data.db")
        form_path = os.path.join(run_dir, "form.json")
        answers_path = os.path.join(run_dir, "answers.json")
        self._write_json(form_path, form)
        self._write_json(answers_path, answers)
        sid = self._submit_ok(
            form_path, answers_path, db_path, "seed"
        )
        return run_dir, db_path, sid

    def _snapshot(self, db_path):
        """读取库中全部记录（编号、表单原文、答卷原文），按编号排序。"""
        with sqlite3.connect(db_path) as conn:
            return conn.execute(
                "SELECT id, form, answers FROM submissions ORDER BY id"
            ).fetchall()

    def _table_names(self, db_path):
        with sqlite3.connect(db_path) as conn:
            return {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }

    # -- 表单修订后旧答卷仍可读，且读取不改库 ----------------------------

    def test_form_revision_keeps_each_submission_readable(self):
        run_dir = tempfile.mkdtemp(dir=self.tmp)
        db_path = os.path.join(run_dir, "data.db")
        form_path = os.path.join(run_dir, "form.json")
        answers_path = os.path.join(run_dir, "answers.json")

        # 第一次提交：表单只有一个 note 文本字段，答卷含中文与换行。
        form_v1 = {"fields": [{"id": "note", "type": "text"}]}
        answers_v1 = {"note": "甲\n乙"}
        self._write_json(form_path, form_v1)
        self._write_json(answers_path, answers_v1)
        id1 = self._submit_ok(form_path, answers_path, db_path, "v1")

        # 修订同一表单文件：note 改为数字字段，再次提交数字答卷。
        form_v2 = {"fields": [{"id": "note", "type": "number"}]}
        answers_v2 = {"note": 7}
        self._write_json(form_path, form_v2)
        self._write_json(answers_path, answers_v2)
        id2 = self._submit_ok(form_path, answers_path, db_path, "v2")
        self.assertNotEqual(id1, id2)

        # 任何 show 之前记录下全部编号与原文快照。
        rows_before = self._snapshot(db_path)
        self.assertEqual(
            {row[0] for row in rows_before}, {id1, id2}
        )
        raw_by_id = {row[0]: (row[1], row[2]) for row in rows_before}
        # 库中两份原文各自解码回提交时的对象（不要求恢复原排版）。
        self.assertEqual(json.loads(raw_by_id[id1][0]), form_v1)
        self.assertEqual(json.loads(raw_by_id[id1][1]), answers_v1)
        self.assertEqual(json.loads(raw_by_id[id2][0]), form_v2)
        self.assertEqual(json.loads(raw_by_id[id2][1]), answers_v2)

        # 第一份：仍是文本字段定义，答卷仍是含换行的原字符串。
        proc = self._run_show(db_path, str(id1))
        self.assertEqual(
            proc.returncode,
            0,
            "show v1 期望退出码 0，实际 %s，stderr=%r"
            % (proc.returncode, proc.stderr.decode("utf-8")),
        )
        shown1 = self._single_json_doc(proc, "show-v1")
        self.assertEqual(
            set(shown1), {"submissionId", "form", "answers"}
        )
        self.assertEqual(shown1["submissionId"], id1)
        self.assertEqual(shown1["form"], form_v1)
        self.assertEqual(shown1["answers"], answers_v1)
        self.assertEqual(
            shown1["form"]["fields"][0],
            {"id": "note", "type": "text"},
        )
        note1 = shown1["answers"]["note"]
        self.assertIsInstance(note1, str)
        self.assertEqual(note1, "甲\n乙")
        self.assertIn("\n", note1)

        # 第二份：返回数字字段定义与数字 7（JSON 整数，非浮点非布尔）。
        proc = self._run_show(db_path, str(id2))
        self.assertEqual(
            proc.returncode,
            0,
            "show v2 期望退出码 0，实际 %s，stderr=%r"
            % (proc.returncode, proc.stderr.decode("utf-8")),
        )
        shown2 = self._single_json_doc(proc, "show-v2")
        self.assertEqual(
            set(shown2), {"submissionId", "form", "answers"}
        )
        self.assertEqual(shown2["submissionId"], id2)
        self.assertEqual(shown2["form"], form_v2)
        self.assertEqual(shown2["answers"], answers_v2)
        self.assertEqual(
            shown2["form"]["fields"][0],
            {"id": "note", "type": "number"},
        )
        note2 = shown2["answers"]["note"]
        self.assertIsInstance(note2, int)
        self.assertNotIsInstance(note2, bool)
        self.assertEqual(note2, 7)

        # 成功读取不得改动任何记录。
        self.assertEqual(self._snapshot(db_path), rows_before)

        # 删除全部输入文件后，旧答卷仍从库中读取成功，内容不变。
        os.remove(form_path)
        os.remove(answers_path)
        self.assertFalse(os.path.exists(form_path))
        self.assertFalse(os.path.exists(answers_path))

        proc = self._run_show(db_path, str(id1))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8"))
        self.assertEqual(
            self._single_json_doc(proc, "show-v1-after-delete"), shown1
        )
        proc = self._run_show(db_path, str(id2))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8"))
        self.assertEqual(
            self._single_json_doc(proc, "show-v2-after-delete"), shown2
        )

        # 读取前后：全部编号、表单原文、答卷原文一致。
        self.assertEqual(self._snapshot(db_path), rows_before)

    # -- 未保存的合法编号：not_found -------------------------------------

    def test_unsaved_legal_id_is_not_found(self):
        _run_dir, db_path, sid = self._seed_submission(
            {"fields": [{"id": "note", "type": "text"}]},
            {"note": "x"},
        )
        rows_before = self._snapshot(db_path)
        # 由库中实际最大编号推导一个必然不存在但合法的编号，
        # 不假设编号恰好从 1 开始。
        absent_id = max(row[0] for row in rows_before) + 1
        self.assertGreaterEqual(absent_id, 1)
        self.assertNotIn(absent_id, {row[0] for row in rows_before})

        proc = self._run_show(db_path, str(absent_id))
        self.assertEqual(
            proc.returncode,
            1,
            "未保存编号期望退出码 1，实际 %s，stderr=%r"
            % (proc.returncode, proc.stderr.decode("utf-8")),
        )
        self.assertEqual(
            self._single_json_doc(proc, "not-found"),
            {"error": "not_found"},
        )
        self.assertEqual(self._snapshot(db_path), rows_before)
        # 基线编号仍可正常读取。
        self.assertEqual(
            self._run_show(db_path, str(sid)).returncode, 0
        )
        self.assertEqual(self._snapshot(db_path), rows_before)

    # -- 非法编号：invalid_input -----------------------------------------

    def test_invalid_ids_exit_invalid_input(self):
        _run_dir, db_path, _sid = self._seed_submission(
            {"fields": [{"id": "note", "type": "text"}]},
            {"note": "x"},
        )
        rows_before = self._snapshot(db_path)
        for raw_id in ("0", "-1", "abc"):
            with self.subTest(raw_id=raw_id):
                proc = self._run_show(db_path, raw_id)
                self.assertEqual(
                    proc.returncode,
                    2,
                    "编号 %r 期望退出码 2，实际 %s，stderr=%r"
                    % (
                        raw_id,
                        proc.returncode,
                        proc.stderr.decode("utf-8"),
                    ),
                )
                self.assertEqual(
                    self._single_json_doc(
                        proc, "invalid-id:" + raw_id
                    ),
                    {"error": "invalid_input"},
                )
                self.assertEqual(self._snapshot(db_path), rows_before)

    # -- 前导零编号读到同一记录 -------------------------------------------

    def test_leading_zero_id_reads_same_record(self):
        _run_dir, db_path, sid = self._seed_submission(
            {"fields": [{"id": "note", "type": "number"}]},
            {"note": 7},
        )
        rows_before = self._snapshot(db_path)

        proc = self._run_show(db_path, str(sid))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8"))
        expected = self._single_json_doc(proc, "show-plain")

        for raw_id in ("0" + str(sid), "00" + str(sid)):
            with self.subTest(raw_id=raw_id):
                proc = self._run_show(db_path, raw_id)
                self.assertEqual(
                    proc.returncode,
                    0,
                    "前导零编号 %r 期望退出码 0，实际 %s，stderr=%r"
                    % (
                        raw_id,
                        proc.returncode,
                        proc.stderr.decode("utf-8"),
                    ),
                )
                payload = self._single_json_doc(
                    proc, "show-leading-zero:" + raw_id
                )
                # 以解码对象核对，与无前导零读取结果完全一致，
                # 返回的 submissionId 仍是整数编号本身。
                self.assertEqual(payload, expected)
                self.assertEqual(payload["submissionId"], sid)
                self.assertEqual(self._snapshot(db_path), rows_before)

    # -- 数据库路径不存在：storage_error 且不创建文件 --------------------

    def test_missing_database_path_is_storage_error(self):
        run_dir = tempfile.mkdtemp(dir=self.tmp)
        db_path = os.path.join(run_dir, "missing.db")
        entries_before = set(os.listdir(run_dir))
        self.assertFalse(os.path.exists(db_path))

        # 使用语法合法的编号，确保真正进入存储打开环节。
        proc = self._run_show(db_path, "1")
        self.assertEqual(
            proc.returncode,
            2,
            "缺失库期望退出码 2，实际 %s，stderr=%r"
            % (proc.returncode, proc.stderr.decode("utf-8")),
        )
        self.assertEqual(
            self._single_json_doc(proc, "missing-db"),
            {"error": "storage_error"},
        )
        self.assertFalse(
            os.path.exists(db_path),
            "读取不存在的数据库不应创建数据库文件",
        )
        self.assertEqual(
            set(os.listdir(run_dir)),
            entries_before,
            "读取失败后目录中不应出现任何新文件",
        )

    # -- 缺少 submissions 表：storage_error 且不建表 ---------------------

    def test_database_without_submissions_table_is_storage_error(self):
        run_dir = tempfile.mkdtemp(dir=self.tmp)
        db_path = os.path.join(run_dir, "data.db")
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE other (id INTEGER PRIMARY KEY, x TEXT)")
            conn.execute("INSERT INTO other (x) VALUES (?)", ("基线",))
            conn.commit()
        tables_before = self._table_names(db_path)
        self.assertNotIn("submissions", tables_before)
        with sqlite3.connect(db_path) as conn:
            other_before = conn.execute(
                "SELECT id, x FROM other ORDER BY id"
            ).fetchall()

        proc = self._run_show(db_path, "1")
        self.assertEqual(
            proc.returncode,
            2,
            "缺少 submissions 表期望退出码 2，实际 %s，stderr=%r"
            % (proc.returncode, proc.stderr.decode("utf-8")),
        )
        self.assertEqual(
            self._single_json_doc(proc, "missing-table"),
            {"error": "storage_error"},
        )

        # 不应顺手创建 submissions 表，其他表的内容也保持不变。
        self.assertEqual(self._table_names(db_path), tables_before)
        self.assertNotIn("submissions", self._table_names(db_path))
        with sqlite3.connect(db_path) as conn:
            other_after = conn.execute(
                "SELECT id, x FROM other ORDER BY id"
            ).fetchall()
        self.assertEqual(other_after, other_before)


if __name__ == "__main__":
    unittest.main()
