from __future__ import annotations

import json

from hl import backlog, contractfmt, items
from hl.common import Refusal, read_json, sha256_text

from .support import BACKLOG_A, RunCase


# HD-41's Accept list as its backlog file has it (a help-desk feature program, HD-R8-ITEMS.md): criterion 2's
# sub-bullets are followed by a blank line and an indented paragraph that still belongs to it.
HD_41 = r"""## HD-41 — Platform Apply imports the built API package and render image, layout included, before it applies anything

**Scope.** Program help-desk features, stream hd-platform.

**Accept.**
1. Step order. `services/platform/.venv/bin/python -c "import yaml; print('\n'.join(s['name'] for s in yaml.safe_load(open('.github/workflows/platform-apply.yml'))['jobs']['platform-apply']['steps'] if 'name' in s))"` prints exactly these lines, in order: `Build Lambda package`, `Import built Lambda package`, `Terraform init`, `Ensure render ECR repository`, `Build render image`, `Import built render image`, `Push render image`, `Terraform apply`, `Readback outputs`, `Alert relay smoke`, `Render function smoke`, `Render end-to-end`, `Billing admins probe`, `KB portal policy end-to-end`, `Object size end-to-end`. At `fcfa6b5` it prints 13 lines, with `Build and push render image` in place of the four new or split steps.
2. The checks are blocking and run the built artefacts. `services/platform/.venv/bin/python -c "import yaml; [print(s['name'], s.get('working-directory'), s.get('continue-on-error', False), s['run'].strip(), sep='|') for s in yaml.safe_load(open('.github/workflows/platform-apply.yml'))['jobs']['platform-apply']['steps'] if s.get('name', '').startswith('Import built')]"` prints exactly two lines (none at `fcfa6b5`):
   - `Import built Lambda package|.|False|python3 services/platform/scripts/check_built_package.py --zip services/platform/dist/platform.zip`
   - `Import built render image|.|False|python3 services/platform/scripts/check_built_package.py --image "${RENDER_IMAGE_URI}"`

   Also `grep -c 'docker push' .github/workflows/platform-apply.yml` and `grep -c 'docker build' .github/workflows/platform-apply.yml` each print `1` (`1` at `fcfa6b5`).
3. Nothing else in the workflow changed. Both `diff <(git show origin/main:.github/workflows/platform-apply.yml | sed -n '1,/- name: Build Lambda package/p') <(sed -n '1,/- name: Build Lambda package/p' .github/workflows/platform-apply.yml)` and `diff <(git show origin/main:.github/workflows/platform-apply.yml | sed -n '/- name: Terraform apply/,$p') <(sed -n '/- name: Terraform apply/,$p' .github/workflows/platform-apply.yml)` print nothing.
4. The built zip passes, imported from its extracted copy. After the build, `$P services/platform/scripts/check_built_package.py --zip services/platform/dist/platform.zip > $D/ok.txt; echo "exit=$?"` (with `D=$(mktemp -d)` first) prints `exit=0`. Then `grep -c '^IMPORTED ' $D/ok.txt` prints `6`, `grep '^IMPORTED ' $D/ok.txt | grep -cF "$PWD"` prints `0`, and `tail -1 $D/ok.txt` prints `BUILT PACKAGE OK`.
5. A zip missing its layout fails the step's command. Run `cp services/platform/dist/platform.zip $D/nolayout.zip; zip -q -d $D/nolayout.zip 'platform_svc/assets/brand/mail/desk_mail.py'; $P services/platform/scripts/check_built_package.py --zip $D/nolayout.zip > $D/nolayout.txt 2>&1; echo "exit=$?"; grep -c 'FileNotFoundError: the mail layout was not found' $D/nolayout.txt`. It prints `exit=1`, then a count of at least `1`.
6. A zip with a broken layout fails it too. Run `mkdir -p $D/x/platform_svc/assets/brand/mail; printf 'raise RuntimeError("hd41 broken layout")\n' > $D/x/platform_svc/assets/brand/mail/desk_mail.py; cp services/platform/dist/platform.zip $D/broken.zip; (cd $D/x && zip -q $D/broken.zip platform_svc/assets/brand/mail/desk_mail.py); $P services/platform/scripts/check_built_package.py --zip $D/broken.zip > $D/broken.txt 2>&1; echo "exit=$?"; grep -c 'RuntimeError: hd41 broken layout' $D/broken.txt`. It prints `exit=1`, then a count of at least `1`.
7. The render image passes, and an image without the layout fails. Run, in order:
   - `docker build -q -f services/platform/render/Dockerfile -t hd41-render . && $P services/platform/scripts/check_built_package.py --image hd41-render; echo "exit=$?"` prints 2 `IMPORTED` lines under `/var/task/`, then `BUILT PACKAGE OK`, then `exit=0`.
   - `printf 'FROM hd41-render\nRUN rm /var/task/platform_svc/assets/brand/mail/desk_mail.py\n' | docker build -q -t hd41-render-nolayout - && $P services/platform/scripts/check_built_package.py --image hd41-render-nolayout; echo "exit=$?"` prints the `FileNotFoundError: the mail layout was not found` line, then `exit=1`.
   - Afterwards, `docker rmi hd41-render-nolayout hd41-render`.
8. From `services/platform`: `.venv/bin/python -m pytest -q tests/test_check_built_package.py tests/test_package_boundary.py` exits 0. `grep -c '^def test_' tests/test_check_built_package.py` prints `4`, and `.venv/bin/ruff check scripts/check_built_package.py tests/test_check_built_package.py` exits 0.
9. From the repository root:
   - `git diff --name-only origin/main...HEAD | grep -vxE '\.github/workflows/platform-apply\.yml|services/platform/scripts/check_built_package\.py|services/platform/tests/test_check_built_package\.py'` prints nothing.
   - `git diff --name-only --diff-filter=A origin/main...HEAD` prints exactly the two new files.
10. On the pull request, `gh pr checks <number> --repo owner/helpdesk --json name,state --jq '.[] | select(.name == "Platform CI" or .name == "Platform Terraform Plan") | .name + "=" + .state'` prints `Platform CI=SUCCESS` and `Platform Terraform Plan=SUCCESS`.

**Depends.** None.
"""


