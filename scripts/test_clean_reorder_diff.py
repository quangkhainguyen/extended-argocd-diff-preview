import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clean_reorder_diff as c  # noqa: E402

TESTDATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata")
STATS = "_Stats_:\n[Applications: 2], [Full Run: Xs], [Rendering: Xs], [Cluster: Xs], [Argo CD: Xs]"


def cfg(keys=c.DEFAULT_KEYS, line_count=5, context=None, **kwargs) -> c.Config:
    return c.Config(
        keys=set(keys.split(",")),
        line_count=line_count,
        context=line_count if context is None else context,
        **kwargs,
    )


def diff(*lines: str) -> str:
    return "\n".join(lines)


def resource(header: str, content: str) -> str:
    return f"#### {header}\n```diff\n{content}\n```\n"


def section(name: str, *resources: str, path: str = "apps/app.yaml") -> str:
    return f"<details>\n<summary>{name} ({path})</summary>\n<br>\n\n" + "".join(resources) + "</details>"


def md(summary: str, *sections: str) -> str:
    body = "\n\n".join(sections) if sections else "No changes found"
    return f"## Argo CD Diff Preview\n\nSummary:\n```yaml\n{summary}\n```\n\n{body}\n\n{STATS}\n"


ENV_REORDER = diff(
    "     spec:",
    "       containers:",
    "       - env:",
    "-        - name: A",
    '-          value: "1"',
    "         - name: B",
    '           value: "2"',
    "+        - name: A",
    '+          value: "1"',
    "         image: nginx",
    "         name: app",
)

IMAGE_CHANGE = diff(
    "       containers:",
    "       - image: nginx",
    "-        name: app",
    "+        name: web",
    "         ports:",
)


