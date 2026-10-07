"""Disposable SDLC handoff exercise; scripted reviews are not agent reviews."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile


SOURCE = Path(__file__).resolve().parents[1] / 'skills' / 'sov-tasks'


def run(*args, cwd=None, code=0):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    assert result.returncode == code, (args, result.returncode, result.stdout, result.stderr)
    return result.stdout.strip()


def main():
    with tempfile.TemporaryDirectory(prefix='sov-plan044-') as temporary:
        root = Path(temporary)
        binary = root / 'sov-task'
        run('go', 'build', '-o', str(binary), '.', cwd=SOURCE)
        project, remote = root / 'project', root / 'remote.git'
        project.mkdir()
        run('git', 'init', '--bare', '--initial-branch=main', str(remote))
        run('git', 'init', '--initial-branch=main', str(project))
        run('git', 'remote', 'add', 'origin', str(remote), cwd=project)
        state = project / '.sov'
        for name in ('tasks', 'claims', 'completed', 'archive'):
            (state / name).mkdir(parents=True)
        (project / '.gitignore').write_text('.sov/\n', encoding='utf-8')
        (project / 'AGENTS.md').write_text('Check docs/specs/ and run python3 -m unittest discover -s tests.\n', encoding='utf-8')
        (project / 'rules').mkdir()
        (project / 'rules' / 'index.md').write_text('Follow AGENTS.md.\n', encoding='utf-8')
        (project / 'docs' / 'specs').mkdir(parents=True)
        (project / 'docs' / 'index.md').write_text('Specifications: specs/labels.md\n', encoding='utf-8')
        (project / 'tests').mkdir()
        (project / 'tests' / '__init__.py').write_text('', encoding='utf-8')
        run('git', 'add', '.', cwd=project)
        commit = ('git', '-c', 'user.name=Trial', '-c', 'user.email=trial@example.invalid', 'commit')
        run(*commit, '-m', 'Initialize trial', cwd=project)
        run('git', 'push', '-u', 'origin', 'main', cwd=project)

        def cli(*args):
            return json.loads(run(str(binary), '--state-dir', str(state), *args, cwd=project))

        # Decisions are fixture inputs, not answers obtained from a human in this run.
        decisions = ('Input: a missing label; question: reject or use a default? '
                     'Decision: reject missing; empty string is a valid label. '
                     'Output: display the supplied text surrounded by brackets.\n')
        spec = project / 'docs' / 'specs' / 'labels.md'
        spec.write_text('# Label display\n\nMissing input raises ValueError; an empty string displays []. '
                        'Other strings display [<input>] without trimming.\n\n'
                        'Criteria: missing rejected; empty accepted; text preserved.\n', encoding='utf-8')
        # A scripted content check models, but does not constitute, independent spec review.
        assert 'empty string displays []' in spec.read_text(encoding='utf-8')
        assert cli('list') == []  # reviewed draft has created no implementation tasks
        run('git', 'add', 'docs/specs/labels.md', cwd=project)
        run(*commit, '-m', 'Publish trial requirements', cwd=project)
        run('git', 'push', 'origin', 'main', cwd=project)
        spec_sha = run('git', '--git-dir', str(remote), 'rev-parse', 'main')
        assert spec_sha == run('git', 'rev-parse', 'HEAD', cwd=project)

        # Publishing alone does not trigger decomposition; the fixture supplies a separate request.
        assert cli('list') == []
        request = '/sov-decompose docs/specs/labels.md'
        assert request.startswith('/sov-decompose ') and spec_sha

        def card(slug, description, criteria, deps=()):
            path = root / f'{slug}.yaml'
            path.write_text('title: ' + slug + '\ndescription: ' + json.dumps(description) + '\n'
                            'depends_on: [' + ', '.join(f'"{id}"' for id in deps) + ']\n'
                            'acceptance_criteria: [' + json.dumps(criteria) + ']\n', encoding='utf-8')
            return cli('create', '--file', str(path), '--slug', slug)

        first = card('display-label', 'Display valid labels including empty text', 'Empty text displays []')
        second = card('reject-missing', 'Reject missing labels', 'Missing input raises ValueError', [first['id']])
        assert [row['id'] for row in cli('list-ready')] == [first['id']]
        assert cli('show', second['id'])['state'] == 'BLOCKED'
        assert cli('validate') == {'valid': True, 'errors': []}
        assert cli('claim', first['id'], '--owner', 'trial-author')['state'] == 'IN PROGRESS'
        task = state / 'tasks' / first['name']
        (task / 'task.md').write_text('# Handoff\n' + decisions + 'Spec: docs/specs/labels.md @ ' + spec_sha +
                                     '\nTask: implement valid inputs only; missing input belongs to ' + second['id'] +
                                     '. Next: add test for empty string and run unittest.\n', encoding='utf-8')
        (task / 'plan.md').write_text('Use a pure format_label(value) function in labels.py. '
                                     'Test ordinary and empty labels with unittest; run discover. '
                                     'Do not implement missing-input handling in this task.\n', encoding='utf-8')
        cli('release', first['id'], '--owner', 'trial-author')

        # Fresh interpreter receives paths only; no dialogue history or in-process state.
        observer = ('import json, pathlib, sys; p=pathlib.Path(sys.argv[1]); '
                    's=pathlib.Path(sys.argv[2]); t=(p/"task.md").read_text(); '
                    'plan=(p/"plan.md").read_text(); spec=s.read_text(); '
                    'print(json.dumps({"task": "empty string" in t, '
                    '"next": "add test for empty string" in t, '
                    '"plan": "unittest" in plan, "spec": "without trimming" in spec}))')
        handoff = json.loads(run(sys.executable, '-c', observer, str(task), str(spec)))
        assert all(handoff.values()), handoff
        assert cli('claim', first['id'], '--owner', 'trial-resumed')['state'] == 'IN PROGRESS'
        tests = project / 'tests' / 'test_labels.py'
        tests.write_text('import unittest\nfrom labels import format_label\n\n'
                         'class LabelsTest(unittest.TestCase):\n'
                         '    def test_text(self):\n        self.assertEqual(format_label(" hi "), "[ hi ]")\n'
                         '    def test_empty(self):\n        self.assertEqual(format_label(""), "[]")\n', encoding='utf-8')
        (project / 'labels.py').write_text('def format_label(value):\n    return "[" + (value or "none") + "]"\n', encoding='utf-8')
        checks = task / 'checks.md'
        # Initial author check deliberately covers only the normal case; defect exposed by review.
        initial = run(sys.executable, '-m', 'unittest', 'tests.test_labels.LabelsTest.test_text', cwd=project)
        assert initial == ''
        checks.write_text('Initial state: labels.py uses fallback "none"; normal-case unittest: PASS. '
                          'Full unittest not yet run; cannot publish.\n', encoding='utf-8')
        assert run(sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', cwd=project, code=1) == ''
        review = task / 'review.md'
        review.write_text('# Scripted review (not independent agent review)\n\n'
                          '## R1 — implementation, fixture reviewer, initial uncommitted labels.py\n'
                          '- Requirements axis: F1 medium; empty string returns [none], violates labels.md and task criterion.\n'
                          '- Engineering axis: only normal-case check was run; full unittest fails on test_empty.\n'
                          '- Result: blocked; F1 open; add failing-case coverage and fix, rerun full suite.\n', encoding='utf-8')
        assert not (state / 'completed' / first['id']).exists()
        review_observer = ('import json, pathlib, sys; p=pathlib.Path(sys.argv[1]); '
                           't=(p/"task.md").read_text(); c=(p/"checks.md").read_text(); '
                           'r=(p/"review.md").read_text(); print(json.dumps({"task": '
                           '"Spec: docs/specs/labels.md" in t, "blocker": "F1 open" in r, '
                           '"fix": "empty string returns [none]" in r, '
                           '"checks": "Full unittest not yet run" in c}))')
        after_review = json.loads(run(sys.executable, '-c', review_observer, str(task)))
        assert all(after_review.values()), after_review
        (project / 'labels.py').write_text('def format_label(value):\n    return "[" + value + "]"\n', encoding='utf-8')
        run(sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', cwd=project)
        checks.write_text(checks.read_text(encoding='utf-8') +
                          'After F1: changed labels.py to preserve empty value; discover -s tests: 2 PASS.\n', encoding='utf-8')
        review.write_text(review.read_text(encoding='utf-8') +
                          '\n## R2 — repeat scripted check, corrected uncommitted labels.py\n'
                          '- Link: R1/F1; requirement: [] for empty and preserved text; both tests pass.\n'
                          '- Engineering: full unittest discover passed for corrected state. F1 closed; no open blockers.\n', encoding='utf-8')
        run('git', 'add', 'labels.py', 'tests/test_labels.py', cwd=project)
        run(*commit, '-m', 'Implement trial label display', cwd=project)
        run('git', 'push', 'origin', 'main', cwd=project)
        result_sha = run('git', 'rev-parse', 'HEAD', cwd=project)
        assert run('git', '--git-dir', str(remote), 'rev-parse', 'main') == result_sha
        (task / 'task.md').write_text((task / 'task.md').read_text(encoding='utf-8') +
                                     'Published origin/main @ ' + result_sha + '; full tests passed; R2 closes F1.\n', encoding='utf-8')
        assert cli('complete', first['id'], '--owner', 'trial-resumed')['state'] == 'completed'
        assert (state / 'archive' / first['name'] / 'review.md').exists()
        assert [row['id'] for row in cli('list-ready')] == [second['id']]
        assert cli('validate') == {'valid': True, 'errors': []}
        print(json.dumps({'result': 'PASS', 'spec_sha': spec_sha, 'result_sha': result_sha,
                          'handoff': handoff, 'review_handoff': after_review,
                          'review': 'scripted R1/F1 -> R2',
                          'next_ready': second['id'], 'final_validate': True}))


if __name__ == '__main__':
    main()