class ParseTest(RunCase):
    def test_items_parse_in_order_with_their_fields(self):
        parsed = backlog.parse_backlog(BACKLOG_A)
        self.assertEqual([it.id for it in parsed], ["A-1", "A-2"])
        first = parsed[0]
        self.assertEqual(first.accept, ["`make test` exits 0.", "`grep -c first out.txt` prints `1`."])
        self.assertEqual((first.verify_command, first.verify_wait_min), ("true", 5))
        self.assertEqual(parsed[1].depends_on, ["A-1"])
        self.assertTrue(first.text.startswith("## A-1 — First item"))
        self.assertNotIn("## A-2", first.text)
        self.assertNotIn("---", first.text)

    def test_a_blank_line_inside_an_accept_entry_does_not_end_the_list(self):
        accept = backlog.parse_backlog(HD_41)[0].accept
        self.assertEqual(len(accept), 10)
        self.assertTrue(accept[1].startswith("The checks are blocking and run the built artefacts."))
        self.assertTrue(accept[1].endswith("each print `1` (`1` at `fcfa6b5`)."))
        self.assertTrue(accept[2].startswith("Nothing else in the workflow changed."))
        self.assertTrue(accept[9].startswith("On the pull request,"))

    def test_an_indented_numbered_sub_list_after_a_blank_line_stays_in_its_entry(self):
        # Only a numbered line at column 0 starts an entry; `   1. sub a` belongs to the entry above it.
        text = "## A-1 — x\n\n**Scope.** s\n\n**Accept.**\n1. Run, in order:\n\n   1. `make a` exits 0.\n   2. `make b` exits 0.\n2. `make test` exits 0.\n\n**Depends.** None.\n"
        accept = backlog.parse_backlog(text)[0].accept
        self.assertEqual(accept, ["Run, in order: 1. `make a` exits 0. 2. `make b` exits 0.", "`make test` exits 0."])
        self.assertEqual(backlog.numbered_lines(text, backlog.MARKER_ACCEPT), 2)

    def test_a_section_without_accept_is_refused_by_name(self):
        with self.assertRaisesRegex(backlog.BacklogError, "A-3: no \\*\\*Accept"):
            backlog.parse_backlog("## A-3 — x\n\n**Scope.** y\n")

    def test_a_repeated_id_is_refused(self):
        with self.assertRaisesRegex(backlog.BacklogError, "duplicate id A-1"):
            backlog.parse_backlog(BACKLOG_A + "\n## A-1 — again\n\n**Scope.** s\n\n**Accept.**\n1. a\n")


class InitTest(RunCase):
    def test_init_freezes_items_in_file_order(self):
        run = self.init()
        self.assertEqual(run["order"], ["A-1", "A-2", "B-1"])
        self.assertIn("**Scope.** Build the first thing.", (self.item_dir() / "item.md").read_text())
        self.assertEqual(items.load_item(self.run_dir, "B-1")["depends_on"], ["A-2", "X-9"])

    def test_init_twice_with_the_same_files_changes_nothing(self):
        self.init()
        items.start_phase(self.run_dir, "A-1", "agree", session="e")
        self.init()
        self.assertEqual(items.load_item(self.run_dir, "A-1")["status"], "in_progress")

    def test_init_refuses_files_that_changed(self):
        self.init()
        self.file_a.write_text(BACKLOG_A.replace("First item", "Easier item"))
        with self.assertRaisesRegex(Refusal, "never change"):
            self.init()

    def test_an_id_in_two_files_is_refused(self):
        self.file_b.write_text(BACKLOG_A)
        with self.assertRaisesRegex(Refusal, "duplicate id A-1"):
            self.init()


class NextTest(RunCase):
    def test_first_item_without_unmet_dependencies_is_ready(self):
        self.init()
        self.assertEqual(items.next_item(self.run_dir)["next"], "A-1")

    def test_an_item_in_progress_is_resumed_before_anything_starts(self):
        self.init()
        items.start_phase(self.run_dir, "A-1", "agree", session="e")
        result = items.next_item(self.run_dir)
        self.assertEqual((result["state"], result["next"]), ("resume", "A-1"))

    def test_dependents_of_a_failed_item_are_reported_not_dropped(self):
        self.init()
        items.start_phase(self.run_dir, "A-1", "agree", session="e")
        items.set_status(self.run_dir, "A-1", "failed", "build: wrong twice")
        result = items.next_item(self.run_dir)
        self.assertEqual(result["state"], "blocked-dependencies")
        self.assertEqual(result["blocked"][0], {"id": "A-2", "unmet": [{"id": "A-1", "status": "failed"}]})
        self.assertEqual(result["blocked"][1]["unmet"][1], {"id": "X-9", "status": "not-in-this-run"})

    def test_an_external_dependency_counts_once_recorded(self):
        self.init(self.file_b)
        self.assertEqual(items.next_item(self.run_dir)["state"], "blocked-dependencies")
        items.mark_external(self.run_dir, "A-2", "merged on main as abc123")
        items.mark_external(self.run_dir, "X-9", "merged on main as def456")
        self.assertEqual(items.next_item(self.run_dir)["next"], "B-1")

    def test_complete_when_nothing_is_pending(self):
        self.init(self.file_a)
        for item_id in ("A-1", "A-2"):
            items.set_status(self.run_dir, item_id, "skipped", "operator: not wanted")
        self.assertEqual(items.next_item(self.run_dir)["state"], "complete")

    def test_three_failures_of_one_kind_stop_the_stream(self):
        self.init()
        for item_id in ("A-1", "A-2", "B-1"):
            items.start_phase(self.run_dir, item_id, "agree", session=f"e-{item_id}")
            items.set_status(self.run_dir, item_id, "parked", "environment: gh cannot reach GitHub")
        result = items.next_item(self.run_dir)
        self.assertEqual((result["state"], result["reason_class"]), ("brake", "environment"))
        items.clear_brake(self.run_dir, "token had expired; replaced")
        self.assertNotEqual(items.next_item(self.run_dir)["state"], "brake")

    def test_failures_of_different_kinds_do_not_trip_the_brake(self):
        self.init()
        for item_id, note in (("A-1", "build: a"), ("A-2", "contract: b"), ("B-1", "build: c")):
            items.start_phase(self.run_dir, item_id, "agree", session=f"e-{item_id}")
            items.set_status(self.run_dir, item_id, "failed", note)
        self.assertNotEqual(items.next_item(self.run_dir)["state"], "brake")