class ProcessDocumentTest(unittest.TestCase):
    def run_doc(self, text: str, config: c.Config) -> str:
        result, _ = c.process_document(text, config)
        # Running it again must not change anything
        again, _ = c.process_document(result, config)
        self.assertEqual(result, again)
        return result

    def test_html_fixture_keeps_real_changes_in_target_order(self):
        with open(os.path.join(TESTDATA, "reorder.html"), encoding="utf-8") as f:
            text = f.read()
        result = self.run_doc(text, cfg(line_count=10))

        self.assertIn("± ignore-array-order (+2|-2)</pre>", result)
        rows = [line for line in result.split("\n") if line.startswith("\t<tr class=")]
        changed = [r for r in rows if "added_line" in r or "removed_line" in r]
        self.assertEqual(
            [
                '\t<tr class="added_line"><td><pre>+        - --second</pre></td></tr>',
                '\t<tr class="removed_line"><td><pre>-        - --second</pre></td></tr>',
                '\t<tr class="removed_line"><td><pre>-          value: &#34;5432&#34;</pre></td></tr>',
                '\t<tr class="added_line"><td><pre>+          value: &#34;5433&#34;</pre></td></tr>',
            ],
            changed,
        )
        # env is shown in the actual order of the target branch
        env = [r for r in rows if "- name: " in r]
        self.assertEqual(["DB_PORT", "LOG_LEVEL", "DB_HOST"], [r.split("name: ")[1].split("<")[0] for r in env])

    def test_pure_reorder_removes_application(self):
        text = md("Modified (1):\n± app (+2|-2)", section("app", resource("Deployment: default/app", ENV_REORDER)))
        result = self.run_doc(text, cfg())
        self.assertEqual(md("No changes found"), result)

    def test_other_applications_and_groups_are_kept(self):
        text = md(
            "Added (1):\n+ new-app (+3)\n\nModified (2):\n± app (+2|-2)\n± other (+1|-1)",
            section("app", resource("Deployment: default/app", ENV_REORDER)),
            section("other", resource("Deployment: default/other", IMAGE_CHANGE)),
            section("new-app", resource("ConfigMap: default/new", diff("+a: 1", "+b: 2", "+c: 3"))),
        )
        expected = md(
            "Added (1):\n+ new-app (+3)\n\nModified (1):\n± other (+1|-1)",
            section("other", resource("Deployment: default/other", IMAGE_CHANGE)),
            section("new-app", resource("ConfigMap: default/new", diff("+a: 1", "+b: 2", "+c: 3"))),
        )
        self.assertEqual(expected, self.run_doc(text, cfg()))

    def test_last_modified_group_removed_with_separator(self):
        text = md(
            "Added (1):\n+ new-app (+1)\n\nModified (1):\n± app (+2|-2)",
            section("app", resource("Deployment: default/app", ENV_REORDER)),
            section("new-app", resource("ConfigMap: default/new", diff("+a: 1"))),
        )
        expected = md(
            "Added (1):\n+ new-app (+1)",
            section("new-app", resource("ConfigMap: default/new", diff("+a: 1"))),
        )
        self.assertEqual(expected, self.run_doc(text, cfg()))

    def test_only_reordered_resource_is_removed(self):
        text = md(
            "Modified (1):\n± app (+3|-3)",
            section(
                "app",
                resource("Deployment: default/app", ENV_REORDER),
                resource("Deployment: default/other", IMAGE_CHANGE),
            ),
        )
        expected = md(
            "Modified (1):\n± app (+1|-1)",
            section("app", resource("Deployment: default/other", IMAGE_CHANGE)),
        )
        self.assertEqual(expected, self.run_doc(text, cfg()))

    def test_renamed_application_keeps_section(self):
        text = md(
            "Modified (1):\n± old -> new (+2|-2)",
            section("old -> new", resource("Deployment: default/app", ENV_REORDER)),
        )
        expected = md(
            "Modified (1):\n± old -> new",
            "<details>\n<summary>old -> new (apps/app.yaml)</summary>\n<br>\n\n"
            "_Application name changed, but rendered resources are unchanged_\n\n</details>",
        )
        self.assertEqual(expected, self.run_doc(text, cfg()))

    def test_list_cut_by_skip_marker_is_untouched(self):
        content = diff(
            "       - env:",
            "-        - name: A",
            '-          value: "1"',
            "         - name: B",
            '           value: "2"',
            "+        - name: A",
            '+          value: "1"',
            "         - name: C",
            "@@ skipped 20 lines (11 -> 30) @@",
            "         - name: Y",
            "-          value: old",
            "+          value: new",
        )
        text = md("Modified (1):\n± app (+3|-3)", section("app", resource("Deployment: default/app", content)))
        self.assertEqual(text, self.run_doc(text, cfg()))

    def test_list_until_last_line_with_full_context_is_untouched(self):
        # The hunk ends `line-count` lines after the last change, so the list may continue
        content = diff(
            "       - env:",
            "-        - name: A",
            "         - name: B",
            "+        - name: A",
            "         - name: C",
            "         - name: D",
            "         - name: E",
            "         - name: F",
            "         - name: G",
        )
        text = md("Modified (1):\n± app (+1|-1)", section("app", resource("Deployment: default/app", content)))
        self.assertEqual(text, self.run_doc(text, cfg()))

    def test_list_at_end_of_document_is_sorted(self):
        content = diff(
            "       volumes:",
            "       - emptyDir: {}",
            "-        name: config",
            "-      - emptyDir: {}",
            "         name: cache",
            "+      - emptyDir: {}",
            "+        name: config",
        )
        text = md("Modified (1):\n± app (+2|-2)", section("app", resource("Deployment: default/app", content)))
        self.assertEqual(md("No changes found"), self.run_doc(text, cfg()))

    def test_entry_moved_between_containers_is_kept(self):
        content = diff(
            "       containers:",
            "       - env:",
            "-        - name: A",
            "         - name: B",
            "         name: first",
            "       - env:",
            "         - name: C",
            "+        - name: A",
            "         name: second",
            "       volumes: []",
        )
        text = md("Modified (1):\n± app (+1|-1)", section("app", resource("Deployment: default/app", content)))
        self.assertEqual(text, self.run_doc(text, cfg()))

    def test_keys_are_respected(self):
        content = diff(
            "         ports:",
            "-        - containerPort: 80",
            "         - containerPort: 443",
            "+        - containerPort: 80",
            "         resources: {}",
        )
        text = md("Modified (1):\n± app (+1|-1)", section("app", resource("Deployment: default/app", content)))
        self.assertEqual(text, self.run_doc(text, cfg()))
        self.assertEqual(md("No changes found"), self.run_doc(text, cfg(keys="ports")))
        self.assertEqual(md("No changes found"), self.run_doc(text, cfg(all_lists=True)))

    def test_dropped_hunk_merges_skip_markers(self):
        content = diff(
            "-a: 1",
            "+a: 2",
            " b: 1",
            "@@ skipped 10 lines (3 -> 12) @@",
            "       - env:",
            "-        - name: A",
            "         - name: B",
            "+        - name: A",
            "         image: nginx",
            "@@ skipped 10 lines (18 -> 27) @@",
            "-z: 1",
            "+z: 2",
        )
        expected = diff(
            "-a: 1",
            "+a: 2",
            " b: 1",
            "@@ skipped 25 lines (3 -> 27) @@",
            "-z: 1",
            "+z: 2",
        )
        result, stats = c.process_document(
            md("Modified (1):\n± app (+3|-3)", section("app", resource("Deployment: default/app", content))), cfg()
        )
        self.assertEqual(
            md("Modified (1):\n± app (+2|-2)", section("app", resource("Deployment: default/app", expected))), result
        )
        self.assertEqual(1, stats.hunks_removed)

    def test_hidden_changes_do_not_keep_a_hunk(self):
        content = diff(
            "     metadata:",
            "       annotations:",
            "-        checksum/config: abc",
            "+        checksum/config: def",
            "     spec:",
            "       containers:",
            "       - env:",
            "-        - name: A",
            "         - name: B",
            "+        - name: A",
            "         image: nginx",
        )
        text = md("Modified (1):\n± app (+2|-2)", section("app", resource("Deployment: default/app", content)))
        self.assertEqual(md("No changes found"), self.run_doc(text, cfg()))

    def test_block_scalar_content_is_not_parsed(self):
        content = diff(
            "   script: |",
            "     env:",
            "-    - name: A",
            "     - name: B",
            "+    - name: A",
            " kind: ConfigMap",
        )
        text = md("Modified (1):\n± app (+1|-1)", section("app", resource("ConfigMap: default/app", content)))
        self.assertEqual(text, self.run_doc(text, cfg()))

    def test_truncated_markdown_resource_is_untouched(self):
        body = (
            "<details>\n<summary>app (apps/app.yaml)</summary>\n<br>\n\n"
            + resource("Deployment: default/app", ENV_REORDER[:120])
            + "\n🚨 Diff is too long\n</details>"
        )
        text = md("Modified (1):\n± app (+2|-2)", body)
        self.assertEqual(text, self.run_doc(text, cfg()))

    def test_context_shrinks_output(self):
        content = diff(
            "       - env:",
            "-        - name: A",
            "         - name: B",
            "+        - name: A",
            "         image: nginx",
            "         name: app",
            "         ports:",
            "-        - containerPort: 80",
            "+        - containerPort: 81",
            "         resources: {}",
        )
        expected = diff(
            "         ports:",
            "-        - containerPort: 80",
            "+        - containerPort: 81",
            "         resources: {}",
        )
        text = md("Modified (1):\n± app (+2|-2)", section("app", resource("Deployment: default/app", content)))
        result = self.run_doc(text, cfg(line_count=5, context=1))
        self.assertEqual(
            md("Modified (1):\n± app (+1|-1)", section("app", resource("Deployment: default/app", expected))), result
        )

    def test_changed_item_without_counterpart_keeps_position(self):
        content = diff(
            "       - args:",
            "-        - --a",
            "-        - --x=1",
            "         - --b",
            "+        - --x=2",
            "+        - --a",
            "         image: nginx",
        )
        expected = diff(
            "       - args:",
            "         - --b",
            "-        - --x=1",
            "+        - --x=2",
            "         - --a",
            "         image: nginx",
        )
        text = md("Modified (1):\n± app (+2|-2)", section("app", resource("Deployment: default/app", content)))
        self.assertEqual(
            md("Modified (1):\n± app (+1|-1)", section("app", resource("Deployment: default/app", expected))),
            self.run_doc(text, cfg(all_lists=True)),
        )

    def test_keep_mixed_hunks(self):
        content = diff(
            "       - env:",
            "-        - name: A",
            "         - name: B",
            "+        - name: A",
            "-        image: nginx:1",
            "+        image: nginx:2",
            "         name: app",
        )
        text = md("Modified (1):\n± app (+2|-2)", section("app", resource("Deployment: default/app", content)))
        self.assertEqual(text, self.run_doc(text, cfg(keep_mixed_hunks=True)))
        result = self.run_doc(text, cfg())
        self.assertIn("± app (+1|-1)", result)
        diff_lines = result.split("```diff\n")[1].split("\n```")[0].split("\n")
        self.assertEqual(
            ["-        image: nginx:1", "+        image: nginx:2"],
            [l for l in diff_lines if l[:1] in "+-"],
        )


class CliTest(unittest.TestCase):
    def test_in_place_and_output(self):
        import tempfile

        text = md("Modified (1):\n± app (+2|-2)", section("app", resource("Deployment: default/app", ENV_REORDER)))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "diff.md")
            out = os.path.join(tmp, "clean.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            self.assertEqual(0, c.main(["-o", out, path]))
            with open(path, encoding="utf-8") as f:
                self.assertEqual(text, f.read())
            with open(out, encoding="utf-8") as f:
                self.assertEqual(md("No changes found"), f.read())
            self.assertEqual(0, c.main([path]))
            with open(path, encoding="utf-8") as f:
                self.assertEqual(md("No changes found"), f.read())


if __name__ == "__main__":
    unittest.main()
