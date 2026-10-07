"""Isolated, disposable end-to-end exercise of the SOV filesystem task CLI."""

import concurrent.futures
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


SOURCE = Path(__file__).resolve().parents[1] / 'skills' / 'sov-tasks'


def proc(*args, cwd=None, code=0):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    assert p.returncode == code, (args, p.returncode, p.stdout, p.stderr)
    return p.stdout.strip()


def main():
    with tempfile.TemporaryDirectory(prefix='sov-plan038-') as temporary:
        base = Path(temporary)
        binary = base / 'sov-task'
        proc('go', 'build', '-o', str(binary), '.', cwd=SOURCE)
        project, remote = base / 'project', base / 'remote.git'
        project.mkdir()
        proc('git', 'init', '--bare', '--initial-branch=main', str(remote))
        proc('git', 'init', '--initial-branch=main', str(project))
        proc('git', 'remote', 'add', 'origin', str(remote), cwd=project)
        (project / '.gitignore').write_text('.sov/\n', encoding='utf-8')
        (project / 'README.md').write_text('Isolated SOV task probe\n', encoding='utf-8')
        proc('git', 'add', '.gitignore', 'README.md', cwd=project)
        proc('git', '-c', 'user.name=Probe', '-c', 'user.email=probe@example.invalid', 'commit', '-m', 'init', cwd=project)
        proc('git', 'push', '-u', 'origin', 'main', cwd=project)
        worktree = base / 'worktree'
        proc('git', 'worktree', 'add', '--detach', str(worktree), 'HEAD', cwd=project)
        state = project / '.sov'
        for folder in ('tasks', 'claims', 'completed', 'archive'):
            (state / folder).mkdir(parents=True)

        def cli(*args, code=0):
            p = subprocess.run([str(binary), '--state-dir', str(state), *args], cwd=project, capture_output=True, text=True)
            assert p.returncode == code, (args, p.returncode, p.stdout, p.stderr)
            return json.loads(p.stdout or p.stderr)

        probe = subprocess.run([str(binary), '--state-dir', str(state), 'list'], cwd=worktree, capture_output=True, text=True)
        assert probe.returncode == 0 and json.loads(probe.stdout) == [], probe.stderr

        def card(slug, deps=()):
            source = base / f'{slug}.yaml'
            source.write_text('title: ' + slug + '\ntype: development\nexecutor: agent\ndescription: Isolated probe\ndepends_on: [' + ', '.join(f'"{d}"' for d in deps) + ']\nacceptance_criteria: [Verified]\n', encoding='utf-8')
            return cli('create', '--file', str(source), '--slug', slug)

        a = card('foundation')
        probe = subprocess.run([str(binary), '--state-dir', str(state), 'show', a['id']], cwd=worktree, capture_output=True, text=True)
        assert probe.returncode == 0 and json.loads(probe.stdout)['id'] == a['id'], probe.stderr
        b = card('dependent', [a['id']])
        c = card('independent')
        d = card('another-dependent', [a['id']])
        assert [row['id'] for row in cli('list-ready')] == ['0001', '0003']
        assert cli('claim', b['id'])['status'] == 'not_ready'
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda owner: cli('claim-next', '--owner', owner), ('worker-a', 'worker-b')))
        by_id = {claim['id']: claim for claim in claims}
        assert set(by_id) == {'0001', '0003'}, claims
        owners = {claim['id']: (state / 'claims' / claim['name'] / 'owner').read_text().strip() for claim in claims}
        assert cli('claim', a['id'])['status'] == 'not_ready'
        assert cli('claim-next')['status'] == 'no_ready_tasks'
        assert cli('release', c['id'], '--owner', owners[c['id']])['state'] == 'released'
        assert cli('show', c['id'])['state'] == 'READY'
        assert cli('claim', c['id'], '--owner', 'stale-probe')['state'] == 'IN PROGRESS'
        assert cli('release', c['id'], '--owner', 'wrong', code=2)['error'].startswith('claim owner mismatch')
        # The claimant CLI process has exited. Inspect work, owner and Git before manual cleanup.
        stale = state / 'claims' / c['name']
        assert (stale / 'owner').read_text().strip() == 'stale-probe'
        assert not (state / 'tasks' / c['name'] / 'task.md').exists()
        assert not proc('git', 'status', '--porcelain', cwd=project)
        assert cli('show', c['id'])['state'] == 'IN PROGRESS'
        shutil.rmtree(stale)
        assert cli('claim', c['id'], '--owner', 'recovered')['state'] == 'IN PROGRESS'
        assert cli('validate') == {'valid': True, 'errors': []}

        task_dir = state / 'tasks' / a['name']
        (task_dir / 'task.md').write_text('Plan and checks recorded.\n', encoding='utf-8')
        (task_dir / 'checks.md').write_text('Probe checks passed.\n', encoding='utf-8')
        assert not (state / 'completed' / a['id']).exists()
        assert cli('list-ready') == []
        # Publication is an external precondition: CLI does not verify it.
        (project / 'result.txt').write_text('published result\n', encoding='utf-8')
        proc('git', 'add', 'result.txt', cwd=project)
        proc('git', '-c', 'user.name=Probe', '-c', 'user.email=probe@example.invalid', 'commit', '-m', 'Publish probe result', cwd=project)
        sha = proc('git', 'rev-parse', 'HEAD', cwd=project)
        assert not (state / 'completed' / a['id']).exists()
        proc('git', 'push', 'origin', 'main', cwd=project)
        assert proc('git', '--git-dir', str(remote), 'rev-parse', 'main') == sha

        # Inject an interruption after marker, before rename.
        (state / 'completed' / a['id']).touch()
        assert cli('show', a['id'])['state'] == 'INCOMPLETE_COMPLETION'
        assert cli('validate', code=2)['valid'] is False
        assert cli('release', a['id'], '--owner', owners[a['id']], code=2)['error'].startswith('completion started')
        assert [row['id'] for row in cli('list-ready')] == ['0002', '0004']
        assert (state / 'tasks' / a['name'] / 'checks.md').exists()
        assert cli('complete', a['id'], '--owner', owners[a['id']])['state'] == 'completed'
        assert (state / 'archive' / a['name'] / 'checks.md').read_text() == 'Probe checks passed.\n'
        assert (state / 'archive' / a['name'] / 'task.md').exists()
        assert not (state / 'tasks' / a['name']).exists()
        assert not (state / 'claims' / a['name']).exists()

        # Second interruption point: after rename, before claim removal.
        e = card('transfer-probe')
        cli('claim', e['id'], '--owner', 'transfer')
        (state / 'tasks' / e['name'] / 'research.md').write_text('Retained artifact\n', encoding='utf-8')
        (state / 'completed' / e['id']).touch()
        (state / 'tasks' / e['name']).rename(state / 'archive' / e['name'])
        assert cli('validate', code=2)['valid'] is False
        assert cli('complete', e['id'], '--owner', 'transfer')['state'] == 'completed'
        assert (state / 'archive' / e['name'] / 'research.md').read_text() == 'Retained artifact\n'
        assert not (state / 'claims' / e['name']).exists()
        assert cli('complete', d['id'], code=2)['error']
        assert not (state / 'completed' / d['id']).exists()
        assert cli('validate') == {'valid': True, 'errors': []}
        assert [row['id'] for row in cli('list-ready')] == ['0002', '0004']
        assert cli('show', b['id'])['state'] == 'READY'
        print(json.dumps({'result': 'PASS', 'created': [a['id'], b['id'], c['id'], d['id'], e['id']], 'concurrent_claims': sorted(by_id), 'ready_after_marker': ['0002', '0004'], 'published_sha': sha, 'final_validate': True, 'archived': [a['id'], e['id']]}))


if __name__ == '__main__':
    main()