class BoundsTest(RunCase):
    def test_a_phase_past_its_bound_is_refused(self):
        self.init()
        items.contract_from_accept(self.run_dir, "A-1")
        items.start_phase(self.run_dir, "A-1", "agree", session="e1")
        items.start_phase(self.run_dir, "A-1", "agree", session="e2")
        with self.assertRaisesRegex(Refusal, "agree bound reached"):
            items.start_phase(self.run_dir, "A-1", "agree", session="e3")

    def test_build_needs_an_agreed_contract(self):
        self.init()
        with self.assertRaisesRegex(Refusal, "no contract installed"):
            items.start_phase(self.run_dir, "A-1", "build", session="b")
        items.contract_from_accept(self.run_dir, "A-1")
        with self.assertRaisesRegex(Refusal, "no agree round has run"):
            items.start_phase(self.run_dir, "A-1", "build", session="b")
        items.start_phase(self.run_dir, "A-1", "agree", session="e")
        with self.assertRaisesRegex(Refusal, "not written yet"):
            items.start_phase(self.run_dir, "A-1", "build", session="b")
        self.write("A-1", "agree-1.md", "One criterion is stale.\n\nREVISE\n")
        with self.assertRaisesRegex(Refusal, "amended contract is not installed"):
            items.start_phase(self.run_dir, "A-1", "build", session="b")

    def amended(self, name, word):
        self.write(
            "A-1",
            name,
            f"Criterion 2 is stale.\n\n## Amended contract\n\n# Contract: A-1\n\n## Criteria\n\n"
            f"1. `make test` exits 0.\n2. `grep -c first out.txt` prints at least `1`.\n\n{word}\n",
        )
        return items.contract_from_amended(self.run_dir, "A-1", name)

    def test_a_correction_installed_after_a_revise_still_builds(self):
        # TDF-46: agree-1 said REVISE and its amended contract was installed, build 1 ran, a correction was
        # installed over it, and build 2 was refused because the installed contract was no longer agree-1's.
        self.init()
        items.contract_from_accept(self.run_dir, "A-1")
        items.start_phase(self.run_dir, "A-1", "agree", session="e1")
        self.amended("agree-1.md", "REVISE")
        self.build(session="b1")
        self.grade(session="g1", verdict="FAIL", results=["pass", "fail"])
        items.start_phase(self.run_dir, "A-1", "correction", session="c1")
        self.amended("correction-1.md", "CORRECTED")
        self.build(session="b1")
        # A later REVISE is acted on only by installing its own amended contract.
        items.start_phase(self.run_dir, "A-1", "agree", session="e2")
        self.write("A-1", "agree-2.md", "Criterion 1 is stale too.\n\nREVISE\n")
        with self.assertRaisesRegex(Refusal, "agree-2.md says REVISE and its amended contract is not installed"):
            items.start_phase(self.run_dir, "A-1", "build", session="b1")

    def test_a_correction_written_before_the_latest_revise_does_not_stand_in_for_its_amendment(self):
        # agree-2 never saw correction-1's contract, so installing it over agree-2's amendment agrees nothing.
        self.init()
        items.contract_from_accept(self.run_dir, "A-1")
        items.start_phase(self.run_dir, "A-1", "agree", session="e1")
        self.amended("agree-1.md", "REVISE")
        self.build(session="b1")
        self.grade(session="g1", verdict="FAIL", results=["pass", "fail"])
        items.start_phase(self.run_dir, "A-1", "correction", session="c1")
        self.write("A-1", "correction-1.md", "Criterion 2 is wrong.\n\n## Amended contract\n\n# Contract: A-1\n\n## Criteria\n\n1. `make test` exits 0.\n\nCORRECTED\n")
        items.start_phase(self.run_dir, "A-1", "agree", session="e2")
        self.amended("agree-2.md", "REVISE")
        items.contract_from_amended(self.run_dir, "A-1", "correction-1.md")
        with self.assertRaisesRegex(Refusal, "agree-2.md says REVISE and its amended contract is not installed"):
            items.start_phase(self.run_dir, "A-1", "build", session="b1")

    def test_a_correction_started_after_the_latest_revise_still_builds(self):
        self.init()
        items.contract_from_accept(self.run_dir, "A-1")
        items.start_phase(self.run_dir, "A-1", "agree", session="e1")
        self.amended("agree-1.md", "REVISE")
        self.build(session="b1")
        self.grade(session="g1", verdict="FAIL", results=["pass", "fail"])
        items.start_phase(self.run_dir, "A-1", "agree", session="e2")
        self.amended("agree-2.md", "REVISE")
        items.start_phase(self.run_dir, "A-1", "correction", session="c1")
        self.amended("correction-1.md", "CORRECTED")
        items.start_phase(self.run_dir, "A-1", "build", session="b1")

    def test_a_correction_does_not_stand_in_for_a_revise_that_was_never_installed(self):
        self.init()
        items.contract_from_accept(self.run_dir, "A-1")
        items.start_phase(self.run_dir, "A-1", "agree", session="e1")
        self.write("A-1", "agree-1.md", "Criterion 2 is stale.\n\nREVISE\n")
        text = "## Criteria\n\n1. `make test` exits 0.\n"
        items._install_contract(self.run_dir, items.load_item(self.run_dir, "A-1"), text, {"source": "amended", "file": "correction-1.md"})
        with self.assertRaisesRegex(Refusal, "agree-1.md says REVISE and its amended contract is not installed"):
            items.start_phase(self.run_dir, "A-1", "build", session="b1")

    def test_two_grades_per_build_and_a_correction_buys_one_more(self):
        self.init()
        self.agree()
        self.build()
        self.grade(session="g1", verdict="FAIL", results=["fail", "pass"])
        self.grade(session="g2", verdict="FAIL", results=["fail", "pass"])
        with self.assertRaisesRegex(Refusal, "grade bound reached for build 1"):
            self.grade(session="g3")
        items.start_phase(self.run_dir, "A-1", "correction", session="c1")
        self.grade(session="g3")
        with self.assertRaisesRegex(Refusal, "correction bound reached"):
            items.start_phase(self.run_dir, "A-1", "correction", session="c2")

    def test_a_second_build_gets_its_own_grades_and_a_third_is_refused(self):
        self.init()
        self.agree()
        self.build()
        self.grade(session="g1", verdict="FAIL", results=["fail", "pass"])
        self.build(session="build-1")  # rework resumes the same builder session
        self.grade(session="g2")
        self.assertEqual(items.load_attempts(self.run_dir, "A-1")["log"][-1]["build"], 2)
        with self.assertRaisesRegex(Refusal, "build bound reached"):
            self.build()

    def test_grade_records_the_head_and_the_contract_it_graded(self):
        self.init()
        self.agree()
        self.build()
        entry = self.grade(head="b" * 40)
        self.assertEqual(entry["head_sha"], "b" * 40)
        self.assertEqual(entry["contract_sha"], items.load_item(self.run_dir, "A-1")["contract"]["sha256"])

    def test_verify_runs_only_after_the_merge(self):
        self.init()
        self.agree()
        with self.assertRaisesRegex(Refusal, "only after the merge"):
            items.start_phase(self.run_dir, "A-1", "verify")


class SessionTest(RunCase):
    def test_a_builder_session_cannot_evaluate(self):
        self.init()
        self.agree()
        self.build(session="s-build")
        with self.assertRaisesRegex(Refusal, "cannot evaluate"):
            self.grade(session="s-build")

    def test_an_evaluator_session_cannot_build(self):
        self.init()
        self.agree(session="s-eval")
        with self.assertRaisesRegex(Refusal, "cannot build"):
            self.build(session="s-eval")

    def test_every_evaluation_is_a_fresh_session(self):
        self.init()
        self.agree(session="s-eval")
        self.build()
        with self.assertRaisesRegex(Refusal, "fresh session"):
            self.grade(session="s-eval")

    def test_the_drafter_of_a_contract_cannot_review_it(self):
        self.init()
        with self.assertRaisesRegex(Refusal, "cannot evaluate"):
            items.start_phase(self.run_dir, "A-1", "agree", session="s", drafter_session="s")


class VoidTest(RunCase):
    def test_an_attempt_with_no_result_is_given_back(self):
        self.init()
        self.agree()
        self.build()
        items.start_phase(self.run_dir, "A-1", "grade", session="g1", head_sha="a" * 40)
        items.void_attempt(self.run_dir, "A-1", "grade", 1, "evaluator pane died before writing a verdict")
        self.assertEqual(items.load_attempts(self.run_dir, "A-1")["grade"], 0)
        self.grade(session="g2")
        self.grade(session="g3")

    def test_an_attempt_that_wrote_its_result_stays_counted(self):
        self.init()
        self.agree()
        self.build()
        self.grade(session="g1", verdict="FAIL", results=["fail", "pass"])
        with self.assertRaisesRegex(Refusal, "produced a result"):
            items.void_attempt(self.run_dir, "A-1", "grade", 1, "did not like it")


    def test_only_the_latest_attempt_of_a_phase_is_voided(self):
        self.init()
        self.agree()
        self.build()
        for session in ("g1", "g2"):
            items.start_phase(self.run_dir, "A-1", "grade", session=session, head_sha="a" * 40)
        with self.assertRaisesRegex(Refusal, r"only the latest grade attempt \(2\) can be voided"):
            items.void_attempt(self.run_dir, "A-1", "grade", 1, "the first evaluator's pane died")
        self.assertEqual(items.load_attempts(self.run_dir, "A-1")["grade"], 2)


class StatusTest(RunCase):
    def test_only_the_merge_script_makes_an_item_merged(self):
        self.init()
        self.agree()
        with self.assertRaisesRegex(Refusal, "only hl-merge"):
            items.set_status(self.run_dir, "A-1", "merged", None)
        self.write("A-1", "merge.json", json.dumps({"state": "MERGE-COMMAND-FAILED"}))
        with self.assertRaisesRegex(Refusal, "only hl-merge"):
            items.set_status(self.run_dir, "A-1", "merged", None)

    def test_failing_or_parking_needs_a_reason(self):
        self.init()
        self.agree()
        with self.assertRaisesRegex(Refusal, "--note is required"):
            items.set_status(self.run_dir, "A-1", "failed", None)

    def test_a_finished_item_takes_no_further_phases(self):
        self.init()
        self.agree()
        items.set_status(self.run_dir, "A-1", "parked", "operator: needs a decision")
        with self.assertRaisesRegex(Refusal, "takes no further phases"):
            self.build()

    def test_reopening_keeps_the_attempts_already_spent(self):
        self.init()
        self.agree()
        self.build()
        items.set_status(self.run_dir, "A-1", "parked", "operator: needs a decision")
        items.set_status(self.run_dir, "A-1", "pending", "operator decided: keep the endpoint", reopen=True)
        self.assertEqual(items.load_attempts(self.run_dir, "A-1")["build"], 1)

    def test_a_reopened_item_that_had_started_is_in_progress_where_it_stopped(self):
        self.init()
        self.agree()
        self.build()
        items.set_status(self.run_dir, "A-1", "parked", "environment: port 8401 taken")
        self.assertNotEqual(items.next_item(self.run_dir)["next"], "A-1")
        item = items.set_status(self.run_dir, "A-1", "pending", "the port is free again", reopen=True)
        self.assertEqual((item["status"], item["note"], "reopened_at" in item), ("in_progress", "the port is free again", True))
        coming = items.next_item(self.run_dir)
        self.assertEqual((coming["state"], coming["next"]), ("resume", "A-1"))
        self.assertEqual(items.load_run(self.run_dir)["events"][-1]["status"], "in_progress")
        # It goes on from its build: a grade is the next phase, not a second agree round or build.
        self.grade()

    def test_a_reopened_item_that_never_started_is_pending(self):
        self.init()
        items.set_status(self.run_dir, "A-1", "skipped", "operator: not this round")
        item = items.set_status(self.run_dir, "A-1", "pending", "operator: this round after all", reopen=True)
        self.assertEqual(item["status"], "pending")
        self.assertEqual(items.next_item(self.run_dir)["state"], "ready")

    def test_an_item_whose_only_attempts_were_voided_reopens_as_pending(self):
        self.init()
        items.contract_from_accept(self.run_dir, "A-1")
        items.start_phase(self.run_dir, "A-1", "agree", session="e1")
        items.void_attempt(self.run_dir, "A-1", "agree", None, "the evaluator never came up")
        items.set_status(self.run_dir, "A-1", "parked", "environment: no worker starts")
        item = items.set_status(self.run_dir, "A-1", "pending", "workers start again", reopen=True)
        self.assertEqual(item["status"], "pending")

    def test_reopening_again_puts_right_an_item_an_earlier_kit_left_pending_with_its_attempts(self):
        self.init()
        self.agree()
        self.build()
        item = items.load_item(self.run_dir, "A-1")
        item["status"] = "pending"
        items.save_item(self.run_dir, item)
        item = items.set_status(self.run_dir, "A-1", "pending", "finishing the reopen", reopen=True)
        self.assertEqual(item["status"], "in_progress")

    def _fail(self, item_id="A-1"):
        self.agree(item_id)
        self.build(item_id)
        return items.set_status(self.run_dir, item_id, "failed", "build: superseded by a repair")

    def _ship(self, run_dir, item_id, verify="verified"):
        item = items.load_item(run_dir, item_id)
        item["status"] = "merged"
        if verify:
            item["fields"]["verify"] = verify
        items.save_item(run_dir, item)

    def test_a_failed_item_is_skipped_as_superseded_only_once_its_repair_has_shipped(self):
        self.init()
        self._fail()
        with self.assertRaisesRegex(Refusal, "only once the item that replaces it has shipped: A-2 is pending"):
            items.set_status(self.run_dir, "A-1", "skipped", "superseded by A-2", superseded_by="A-2")
        self._ship(self.run_dir, "A-2", verify=None)
        if items.load_item(self.run_dir, "A-2").get("verify_command"):
            with self.assertRaisesRegex(Refusal, "Verify is not recorded as passed"):
                items.set_status(self.run_dir, "A-1", "skipped", "superseded by A-2", superseded_by="A-2")
            self._ship(self.run_dir, "A-2")
        item = items.set_status(self.run_dir, "A-1", "skipped", "superseded by A-2", superseded_by="A-2")
        self.assertEqual((item["status"], item["superseded_by"]["id"]), ("skipped", "A-2"))
        self.assertEqual(item["superseded_by"]["run_dir"], str(self.run_dir.resolve()))
        self.assertEqual(items.load_run(self.run_dir)["events"][-1]["status"], "skipped")

    def test_the_repair_may_be_another_runs_item(self):
        self.init()
        self._fail()
        other = self.tmp / "repair-run"
        items.init_run(other, "repair-1", "owner/repo", [str(self.file_b)])
        with self.assertRaisesRegex(Refusal, "B-1 is pending"):
            items.set_status(self.run_dir, "A-1", "skipped", "superseded by B-1", superseded_by="B-1", superseded_run=str(other))
        self._ship(other, "B-1")
        item = items.set_status(self.run_dir, "A-1", "skipped", "superseded by B-1", superseded_by="B-1", superseded_run=str(other))
        self.assertEqual(item["superseded_by"], {"id": "B-1", "run_dir": str(other.resolve())})

    def test_a_dependency_skipped_as_superseded_no_longer_blocks_its_dependents(self):
        self.init()
        self._fail()
        self.assertEqual(items.next_item(self.run_dir)["blocked"][0], {"id": "A-2", "unmet": [{"id": "A-1", "status": "failed"}]})
        other = self.tmp / "repair-run"
        items.init_run(other, "repair-1", "owner/repo", [str(self.file_b)])
        self._ship(other, "B-1")
        items.set_status(self.run_dir, "A-1", "skipped", "superseded by B-1", superseded_by="B-1", superseded_run=str(other))
        result = items.next_item(self.run_dir)
        self.assertEqual((result["state"], result["next"]), ("ready", "A-2"))

    def test_a_plainly_skipped_dependency_still_blocks(self):
        self.init()
        items.set_status(self.run_dir, "A-1", "skipped", "not needed")
        self.assertEqual(items.next_item(self.run_dir)["blocked"][0], {"id": "A-2", "unmet": [{"id": "A-1", "status": "skipped"}]})

    def test_superseding_is_refused_where_it_does_not_apply(self):
        self.init()
        self._ship(self.run_dir, "A-2")
        with self.assertRaisesRegex(Refusal, "A-1 is pending; only a failed item is set skipped as superseded"):
            items.set_status(self.run_dir, "A-1", "skipped", "x", superseded_by="A-2")
        self._fail()
        with self.assertRaisesRegex(Refusal, "--superseded-by goes only with --status skipped"):
            items.set_status(self.run_dir, "A-1", "pending", "x", reopen=True, superseded_by="A-2")
        with self.assertRaisesRegex(Refusal, "cannot supersede itself"):
            items.set_status(self.run_dir, "A-1", "skipped", "x", superseded_by="A-1")
        with self.assertRaisesRegex(Refusal, "is not a run directory"):
            items.set_status(self.run_dir, "A-1", "skipped", "x", superseded_by="A-2", superseded_run=str(self.tmp / "nowhere"))
        with self.assertRaisesRegex(Refusal, "--note is required"):
            items.set_status(self.run_dir, "A-1", "skipped", None, superseded_by="A-2")
        with self.assertRaisesRegex(Refusal, "only a pending item can be skipped, or a failed one with --superseded-by"):
            items.set_status(self.run_dir, "A-1", "skipped", "x")
        self.assertEqual(items.load_item(self.run_dir, "A-1")["status"], "failed")

    def test_an_item_that_is_simply_pending_or_in_progress_is_not_reopened(self):
        self.init()
        with self.assertRaisesRegex(Refusal, "A-1 is pending; reopening needs --reopen on a failed, parked or skipped item"):
            items.set_status(self.run_dir, "A-1", "pending", "x", reopen=True)
        self.agree()
        with self.assertRaisesRegex(Refusal, "A-1 is in_progress"):
            items.set_status(self.run_dir, "A-1", "pending", "x", reopen=True)


class ContractTest(RunCase):
    def test_a_well_authored_item_takes_its_accept_list_verbatim(self):
        self.init()
        record = items.contract_from_accept(self.run_dir, "A-1")["contract"]
        self.assertEqual((record["source"], record["criteria"]), ("accept", 2))
        criteria = contractfmt.parse_criteria((self.item_dir() / "contract.md").read_text())
        self.assertEqual(criteria[1], (2, "`grep -c first out.txt` prints `1`."))

    def test_an_evaluators_amended_contract_replaces_it_and_the_old_one_is_kept(self):
        self.init()
        items.contract_from_accept(self.run_dir, "A-1")
        items.start_phase(self.run_dir, "A-1", "agree", session="e1")
        self.write(
            "A-1",
            "agree-1.md",
            "Criterion 2 names a file main no longer has.\n\n## Amended contract\n\n"
            "# Contract: A-1\n\n## Criteria\n\n1. `make test` exits 0.\n2. `grep -c first result.txt` prints `1`.\n\n"
            "## Out of scope\n\nNothing else.\n\nREVISE\n",
        )
        record = items.contract_from_amended(self.run_dir, "A-1", "agree-1.md")["contract"]
        self.assertEqual((record["source"], record["session"]), ("amended", "e1"))
        self.assertIn("result.txt", (self.item_dir() / "contract.md").read_text())
        self.assertNotIn("REVISE", (self.item_dir() / "contract.md").read_text())
        self.assertIn("out.txt", (self.item_dir() / "contract.v1.md").read_text())
        # Written by something else, it is put back from the amended contract it was installed from.
        installed = (self.item_dir() / "contract.md").read_text()
        (self.item_dir() / "contract.md").write_text(installed.replace("prints `1`", "prints anything"))
        self.assertIn("is not the contract that was installed", items.contract_problem(self.run_dir, "A-1"))
        items.contract_restore(self.run_dir, "A-1")
        self.assertEqual(((self.item_dir() / "contract.md").read_text(), items.contract_problem(self.run_dir, "A-1")), (installed, None))
        self.build()  # the amended contract is binding: the item can build

    def test_an_amended_contract_is_only_taken_from_a_started_evaluation(self):
        self.init()
        self.write("A-1", "agree-1.md", "## Amended contract\n\n## Criteria\n\n1. x\n\nREVISE\n")
        with self.assertRaisesRegex(Refusal, "no agree attempt 1"):
            items.contract_from_amended(self.run_dir, "A-1", "agree-1.md")
        with self.assertRaisesRegex(Refusal, "agree-<n>.md or correction-<n>.md"):
            items.contract_from_amended(self.run_dir, "A-1", "notes.md")

    def test_a_draft_is_installed_only_when_the_review_agreed(self):
        self.init()
        items.start_phase(self.run_dir, "A-1", "agree", session="e1", drafter_session="b1")
        self.write("A-1", "contract-draft-1.md", "## Criteria\n\n1. It builds.\n\n## Out of scope\n\nRest.\n")
        self.write("A-1", "agree-1.md", "Criterion 1 cannot be checked.\n\nREVISE\n")
        with self.assertRaisesRegex(Refusal, "does not end with AGREED"):
            items.contract_from_draft(self.run_dir, "A-1", 1)
        items.start_phase(self.run_dir, "A-1", "agree", session="e2", drafter_session="b1")
        self.write("A-1", "contract-draft-2.md", "## Criteria\n\n1. `make` exits 0.\n\n## Out of scope\n\nRest.\n")
        self.write("A-1", "agree-2.md", "Checkable now.\n\nAGREED\n")
        items.contract_from_draft(self.run_dir, "A-1", 2)
        # Written by something else, it is put back from the agreed draft; with the draft gone too, nothing says what was agreed.
        installed = (self.item_dir() / "contract.md").read_text()
        (self.item_dir() / "contract.md").write_text("## Criteria\n\n1. Anything.\n")
        items.contract_restore(self.run_dir, "A-1")
        self.assertEqual((self.item_dir() / "contract.md").read_text(), installed)
        (self.item_dir() / "contract-draft-2.md").rename(self.item_dir() / "elsewhere.md")
        with self.assertRaisesRegex(Refusal, r"installed from \(draft\) no longer gives the text that was installed"):
            items.contract_restore(self.run_dir, "A-1")
        (self.item_dir() / "elsewhere.md").rename(self.item_dir() / "contract-draft-2.md")
        with self.assertRaisesRegex(Refusal, "A-2 has no contract installed"):
            items.contract_restore(self.run_dir, "A-2")
        self.build(session="b1")  # the drafter may build

    def graded_and_failed(self):
        self.init()
        self.agree()
        self.build()
        self.grade(session="g1", verdict="FAIL", results=["pass", "fail"])
        self.grade(session="g2", verdict="FAIL", results=["pass", "fail"])
        return items.start_phase(self.run_dir, "A-1", "correction", session="c1")

    def test_a_correction_that_stands_installs_nothing(self):
        self.graded_and_failed()
        before = (self.item_dir() / "contract.md").read_text()
        self.write("A-1", "correction-1.md", "The item asks for exactly this and the build does not do it.\n\nSTANDS\n")
        with self.assertRaisesRegex(Refusal, "does not end with CORRECTED"):
            items.contract_from_amended(self.run_dir, "A-1", "correction-1.md")
        self.assertEqual((self.item_dir() / "contract.md").read_text(), before)

    def test_a_corrected_contract_is_installed_and_graded_once_more(self):
        self.graded_and_failed()
        self.write(
            "A-1",
            "correction-1.md",
            "Criterion 2 over-reads the item.\n\n## Amended contract\n\n# Contract: A-1\n\n## Criteria\n\n"
            "1. `make test` exits 0.\n2. `grep -c first out.txt` prints at least `1`.\n\n## Out of scope\n\nRest.\n\nCORRECTED\n",
        )
        record = items.contract_from_amended(self.run_dir, "A-1", "correction-1.md")["contract"]
        self.assertEqual((record["file"], record["session"]), ("correction-1.md", "c1"))
        entry = self.grade(session="g3")
        self.assertEqual(entry["contract_sha"], record["sha256"])
        with self.assertRaisesRegex(Refusal, "grade bound reached"):
            self.grade(session="g4")

    def test_a_contract_that_does_not_parse_is_refused(self):
        self.init()
        items.start_phase(self.run_dir, "A-1", "agree", session="e1")
        self.write("A-1", "agree-1.md", "## Amended contract\n\n## Criteria\n\n1. a\n   1. nested step\n\nREVISE\n")
        with self.assertRaisesRegex(Refusal, "bullets"):
            items.contract_from_amended(self.run_dir, "A-1", "agree-1.md")

    def test_criteria_must_run_one_to_n(self):
        with self.assertRaisesRegex(contractfmt.ContractError, "numbered 1..2"):
            contractfmt.parse_criteria("## Criteria\n\n1. a\n3. b\n")
        eleven = "## Criteria\n\n" + "".join(f"{n}. c\n" for n in range(1, 12))
        with self.assertRaisesRegex(contractfmt.ContractError, "limit is 10"):
            contractfmt.parse_criteria(eleven)

    def test_serve_and_ready_are_read_from_a_build_report(self):
        report = "Built it.\n\n**Serve.** `python3 -m http.server 8210`\n**Ready.** URL `http://127.0.0.1:8210/`\n"
        self.assertEqual(
            contractfmt.how_to_run(report),
            {"serve": "python3 -m http.server 8210", "ready_kind": "URL", "ready": "http://127.0.0.1:8210/"},
        )
        with self.assertRaisesRegex(contractfmt.ContractError, "exactly one '\\*\\*Serve"):
            contractfmt.how_to_run("no lines here")


class CliTest(RunCase):
    def test_a_refusal_exits_2_and_prints_the_reason_as_json(self):
        import contextlib
        import io

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = items.main(["--run-dir", str(self.run_dir), "next"])
        self.assertEqual(code, 2)
        self.assertIn("run `hl-items init` first", json.loads(out.getvalue())["refused"][0])

    def test_init_then_next_through_the_cli(self):
        import contextlib
        import io

        argv = ["--run-dir", str(self.run_dir), "init", "--run-id", "r", "--repo", "o/r", "--file", str(self.file_a)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(items.main(argv), 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(items.main(["--run-dir", str(self.run_dir), "next"]), 0)
        self.assertEqual(json.loads(out.getvalue())["next"], "A-1")
        self.assertEqual(read_json(self.run_dir / "run.json")["repo"], "o/r")


class VerifyLineTest(RunCase):
    """A Verify line corrected in the backlog file after its run began."""

    CORRECTED = BACKLOG_A.replace("**Verify.** `true`\nWait. 5", "**Verify.** `echo deployed`\nWait. 7")

    def correct(self, text=None):
        self.file_a.write_text(text or self.CORRECTED)

    def merged(self, item_id="A-1", verify=None, attempt=False, result=True):
        item = items.load_item(self.run_dir, item_id)
        item["status"] = "merged"
        if verify:
            item["fields"]["verify"] = verify
        items.save_item(self.run_dir, item)
        if attempt:
            entry = items.start_phase(self.run_dir, item_id, "verify")
            if result:
                self.write(item_id, f"verify-{entry['n']}.txt", "command: true\nexit: 1\n")

    def test_without_verify_drops_only_the_verify_block(self):
        text = backlog.parse_backlog(BACKLOG_A)[0].text
        stripped = backlog.without_verify(text)
        self.assertNotIn("**Verify.**", stripped)
        self.assertNotIn("Wait. 5", stripped)
        self.assertIn("**Accept.**\n1. `make test` exits 0.", stripped)
        self.assertEqual(stripped, backlog.without_verify(backlog.parse_backlog(self.CORRECTED)[0].text))

    def test_staging_again_after_a_corrected_verify_line_leaves_the_run_as_it_is(self):
        self.init()
        self.correct()
        self.init()
        self.assertEqual(items.load_item(self.run_dir, "A-1")["verify_command"], "true")
        self.assertEqual(items.verify_drift(self.run_dir), ["A-1"])

    def test_the_corrected_line_is_loaded_and_the_old_one_kept(self):
        self.init()
        was_hash = items.load_run(self.run_dir)["files"][0]["sha256"]
        self.correct()
        out = items.reload_verify(self.run_dir, "A-1", "the old line read the wrong log")
        item = items.load_item(self.run_dir, "A-1")
        self.assertEqual((item["verify_command"], item["verify_wait_min"]), ("echo deployed", 7))
        self.assertEqual(out["was"], {"command": "true", "wait_min": 5})
        self.assertEqual(item["verify_history"][0]["note"], "the old line read the wrong log")
        self.assertEqual(items.verify_drift(self.run_dir), [])
        run = items.load_run(self.run_dir)
        # The run agrees with its files again, so they are recorded as they are now, and a later init is a no-op.
        self.assertNotEqual(run["files"][0]["sha256"], was_hash)
        self.assertEqual(run["files"][0]["sha256"], sha256_text(self.CORRECTED))
        self.assertEqual(run["events"][-1]["status"], "verify-line-reloaded")
        self.init()

    def test_a_failed_verify_takes_the_corrected_line_and_points_at_the_retry(self):
        self.init()
        self.merged(verify="verify-failed", attempt=True)
        self.correct()
        out = items.reload_verify(self.run_dir, "A-1", "colour codes in the log")
        self.assertEqual(out["verify"], "verify-failed")
        self.assertIn("--retry", out["next"])
        self.assertEqual(items.load_item(self.run_dir, "A-1")["fields"]["verify"], "verify-failed")

    def test_a_settled_verify_is_never_rewritten(self):
        self.init()
        for settled in ("verified", "fixed-forward"):
            self.merged(verify=settled)
            self.correct()
            with self.assertRaisesRegex(Refusal, f"recorded {settled}"):
                items.reload_verify(self.run_dir, "A-1", "x")
        self.assertEqual(items.load_item(self.run_dir, "A-1")["verify_command"], "true")

    def test_tries_spent_on_the_wrong_line_do_not_count_against_the_corrected_one(self):
        self.init()
        self.merged(verify="verify-failed")
        for _ in range(items.BOUNDS["verify"]):
            entry = items.start_phase(self.run_dir, "A-1", "verify")
            self.write("A-1", f"verify-{entry['n']}.txt", "command: true\nexit: 1\n")
        self.assertIn("verify bound reached", items.bound_reached(self.run_dir, "A-1", "verify"))
        self.correct()
        out = items.reload_verify(self.run_dir, "A-1", "the old line named the wrong workflow")
        self.assertEqual(out["attempts_set_aside"], items.BOUNDS["verify"])
        self.assertEqual(items.load_item(self.run_dir, "A-1")["verify_history"][-1]["attempts_set_aside"], items.BOUNDS["verify"])
        self.assertIsNone(items.bound_reached(self.run_dir, "A-1", "verify"))
        self.assertEqual(items.start_phase(self.run_dir, "A-1", "verify")["n"], items.BOUNDS["verify"] + 1)

    def test_an_attempt_without_its_result_is_refused(self):
        self.init()
        self.merged(attempt=True, result=False)
        self.correct()
        with self.assertRaisesRegex(Refusal, "verify-1 has no result yet"):
            items.reload_verify(self.run_dir, "A-1", "x")

    def test_a_running_hl_verify_holds_it_off(self):
        import os
        import socket
        import time

        self.init()
        self.correct()
        lock = self.item_dir() / "verify.lock.d"
        lock.mkdir()
        (lock / "owner.json").write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "started_at": time.time()}))
        with self.assertRaisesRegex(Refusal, "another hl-verify is running"):
            items.reload_verify(self.run_dir, "A-1", "x")
        self.assertTrue(lock.exists())

    def test_an_unchanged_line_is_refused(self):
        self.init()
        with self.assertRaisesRegex(Refusal, "already has: correct the line"):
            items.reload_verify(self.run_dir, "A-1", "x")

    def test_any_other_change_is_refused_by_init_and_by_verify_line(self):
        self.init()
        self.correct(self.CORRECTED.replace("1. `make test` exits 0.", "1. `make test` exits 0 quickly."))
        with self.assertRaisesRegex(Refusal, "never change.*A-1.*outside the Verify line"):
            self.init()
        with self.assertRaisesRegex(Refusal, "outside the Verify line"):
            items.reload_verify(self.run_dir, "A-1", "x")
        self.assertEqual(items.load_item(self.run_dir, "A-1")["verify_command"], "true")

    def test_an_item_added_to_the_file_is_refused(self):
        self.init()
        self.correct(self.CORRECTED + "\n## A-3 — Third\n\n**Scope.** More.\n\n**Accept.**\n1. Yes.\n")
        with self.assertRaisesRegex(Refusal, "now hold A-1, A-2, A-3"):
            items.reload_verify(self.run_dir, "A-1", "x")

    def test_a_verify_line_is_neither_added_nor_taken_away(self):
        self.init()
        self.correct(BACKLOG_A.replace("**Verify.** `true`\nWait. 5\n", ""))
        self.assertEqual(items.verify_drift(self.run_dir), ["A-1"])
        with self.assertRaisesRegex(Refusal, "never added or taken away"):
            items.reload_verify(self.run_dir, "A-1", "x")
        self.correct(BACKLOG_A.replace("**Depends.** A-1.", "**Depends.** A-1.\n\n**Verify.** `true`"))
        with self.assertRaisesRegex(Refusal, "never added or taken away"):
            items.reload_verify(self.run_dir, "A-2", "x")

    def test_a_note_is_required(self):
        self.init()
        self.correct()
        with self.assertRaisesRegex(Refusal, "--note is required"):
            items.reload_verify(self.run_dir, "A-1", "  ")

    def test_through_the_cli(self):
        import contextlib
        import io

        self.init()
        self.correct()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = items.main(["--run-dir", str(self.run_dir), "verify-line", "--id", "A-1", "--note", "colour codes"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["verify_command"], "echo deployed")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(items.main(["--run-dir", str(self.run_dir), "verify-line", "--id", "A-1", "--note", "again"]), 2)
